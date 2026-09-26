"""验证严格模式下的媒体工具解析：只接受显式、真实、绝对的可执行文件。

既有的 ``utils.get_ffmpeg_binary()`` 允许一路退化到字符串 ``"ffmpeg"``，交给
subprocess 在运行时才报错。那对视频链路是可接受的既有行为，但新链路要在
**启动时**就知道自己用的是哪个二进制、版本是多少，否则产物出问题时无从追溯。
"""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.utils.strict_media_tools import (
    MediaTools,
    MediaToolsError,
    probe_media_tools,
    resolve_media_tool,
)


class TestResolveMediaTool(unittest.TestCase):
    """解析结果必须是绝对、存在、非链接、可执行的真实文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir()

        self.real_ffmpeg = self._make_executable(self.bin_dir / "ffmpeg")
        self.real_ffprobe = self._make_executable(self.bin_dir / "ffprobe")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_executable(self, path: Path) -> Path:
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def test_returns_the_explicit_configured_path(self):
        """显式配置的优先级最高。"""
        resolved = resolve_media_tool(
            "ffmpeg",
            configured_path=self.real_ffmpeg,
            env_path=None,
        )

        self.assertEqual(resolved, self.real_ffmpeg)

    def test_falls_back_to_the_environment_variable(self):
        """没有显式配置时使用环境变量。"""
        resolved = resolve_media_tool(
            "ffprobe",
            configured_path=None,
            env_path=str(self.real_ffprobe),
        )

        self.assertEqual(resolved, self.real_ffprobe)

    def test_falls_back_to_path_lookup_as_a_bare_name(self):
        """
        都没有配置时只回退到 PATH 查找，并且必须解析成绝对路径。
        绝不接受字符串 "ffmpeg" 直接交给 subprocess。
        """
        with patch.dict(os.environ, {"PATH": str(self.bin_dir)}):
            resolved = resolve_media_tool("ffprobe", configured_path=None, env_path=None)

        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.name, "ffprobe")

    def test_rejects_a_bare_command_name(self):
        """
        退化到裸名字正是要消除的行为：出错点会被推迟到运行时的某一帧。
        """
        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "ffmpeg",
                configured_path="ffmpeg",
                env_path=None,
            )

    def test_rejects_relative_paths(self):
        """相对路径的落点依赖进程工作目录。"""
        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "ffmpeg",
                configured_path="./bin/ffmpeg",
                env_path=None,
            )

    def test_rejects_missing_file(self):
        with self.assertRaises(MediaToolsError) as ctx:
            resolve_media_tool(
                "ffmpeg",
                configured_path=self.base / "not-here",
                env_path=None,
            )

        self.assertIn("ffmpeg", str(ctx.exception))

    def test_rejects_directory(self):
        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "ffmpeg",
                configured_path=self.bin_dir,
                env_path=None,
            )

    def test_symlink_resolves_to_the_real_binary(self):
        """
        Homebrew 的 /opt/homebrew/bin/ffmpeg 本身就是软链，所以不能一律拒绝。
        正确做法是解引用到真实文件并校验/记录真实文件——否则这个模块在
        本机上根本不可用。返回的必须是真实路径，不是链接路径。
        """
        linked = self.base / "ffmpeg-link"
        linked.symlink_to(self.real_ffmpeg)

        resolved = resolve_media_tool(
            "ffmpeg",
            configured_path=linked,
            env_path=None,
        )

        self.assertEqual(resolved, self.real_ffmpeg)
        self.assertFalse(resolved.is_symlink())

    def test_dangling_symlink_is_rejected(self):
        """指向不存在目标的链接无法确定执行的是哪个程序。"""
        dangling = self.base / "ffmpeg-dangling"
        dangling.symlink_to(self.base / "gone")

        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "ffmpeg",
                configured_path=dangling,
                env_path=None,
            )

    def test_symlink_to_a_non_executable_target_is_rejected(self):
        """解引用之后仍要校验可执行性。"""
        plain = self.base / "ffmpeg-plain"
        plain.write_text("not executable")
        linked = self.base / "ffmpeg-to-plain"
        linked.symlink_to(plain)

        with self.assertRaises(MediaToolsError) as ctx:
            resolve_media_tool(
                "ffmpeg",
                configured_path=linked,
                env_path=None,
            )

        self.assertIn("可执行", str(ctx.exception))

    def test_symlink_to_a_directory_is_rejected(self):
        linked = self.base / "ffmpeg-to-dir"
        linked.symlink_to(self.bin_dir, target_is_directory=True)

        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "ffmpeg",
                configured_path=linked,
                env_path=None,
            )

    def test_rejects_non_executable_file(self):
        plain = self.base / "ffmpeg-plain"
        plain.write_text("not executable")

        with self.assertRaises(MediaToolsError) as ctx:
            resolve_media_tool(
                "ffmpeg",
                configured_path=plain,
                env_path=None,
            )

        self.assertIn("可执行", str(ctx.exception))

    def test_rejects_blank_configuration(self):
        """空白字符串不是有效配置，且不能被当成"未配置"以外的意思。"""
        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "ffmpeg",
                configured_path="   ",
                env_path=None,
            )

    def test_falls_through_a_blank_env_value_to_path_lookup(self):
        """
        环境变量存在但为空时应继续往下找，而不是直接失败——
        空值通常来自 CI 里未展开的变量。
        """
        with patch.dict(os.environ, {"PATH": str(self.bin_dir)}):
            resolved = resolve_media_tool(
                "ffprobe",
                configured_path=None,
                env_path="  ",
            )

        self.assertEqual(resolved.name, "ffprobe")

    def test_unsupported_tool_name_is_rejected(self):
        """只认白名单里的工具名，避免把任意字符串拼进查找逻辑。"""
        with self.assertRaises(MediaToolsError):
            resolve_media_tool(
                "rm",
                configured_path=self.real_ffmpeg,
                env_path=None,
            )

    def test_path_lookup_failure_names_the_tool(self):
        """PATH 里找不到时报错必须点名，方便排查缺失的依赖。"""
        with patch.dict(os.environ, {"PATH": str(self.base / "empty")}):
            with self.assertRaises(MediaToolsError) as ctx:
                resolve_media_tool("ffprobe", configured_path=None, env_path=None)

        self.assertIn("ffprobe", str(ctx.exception))


class TestMediaTools(unittest.TestCase):
    """MediaTools 必须把版本和二进制哈希一并记录下来。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_records_paths_versions_and_digests(self):
        """
        启动时固定"用的是哪个二进制"，产物出问题时才能复现。
        """
        tools = MediaTools(
            ffmpeg=self.base / "ffmpeg",
            ffprobe=self.base / "ffprobe",
            ffmpeg_version="8.0.1",
            ffprobe_version="8.0.1",
            ffmpeg_sha256="a" * 64,
            ffprobe_sha256="b" * 64,
        )

        self.assertEqual(tools.ffmpeg_version, "8.0.1")
        self.assertEqual(len(tools.ffmpeg_sha256), 64)
        self.assertEqual(len(tools.ffprobe_sha256), 64)

    def test_manifest_records_the_toolchain(self):
        """记录形式要能直接写进产物元数据。"""
        tools = MediaTools(
            ffmpeg=self.base / "ffmpeg",
            ffprobe=self.base / "ffprobe",
            ffmpeg_version="8.0.1",
            ffprobe_version="8.0.1",
            ffmpeg_sha256="a" * 64,
            ffprobe_sha256="b" * 64,
        )

        manifest = tools.to_manifest()

        self.assertEqual(manifest["ffmpeg_version"], "8.0.1")
        self.assertEqual(manifest["ffprobe_sha256"], "b" * 64)
        self.assertNotIn(str(self.base), str(manifest))

    def test_is_frozen(self):
        """工具链信息在进程内共享，可变会让记录失真。"""
        tools = MediaTools(
            ffmpeg=self.base / "ffmpeg",
            ffprobe=self.base / "ffprobe",
            ffmpeg_version="8.0.1",
            ffprobe_version="8.0.1",
            ffmpeg_sha256="a" * 64,
            ffprobe_sha256="b" * 64,
        )

        with self.assertRaises(Exception):
            tools.ffmpeg_version = "9.9.9"


class TestProbeMediaTools(unittest.TestCase):
    """启动时固定工具链身份：版本号与二进制哈希都必须拿得到。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _script(self, name: str, body: str) -> Path:
        path = self.base / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def test_records_version_and_digest_from_the_real_binaries(self):
        """
        解析成功后必须拿到非空版本号和 64 位十六进制哈希，
        否则"产物出自哪个工具链"就无从追溯。
        """
        tools = probe_media_tools()

        self.assertTrue(tools.ffmpeg.is_absolute())
        self.assertTrue(tools.ffprobe.is_absolute())
        self.assertTrue(tools.ffmpeg_version)
        self.assertTrue(tools.ffprobe_version)
        for digest in (tools.ffmpeg_sha256, tools.ffprobe_sha256):
            self.assertEqual(len(digest), 64)
            int(digest, 16)

    def test_manifest_round_trips_into_a_serialisable_mapping(self):
        tools = probe_media_tools()
        manifest = tools.to_manifest()

        self.assertEqual(
            set(manifest),
            {
                "ffmpeg_version",
                "ffprobe_version",
                "ffmpeg_sha256",
                "ffprobe_sha256",
            },
        )
        self.assertEqual(manifest["ffmpeg_sha256"], tools.ffmpeg_sha256)

    def test_reads_the_first_line_of_version_output(self):
        ffmpeg = self._script("ffmpeg", 'echo "ffmpeg version 8.0.1 Copyright"')
        ffprobe = self._script("ffprobe", 'echo "ffprobe version 8.0.1 Copyright"')

        tools = probe_media_tools(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

        self.assertEqual(tools.ffmpeg_version, "ffmpeg version 8.0.1 Copyright")
        self.assertEqual(tools.ffprobe_version, "ffprobe version 8.0.1 Copyright")

    def test_digest_matches_the_actual_file_content(self):
        ffmpeg = self._script("ffmpeg", 'echo "ffmpeg version 8.0.1"')
        ffprobe = self._script("ffprobe", 'echo "ffprobe version 8.0.1"')

        tools = probe_media_tools(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

        self.assertEqual(tools.ffmpeg_sha256, hashlib.sha256(ffmpeg.read_bytes()).hexdigest())
        self.assertEqual(
            tools.ffprobe_sha256, hashlib.sha256(ffprobe.read_bytes()).hexdigest()
        )

    def test_nonzero_exit_fails_closed(self):
        """
        拿不到版本号就不能继续：无法追溯来源的产物等于没有元数据。
        """
        ffmpeg = self._script("ffmpeg", "exit 3")
        ffprobe = self._script("ffprobe", 'echo "ffprobe version 8.0.1"')

        with self.assertRaises(MediaToolsError) as ctx:
            probe_media_tools(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

        self.assertIn("ffmpeg", str(ctx.exception))

    def test_empty_version_output_fails_closed(self):
        ffmpeg = self._script("ffmpeg", "true")
        ffprobe = self._script("ffprobe", 'echo "ffprobe version 8.0.1"')

        with self.assertRaises(MediaToolsError):
            probe_media_tools(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

    def test_unrunnable_binary_fails_closed(self):
        """
        校验通过但执行不了（例如损坏的二进制）也必须失败。
        """
        ffmpeg = self.base / "ffmpeg-broken"
        ffmpeg.write_bytes(b"\x7fELF-not-really")
        ffmpeg.chmod(0o755)
        ffprobe = self._script("ffprobe", 'echo "ffprobe version 8.0.1"')

        with self.assertRaises(MediaToolsError):
            probe_media_tools(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)

    def test_environment_variable_is_used_when_no_path_is_configured(self):
        ffmpeg = self._script("ffmpeg", 'echo "ffmpeg version 8.0.1"')
        ffprobe = self._script("ffprobe", 'echo "ffprobe version 8.0.1"')

        with patch.dict(
            os.environ,
            {
                "IMAGEIO_FFMPEG_EXE": str(ffmpeg),
                "MPT_FFPROBE_PATH": str(ffprobe),
            },
        ):
            tools = probe_media_tools()

        self.assertEqual(tools.ffmpeg, ffmpeg)
        self.assertEqual(tools.ffprobe, ffprobe)


if __name__ == "__main__":
    unittest.main()
