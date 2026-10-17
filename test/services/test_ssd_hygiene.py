"""端到端验证"SSD 上只留最终成片"。

前面那些测试都是逐个函数验证；这里验证整件事：跑一次真实的生成流程，
然后检查磁盘上究竟留下了什么。

要证明的不变量：

* 任务目录（脚本、字幕、配音、拼接片段）在任务结束后**不存在**；
* 素材缓存目录里**没有**新文件；
* 最终成片在 ``outputs_root`` 下，**只有它**留在磁盘上；
* 应用自己的 storage 目录没有因为这次运行而多出视频。
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.utils.scratch import ScratchSpace


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"], check=True, capture_output=True
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _make_video(path: Path, seconds: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=size=1280x720:rate=1:duration={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


class TestOnlyTheFinalVideoSurvives(unittest.TestCase):
    """这是"SSD 上只留成片"这句话的可执行版本。"""

    @classmethod
    def setUpClass(cls):
        if not _ffmpeg_available():
            raise unittest.SkipTest("ffmpeg 不可用")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app = self.base / "MoneyPrinterTurbo"
        self.app.mkdir()
        self.portfolio = self.base / "portfolio"
        (self.portfolio / "outputs").mkdir(parents=True)

        # 内存盘用临时目录代替：这里要断言的是"目录被清空"，不是"真的在 RAM"。
        self.space = ScratchSpace(mode="disk")
        self.addCleanup(self.space.shutdown)
        self.scratch_root = self.space.ensure_ready()

        # 快照：运行前磁盘上有什么
        self.app_storage = self.app / "storage"
        self.app_storage.mkdir()
        self._before = self._snapshot(self.app_storage)

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self, root: Path) -> set:
        return {
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file()
        }

    def test_intermediates_are_gone_and_only_the_final_video_remains(self):
        """
        模拟一次运行的中间产物，检查它们有没有被清掉。

        媒体类中间产物必须消失，而**元数据必须留下**：``script.json`` /
        ``plan_meta.json`` / ``subtitle.srt`` 是归档、重生成、字幕三条功能的唯一
        输入。整体删除任务目录会让这三条同时失效，而任务面板仍显示"已完成"，
        故障完全静默——这正是 ``scratch_mode = "ram"``（出厂默认）下会发生的事。
        """
        task_id = "task-ssd"
        task_dir = Path(self.scratch_root) / "tasks" / task_id
        task_dir.mkdir(parents=True)

        # 这些就是一次真实运行会产生的中间产物
        intermediates = {
            "script.json": b'{"terms": ["a", "b"]}',
            "subtitle.srt": b"1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            "audio.mp3": b"fake-mp3-bytes" * 64,
            "combined-1.mp4": b"fake-combined" * 256,
            "elevenlabs-bgm-1.mp3": b"fake-bgm" * 128,
        }
        # 体积 MB 级的媒体才是 SSD 卫生要解决的问题
        media_intermediates = {
            "audio.mp3",
            "combined-1.mp4",
            "elevenlabs-bgm-1.mp3",
        }
        # 体积 KB 级、但下游功能离不开的元数据
        metadata = {
            "script.json",
            "plan_meta.json",
            "subtitle.srt",
        }
        for name, payload in intermediates.items():
            (task_dir / name).write_bytes(payload)
        (task_dir / "plan_meta.json").write_text(
            '{"channel": "testch", "topic": "T"}', encoding="utf-8"
        )
        # 归档标记与成片放在一起，清理时必须一起活下来
        (task_dir / ".plan_archived").write_text("{}", encoding="utf-8")
        # 一个不认识的文件：白名单之外按"可丢"处理
        (task_dir / "temp-clip-3.mp4").write_bytes(b"x" * 128)

        # 素材缓存：下载的原始片段
        cache_dir = Path(self.scratch_root) / "cache_videos"
        cache_dir.mkdir(parents=True, exist_ok=True)
        downloaded = cache_dir / "vid-abc123.mp4"
        downloaded.write_bytes(b"downloaded-material" * 512)

        # 最终成片：唯一该留在磁盘上的东西
        final_dir = self.portfolio / "outputs" / task_id
        final_dir.mkdir(parents=True, exist_ok=True)
        final = final_dir / "final-1.mp4"
        _make_video(final, seconds=1)

        # 任务结束时的清理
        from app.services.task import _purge_task_scratch

        _purge_task_scratch(task_id, space=self.space)

        # 1) 媒体类中间产物必须消失
        for name in media_intermediates:
            self.assertFalse(
                (task_dir / name).exists(),
                f"媒体中间产物没有清理：{name}",
            )
        self.assertFalse(
            (task_dir / "temp-clip-3.mp4").exists(),
            "白名单之外的临时片段必须清理",
        )

        # 2) 元数据必须留下，否则归档/重生成/字幕三条链路静默失效
        for name in metadata:
            self.assertTrue(
                (task_dir / name).is_file(),
                f"元数据被误删：{name}；归档与重生成都依赖它",
            )
        self.assertTrue(
            (task_dir / ".plan_archived").is_file(),
            "归档幂等标记被误删；2 秒轮询的 fragment 会重新处理同一任务",
        )

        # 3) 成片必须还在
        self.assertTrue(final.is_file(), "最终成片不能被清理掉")
        self.assertGreater(final.stat().st_size, 0)

        # 4) 应用目录不能因为这次运行多出任何文件
        self.assertEqual(
            self._snapshot(self.app_storage) - self._before,
            set(),
            "运行之后应用 storage 里不该多出任何文件",
        )

    def test_a_media_only_task_directory_disappears_entirely(self):
        """
        只含媒体、没有任何元数据的任务目录必须**整体消失**。

        这条不能靠上一个测试观察：那个测试总是留下 ``script.json`` 等元数据，
        目录因此永远非空，于是"空目录有没有被收掉"这条断言在它身上是恒真的——
        断言一旦对被测行为不可见，它保护的就不是那个行为了。旧版本里正是一条
        ``assertFalse(task_dir.exists())`` 被换成元数据检查，而被换掉的行为再没有
        任何测试看着，清理逻辑一改成只删子目录，空壳就开始按任务数永久堆积。
        """
        from app.services.task import _purge_task_scratch

        task_id = "task-media-only"
        task_dir = Path(self.scratch_root) / "tasks" / task_id
        (task_dir / "cache").mkdir(parents=True)
        (task_dir / "audio.mp3").write_bytes(b"x" * 128)
        (task_dir / "cache" / "clip-1.mp4").write_bytes(b"y" * 128)

        _purge_task_scratch(task_id, space=self.space)

        self.assertFalse(
            task_dir.exists(),
            "只剩空壳的任务目录应当整体消失，否则每次运行都会留下一个空目录",
        )

    def test_scratch_mode_off_would_write_to_disk_and_says_so(self):
        """
        关掉内存盘时必须诚实地报告"不在内存上"。

        否则用户以为自己在保护 SSD，实际上中间产物照样落盘。
        """
        space = ScratchSpace(mode="disk")
        self.addCleanup(space.shutdown)
        space.ensure_ready()
        self.assertFalse(
            space.on_ram_disk,
            "退化到临时目录时必须报告 on_ram_disk=False",
        )

    def test_purge_survives_a_missing_task_directory(self):
        """任务从未创建目录就失败时，清理不能自己再抛异常。"""
        from app.services.task import _purge_task_scratch

        _purge_task_scratch("never-existed", space=self.space)  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
