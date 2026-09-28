"""中间产物必须只落在内存盘里，且用完即删。

这些测试盯住的核心不变量只有一条：**SSD 上不留任何中间产物**。
之前 ``storage/tasks/<id>/`` 里会攒下 audio.mp3、subtitle.srt、script.json
和 combined-N.mp4，``storage/cache_videos/`` 更是能到几个 GB——全是
"用完就没用"的中间产物，却一直躺在固态硬盘上。

单元测试不碰真实的 hdiutil：内存盘相关命令都被替换掉，只验证编排逻辑
是否正确。真实挂载由下面的集成用例单独验证。
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils.scratch import ScratchError, ScratchSpace


class _FakeDisk:
    """假的 diskutil/newfs：把"内存盘"落在临时目录里。"""

    def __init__(self, base: Path):
        self.base = base
        self.attached: list[str] = []
        self.mounted: list[str] = []
        self.unmounted: list[str] = []
        self.fail_attach = False

    def run(self, cmd, **kwargs):
        if self.fail_attach:
            raise subprocess.CalledProcessError(1, cmd, stderr="no permission")
        if cmd[:2] == ["diskutil", "image"]:
            if self.fail_attach:
                raise subprocess.CalledProcessError(1, cmd)
            self.attached.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "/dev/disk99\n", "")
        if cmd[:2] == ["diskutil", "mount"]:
            mount_point = Path(cmd[cmd.index("-mountPoint") + 1])
            mount_point.mkdir(parents=True, exist_ok=True)
            self.mounted.append(str(mount_point))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["diskutil", "unmount"]:
            self.unmounted.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "newfs_hfs":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")


class TestScopedScratchIsAlwaysRemoved(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.fake = _FakeDisk(self.base)
        self.space = ScratchSpace(
            mode="ram", size_mb=64, mount_point=self.base / "mnt"
        )
        patcher = patch("app.utils.scratch.subprocess.run", side_effect=self.fake.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.space.shutdown)

    def test_directory_is_removed_after_normal_exit(self):
        with self.space.scoped("task") as scratch:
            target = scratch
            (target / "audio.mp3").write_bytes(b"x" * 32)
            self.assertTrue(target.is_dir())
        self.assertFalse(target.exists(), "正常退出后中间目录必须消失")

    def test_directory_is_removed_when_the_body_raises(self):
        captured = {}
        with self.assertRaises(ValueError):
            with self.space.scoped("task") as scratch:
                captured["path"] = scratch
                (scratch / "subtitle.srt").write_text("x")
                raise ValueError("boom")
        self.assertFalse(captured["path"].exists(), "异常退出也必须清理干净")

    def test_nothing_survives_under_the_scratch_root(self):
        """根目录里不该剩下任何东西——否则"用完即删"就是空话。"""
        for _ in range(3):
            with self.space.scoped("task") as scratch:
                (scratch / "clip.mp4").write_bytes(b"y" * 16)
        leftovers = [p for p in self.space.ensure_ready().iterdir()]
        self.assertEqual(leftovers, [], f"残留：{leftovers}")

    def test_every_call_gets_a_unique_directory(self):
        seen = set()
        for _ in range(5):
            with self.space.scoped("task") as scratch:
                self.assertNotIn(scratch, seen)
                seen.add(scratch)
        self.assertEqual(len(seen), 5)

    def test_purge_removes_leftovers_from_a_crash(self):
        root = self.space.ensure_ready()
        orphan = root / "leftover-from-crash"
        orphan.mkdir()
        (orphan / "big.mp4").write_bytes(b"z" * 64)
        removed = self.space.purge()
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(orphan.exists())


class TestScratchFallbackIsLoud(unittest.TestCase):
    """内存盘建不起来时可以降级，但必须让人知道。"""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _space_with_broken_disk(self):
        space = ScratchSpace(
            mode="ram", size_mb=64, mount_point=Path(self._tmp.name) / "mnt"
        )
        patcher = patch(
            "app.utils.scratch.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["diskutil"]),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(space.shutdown)
        return space

    def test_falls_back_to_a_temp_dir_and_records_why(self):
        space = self._space_with_broken_disk()
        root = space.ensure_ready()
        self.assertTrue(root.is_dir())
        self.assertFalse(space.on_ram_disk, "降级后必须诚实地报告不在内存盘上")
        self.assertIn("CalledProcessError", space.fallback_reason)

    def test_fallback_is_logged_as_a_warning(self):
        space = self._space_with_broken_disk()
        with self.assertLogs("app.utils.scratch", level="WARNING") as logs:
            space.ensure_ready()
        self.assertTrue(
            any("磁盘" in line for line in logs.output),
            f"降级必须告警，实际：{logs.output}",
        )


class TestScratchModes(unittest.TestCase):
    def test_off_mode_refuses_to_allocate(self):
        """显式关闭时不能假装有内存盘——那等于把中间产物写回应用目录。"""
        space = ScratchSpace(mode="off")
        self.addCleanup(space.shutdown)
        self.assertIsNone(space.ensure_ready())
        with self.assertRaises(ScratchError):
            with space.scoped("task"):
                pass

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ScratchError):
            ScratchSpace(mode="somewhere-else")

    def test_disk_mode_uses_a_temp_dir_without_pretending(self):

        space = ScratchSpace(mode="disk")
        self.addCleanup(space.shutdown)
        root = space.ensure_ready()
        self.assertTrue(root.is_dir())
        self.assertFalse(space.on_ram_disk)


class TestRealRamDiskMounts(unittest.TestCase):
    """真挂一次内存盘：确认这台机器上确实能建起来。"""

    def test_real_ram_disk_supports_write_and_unmount(self):
        space = ScratchSpace(mode="ram", size_mb=32)
        try:
            root = space.ensure_ready()
            if not space.on_ram_disk:
                self.skipTest(f"本机无法建立内存盘：{space.fallback_reason}")
            probe = root / "probe.bin"
            probe.write_bytes(b"z" * (2 * 1024 * 1024))
            self.assertEqual(probe.stat().st_size, 2 * 1024 * 1024)
            probe.unlink()
        finally:
            space.shutdown()


if __name__ == "__main__":
    unittest.main()
