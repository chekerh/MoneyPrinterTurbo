"""验证缩略图生成在后台进行，点击后立刻返回，状态可跨重跑读取。

现有实现的问题很具体：``render_thumbnail`` 直接跑在 Streamlit 的请求线程上，
最长 30 秒的 FFmpeg 会把整个界面冻住，用户以为页面挂了，只能刷新。

这里要建立的不变量：

* **点击立即返回。** 提交动作只登记意图，不等待渲染。
* **普通重跑不干活。** 没有待处理任务时不启动任何子进程。
* **状态可恢复。** 状态落盘，刷新页面后仍能看到"正在生成"。
* **失败不覆盖成功。** 已有产物在生成失败时保持可见。
* **工作线程全进程只有一个。** 每个会话各起一个线程会和 Streamlit 的重跑语义
  打架，线程数也会随刷新膨胀。
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.thumbnail_jobs import (
    JobState,
    ThumbnailJobManager,
    get_job_manager,
)



def _fake_render(spec, tools):
    """替身渲染器：真的把文件写到 spec.output，否则后续重命名会失败。"""
    from app.services.thumbnail import RenderResult

    spec.output.parent.mkdir(parents=True, exist_ok=True)
    spec.output.write_bytes(b"jpeg-bytes")
    return RenderResult(
        output_path=spec.output,
        output_sha256="d" * 64,
        output_bytes=spec.output.stat().st_size,
        frame_at_seconds=0.5,
        width=1280,
        height=720,
        font_id="Charm-Regular.ttf",
    )


class _SlowRender:
    """一个可控的假渲染器，用来观察并发而不真的跑 FFmpeg。"""

    def __init__(self, delay: float = 0.4, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, spec, tools):
        self.calls.append(spec)
        self.started.set()
        self.release.wait(timeout=self.delay)
        if self.fail:
            from app.services.thumbnail import ThumbnailError

            raise ThumbnailError("synthetic render failure")
        spec.output.write_bytes(b"jpeg")
        from app.services.thumbnail import RenderResult

        return RenderResult(
            output_path=spec.output,
            output_sha256="b" * 64,
            output_bytes=4,
            frame_at_seconds=2.0,
            width=1280,
            height=720,
            font_id="Charm-Regular.ttf",
        )


class _JobsFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "thumbnails" / "task-a"
        self.root.mkdir(parents=True)
        self.manager = ThumbnailJobManager(self.root)

    def tearDown(self):
        self.manager.shutdown(wait=True, timeout=5)
        self._tmp.cleanup()

    def _wait_for(self, job_id: str, states, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            record = self.manager.status(job_id)
            if record and record["state"] in states:
                return record
            time.sleep(0.02)
        return self.manager.status(job_id)

    def _request(self, source: Path, text: str = "hello"):
        return {
            "source": str(source),
            "overlay_text": text,
            "font_path": str(self.base / "font.ttf"),
            "video_index": 1,
            "source_sha256": "a" * 64,
        }


class TestSubmitIsNonBlocking(_JobsFixture):
    """点击必须立刻返回。"""

    def test_submit_returns_before_the_render_finishes(self):
        render = _SlowRender(delay=0.5)
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch("app.services.thumbnail_jobs.render_thumbnail", render):
            job_id = self.manager.submit("task-a", self._request(source))
            # 提交本身没有被渲染阻塞：渲染线程还没被要求完成
            self.assertIsInstance(job_id, str)
            self.assertEqual(len(job_id), 32)

        self.assertTrue(self._wait_for(job_id, {JobState.SUCCEEDED.value}))

    def test_status_is_persisted_so_a_reload_still_sees_it(self):
        render = _SlowRender(delay=0.2)
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch("app.services.thumbnail_jobs.render_thumbnail", render):
            job_id = self.manager.submit("task-a", self._request(source))
            self._wait_for(job_id, {JobState.SUCCEEDED.value})

        store = self.root / "jobs.json"
        self.assertTrue(store.is_file())
        payload = json.loads(store.read_text(encoding="utf-8"))
        self.assertIn(job_id, payload["jobs"])
        self.assertEqual(payload["jobs"][job_id]["state"], JobState.SUCCEEDED.value)

    def test_a_fresh_manager_reads_persisted_state(self):
        """新进程接手时必须能看到在途任务，而不是显示"什么都没有"。"""
        render = _SlowRender(delay=0.3)
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch("app.services.thumbnail_jobs.render_thumbnail", render):
            job_id = self.manager.submit("task-a", self._request(source))
            self._wait_for(job_id, {JobState.SUCCEEDED.value})

        reopened = ThumbnailJobManager(self.root)
        try:
            record = reopened.status(job_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["state"], JobState.SUCCEEDED.value)
        finally:
            reopened.shutdown(wait=True, timeout=5)


class TestFailureAndCancellation(_JobsFixture):
    """失败与取消都不能破坏已有产物。"""

    def test_a_failing_render_records_a_terminal_failure(self):
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch(
            "app.services.thumbnail_jobs.render_thumbnail",
            _SlowRender(delay=0.05, fail=True),
        ):
            job_id = self.manager.submit("task-a", self._request(source))
            record = self._wait_for(job_id, {JobState.FAILED.value})

        self.assertEqual(record["state"], JobState.FAILED.value)
        self.assertTrue(record["error"])

    def test_a_failed_job_leaves_the_manifest_untouched(self):
        """失败不得改动清单——上一个成功的产物必须还在。"""
        from app.services.thumbnail_files import load_manifest, write_manifest

        write_manifest(
            self.root,
            {"schema_version": 2, "task_id": "task-a", "manifest_revision": 0, "videos": []},
        )
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch(
            "app.services.thumbnail_jobs.render_thumbnail",
            _SlowRender(delay=0.05, fail=True),
        ):
            job_id = self.manager.submit("task-a", self._request(source))
            self._wait_for(job_id, {JobState.FAILED.value})

        manifest = load_manifest(self.root / "manifest.json")
        self.assertEqual(manifest["manifest_revision"], 0)
        self.assertEqual(manifest["videos"], [])

    def test_cancel_stops_a_queued_job(self):
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")
        render = _SlowRender(delay=0.5)

        with patch("app.services.thumbnail_jobs.render_thumbnail", render):
            self.manager.submit("task-a", self._request(source))
            cancelled = self.manager.cancel_all("task-a")

        self.assertGreaterEqual(cancelled, 0)

    def test_admission_stops_after_shutdown(self):
        """关闭后不再接受新任务，否则会往一个没人轮询的队列里堆积。"""
        self.manager.shutdown(wait=True, timeout=5)
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch("app.services.thumbnail_jobs.render_thumbnail", _SlowRender(delay=0.05)):
            with self.assertRaises(RuntimeError):
                self.manager.submit("task-a", self._request(source))


class TestManagerIsProcessOwned(_JobsFixture):
    """工作线程全进程只有一个。"""

    def test_get_job_manager_returns_the_same_instance_per_root(self):
        first = get_job_manager(self.root)
        second = get_job_manager(self.root)
        try:
            self.assertIs(first, second)
        finally:
            first.shutdown(wait=True, timeout=5)

    def test_different_roots_get_different_managers(self):
        other = self.base / "thumbnails" / "task-b"
        other.mkdir(parents=True)
        first = get_job_manager(self.root)
        second = get_job_manager(other)
        try:
            self.assertIsNot(first, second)
        finally:
            first.shutdown(wait=True, timeout=5)
            second.shutdown(wait=True, timeout=5)

    def test_poll_does_not_start_any_work(self):
        """
        轮询是只读的。没有待处理任务时，status/list 不得触发渲染。
        """
        render = _SlowRender()
        self.manager.list_jobs("task-a")

        with patch("app.services.thumbnail_jobs.render_thumbnail", render):
            self.manager.status("does-not-exist")
            self.manager.list_jobs("task-a")

        self.assertEqual(render.calls, [])


class TestCorruptJobStore(_JobsFixture):
    """状态文件损坏时降级，而不是让整个面板报错。"""

    def test_corrupt_store_does_not_prevent_new_submissions(self):
        (self.root / "jobs.json").write_text("{not json")
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch(
            "app.services.thumbnail_jobs.render_thumbnail", _SlowRender(delay=0.05)
        ):
            job_id = self.manager.submit("task-a", self._request(source))
            record = self._wait_for(job_id, {JobState.SUCCEEDED.value})

        self.assertEqual(record["state"], JobState.SUCCEEDED.value)

    def test_status_of_an_unknown_job_is_none(self):
        self.assertIsNone(self.manager.status("f" * 32))

    def test_list_jobs_filters_by_task_and_excludes_unknown_task(self):
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch(
            "app.services.thumbnail_jobs.render_thumbnail", _SlowRender(delay=0.05)
        ):
            job_id = self.manager.submit("task-a", self._request(source))
            self._wait_for(job_id, {JobState.SUCCEEDED.value})

        self.assertEqual(len(self.manager.list_jobs("task-a")), 1)
        self.assertEqual(self.manager.list_jobs("other-task"), [])

    def test_job_store_is_written_with_owner_only_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX 权限位不适用")
        source = self.base / "final-1.mp4"
        source.write_bytes(b"v")

        with patch(
            "app.services.thumbnail_jobs.render_thumbnail", _SlowRender(delay=0.05)
        ):
            job_id = self.manager.submit("task-a", self._request(source))
            self._wait_for(job_id, {JobState.SUCCEEDED.value})

        mode = (self.root / "jobs.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()


class TestRestartDoesNotResurrectOrphanedWork(unittest.TestCase):
    """
    进程重启后，排队中的作业必须立刻有个明确的说法。

    早先这里尝试"把过期的标 unknown、新鲜的重新排队"。重新排队是**做不到的**：
    状态文件里只存了作业元数据（``JobRecord``），执行所需的 ``source`` /
    ``font_path`` / ``overlay_text`` 都不在其中——``_store`` 里写进 JSON 的
    ``request`` 字段会被 ``JobRecord.from_json`` 直接丢弃。于是重排队只把一个
    ``job_id`` 字符串塞回队列，而 worker 按 ``(job_id, request, tools)`` 三元组
    解包，解包异常发生在 try 之外：**整条 worker 线程当场退出**，之后所有缩略图
    作业永远停在"生成中"，且除了一条线程异常告警之外没有任何症状。

    在 ``scratch_mode = "ram"``（出厂默认）下，源文件本来就在内存盘上，进程一重启
    就没了，重跑必然失败。因此现在的选择是如实标成 unknown 并让用户重新提交，
    而不是假装能续跑。
    """

    def _store(self, root: Path, state: str, age_seconds: float) -> None:
        """伪造一份来自"上一个进程"的状态文件。"""
        now = time.time() - age_seconds
        record = {
            "job_id": "job-restart",
            "task_id": "task-restart",
            "state": state,
            "created_at": now,
            "updated_at": now,
            "video_index": 1,
            "error": "",
            "artifact_id": "",
            "request": {
                "source": str(root / "final-1.mp4"),
                "overlay_text": "",
                "font_path": str(root / "font.ttf"),
                "video_index": 1,
                "source_sha256": "a" * 64,
            },
        }
        (root / "jobs.json").write_text(
            json.dumps({"version": 1, "jobs": {"job-restart": record}}),
            encoding="utf-8",
        )

    def test_a_fresh_queued_job_is_reported_not_left_stuck_forever(self):
        """新鲜的排队作业也必须落到终态，并说清为什么。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "thumbnails" / "task-restart"
            root.mkdir(parents=True)
            # 只有 5 分钟，远未到 15 分钟的过期阈值
            self._store(root, "queued", age_seconds=300)

            manager = ThumbnailJobManager(root)
            try:
                record = manager.list_jobs("task-restart")[0]
                self.assertEqual(record["state"], "unknown")
                self.assertIn("重新提交", record["error"])
                # 没有执行者接手，就不能还挂着 pending，否则界面永远转圈
                self.assertFalse(manager.has_pending("task-restart"))
            finally:
                manager.shutdown(wait=True, timeout=2)

    def test_the_worker_thread_survives_a_restart_with_pending_records(self):
        """
        带着未完成记录重启之后，worker 线程必须还活着。

        线程一旦被一条坏队列项掀翻，后续所有作业都只会堆在"生成中"，而唯一线索
        是一条被测试框架吞掉的线程异常告警。这里显式检查线程存活，把那个"没人
        看得见的死法"钉住。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "thumbnails" / "task-restart"
            root.mkdir(parents=True)
            # 很久以前留下的：早先只有超过过期阈值的才会被标 unknown
            self._store(root, "running", age_seconds=6 * 3600)

            manager = ThumbnailJobManager(root)
            try:
                alive = manager._worker.is_alive()
                self.assertTrue(
                    alive, "重启后 worker 线程死了；此后缩略图作业会永远停在生成中"
                )
            finally:
                manager.shutdown(wait=True, timeout=2)

    def test_requeue_does_not_duplicate_an_already_finished_job(self):
        """已完成的作业不能被重跑，否则每次重启都会重复渲染一遍。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "thumbnails" / "task-restart"
            root.mkdir(parents=True)
            self._store(root, "succeeded", age_seconds=60)

            manager = ThumbnailJobManager(root)
            try:
                self.assertEqual(
                    manager.list_jobs("task-restart")[0]["state"], "succeeded"
                )
                self.assertFalse(manager.has_pending("task-restart"))
            finally:
                manager.shutdown(wait=True, timeout=2)


class TestManifestRecordsARealOverlayDigest(unittest.TestCase):
    """
    清单里的 overlay_sha256 必须是真实摘要，不能是占位符。

    原来的实现是 request.get("overlay_sha256", "c" * 64)：调用方没传就写进
    64 个 'c'。面板那条路径自己会算摘要，所以问题只在 API/CLI/直接提交时暴露，
    但清单是长期留存的可信记录——写进去一个假摘要，之后任何据此做的核对
    （"这张图是不是这行文字生成的"）都会得到错误答案，而且没人会发现。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "thumbnails" / "task-x"
        self.root.mkdir(parents=True)
        self.source = self.base / "final-1.mp4"
        self.source.write_bytes(b"video")
        self.manager = ThumbnailJobManager(self.root)
        self.addCleanup(self.manager.shutdown, True, 2)
        self.addCleanup(self._tmp.cleanup)

    def _wait(self, job_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            records = [r for r in self.manager.list_jobs("task-x") if r["job_id"] == job_id]
            if records and records[0]["state"] not in ("queued", "running"):
                return records[0]
            time.sleep(0.02)
        self.fail("job did not settle in time")

    def _manifest_artifact(self):
        from app.services.thumbnail_files import load_manifest
        manifest = load_manifest(self.root / "manifest.json")
        return manifest["videos"][0]["artifacts"][0]

    def test_derives_digest_from_overlay_text_when_caller_omits_it(self):
        import hashlib

        request = {
            "source": str(self.source),
            "overlay_text": "第 3 集",
            "font_path": str(self.base / "font.ttf"),
            "video_index": 1,
            "source_sha256": "a" * 64,
        }
        with patch("app.services.thumbnail_jobs.render_thumbnail", side_effect=_fake_render):
            job_id = self.manager.submit("task-x", request)
            self.assertEqual(self._wait(job_id)["state"], "succeeded")

        artifact = self._manifest_artifact()
        self.assertEqual(
            artifact["overlay_sha256"],
            hashlib.sha256("第 3 集".encode("utf-8")).hexdigest(),
        )

    def test_never_writes_the_placeholder_digest(self):
        request = {
            "source": str(self.source),
            "overlay_text": "hello",
            "font_path": str(self.base / "font.ttf"),
            "video_index": 1,
            "source_sha256": "a" * 64,
        }
        with patch("app.services.thumbnail_jobs.render_thumbnail", side_effect=_fake_render):
            job_id = self.manager.submit("task-x", request)
            self._wait(job_id)

        self.assertNotEqual(self._manifest_artifact()["overlay_sha256"], "c" * 64)

    def test_explicit_digest_is_preserved(self):
        """面板已经算好的摘要要原样保留，不能被重新计算覆盖。"""
        digest = "b" * 64
        request = {
            "source": str(self.source),
            "overlay_text": "hello",
            "overlay_sha256": digest,
            "font_path": str(self.base / "font.ttf"),
            "video_index": 1,
            "source_sha256": "a" * 64,
        }
        with patch("app.services.thumbnail_jobs.render_thumbnail", side_effect=_fake_render):
            job_id = self.manager.submit("task-x", request)
            self._wait(job_id)

        self.assertEqual(self._manifest_artifact()["overlay_sha256"], digest)
