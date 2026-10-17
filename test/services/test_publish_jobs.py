"""发布必须在后台跑，不能冻住页面。

发布一条成片是一次 ``subprocess.run``，超时上限 900 秒——上传 54 MB 视频加上浏览器
的登录与上传流程，几分钟是常态。它原先**直接在 Streamlit 的渲染路径里同步执行**，
于是整个页面在上传期间完全冻结：不能滚动、不能切视图，只能干等一个没有信息量的
spinner。

搬进后台之后，两个性质必须守住：

1. 重复点击不会发布两次。发布可见性硬编码为 public，发出去无法撤回；
2. 作业状态放在模块级而不是 session_state——页面刷新、切视图、重开浏览器都不该让
   一次正在进行的上传"消失"，否则用户会以为没点上而再点一次。
"""

import time
import unittest

from app.services import publish_jobs


def _slow_publish(seconds: float = 0.0, success: bool = True):
    def work():
        if seconds:
            time.sleep(seconds)
        return {"success": success, "video_url": "https://youtu.be/x" if success else None}

    return work


class TestPublishJobs(unittest.TestCase):
    def setUp(self):
        publish_jobs.clear_finished_jobs()
        with publish_jobs._LOCK:
            publish_jobs._JOBS.clear()

    def test_job_runs_in_the_background_and_returns_immediately(self):
        """
        启动必须立刻返回。

        这是整个改动的目的：同步执行时页面会被冻住几分钟。
        """
        started = time.perf_counter()
        job = publish_jobs.start_publish_job("k", "Post", _slow_publish(0.6))
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.2, f"启动耗时 {elapsed:.2f}s，仍然是同步的")
        self.assertFalse(job.done, "刚启动就显示已完成")
        job_finished = job  # 稍后断言
        for _ in range(100):
            if job_finished.done:
                break
            time.sleep(0.02)
        self.assertTrue(job_finished.done)
        self.assertEqual(job_finished.status, "done")
        self.assertTrue(job_finished.result["success"])

    def test_clicking_twice_publishes_once(self):
        """
        重复启动必须被吸收。

        发布是公开可见且无法撤回的，重复发布比等待糟糕得多。
        """
        calls = []

        def counted():
            calls.append(1)
            time.sleep(0.3)
            return {"success": True}

        first = publish_jobs.start_publish_job("dup", "Post", counted)
        second = publish_jobs.start_publish_job("dup", "Post", counted)
        self.assertIs(first, second, "重复点击创建了第二个作业")

        for _ in range(100):
            if first.done:
                break
            time.sleep(0.02)
        self.assertEqual(len(calls), 1, "同一条片子被发布了两次")

    def test_a_finished_job_may_be_retried(self):
        """作业结束后再点应当能重新发布——失败的发布需要能重试。"""
        first = publish_jobs.start_publish_job("retry", "Post", _slow_publish(0, success=False))
        for _ in range(100):
            if first.done:
                break
            time.sleep(0.02)
        self.assertEqual(first.status, "failed")
        self.assertFalse(publish_jobs.is_running("retry"))

        second = publish_jobs.start_publish_job("retry", "Post", _slow_publish(0, success=True))
        for _ in range(100):
            if second.done:
                break
            time.sleep(0.02)
        self.assertEqual(second.status, "done")

    def test_an_exception_becomes_a_failed_job_not_a_dead_thread(self):
        """
        作业线程自己必须兜住异常。

        异常若逃出线程，用户看到的是"按钮点了没反应"，作业永远停在 running。
        """

        def boom():
            raise RuntimeError("uploader exploded")

        job = publish_jobs.start_publish_job("err", "Post", boom)
        for _ in range(100):
            if job.done:
                break
            time.sleep(0.02)
        self.assertEqual(job.status, "failed")
        self.assertIn("uploader exploded", job.result["error"])

    def test_is_running_reflects_state(self):
        self.assertFalse(publish_jobs.is_running("nothing"))
        job = publish_jobs.start_publish_job("live", "Post", _slow_publish(0.25))
        self.assertTrue(publish_jobs.is_running("live"))
        for _ in range(100):
            if job.done:
                break
            time.sleep(0.02)
        self.assertFalse(publish_jobs.is_running("live"))

    def test_snapshot_reports_elapsed_seconds(self):
        job = publish_jobs.start_publish_job("elapsed", "Post", _slow_publish(0, success=True))
        for _ in range(100):
            if job.done:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(job.snapshot()["elapsed"], 0)
        self.assertEqual(job.snapshot()["status"], "done")

    def test_jobs_outlive_the_page(self):
        """
        作业登记表是模块级的，不随 session 消失。

        这是刻意的：一次正在进行的上传不该因为用户刷新页面而"不存在"，否则他会
        以为没点上，于是再点一次，同一条片子被发布两遍。
        """
        job = publish_jobs.start_publish_job("persist", "Post", _slow_publish(0.2))
        self.assertIn(job.key, [j.key for j in publish_jobs.all_jobs()])
        self.assertIsNotNone(publish_jobs.get_job("persist"))
        for _ in range(100):
            if job.done:
                break
            time.sleep(0.02)

    def test_clear_finished_jobs_keeps_running_ones(self):
        """
        清理只能丢已结束的。

        把正在跑的作业一起清掉，等于让用户可以再点一次——重复发布。
        """
        running = publish_jobs.start_publish_job("running", "Post", _slow_publish(0.3))
        finished = publish_jobs.start_publish_job("finished", "Post", _slow_publish(0))
        for _ in range(100):
            if finished.done:
                break
            time.sleep(0.02)

        publish_jobs.clear_finished_jobs()
        self.assertIsNone(publish_jobs.get_job("finished"))
        self.assertIsNotNone(publish_jobs.get_job("running"))
        self.assertFalse(running.done)

    def test_keys_from_different_videos_do_not_collide(self):
        publish_jobs.start_publish_job(
            publish_jobs.job_key("plan", "ch", 1), "a", _slow_publish(0.2)
        )
        publish_jobs.start_publish_job(
            publish_jobs.job_key("plan", "ch", 2), "b", _slow_publish(0.2)
        )
        self.assertEqual(len(publish_jobs.all_jobs()), 2)
        # 不同的调用方（计划表 / 成片库）对同一条片子也必须分开
        self.assertNotEqual(
            publish_jobs.job_key("plan", "ch", 1),
            publish_jobs.job_key("library", "ch", 1),
        )


if __name__ == "__main__":
    unittest.main()
