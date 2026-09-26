"""验证缩略图渲染是确定性的、可校验的，且只依赖本地资源。

本模块只做一件事：把一段已知身份的本地视频变成一张 1280x720 的 JPEG。不碰
任务状态、不写清单、不认识 Streamlit。所有失败都要在没有产物的情况下发生——
一个"看起来成功"的半成品比明确的失败更难排查。
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.services.thumbnail import (
    MAX_OVERLAY_CHARS,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    ThumbnailError,
    ThumbnailSpec,
    _probe_duration_ms,
    _strip_jpeg_metadata,
    plan_frame_timestamp,
    render_thumbnail,
)
from app.utils.strict_media_tools import probe_media_tools


def _make_video(path: Path, width: int, height: int, seconds: int) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=size={width}x{height}:rate=1:duration={seconds}",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class TestPlanFrameTimestamp(unittest.TestCase):
    """抽帧时刻必须由时长确定，不能依赖运行时猜测。"""

    def test_uses_a_quarter_of_the_duration_for_a_long_clip(self):
        """长视频取 25% 处，避开片头。"""
        self.assertEqual(plan_frame_timestamp(40_000), 10.0)

    def test_clamps_to_leave_a_tail_on_a_short_clip(self):
        """
        短视频不能按比例取到接近结尾的位置：关键帧可能还没解码出来。
        """
        self.assertEqual(plan_frame_timestamp(2_000), 0.5)
        self.assertLess(plan_frame_timestamp(2_000), 2.0 - 0.1)

    def test_never_lands_on_the_last_tenth_of_a_second(self):
        for duration_ms in range(600, 5000, 137):
            with self.subTest(duration_ms=duration_ms):
                at = plan_frame_timestamp(duration_ms)
                self.assertLess(at, duration_ms / 1000 - 0.1 + 1e-9)

    def test_rejects_a_clip_shorter_than_the_supported_floor(self):
        """低于 0.6 秒的片段无法稳定抽帧，必须拒绝而不是硬抽。"""
        with self.assertRaises(ThumbnailError):
            plan_frame_timestamp(500)

    def test_rejects_a_non_positive_duration(self):
        for bad in (0, -1):
            with self.subTest(bad=bad):
                with self.assertRaises(ThumbnailError):
                    plan_frame_timestamp(bad)


class _RendererFixture(unittest.TestCase):
    """准备视频、字体与临时输出目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.source = self.base / "final-1.mp4"
        self.output = self.base / "out.jpg"
        self.font = self._font_path()
        self.tools = probe_media_tools()

    def tearDown(self):
        self._tmp.cleanup()

    def _font_path(self) -> Path:
        from app.utils.utils import resource_dir

        return Path(resource_dir("fonts")) / "Charm-Regular.ttf"

    def _video(self, width: int = 640, height: int = 360, seconds: int = 8) -> Path:
        if not self.source.exists():
            _make_video(self.source, width, height, seconds)
        return self.source

    def _spec(self, **overrides) -> ThumbnailSpec:
        fields = {
            "source": self._video(),
            "output": self.output,
            "overlay_text": "Three habits that actually compound",
            "font_path": self.font,
        }
        fields.update(overrides)
        return ThumbnailSpec(**fields)


class TestRenderThumbnail(_RendererFixture):
    """成功路径：尺寸、格式、内容、确定性。"""

    def test_produces_a_decodable_1280x720_jpeg(self):
        result = render_thumbnail(self._spec(), tools=self.tools)

        self.assertTrue(result.output_path.is_file())
        with Image.open(result.output_path) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (TARGET_WIDTH, TARGET_HEIGHT))
            image.verify()

    def test_strips_all_metadata(self):
        """
        JPEG 默认会被写入 EXIF/软件标记。产物要可复现且不泄露本机信息，
        因此不接受任何元数据段。
        """
        result = render_thumbnail(self._spec(), tools=self.tools)

        with Image.open(result.output_path) as image:
            self.assertEqual(image.info, {})
            self.assertFalse(image.getexif())

    def test_embeds_the_overlay_text_onto_the_frame(self):
        """加了文字和没加文字必须产生不同的像素。"""
        with_text = render_thumbnail(
            self._spec(overlay_text="Some overlay copy"), tools=self.tools
        )
        plain_bytes = with_text.output_path.read_bytes()

        without = render_thumbnail(self._spec(overlay_text=""), tools=self.tools)

        self.assertNotEqual(plain_bytes, without.output_path.read_bytes())

    def test_is_deterministic_for_the_same_input(self):
        """
        同样的输入必须产出同样的字节，否则"重新生成"无法与旧结果比较，
        清单里的哈希也就失去意义。
        """
        first = render_thumbnail(self._spec(), tools=self.tools)
        first_bytes = first.output_path.read_bytes()

        second = render_thumbnail(self._spec(), tools=self.tools)

        self.assertEqual(first.output_sha256, second.output_sha256)
        self.assertEqual(first_bytes, second.output_path.read_bytes())

    def test_reports_the_sha256_of_what_was_written(self):
        import hashlib

        result = render_thumbnail(self._spec(), tools=self.tools)

        self.assertEqual(
            result.output_sha256,
            hashlib.sha256(result.output_path.read_bytes()).hexdigest(),
        )

    def test_records_the_frame_timestamp_it_used(self):
        """清单需要知道抽帧时刻，否则换一次渲染结果就不可比。"""
        result = render_thumbnail(self._spec(), tools=self.tools)

        self.assertAlmostEqual(result.frame_at_seconds, 2.0, places=3)

    def test_uses_the_worker_supplied_duration_when_provided(self):
        result = render_thumbnail(self._spec(duration_ms=20_000), tools=self.tools)

        self.assertAlmostEqual(result.frame_at_seconds, 5.0, places=3)

    def test_creates_missing_parent_directories(self):
        nested = self.base / "a" / "b" / "thumb.jpg"
        result = render_thumbnail(self._spec(output=nested), tools=self.tools)

        self.assertTrue(result.output_path.is_file())


class TestRenderThumbnailRejections(_RendererFixture):
    """失败路径：必须没有产物，且错误信息可定位。"""

    def _assert_no_output(self):
        self.assertFalse(
            self.output.exists(), "失败时不得留下任何产物"
        )

    def test_rejects_a_missing_source(self):
        spec = self._spec(source=self.base / "absent.mp4")

        with self.assertRaises(ThumbnailError):
            render_thumbnail(spec, tools=self.tools)

        self._assert_no_output()

    def test_rejects_a_symlinked_source(self):
        """软链源文件意味着读取的是任务目录之外的内容。"""
        real = self.base / "elsewhere.mp4"
        _make_video(real, 320, 180, 2)
        linked = self.base / "linked.mp4"
        linked.symlink_to(real)

        with self.assertRaises(ThumbnailError) as ctx:
            render_thumbnail(self._spec(source=linked), tools=self.tools)

        self.assertIn("符号链接", str(ctx.exception))
        self._assert_no_output()

    def test_rejects_a_non_regular_source(self):
        with self.assertRaises(ThumbnailError):
            render_thumbnail(self._spec(source=self.base), tools=self.tools)

        self._assert_no_output()

    def test_rejects_a_source_that_is_not_a_video(self):
        junk = self.base / "junk.mp4"
        junk.write_bytes(b"definitely not an mp4")

        with self.assertRaises(ThumbnailError):
            render_thumbnail(self._spec(source=junk), tools=self.tools)

        self._assert_no_output()

    def test_rejects_overlong_overlay_text(self):
        with self.assertRaises(ThumbnailError) as ctx:
            render_thumbnail(
                self._spec(overlay_text="x" * (MAX_OVERLAY_CHARS + 1)),
                tools=self.tools,
            )

        self.assertIn(str(MAX_OVERLAY_CHARS), str(ctx.exception))
        self._assert_no_output()

    def test_rejects_control_characters_in_overlay_text(self):
        """
        控制字符会让文字渲染成不可见块，且可能把文字挤出安全区。
        """
        for bad in ("a\nb", "a\tb", "a\x00b", "a\x07b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ThumbnailError):
                    render_thumbnail(
                        self._spec(overlay_text=bad), tools=self.tools
                    )
                self._assert_no_output()

    def test_rejects_bidirectional_override_in_overlay_text(self):
        """
        双向覆盖字符可以让文字显示顺序与实际内容不一致，
        对"即将发布的标题"这类内容是实打实的欺骗手段。
        """
        with self.assertRaises(ThumbnailError):
            render_thumbnail(
                self._spec(overlay_text="safe\u202egnitset"), tools=self.tools
            )

    def test_rejects_a_missing_font(self):
        with self.assertRaises(ThumbnailError) as ctx:
            render_thumbnail(
                self._spec(font_path=self.base / "nope.ttf"), tools=self.tools
            )

        self.assertIn("字体", str(ctx.exception))
        self._assert_no_output()

    def test_rejects_a_font_outside_the_bundled_directory(self):
        """
        字体路径必须来自内置目录。接受调用方给的任意路径等于允许
        加载任意文件来解析。
        """
        outside = self.base / "evil.ttf"
        outside.write_bytes(self.font.read_bytes())

        with self.assertRaises(ThumbnailError) as ctx:
            render_thumbnail(self._spec(font_path=outside), tools=self.tools)

        self.assertIn("字体", str(ctx.context) if False else str(ctx.exception))
        self._assert_no_output()

    def test_rejects_a_non_image_font(self):
        fake = self.base / "Fake.ttf"
        fake.write_bytes(b"not a font")

        with self.assertRaises(ThumbnailError):
            render_thumbnail(self._spec(font_path=fake), tools=self.tools)

        self._assert_no_output()

    def test_rejects_a_clip_shorter_than_the_floor(self):
        short = self.base / "short.mp4"
        _make_video(short, 320, 180, 1)
        # 真实时长 1 秒高于 0.6 秒下限，用 0.5 秒验证下限本身被强制
        with self.assertRaises(ThumbnailError):
            render_thumbnail(
                self._spec(source=short, duration_ms=400), tools=self.tools
            )

        self._assert_no_output()

    def test_rejects_an_unknown_duration(self):
        """
        未知时长不能按 0 处理——那会抽到不存在的帧。
        """
        with self.assertRaises(ThumbnailError):
            render_thumbnail(
                self._spec(duration_ms=0), tools=self.tools
            )

        self._assert_no_output()

    def test_does_not_overwrite_an_existing_output_on_failure(self):
        """失败时不能把已有产物换成空文件。"""
        self.output.write_bytes(b"previous good artifact")

        with self.assertRaises(ThumbnailError):
            render_thumbnail(
                self._spec(overlay_text="x" * 500), tools=self.tools
            )

        self.assertEqual(self.output.read_bytes(), b"previous good artifact")


class TestProbeAndTimingPaths(_RendererFixture):
    """探测、时长与时刻的异常分支。"""

    def _assert_no_output(self):
        self.assertFalse(self.output.exists(), "失败时不得留下任何产物")

    def test_wraps_a_probe_oserror(self):
        # 先把视频造好：patch 会连带拦截造视频用的 subprocess.run。
        spec = self._spec()
        with patch("subprocess.run", side_effect=FileNotFoundError("gone")):
            with self.assertRaises(ThumbnailError):
                render_thumbnail(spec, tools=self.tools)

        self._assert_no_output()

    def test_wraps_a_probe_timeout(self):
        spec = self._spec()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 20)):
            with self.assertRaises(ThumbnailError):
                render_thumbnail(spec, tools=self.tools)

    def test_rejects_a_probe_nonzero_exit(self):
        spec = self._spec()
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Invalid data found"
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ThumbnailError) as ctx:
                render_thumbnail(spec, tools=self.tools)

        self.assertIn("Invalid data found", str(ctx.exception))

    def test_rejects_non_json_probe_output(self):
        spec = self._spec()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="garbage", stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ThumbnailError):
                render_thumbnail(spec, tools=self.tools)

    def test_rejects_an_unparsable_duration(self):
        # 视频必须在 patch 之前造好，否则造视频的 subprocess.run 也会被拦截。
        spec = self._spec()
        for raw in ("abc", "0", "-3", None, "N/A", ""):
            with self.subTest(raw=raw):
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(
                        {"streams": [{"duration": raw}], "format": {"duration": raw}}
                    ),
                    stderr="",
                )
                with patch("subprocess.run", return_value=completed):
                    with self.assertRaises(ThumbnailError):
                        render_thumbnail(spec, tools=self.tools)

    def test_uses_the_probed_duration_to_pick_the_frame(self):
        """探测到的时长决定抽帧时刻：6 秒 -> 1.5 秒。"""
        with patch(
            "app.services.thumbnail._probe_duration_ms", return_value=6_000
        ):
            result = render_thumbnail(self._spec(), tools=self.tools)

        self.assertAlmostEqual(result.frame_at_seconds, 1.5, places=3)

    def test_probe_prefers_stream_duration_over_container_duration(self):
        """
        部分容器只在 format 层给时长。直接测探测函数，
        因为整体 mock 掉 subprocess.run 会连 FFmpeg 抽帧一起拦掉。
        """
        source = self._video()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"streams": [{"duration": "4.5"}], "format": {"duration": "6"}}
            ),
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            self.assertEqual(_probe_duration_ms(source, Path("/bin/true")), 4_500)

    def test_probe_falls_back_to_container_duration(self):
        """流层没有时长时用 format 层的。"""
        source = self._video()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"streams": [{"duration": None}], "format": {"duration": "6"}}
            ),
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            self.assertEqual(_probe_duration_ms(source, Path("/bin/true")), 6_000)

    def test_probe_reports_a_missing_duration(self):
        source = self._video()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"streams": [{}]}), stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ThumbnailError) as ctx:
                _probe_duration_ms(source, Path("/bin/true"))

        self.assertIn("时长未知", str(ctx.exception))

    def test_rejects_an_out_of_range_explicit_frame_timestamp(self):
        for bad in (0.0, -1.0, 999.0):
            with self.subTest(bad=bad):
                with self.assertRaises(ThumbnailError):
                    render_thumbnail(
                        self._spec(frame_at_seconds=bad), tools=self.tools
                    )

    def test_rejects_a_relative_output_path(self):
        with self.assertRaises(ThumbnailError) as ctx:
            render_thumbnail(self._spec(output=Path("relative.jpg")), tools=self.tools)

        self.assertIn("绝对路径", str(ctx.exception))

    def test_rejects_a_relative_source_path(self):
        with self.assertRaises(ThumbnailError):
            render_thumbnail(self._spec(source=Path("relative.mp4")), tools=self.tools)

    def test_rejects_a_non_string_overlay(self):
        with self.assertRaises(ThumbnailError):
            render_thumbnail(self._spec(overlay_text=123), tools=self.tools)

    def test_treats_whitespace_only_overlay_as_absent(self):
        """纯空白等价于不叠加，不该因此失败。"""
        result = render_thumbnail(self._spec(overlay_text="   "), tools=self.tools)

        self.assertTrue(result.output_path.is_file())

    def test_treats_none_overlay_as_absent(self):
        result = render_thumbnail(self._spec(overlay_text=None), tools=self.tools)

        self.assertTrue(result.output_path.is_file())

    def test_rejects_a_missing_font_argument(self):
        with self.assertRaises(ThumbnailError) as ctx:
            render_thumbnail(self._spec(font_path=None), tools=self.tools)

        self.assertIn("字体", str(ctx.exception))

    def test_rejects_a_relative_font_path(self):
        with self.assertRaises(ThumbnailError):
            render_thumbnail(self._spec(font_path=Path("f.ttf")), tools=self.tools)

    def test_rejects_overlay_text_wider_than_the_safe_area(self):
        """
        缩到最小字号仍放不下的文字必须被拒绝，而不是被静默裁掉或压成一行。
        """
        with self.assertRaises(ThumbnailError):
            render_thumbnail(
                self._spec(overlay_text="WWWW " * 40), tools=self.tools
            )

        self._assert_no_output()

    def test_rejects_a_non_integer_duration(self):
        with self.assertRaises(ThumbnailError):
            plan_frame_timestamp("2000")

    def test_rejects_a_boolean_duration(self):
        """布尔值是 int 的子类，必须显式拒绝。"""
        with self.assertRaises(ThumbnailError):
            plan_frame_timestamp(True)


class TestStripJpegMetadata(unittest.TestCase):
    """字节层面的元数据剥离。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data: bytes) -> Path:
        path = self.base / "sample.jpg"
        path.write_bytes(data)
        return path

    def test_rejects_a_file_without_a_soi_marker(self):
        path = self._write(b"not a jpeg at all")
        with self.assertRaises(ThumbnailError):
            _strip_jpeg_metadata(path)

    def test_rejects_a_malformed_segment_length(self):
        path = self._write(b"\xff\xd8\xff\xe0\x00\x01garbage")
        with self.assertRaises(ThumbnailError):
            _strip_jpeg_metadata(path)

    def test_rejects_a_non_marker_byte_in_the_segment_chain(self):
        path = self._write(b"\xff\xd8\x41\x42\x43\x44")
        with self.assertRaises(ThumbnailError):
            _strip_jpeg_metadata(path)

    def test_stops_cleanly_at_eoi(self):
        """以 EOI 结束的文件应原样保留而不报错。"""
        path = self._write(b"\xff\xd8\xff\xd9")
        _strip_jpeg_metadata(path)
        self.assertEqual(path.read_bytes(), b"\xff\xd8\xff\xd9")


if __name__ == "__main__":
    unittest.main()
