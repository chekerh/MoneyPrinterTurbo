import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from moviepy import (
    ImageClip,
    VideoFileClip,
)

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models.schema import MaterialInfo
from app.services import video as vd
from app.utils import utils

resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")


class _FakeMoviePyClip:
    """为最终混音单测提供最小 MoviePy 接口，避免 CI 真实编码大型视频。"""

    def __init__(self, *, duration=5, fps=44100):
        self.duration = duration
        self.fps = fps
        self.close_calls = 0
        self.with_audio_result = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.close_calls += 1

    def with_effects(self, _effects):
        return self

    def with_audio(self, _audio):
        return self.with_audio_result


class TestVideoService(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.test_img_path = os.path.join(resources_dir, "1.png")
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()

    def test_delete_files_deduplicates_paths_and_ignores_missing_files(self):
        """
        循环片段会让同一路径在拼接列表中重复出现，清理时每个路径只能删除一次。

        已不存在的文件属于幂等清理的正常状态，不应再产生误导用户的失败日志。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            existing_file = os.path.join(temp_dir, "temp-clip-1.mp4")
            missing_file = os.path.join(temp_dir, "already-removed.mp4")
            Path(existing_file).write_bytes(b"temporary clip")

            original_remove = os.remove
            with (
                patch.object(vd.os, "remove", wraps=original_remove) as remove,
                patch.object(vd.logger, "warning") as warning,
            ):
                vd.delete_files(
                    [
                        existing_file,
                        existing_file,
                        missing_file,
                        missing_file,
                    ]
                )

        self.assertEqual(
            [item.args[0] for item in remove.call_args_list],
            [existing_file, missing_file],
        )
        warning.assert_not_called()

    def test_delete_files_logs_actionable_os_errors(self):
        """权限等真实清理失败必须保留路径和系统错误，方便定位残留文件。"""
        with (
            patch.object(
                vd.os,
                "remove",
                side_effect=PermissionError("permission denied"),
            ),
            patch.object(vd.logger, "warning") as warning,
        ):
            vd.delete_files(["protected-temp-clip.mp4"])

        warning.assert_called_once()
        message = warning.call_args.args[0]
        self.assertIn("protected-temp-clip.mp4", message)
        self.assertIn("permission denied", message)

    def test_generate_video_reports_successful_bgm_mix_and_closes_sources(self):
        """BGM 混合成功后应返回 True，并释放所有原始文件 reader。"""
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=False,
            bgm_type="sonilo",
        )
        source_video = _FakeMoviePyClip()
        voice_source = _FakeMoviePyClip()
        bgm_source = _FakeMoviePyClip()
        mixed_audio = _FakeMoviePyClip(fps=48000)
        final_video = _FakeMoviePyClip()
        source_video.with_audio_result = final_video

        with (
            patch.object(
                vd, "_open_video_clip_quietly", return_value=source_video
            ),
            patch.object(
                vd, "AudioFileClip", side_effect=[voice_source, bgm_source]
            ),
            patch.object(vd, "CompositeAudioClip", return_value=mixed_audio),
            patch.object(vd, "_write_videofile_with_codec_fallback") as writer,
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"),
        ):
            result = vd.generate_video(
                video_path="combined.mp4",
                audio_path="voice.mp3",
                subtitle_path="",
                output_file="final.mp4",
                params=params,
                bgm_file_override="sonilo.m4a",
            )

        self.assertTrue(result)
        writer.assert_called_once()
        self.assertEqual(writer.call_args.kwargs["audio_fps"], 48000)
        self.assertEqual(source_video.close_calls, 1)
        self.assertEqual(voice_source.close_calls, 1)
        self.assertEqual(bgm_source.close_calls, 1)
        self.assertEqual(final_video.close_calls, 1)

    def test_generate_video_keeps_output_and_reports_failed_bgm_mix(self):
        """BGM 打开失败时仍应只写一次无 BGM 视频，并返回 False。"""
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=False,
            bgm_type="sonilo",
        )
        source_video = _FakeMoviePyClip()
        voice_source = _FakeMoviePyClip()
        final_video = _FakeMoviePyClip()
        source_video.with_audio_result = final_video

        with (
            patch.object(
                vd, "_open_video_clip_quietly", return_value=source_video
            ),
            patch.object(
                vd,
                "AudioFileClip",
                side_effect=[voice_source, RuntimeError("invalid BGM")],
            ),
            patch.object(vd, "CompositeAudioClip") as composite_audio,
            patch.object(vd, "_write_videofile_with_codec_fallback") as writer,
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"),
            patch.object(vd.logger, "exception") as log_exception,
        ):
            result = vd.generate_video(
                video_path="combined.mp4",
                audio_path="voice.mp3",
                subtitle_path="",
                output_file="final.mp4",
                params=params,
                bgm_file_override="broken.m4a",
            )

        self.assertFalse(result)
        writer.assert_called_once()
        composite_audio.assert_not_called()
        log_exception.assert_called_once()
        self.assertEqual(source_video.close_calls, 1)
        self.assertEqual(voice_source.close_calls, 1)
        self.assertEqual(final_video.close_calls, 1)

    def test_generate_video_skips_every_bgm_source_when_volume_is_zero(self):
        """0 音量必须在解析文件前统一短路当前来源和未来提供商。"""
        test_cases = [
            ("random", None),
            ("custom", None),
            ("sonilo", "sonilo.m4a"),
            ("future_provider", "future-provider.wav"),
        ]
        for bgm_type, bgm_override in test_cases:
            with self.subTest(bgm_type=bgm_type):
                params = vd.VideoParams(
                    video_subject="test",
                    subtitle_enabled=False,
                    bgm_type=bgm_type,
                    bgm_file="missing-background.mp3",
                    bgm_volume=0.0,
                )
                source_video = _FakeMoviePyClip()
                voice_source = _FakeMoviePyClip()
                final_video = _FakeMoviePyClip()
                source_video.with_audio_result = final_video

                with (
                    patch.object(
                        vd,
                        "_open_video_clip_quietly",
                        return_value=source_video,
                    ),
                    patch.object(
                        vd, "AudioFileClip", return_value=voice_source
                    ) as audio_file_clip,
                    patch.object(vd, "get_bgm_file") as get_bgm_file,
                    patch.object(vd, "CompositeAudioClip") as composite_audio,
                    patch.object(
                        vd, "_write_videofile_with_codec_fallback"
                    ) as writer,
                    patch.object(
                        vd, "_get_configured_video_codec", return_value="libx264"
                    ),
                ):
                    result = vd.generate_video(
                        video_path="combined.mp4",
                        audio_path="voice.mp3",
                        subtitle_path="",
                        output_file="final.mp4",
                        params=params,
                        bgm_file_override=bgm_override,
                    )

                self.assertTrue(result)
                audio_file_clip.assert_called_once_with("voice.mp3")
                get_bgm_file.assert_not_called()
                composite_audio.assert_not_called()
                writer.assert_called_once()
                self.assertEqual(source_video.close_calls, 1)
                self.assertEqual(voice_source.close_calls, 1)
                self.assertEqual(final_video.close_calls, 1)

    def test_generate_video_chooses_looping_by_bgm_file_source(self):
        """默认曲库需要循环，任务层提供的时长适配文件不应依赖提供商名称。"""
        test_cases = [
            ("random", None, True),
            ("custom", None, True),
            ("sonilo", "sonilo.m4a", False),
            ("future_provider", "future-provider.wav", False),
        ]
        for bgm_type, bgm_override, should_loop in test_cases:
            with self.subTest(bgm_type=bgm_type, bgm_override=bgm_override):
                params = vd.VideoParams(
                    video_subject="test",
                    subtitle_enabled=False,
                    bgm_type=bgm_type,
                    bgm_file="library.mp3",
                    bgm_volume=0.2,
                )
                source_video = _FakeMoviePyClip()
                voice_source = _FakeMoviePyClip()
                bgm_source = _FakeMoviePyClip()
                mixed_audio = _FakeMoviePyClip()
                final_video = _FakeMoviePyClip()
                source_video.with_audio_result = final_video

                with (
                    patch.object(
                        vd,
                        "_open_video_clip_quietly",
                        return_value=source_video,
                    ),
                    patch.object(
                        vd,
                        "AudioFileClip",
                        side_effect=[voice_source, bgm_source],
                    ),
                    patch.object(vd, "get_bgm_file", return_value="library.mp3"),
                    patch.object(vd, "CompositeAudioClip", return_value=mixed_audio),
                    patch.object(vd.afx, "AudioLoop") as audio_loop,
                    patch.object(vd, "_write_videofile_with_codec_fallback"),
                    patch.object(
                        vd, "_get_configured_video_codec", return_value="libx264"
                    ),
                ):
                    result = vd.generate_video(
                        video_path="combined.mp4",
                        audio_path="voice.mp3",
                        subtitle_path="",
                        output_file="final.mp4",
                        params=params,
                        bgm_file_override=bgm_override,
                    )

                self.assertTrue(result)
                if should_loop:
                    audio_loop.assert_called_once_with(duration=source_video.duration)
                else:
                    audio_loop.assert_not_called()

    def test_preprocess_video(self):
        if not os.path.exists(self.test_img_path):
            self.fail(f"test image not found: {self.test_img_path}")

        local_videos_dir = utils.storage_dir("local_videos", create=True)
        safe_img_path = os.path.join(local_videos_dir, "test-preprocess-1.png")
        shutil.copy2(self.test_img_path, safe_img_path)

        # test preprocess_video function
        m = MaterialInfo()
        m.url = os.path.basename(safe_img_path)
        m.provider = "local"
        print(m)

        try:
            materials = vd.preprocess_video([m], clip_duration=4)
            print(materials)

            # verify result
            self.assertIsNotNone(materials)
            self.assertEqual(len(materials), 1)
            self.assertTrue(materials[0].url.endswith(".mp4"))

            # moviepy get video info
            clip = VideoFileClip(materials[0].url)
            try:
                print(clip)
            finally:
                clip.close()

            # clean generated test video file
            if os.path.exists(materials[0].url):
                os.remove(materials[0].url)
        finally:
            if os.path.exists(safe_img_path):
                os.remove(safe_img_path)

    def test_preprocess_video_rejects_material_outside_local_videos(self):
        """
        local 素材路径来自 API 参数，不能允许任意绝对路径进入 MoviePy。
        这里验证非 local_videos 白名单目录内的路径会被跳过，避免任意文件读取。
        """
        m = MaterialInfo(provider="local", url=self.test_img_path)

        materials = vd.preprocess_video([m], clip_duration=4)

        self.assertEqual(materials, [])

    def test_get_bgm_file_accepts_song_directory_filename(self):
        """
        BGM 列表接口现在只暴露文件名；生成视频时应能把文件名安全解析回
        resource/songs 白名单目录，保持正常使用路径可用。
        """
        song_dir = utils.song_dir()
        bgm_path = os.path.join(song_dir, "test-safe-bgm.mp3")
        Path(bgm_path).write_bytes(b"fake-mp3")

        try:
            self.assertEqual(vd.get_bgm_file(bgm_file="test-safe-bgm.mp3"), bgm_path)
        finally:
            if os.path.exists(bgm_path):
                os.remove(bgm_path)

    def test_get_bgm_file_accepts_project_relative_song_path(self):
        """
        用户在 WebUI 中可能直接填写 ./resource/songs/xxx.mp3。该路径虽然是
        项目根目录相对路径，但实际文件仍在 resource/songs 白名单目录内，
        应该被接受，避免自定义背景音乐被误判为不存在。
        """
        song_dir = utils.song_dir()
        bgm_path = os.path.join(song_dir, "test-relative-bgm.mp3")
        Path(bgm_path).write_bytes(b"fake-mp3")

        try:
            self.assertEqual(
                vd.get_bgm_file(bgm_file="./resource/songs/test-relative-bgm.mp3"),
                bgm_path,
            )
        finally:
            if os.path.exists(bgm_path):
                os.remove(bgm_path)

    def test_get_bgm_file_rejects_path_outside_song_directory(self):
        """
        用户传入的 bgm_file 不能直接作为本地路径打开，否则可能读取系统文件。
        即使外部文件存在，也必须因为不在 songs 目录内被拒绝。
        """
        with tempfile.NamedTemporaryFile(suffix=".mp3") as temp_bgm:
            self.assertEqual(vd.get_bgm_file(bgm_file=temp_bgm.name), "")

    def test_get_ffmpeg_binary_uses_configured_env_path(self):
        """配置中显式指定 ffmpeg 时，应优先使用该路径。"""
        with patch.dict(os.environ, {"IMAGEIO_FFMPEG_EXE": "/tmp/custom-ffmpeg"}, clear=True):
            self.assertEqual(utils.get_ffmpeg_binary(), "/tmp/custom-ffmpeg")

    def test_get_ffmpeg_binary_falls_back_to_imageio_ffmpeg(self):
        """
        Windows 便携包里系统 PATH 可能没有 ffmpeg，但 moviepy 依赖的
        imageio-ffmpeg 通常会提供可执行文件。这里验证该兜底路径可用。
        """
        fake_imageio_ffmpeg = types.SimpleNamespace(
            get_ffmpeg_exe=lambda: "/tmp/bundled-ffmpeg"
        )

        with patch.dict(os.environ, {}, clear=True), patch.object(
            utils.shutil, "which", return_value=None
        ), patch.dict(sys.modules, {"imageio_ffmpeg": fake_imageio_ffmpeg}):
            self.assertEqual(utils.get_ffmpeg_binary(), "/tmp/bundled-ffmpeg")

    def test_get_effective_video_codec_falls_back_when_encoder_missing(self):
        """
        用户选择的硬件编码器必须先经过 FFmpeg encoder 列表检测。检测不到
        时直接回退 libx264，避免生成任务在写文件阶段才失败。
        """
        config.app["video_codec"] = "h264_nvenc"

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=False):
            self.assertEqual(vd._get_effective_video_codec(), "libx264")

    def test_get_configured_video_codec_uses_auto_detection_when_unset(self):
        """
        默认模式为 "auto"，自动检测并使用可用的最佳编码器（硬件加速优先）。
        返回值必须是受支持的编码器之一。
        """
        config.app.pop("video_codec", None)

        codec = vd._get_configured_video_codec()
        self.assertIn(codec, vd._SUPPORTED_VIDEO_CODECS)

    def test_get_configured_video_codec_preserves_explicit_libx264(self):
        """
        用户明确选择 libx264 时需要保持固定选择。它与“跟随项目默认策略”当前
        结果相同，但配置语义不同，未来调整默认值时不能影响显式选择。
        """
        config.app["video_codec"] = "libx264"

        self.assertEqual(vd._get_configured_video_codec(), "libx264")

    def test_ffmpeg_encoder_exists_falls_back_when_probe_fails(self):
        """
        Windows 上用户配置的 ffmpeg 可能因为路径损坏、权限或杀软拦截而无法
        正常执行。encoder 探测失败时必须返回 False，让上层稳定回退 libx264。
        """
        with patch.object(
            vd.subprocess,
            "run",
            side_effect=OSError("permission denied"),
        ):
            self.assertFalse(vd._ffmpeg_encoder_exists("C:/ffmpeg/bin/ffmpeg.exe", "h264_nvenc"))

    def test_write_videofile_falls_back_after_runtime_encoder_failure(self):
        """
        FFmpeg 声明支持某个硬件编码器，不代表当前显卡或驱动一定可用。
        首次实际编码失败后，应立即用 libx264 重试，并在本进程禁用该编码器。
        """

        class _FakeClip:
            def __init__(self):
                self.codecs = []

            def write_videofile(self, output_file, codec, **kwargs):
                self.codecs.append(codec)
                if codec == "h264_nvenc":
                    raise RuntimeError("nvenc device not available")

        fake_clip = _FakeClip()

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            used_codec = vd._write_videofile_with_codec_fallback(
                fake_clip,
                "/tmp/fake.mp4",
                codec="h264_nvenc",
                logger=None,
                fps=30,
            )

        self.assertEqual(used_codec, "libx264")
        self.assertEqual(fake_clip.codecs, ["h264_nvenc", "libx264"])
        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_write_videofile_does_not_disable_codec_when_fallback_also_fails(self):
        """
        如果 libx264 兜底也失败，失败原因更可能是输出路径、权限、文件占用等
        通用问题，不能误判为硬件编码器不可用。
        """

        class _FakeClip:
            def write_videofile(self, output_file, codec, **kwargs):
                raise RuntimeError(f"{codec} cannot write output")

        with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
            with self.assertRaises(RuntimeError):
                vd._write_videofile_with_codec_fallback(
                    _FakeClip(),
                    "/tmp/fake.mp4",
                    codec="h264_nvenc",
                    logger=None,
                    fps=30,
                )

        self.assertNotIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_format_ffmpeg_concat_path_normalizes_windows_path(self):
        """
        concat demuxer 的文件列表对 Windows 反斜杠较敏感，写入 list 前统一
        转成正斜杠，并继续保留单引号转义。
        """
        with patch.object(
            vd.os.path,
            "abspath",
            return_value=r"C:\Users\Test User's Videos\clip.mp4",
        ):
            self.assertEqual(
                vd._format_ffmpeg_concat_path(
                    r"C:\Users\Test User's Videos\clip.mp4"
                ),
                "C:/Users/Test User'\\''s Videos/clip.mp4",
            )

    def test_concat_video_clips_falls_back_after_runtime_encoder_failure(self):
        """
        最终 ffmpeg concat 阶段也要具备同样的回退能力。这里用 mock 模拟
        h264_nvenc 编码失败，确认会自动再用 libx264 执行一次。
        """
        config.app["video_codec"] = "h264_nvenc"

        def fake_run(command, capture_output, text, check):
            codec_index = command.index("-c:v") + 1
            codec = command[codec_index]
            if codec == "h264_nvenc":
                return types.SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="nvenc device not available",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
                with patch.object(vd.subprocess, "run", side_effect=fake_run) as run:
                    vd.concat_video_clips_with_ffmpeg(
                        clip_files=[clip_file],
                        output_file=output_file,
                        threads=1,
                        output_dir=temp_dir,
                    )

        used_codecs = [
            call.args[0][call.args[0].index("-c:v") + 1]
            for call in run.call_args_list
        ]
        self.assertEqual(used_codecs, ["h264_nvenc", "libx264"])
        self.assertIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_concat_video_clips_does_not_disable_codec_when_fallback_also_fails(self):
        """
        concat 阶段如果 libx264 也失败，说明可能是输入 list、路径或输出权限
        问题，不能把硬件编码器加入运行时禁用列表。
        """
        config.app["video_codec"] = "h264_nvenc"

        def fake_run(command, capture_output, text, check):
            codec_index = command.index("-c:v") + 1
            codec = command[codec_index]
            return types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=f"{codec} cannot write output",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            with patch.object(vd, "_ffmpeg_encoder_exists", return_value=True):
                with patch.object(vd.subprocess, "run", side_effect=fake_run):
                    with self.assertRaises(RuntimeError):
                        vd.concat_video_clips_with_ffmpeg(
                            clip_files=[clip_file],
                            output_file=output_file,
                            threads=1,
                            output_dir=temp_dir,
                        )

        self.assertNotIn("h264_nvenc", vd._runtime_disabled_video_codecs)

    def test_open_video_clip_quietly_suppresses_moviepy_stdout(self):
        """
        MoviePy 2.1.x 的 FFMPEG_VideoReader 会直接向 stdout 打印 metadata
        和 ffmpeg 命令。项目服务层应屏蔽这类依赖库噪声，避免用户把
        `audio_found: False` 误判为最终视频没有音频。
        """
        # 测试只关心服务层是否屏蔽 MoviePy 的读取噪声，不应长期保存一份由 PNG
        # 编码而来的二进制 MP4 fixture。运行时生成短视频既能保持测试独立，也能
        # 避免 fixture 因不同编码参数产生帧间闪烁后被误用于视觉效果验证。
        image_path = os.path.join(resources_dir, "1.png")
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = os.path.join(temp_dir, "image-fixture.mp4")
            source_clip = ImageClip(image_path).with_duration(0.2)
            try:
                source_clip.write_videofile(
                    video_path,
                    codec="libx264",
                    fps=5,
                    audio=False,
                    logger=None,
                )
            finally:
                source_clip.close()

            stdout = StringIO()
            with redirect_stdout(stdout):
                clip = vd._open_video_clip_quietly(video_path)

            try:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIsNone(clip.audio)
                self.assertGreater(clip.duration, 0)
            finally:
                vd.close_clip(clip)

    def test_combine_videos_closes_audio_clip_when_duration_read_fails(self):
        """
        `combine_videos()` 只需要读取旁白音频时长。即使读取 duration
        时发生异常，也必须关闭 AudioFileClip，避免文件句柄泄漏。
        """

        class _FakeAudioReader:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _BrokenAudioClip:
            def __init__(self):
                self.reader = _FakeAudioReader()

            @property
            def duration(self):
                raise RuntimeError("failed to read duration")

        fake_audio_clip = _BrokenAudioClip()

        with patch.object(vd, "AudioFileClip", return_value=fake_audio_clip):
            with self.assertRaises(RuntimeError):
                vd.combine_videos(
                    combined_video_path="/tmp/unused-combined.mp4",
                    video_paths=[],
                    audio_file="/tmp/unused-audio.mp3",
                )

        self.assertTrue(fake_audio_clip.reader.closed)

    def test_combine_videos_handles_none_transition_mode(self):
        """
        Ensure `combine_videos` safely handles
        `video_transition_mode=None`.
        """
        class _FakeAudioClip:
            @property
            def duration(self):
                return 10.0

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")
            audio_file = os.path.join(temp_dir, "audio.mp3")

            with patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()):
                # Use empty video_paths to avoid heavy video processing while
                # still exercising transition mode normalization logic.
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=[],
                    audio_file=audio_file,
                    video_transition_mode=None,
                )
                self.assertEqual(result, combined_video_path)

    def _capture_source_ranges_for_clip_speed(
        self,
        *,
        source_duration,
        audio_duration,
        clip_speed,
        max_clip_duration=3,
    ):
        """使用轻量假视频记录 combine_videos 实际读取的源时间范围。"""

        source_ranges = []
        written_durations = []

        class _FakeAudioClip:
            duration = audio_duration

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self, duration, records_source_range=False):
                self.duration = duration
                self.size = (1080, 1920)
                self.w = 1080
                self.h = 1920
                self.records_source_range = records_source_range

            def subclipped(self, start_time, end_time):
                # 只记录直接从源文件读取的范围。变速后的安全裁剪也会调用
                # subclipped，但它不代表新的源时间段，不能混入断层判断。
                if self.records_source_range:
                    source_ranges.append((start_time, end_time))
                return _FakeVideoClip(end_time - start_time)

            def with_speed_scaled(self, factor):
                return _FakeVideoClip(self.duration / factor)

            def close(self):
                pass

        def _open_fake_video_clip(_video_path):
            return _FakeVideoClip(source_duration, records_source_range=True)

        def _capture_written_clip(clip, *_args, **_kwargs):
            written_durations.append(clip.duration)

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")
            with (
                patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()),
                patch.object(
                    vd,
                    "_open_video_clip_quietly",
                    side_effect=_open_fake_video_clip,
                ),
                patch.object(
                    vd,
                    "_write_videofile_with_codec_fallback",
                    side_effect=_capture_written_clip,
                ),
                # random 模式默认会打乱同一源视频的切片。这里保持生成顺序，
                # 才能精确验证相邻源时间段是否连续。
                patch.object(
                    vd,
                    "_prioritize_unique_source_clips",
                    side_effect=lambda subclipped_items, concat_mode: subclipped_items,
                ),
                patch.object(vd, "concat_video_clips_with_ffmpeg"),
                patch.object(vd, "delete_files"),
            ):
                vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["clip.mp4"],
                    audio_file="audio.mp3",
                    video_concat_mode=vd.VideoConcatMode.random,
                    max_clip_duration=max_clip_duration,
                    clip_speed=clip_speed,
                )

        return source_ranges, written_durations

    def test_combine_videos_slow_speed_keeps_source_timeline_continuous(self):
        """0.5 倍慢放应连续读取 1.5 秒源片段，不能跳过中间画面。"""

        source_ranges, written_durations = self._capture_source_ranges_for_clip_speed(
            source_duration=4.0,
            audio_duration=5.9,
            clip_speed=0.5,
        )

        self.assertEqual(source_ranges, [(0, 1.5), (1.5, 3.0)])
        self.assertEqual(written_durations, [3.0, 3.0])

    def test_combine_videos_fast_speed_reads_enough_source_content(self):
        """2 倍快放应读取 6 秒源画面，使最终片段仍保持 3 秒。"""

        source_ranges, written_durations = self._capture_source_ranges_for_clip_speed(
            source_duration=8.0,
            audio_duration=2.9,
            clip_speed=2.0,
        )

        self.assertEqual(source_ranges, [(0, 6.0)])
        self.assertEqual(written_durations, [3.0])

    def test_combine_videos_keeps_small_duration_safety_margin(self):
        """
        音频和素材累计时长刚好相等时，仍应继续追加一个短片段作为安全余量。

        FFmpeg 按帧率拼接后可能让最终视频比理论时长短几十毫秒。如果这里
        在 10.0s == 10.0s 时立即停止，成片末尾就可能出现音频还在播放但
        视频素材已经结束的边界问题。
        """

        class _FakeAudioClip:
            duration = 10.0

            def close(self):
                pass

        class _FakeVideoClip:
            def __init__(self, duration):
                self.duration = duration
                self.size = (1080, 1920)
                self.w = 1080
                self.h = 1920

            def subclipped(self, start_time, end_time):
                return _FakeVideoClip(end_time - start_time)

        video_durations = {
            "clip-1.mp4": 3.0,
            "clip-2.mp4": 4.0,
            "clip-3.mp4": 3.0,
            "clip-4.mp4": 2.0,
        }

        def _open_fake_video_clip(video_path):
            return _FakeVideoClip(video_durations[video_path])

        with tempfile.TemporaryDirectory() as temp_dir:
            combined_video_path = os.path.join(temp_dir, "combined.mp4")

            with patch.object(vd, "AudioFileClip", return_value=_FakeAudioClip()):
                with patch.object(
                    vd, "_open_video_clip_quietly", side_effect=_open_fake_video_clip
                ):
                    with patch.object(
                        vd, "_write_videofile_with_codec_fallback"
                    ) as write_mock:
                        with patch.object(vd, "concat_video_clips_with_ffmpeg") as concat_mock:
                            with patch.object(vd, "delete_files"):
                                result = vd.combine_videos(
                                    combined_video_path=combined_video_path,
                                    video_paths=list(video_durations.keys()),
                                    audio_file=os.path.join(temp_dir, "audio.mp3"),
                                    video_aspect=vd.VideoAspect.portrait,
                                    video_concat_mode=vd.VideoConcatMode.sequential,
                                    video_transition_mode=None,
                                    max_clip_duration=10,
                                )

        self.assertEqual(result, combined_video_path)
        self.assertEqual(write_mock.call_count, 4)
        self.assertEqual(concat_mock.call_args.kwargs["max_duration"], 10.0)

    def test_concat_video_clips_limits_output_to_audio_duration(self):
        """最终拼接时应裁到音频时长，避免安全余量带来明显静音尾巴。"""

        def fake_run(command, capture_output, text, check, timeout=None):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")

            # Mock encoder detection to avoid real ffmpeg calls
            with patch.object(vd, "_get_effective_video_codec", return_value="libx264"):
                with patch.object(vd.subprocess, "run", side_effect=fake_run) as run:
                    vd.concat_video_clips_with_ffmpeg(
                        clip_files=[clip_file],
                        output_file=output_file,
                        threads=1,
                        output_dir=temp_dir,
                        max_duration=10.0,
                    )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-t") + 1], "10.000")
        self.assertLess(command.index("-t"), command.index(output_file))

    def test_prioritize_unique_source_clips_uses_each_source_before_reuse(self):
        """
        随机模式下，一个长素材会被拆成多个片段。调度层应先让每个源素材
        至少出现一次，再使用同一源素材的其他切片，降低用户感知到的重复。
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 4, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("a.mp4", 4, 8, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 4, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("b.mp4", 4, 8, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("c.mp4", 0, 4, source_file_path="c.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.random,
        )

        self.assertCountEqual(ordered_clips, clips)
        first_round_sources = [clip.source_file_path for clip in ordered_clips[:3]]
        self.assertCountEqual(first_round_sources, ["a.mp4", "b.mp4", "c.mp4"])

    def test_prioritize_unique_source_clips_keeps_sequential_order(self):
        """
        顺序模式本身只取每个素材的首段，不应被随机调度逻辑改变顺序。
        """
        clips = [
            vd.SubClippedVideoClip("a.mp4", 0, 4, source_file_path="a.mp4"),
            vd.SubClippedVideoClip("b.mp4", 0, 4, source_file_path="b.mp4"),
            vd.SubClippedVideoClip("c.mp4", 0, 4, source_file_path="c.mp4"),
        ]

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=clips,
            concat_mode=vd.VideoConcatMode.sequential,
        )

        self.assertEqual(ordered_clips, clips)

    def test_prioritize_unique_source_clips_prefers_long_primary_clip(self):
        """
        同一个源素材的最后一个切片可能短于目标片段时长。首轮去重时应优先
        选择较长片段，否则会因为累计时长不足而提前复用素材。
        """
        short_tail = vd.SubClippedVideoClip(
            "a.mp4", 6, 6.5, source_file_path="a.mp4"
        )
        full_clip = vd.SubClippedVideoClip(
            "a.mp4", 0, 3, source_file_path="a.mp4"
        )
        other_source = vd.SubClippedVideoClip(
            "b.mp4", 0, 3, source_file_path="b.mp4"
        )

        ordered_clips = vd._prioritize_unique_source_clips(
            subclipped_items=[short_tail, full_clip, other_source],
            concat_mode=vd.VideoConcatMode.random,
        )

        first_a_clip = next(
            clip for clip in ordered_clips if clip.source_file_path == "a.mp4"
        )
        self.assertEqual(first_a_clip, full_clip)
    
    def test_wrap_text(self):
        """test text wrapping function"""
        try:
            font_path = os.path.join(utils.font_dir(), "STHeitiMedium.ttc")
            if not os.path.exists(font_path):
                self.fail(f"font file not found: {font_path}")
                
            # test english text wrapping
            test_text_en = "This is a test text for wrapping long sentences in english language"
            
            wrapped_text_en, text_height_en = vd.wrap_text(
                text=test_text_en,
                max_width=300,
                font=font_path,
                fontsize=30
            )
            print(wrapped_text_en, text_height_en)
            # verify text is wrapped
            self.assertIn("\n", wrapped_text_en)
            
            # test chinese text wrapping
            test_text_zh = "这是一段用来测试中文长句换行的文本内容，应该会根据宽度限制进行换行处理"
            wrapped_text_zh, text_height_zh = vd.wrap_text(
                text=test_text_zh,
                max_width=300,
                font=font_path,
                fontsize=30
            )   
            print(wrapped_text_zh, text_height_zh)
            # verify chinese text is wrapped
            self.assertIn("\n", wrapped_text_zh)
        except Exception as e:
            self.fail(f"test wrap_text failed: {str(e)}")

    def test_wrap_text_uses_stable_line_metrics_for_all_bundled_fonts(self):
        """
        字幕高度必须来自字体自身的 ascent/descent，而不能取决于当前文字。

        不含 g/j/p/q/y 的拉丁文本只有大写字母和 x-height，Pillow 的字形
        bbox 会比字体真实行高短很多；多行时误差累积，最终会裁掉最后一行。
        这里遍历全部内置字体，并同时覆盖含下伸部与不含下伸部的英文文本，
        防止以后重新引入“按当前字形墨迹计算行高”的实现。
        """
        font_size = 60
        max_width = 360
        text_cases = {
            "without_descenders": "A man survived the Hiroshima atomic bomb blast",
            "with_descenders": "Typing quickly brings joyful progress",
        }
        font_paths = sorted(
            path
            for path in Path(utils.font_dir()).iterdir()
            if path.suffix.lower() in {".ttf", ".ttc"}
        )

        self.assertTrue(font_paths, "expected bundled subtitle fonts")
        for font_path in font_paths:
            font = vd.ImageFont.truetype(str(font_path), font_size)
            expected_line_height = sum(font.getmetrics())
            for case_name, text in text_cases.items():
                with self.subTest(font=font_path.name, case=case_name):
                    wrapped_text, text_height = vd.wrap_text(
                        text=text,
                        max_width=max_width,
                        font=str(font_path),
                        fontsize=font_size,
                    )
                    line_count = wrapped_text.count("\n") + 1

                    self.assertGreater(line_count, 1)
                    self.assertEqual(
                        text_height,
                        line_count * expected_line_height,
                    )

    def test_wrap_text_counts_existing_subtitle_line_breaks(self):
        """
        SRT 文本可能已经包含人工换行；即使每行都不需要再次折行，高度也必须
        按最终两行计算。否则宽画面上的短句会绕过自动换行分支并再次裁掉末行。
        """
        font_size = 60
        font_path = os.path.join(utils.font_dir(), "MicrosoftYaHeiBold.ttc")
        text = "SAFE TEXT\nMORE SAFE"
        font = vd.ImageFont.truetype(font_path, font_size)

        wrapped_text, text_height = vd.wrap_text(
            text=text,
            max_width=972,
            font=font_path,
            fontsize=font_size,
        )

        self.assertEqual(wrapped_text, text)
        self.assertEqual(text_height, 2 * sum(font.getmetrics()))

    def test_small_subtitle_with_thick_stroke_keeps_a_bottom_margin(self):
        """
        小字号配粗描边是最容易重新触底的比例边界。遍历全部内置字体并读取
        MoviePy 的真实 mask，确保额外高度至少容纳向上下扩张的完整描边。
        """
        font_size = 24
        stroke_width = 6
        max_width = 240
        text = "A man survived the Hiroshima atomic bomb blast"
        font_paths = sorted(
            path
            for path in Path(utils.font_dir()).iterdir()
            if path.suffix.lower() in {".ttf", ".ttc"}
        )

        for font_path in font_paths:
            with self.subTest(font=font_path.name):
                wrapped_text, text_height = vd.wrap_text(
                    text=text,
                    max_width=max_width,
                    font=str(font_path),
                    fontsize=font_size,
                )
                line_count = wrapped_text.count("\n") + 1
                interline = int(font_size * 0.25)
                vertical_padding = int(font_size * 0.35)
                stroke_padding = stroke_width * 2 * line_count
                clip_height = int(
                    text_height
                    + vertical_padding
                    + interline * line_count
                    + stroke_padding
                )
                text_clip = vd.TextClip(
                    text=wrapped_text,
                    font=str(font_path),
                    font_size=font_size,
                    color="#FFFFFF",
                    stroke_color="#000000",
                    stroke_width=stroke_width,
                    interline=interline,
                    size=(max_width, clip_height),
                    text_align="center",
                )
                try:
                    mask = text_clip.mask.get_frame(0)
                    visible_rows, _ = vd.np.where(mask > 0.01)

                    self.assertGreater(len(visible_rows), 0)
                    self.assertLess(int(visible_rows.max()), clip_height - 1)
                finally:
                    text_clip.close()

    def test_multilingual_textclip_last_line_keeps_a_visible_bottom_margin(self):
        """
        使用 MoviePy 真实绘制多语种字幕，确保最后一行没有贴到画布底边。

        仅检查 wrap_text() 返回值会漏掉 Pillow/MoviePy 在 baseline、描边和
        行间距上的组合差异，因此这里直接读取 TextClip 的透明 mask。覆盖文本
        均由对应内置字体完整支持，包括英文、越南语、泰语、简繁中文、俄语
        和希腊语；只要可见像素触及最后一行，就说明仍存在静默裁切风险。
        """
        font_size = 60
        max_width = 360
        interline = int(font_size * 0.25)
        vertical_padding = int(font_size * 0.35)
        stroke_width = 2
        cases = (
            (
                "english_without_descenders",
                "BeVietnamPro-Bold.ttf",
                "A man survived the Hiroshima atomic bomb blast",
            ),
            (
                "vietnamese",
                "BeVietnamPro-Medium.ttf",
                "Tôi vẫn luôn tin vào một tương lai tươi sáng",
            ),
            (
                "thai",
                "Charm-Regular.ttf",
                "นี่คือข้อความสำหรับตรวจสอบบรรทัดสุดท้ายของคำบรรยาย",
            ),
            (
                "simplified_chinese",
                "MicrosoftYaHeiBold.ttc",
                "这是一个用于检查字幕最后一行是否完整显示的测试句子",
            ),
            (
                "traditional_chinese",
                "STHeitiMedium.ttc",
                "這是一個用於檢查字幕最後一行是否完整顯示的測試句子",
            ),
            (
                "cyrillic",
                "MicrosoftYaHeiNormal.ttc",
                "Это текст для проверки последней строки субтитров",
            ),
            (
                "greek",
                "STHeitiLight.ttc",
                "Αυτό είναι κείμενο για τον έλεγχο της τελευταίας γραμμής",
            ),
        )

        for language, font_name, text in cases:
            font_path = os.path.join(utils.font_dir(), font_name)
            with self.subTest(language=language, font=font_name):
                self.assertTrue(vd.subtitle_font_supports_text(font_path, text))
                wrapped_text, text_height = vd.wrap_text(
                    text=text,
                    max_width=max_width,
                    font=font_path,
                    fontsize=font_size,
                )
                line_count = wrapped_text.count("\n") + 1
                stroke_padding = stroke_width * 2 * line_count
                clip_height = int(
                    text_height
                    + vertical_padding
                    + interline * line_count
                    + stroke_padding
                )
                text_clip = vd.TextClip(
                    text=wrapped_text,
                    font=font_path,
                    font_size=font_size,
                    color="#FFFFFF",
                    stroke_color="#000000",
                    stroke_width=stroke_width,
                    interline=interline,
                    size=(max_width, clip_height),
                    text_align="center",
                )
                try:
                    mask = text_clip.mask.get_frame(0)
                    visible_rows, _ = vd.np.where(mask > 0.01)

                    self.assertGreater(line_count, 1)
                    self.assertGreater(len(visible_rows), 0)
                    self.assertLess(int(visible_rows.max()), clip_height - 1)
                finally:
                    text_clip.close()

    def test_rounded_subtitle_background_clip_has_transparent_corners(self):
        """
        圆角字幕背景只在用户显式开启时使用。这里直接验证生成的 RGBA
        背景具备透明圆角和半透明中心，避免后续改动把圆角效果退化成实心矩形。
        """
        clip = vd._rounded_subtitle_background_clip(
            width=120,
            height=48,
            color="#123456",
            alpha=140,
            radius=16,
        )
        try:
            frame = clip.get_frame(0)
            mask = clip.mask.get_frame(0)

            self.assertEqual(frame.shape[0:2], (48, 120))
            self.assertEqual(tuple(frame[24, 60]), (18, 52, 86))
            self.assertEqual(mask[0, 0], 0)
            self.assertGreater(mask[24, 60], 0.5)
            self.assertLess(mask[24, 60], 0.6)
        finally:
            clip.close()

    def test_get_temp_audio_dir_returns_system_temp_on_windows(self):
        with patch("sys.platform", "win32"):
            result = vd._get_temp_audio_dir("/some/output/dir")
            self.assertEqual(result, tempfile.gettempdir())

    def test_get_temp_audio_dir_returns_output_dir_on_non_windows(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform):
                with patch("sys.platform", platform):
                    result = vd._get_temp_audio_dir("/some/output/dir")
                    self.assertEqual(result, "/some/output/dir")


class TestMaterialResolutionTolerance(unittest.TestCase):
    def test_accepts_material_at_the_nominal_minimum(self):
        self.assertTrue(vd.is_material_resolution_acceptable(480, 480))

    def test_accepts_whatsapp_recompressed_portrait_clip(self):
        # WhatsApp delivers 9:16 clips as 478x850, two pixels under the
        # nominal 480 minimum. Rejecting them fails the whole task.
        self.assertTrue(vd.is_material_resolution_acceptable(478, 850))

    def test_accepts_material_exactly_at_the_tolerance_bound(self):
        bound = vd._MIN_MATERIAL_DIMENSION - vd._MIN_DIMENSION_TOLERANCE
        self.assertTrue(vd.is_material_resolution_acceptable(bound, bound))

    def test_rejects_material_just_below_the_tolerance_bound(self):
        bound = vd._MIN_MATERIAL_DIMENSION - vd._MIN_DIMENSION_TOLERANCE
        self.assertFalse(vd.is_material_resolution_acceptable(bound - 1, 850))
        self.assertFalse(vd.is_material_resolution_acceptable(850, bound - 1))

    def test_rejects_genuinely_low_resolution_material(self):
        self.assertFalse(vd.is_material_resolution_acceptable(320, 240))


class _FakeSubtitlesClip:
    """SubtitlesClip 的替身：只暴露字幕条目，并按上下文管理器协议工作。"""

    def __init__(self, subtitle_items):
        self.subtitles = list(subtitle_items)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def close(self):
        pass


class _FakeSubtitleTextClip:
    """create_text_clip 需要的最小 MoviePy 接口：记录布局调用但不编码渲染。"""

    def __init__(self, *, w=300, h=80):
        self.w = w
        self.h = h
        self.mask = None
        self.position = None
        self._start = None
        self._end = None
        self._duration = None

    def with_position(self, pos):
        self.position = pos
        return self

    def with_start(self, start_time):
        self._start = start_time
        return self

    def with_end(self, end_time):
        self._end = end_time
        return self

    def with_duration(self, duration):
        self._duration = duration
        return self

    def with_audio(self, audio):
        return self

    def close(self):
        pass


class _FakeMask:
    """可注入的 TextClip mask，让可见像素的中心定位逻辑可控。"""

    def __init__(self, frame):
        self._frame = vd.np.array(frame, dtype=vd.np.float32)

    def get_frame(self, t=0):
        return self._frame


class _FakePilFontSized:
    """PIL 字体的替身：宽度按字符数 * char_width，metrics 可配置。"""

    def __init__(self, ascent=25, descent=25, char_width=10):
        self._ascent = ascent
        self._descent = descent
        self._char_width = char_width

    def getmetrics(self):
        return self._ascent, self._descent

    def getbbox(self, text):
        return (0, 0, self._char_width * len(text), self._char_width)


class _FakePilFontWidths(_FakePilFontSized):
    def __init__(self, widths, ascent=25, descent=25):
        super().__init__(ascent=ascent, descent=descent, char_width=0)
        self._widths = widths

    def getbbox(self, text):
        width = sum(self._widths.get(char, 1) for char in text)
        return (0, 0, width, width)


class _FakeGlyphMask:
    def __init__(self, size=(30, 30), bbox=(0, 0, 10, 10)):
        self.size = size
        self._bbox = bbox

    def getbbox(self):
        return self._bbox

    def __bytes__(self):
        return b"m" * (self.size[0] * self.size[1])


class _FakeGlyphFont:
    """字体字形探测的替身：可以精确指定哪些字符“缺失”。"""

    missing = _FakeGlyphMask(size=(30, 30), bbox=None)
    present = _FakeGlyphMask(size=(30, 30), bbox=(0, 0, 10, 10))

    def __init__(self, missing_chars=()):
        self._missing_chars = set(missing_chars)

    def getmask(self, char):
        if char == "\U0010ffff" or char in self._missing_chars:
            return self.missing
        return self.present


class _FakeBgClip:
    def with_duration(self, duration):
        return self


class _FakeSizedClip:
    def __init__(self, size=(1080, 1920)):
        self.size = size
        self.duration = 4.0

    def close(self):
        pass


class _RichFakeClip:
    """combine_videos 单测用的富假片段，逐个记录 geometry/效果调用。"""

    def __init__(self, duration=5.0, size=(1080, 1920), events=None):
        self.duration = duration
        self.size = size
        self.w, self.h = size
        self.events = events if events is not None else []
        self.fail_subclipped = False
        self.subclipped_to = None

    def subclipped(self, start_time, end_time):
        if self.fail_subclipped:
            raise RuntimeError("subclip failed")
        self.subclipped_to = (start_time, end_time)
        return _RichFakeClip(
            duration=end_time - start_time, size=self.size, events=self.events
        )

    def with_speed_scaled(self, factor):
        return _RichFakeClip(
            duration=self.duration / factor, size=self.size, events=self.events
        )

    def resized(self, *args, **kwargs):
        self.events.append(("resized", args, kwargs))
        return self

    def with_position(self, pos):
        self.events.append(("position", pos))
        return self

    def close(self):
        self.events.append(("closed",))


class TestWrapTextBranches(unittest.TestCase):
    def test_wrap_text_falls_back_when_font_metrics_are_invalid(self):
        """损坏字体的 metrics 可能返回 0 高度，此时应回退到字号并告警。"""
        font = _FakePilFontSized(ascent=0, descent=0)
        with (
            patch.object(vd.ImageFont, "truetype", return_value=font),
            patch.object(vd.logger, "warning") as warning,
        ):
            wrapped_text, text_height = vd.wrap_text(
                text="long text that must wrap and wrap again",
                max_width=10,
                font="broken.ttf",
                fontsize=60,
            )

        warning.assert_called_once()
        line_count = wrapped_text.count("\n") + 1
        self.assertEqual(text_height, line_count * 60)

    def test_wrap_text_handles_whitespace_only_input(self):
        """空视觉宽度文本不应触发折行，高度仍按字体行高计算。"""
        wrapped_text, text_height = vd.wrap_text(
            text=" ",
            max_width=972,
            font=os.path.join(utils.font_dir(), "STHeitiMedium.ttc"),
            fontsize=30,
        )
        self.assertEqual(wrapped_text, " ")
        self.assertGreater(text_height, 0)

    def test_wrap_text_skips_merge_when_punctuation_join_exceeds_width(self):
        """宽标点让“上一行末字 + 标点”超出可用宽度时，放弃合并标点。"""
        font = _FakePilFontWidths({"a": 1, "x": 1, "。": 25})
        with patch.object(vd.ImageFont, "truetype", return_value=font):
            wrapped_text, text_height = vd.wrap_text(
                text="ax。",
                max_width=22,
                font="widepunct.ttf",
                fontsize=60,
            )

        self.assertEqual(wrapped_text, "ax\n。")
        self.assertEqual(text_height, 2 * 50)

    def test_wrap_text_merges_closing_punctuation_back_to_previous_line(self):
        """字符级拆分后落在行首的闭合标点应合并回上一行。"""
        font = _FakePilFontSized(ascent=25, descent=25, char_width=10)
        with patch.object(vd.ImageFont, "truetype", return_value=font):
            wrapped_text, text_height = vd.wrap_text(
                text="ABCD。",
                max_width=40,
                font="metric.ttf",
                fontsize=60,
            )

        self.assertEqual(wrapped_text, "ABC\nD。")
        self.assertEqual(text_height, 2 * 50)

    def test_wrap_text_keeps_punctuation_line_when_previous_line_is_single_char(self):
        """上一行只剩一个字符时不强行合并标点，避免把单字行再次压短。"""
        font = _FakePilFontSized(ascent=25, descent=25, char_width=10)
        with patch.object(vd.ImageFont, "truetype", return_value=font):
            wrapped_text, text_height = vd.wrap_text(
                text="AB。",
                max_width=10,
                font="metric.ttf",
                fontsize=60,
            )

        self.assertEqual(wrapped_text, "A\nB\n。")
        self.assertEqual(text_height, 3 * 50)


class TestHexAndSubtitleColorHelpers(unittest.TestCase):
    def test_hex_to_rgb_parses_valid_color(self):
        self.assertEqual(vd._hex_to_rgb("#123456"), (18, 52, 86))

    def test_hex_to_rgb_falls_back_black_on_invalid_digits(self):
        self.assertEqual(vd._hex_to_rgb("#GGGGGG"), (0, 0, 0))

    def test_hex_to_rgb_falls_back_black_on_malformed_input(self):
        for bad in (None, "", "123456", "#12345", "#1234567", 123, True):
            with self.subTest(value=repr(bad)):
                self.assertEqual(vd._hex_to_rgb(bad), (0, 0, 0))

    def _fake_params(self, *, enabled=True, background="#000000", foreground="#FFFFFF"):
        # text_fore_color 在 VideoParams 里被约束为 str，bool 分支只能通过
        # 结构上一模一样的既存对象触发，因此这里用鸭子类型参数。
        return types.SimpleNamespace(
            subtitle_enabled=enabled,
            text_background_color=background,
            text_fore_color=foreground,
        )

    def test_subtitle_colors_not_indistinguishable_when_subtitles_disabled(self):
        params = self._fake_params(enabled=False)
        self.assertFalse(vd.subtitle_colors_are_indistinguishable(params))

    def test_subtitle_colors_not_indistinguishable_without_background(self):
        params = self._fake_params(background=None)
        self.assertFalse(vd.subtitle_colors_are_indistinguishable(params))

    def test_subtitle_colors_not_indistinguishable_when_background_empty_string(self):
        params = self._fake_params(background="")
        self.assertFalse(vd.subtitle_colors_are_indistinguishable(params))

    def test_subtitle_colors_indistinguishable_for_bool_true_fore_color(self):
        params = self._fake_params(foreground=True)
        self.assertTrue(vd.subtitle_colors_are_indistinguishable(params))

    def test_subtitle_colors_not_indistinguishable_for_bool_false_fore_color(self):
        params = self._fake_params(foreground=False)
        self.assertFalse(vd.subtitle_colors_are_indistinguishable(params))

    def test_subtitle_colors_indistinguishable_case_insensitive(self):
        params = vd.VideoParams(
            video_subject="t",
            subtitle_enabled=True,
            text_fore_color="#ffffff",
            text_background_color="#FFFFFF",
        )
        self.assertTrue(vd.subtitle_colors_are_indistinguishable(params))

    def test_subtitle_colors_not_indistinguishable_when_different(self):
        params = vd.VideoParams(
            video_subject="t",
            subtitle_enabled=True,
            text_fore_color="#ffffff",
            text_background_color="#000000",
        )
        self.assertFalse(vd.subtitle_colors_are_indistinguishable(params))


class TestVisibleCenterPosition(unittest.TestCase):
    def _clip_with_mask(self, mask):
        return type("Clip", (), {"w": 300, "h": 80, "mask": mask})()

    def test_returns_geometric_center_when_no_mask(self):
        clip = self._clip_with_mask(None)
        self.assertEqual(vd._get_visible_center_position(clip, 400, 200), (50, 60))

    def test_returns_geometric_center_when_mask_is_empty(self):
        mask = _FakeMask(vd.np.zeros((80, 300), dtype=vd.np.float32))
        clip = self._clip_with_mask(mask)
        self.assertEqual(vd._get_visible_center_position(clip, 400, 200), (50, 60))

    def test_offsets_by_visible_pixel_bbox(self):
        frame = vd.np.zeros((80, 300), dtype=vd.np.float32)
        frame[5:15, :] = 1.0
        clip = self._clip_with_mask(_FakeMask(frame))
        self.assertEqual(vd._get_visible_center_position(clip, 400, 200), (50, 90))

    def test_falls_back_when_mask_read_fails(self):
        def _broken_get_frame(t=0):
            raise RuntimeError("mask read failed")

        mask = type("BrokenMask", (), {"get_frame": _broken_get_frame})()
        clip = self._clip_with_mask(mask)
        with patch.object(vd.logger, "debug") as log_debug:
            self.assertEqual(vd._get_visible_center_position(clip, 400, 200), (50, 60))
        log_debug.assert_called_once()


class TestSubtitleFontSupport(unittest.TestCase):
    def setUp(self):
        vd._subtitle_font_supports_sample.cache_clear()

    def tearDown(self):
        vd._subtitle_font_supports_sample.cache_clear()

    def test_font_supports_all_sample_chars(self):
        with patch.object(vd.ImageFont, "truetype", return_value=_FakeGlyphFont()):
            self.assertTrue(vd._subtitle_font_supports_sample("/f/regular.ttf", "hello"))

    def test_font_missing_glyph_returns_false(self):
        font = _FakeGlyphFont(missing_chars={"b"})
        with patch.object(vd.ImageFont, "truetype", return_value=font):
            self.assertFalse(vd._subtitle_font_supports_sample("/f/partial.ttf", "abc"))

    def test_font_probe_failure_logs_and_returns_true(self):
        with (
            patch.object(
                vd.ImageFont, "truetype", side_effect=OSError("cannot load font")
            ),
            patch.object(vd.logger, "warning") as warning,
        ):
            self.assertTrue(vd._subtitle_font_supports_sample("/f/empty.ttf", "abc"))
        warning.assert_called_once()

    def test_text_without_letters_is_always_considered_supported(self):
        self.assertTrue(vd.subtitle_font_supports_text("/any/font.ttf", "！？,.。 "))

    def test_text_filters_sample_to_letters_and_digits(self):
        with patch.object(
            vd, "_subtitle_font_supports_sample", return_value=True
        ) as sample_check:
            self.assertTrue(vd.subtitle_font_supports_text("/f/x.ttf", "héllo 123!"))
        sample = sample_check.call_args.args[1]
        self.assertEqual(sample, "hélo123")


class TestGenerateVideoSubtitleRendering(unittest.TestCase):
    def _run_generate_video_with_subtitles(self, params, subtitle_items, *, patch_nt=False):
        """带字幕跑完整 generate_video，模拟 create_text_clip 的全部渲染分支。"""
        subtitle_text_clips = []
        bg_calls = []
        composite_clips = []
        layout_clips = []

        def _new_text_clip(**kwargs):
            clip = _FakeSubtitleTextClip()
            subtitle_text_clips.append(clip)
            layout_clips.append(clip)
            return clip

        def _new_bg_clip(*args, **kwargs):
            bg_calls.append((args, kwargs))
            clip = _FakeSubtitleTextClip()
            layout_clips.append(clip)
            return clip

        def _new_composite(clips, **kwargs):
            clip = _FakeSubtitleTextClip()
            clip.child_clips = list(clips)
            composite_clips.append(clip)
            layout_clips.append(clip)
            return clip

        def _build_fake_subtitles_clip(**kwargs):
            make_textclip = kwargs.pop("make_textclip", None)
            if make_textclip is not None:
                make_textclip("probe")
            return _FakeSubtitlesClip(subtitle_items)

        source_video = _FakeMoviePyClip()
        voice_source = _FakeMoviePyClip()
        source_video.with_audio_result = source_video

        with tempfile.TemporaryDirectory() as tmp, \
            patch.object(vd, "_open_video_clip_quietly", return_value=source_video), \
            patch.object(vd, "AudioFileClip", return_value=voice_source), \
            patch.object(vd.bgm_service, "should_use_bgm", return_value=False), \
            patch.object(vd, "SubtitlesClip", side_effect=_build_fake_subtitles_clip), \
            patch.object(vd, "TextClip", side_effect=_new_text_clip), \
            patch.object(vd, "wrap_text", return_value=("LINE ONE\nLINE TWO", 120)), \
            patch.object(vd, "_rounded_subtitle_background_clip", side_effect=_new_bg_clip), \
            patch.object(vd, "CompositeVideoClip", side_effect=_new_composite), \
            patch.object(vd, "_write_videofile_with_codec_fallback"), \
            patch.object(vd, "_get_configured_video_codec", return_value="libx264"), \
            (patch.object(vd.os, "name", "nt") if patch_nt else nullcontext()):
            subtitle_file = os.path.join(tmp, "subs.srt")
            with open(subtitle_file, "wb"):
                pass
            output_file = os.path.join(tmp, "out", "final.mp4")
            os.makedirs(os.path.dirname(output_file), exist_ok=True)
            result = vd.generate_video(
                video_path="combined.mp4",
                audio_path="voice.mp3",
                subtitle_path=subtitle_file,
                output_file=output_file,
                params=params,
            )
        return (
            result,
            layout_clips,
            subtitle_text_clips,
            bg_calls,
            composite_clips,
        )

    def _finalized_subtitle_clip(self, layout_clips):
        finalized = [c for c in layout_clips if c._start is not None]
        self.assertEqual(len(finalized), 1)
        return finalized[0]

    def test_generate_video_renders_rounded_subtitle_background(self):
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=True,
            subtitle_position="bottom",
            rounded_subtitle_background=True,
            text_background_color="#123456",
            text_fore_color="#FFFFFF",
            stroke_width=2,
            font_size=60,
        )
        result, layout_clips, _, bg_calls, _ = self._run_generate_video_with_subtitles(
            params, [((0.0, 2.0), "Hello")], patch_nt=False
        )

        self.assertTrue(result)
        self.assertEqual(len(bg_calls), 1)
        self.assertEqual(bg_calls[0][1]["color"], "#123456")
        self.assertEqual(bg_calls[0][1]["alpha"], 140)
        self.assertEqual(bg_calls[0][1]["radius"], max(8, int(60 * 0.4)))
        final = self._finalized_subtitle_clip(layout_clips)
        self.assertEqual(final._start, 0.0)
        self.assertEqual(final._end, 2.0)
        self.assertEqual(final._duration, 2.0)
        self.assertEqual(final.position, ("center", 1744.0))

    def test_generate_video_renders_rect_subtitle_background(self):
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=True,
            subtitle_position="bottom",
            text_background_color="#123456",
        )
        result, layout_clips, _, bg_calls, _ = self._run_generate_video_with_subtitles(
            params, [((0.0, 2.0), "Hello")]
        )

        self.assertTrue(result)
        self.assertEqual(len(bg_calls), 1)
        self.assertEqual(bg_calls[0][1]["alpha"], 255)
        self.assertEqual(bg_calls[0][1]["radius"], 0)
        final = self._finalized_subtitle_clip(layout_clips)
        self.assertEqual(final.position, ("center", 1744.0))

    def test_generate_video_renders_plain_subtitle_without_background(self):
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=True,
            subtitle_position="center",
            text_background_color=False,
        )
        result, layout_clips, text_clips, bg_calls, _ = self._run_generate_video_with_subtitles(
            params, [((0.0, 2.0), "Hello")]
        )

        self.assertTrue(result)
        self.assertEqual(bg_calls, [])
        final = self._finalized_subtitle_clip(layout_clips)
        self.assertIn(final, text_clips)
        self.assertEqual(final.position, ("center", "center"))

    def test_generate_video_positions_subtitles_in_every_mode(self):
        cases = [
            ("bottom", 70.0, ("center", 1744.0)),
            ("top", 70.0, ("center", 96.0)),
            ("custom", 0.0, ("center", 10.0)),
            ("custom", 100.0, ("center", 1830.0)),
            ("center", 70.0, ("center", "center")),
        ]
        for position, custom_position, expected in cases:
            with self.subTest(position=position, custom_position=custom_position):
                params = vd.VideoParams(
                    video_subject="test",
                    subtitle_enabled=True,
                    subtitle_position=position,
                    custom_position=custom_position,
                    text_background_color=False,
                )
                result, layout_clips, _, _, _ = self._run_generate_video_with_subtitles(
                    params, [((0.0, 2.0), "Hello")]
                )
                self.assertTrue(result)
                final = self._finalized_subtitle_clip(layout_clips)
                self.assertEqual(final.position, expected)

    def test_generate_video_normalizes_true_bool_background_to_black(self):
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=True,
            text_background_color=True,
        )
        result, _, _, bg_calls, _ = self._run_generate_video_with_subtitles(
            params, [((0.0, 2.0), "Hello")]
        )
        self.assertTrue(result)
        self.assertEqual(len(bg_calls), 1)
        self.assertEqual(bg_calls[0][1]["color"], "#000000")

    def test_generate_video_uses_default_font_and_nt_path_slashes(self):
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=True,
            subtitle_position="center",
            font_name=None,
        )
        self.assertIsNone(params.font_name)
        result, layout_clips, _, _, _ = self._run_generate_video_with_subtitles(
            params, [((0.0, 2.0), "Hello")], patch_nt=True
        )
        self.assertTrue(result)
        self.assertEqual(params.font_name, "STHeitiMedium.ttc")
        self._finalized_subtitle_clip(layout_clips)

    def test_generate_video_measures_rounded_bg_width_with_fallback(self):
        """圆角背景测量字体失败时应退回最大宽度，不中断后台生成。"""
        params = vd.VideoParams(
            video_subject="test",
            subtitle_enabled=True,
            subtitle_position="bottom",
            rounded_subtitle_background=True,
            text_background_color="#123456",
            font_name="definitely-does-not-exist.ttf",
        )
        result, layout_clips, _, _, _ = self._run_generate_video_with_subtitles(
            params, [((0.0, 2.0), "Hello")]
        )
        self.assertTrue(result)
        final = self._finalized_subtitle_clip(layout_clips)
        self.assertEqual(final.position, ("center", 1744.0))


class TestCodecAndFfmpegBranches(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        vd._runtime_disabled_video_codecs.clear()
        vd._ffmpeg_encoder_exists.cache_clear()

    def test_write_videofile_returns_effective_codec_on_success(self):
        calls = []

        class _FakeClip:
            def write_videofile(self, output_file, codec, **kwargs):
                calls.append(codec)

        used = vd._write_videofile_with_codec_fallback(
            _FakeClip(), "/tmp/ok.mp4", codec="libx264", logger=None, fps=30
        )
        self.assertEqual(used, "libx264")
        self.assertEqual(calls, ["libx264"])

    def test_write_videofile_reraises_when_default_codec_fails(self):
        class _FakeClip:
            def write_videofile(self, output_file, codec, **kwargs):
                raise RuntimeError(f"{codec} cannot write output")

        with self.assertRaises(RuntimeError):
            vd._write_videofile_with_codec_fallback(
                _FakeClip(), "/tmp/fail.mp4", codec="libx264", logger=None, fps=30
            )
        self.assertNotIn("libx264", vd._runtime_disabled_video_codecs)

    def test_disable_runtime_video_codec_ignores_default(self):
        vd._disable_runtime_video_codec("libx264", "some reason")
        self.assertNotIn("libx264", vd._runtime_disabled_video_codecs)

    def test_effective_video_codec_falls_back_when_codec_disabled(self):
        vd._runtime_disabled_video_codecs.add("h264_nvenc")
        with patch.object(vd.utils, "get_ffmpeg_binary") as ffmpeg:
            self.assertEqual(vd._get_effective_video_codec("h264_nvenc"), "libx264")
        ffmpeg.assert_not_called()

    def test_ffmpeg_encoder_exists_falls_back_on_nonzero_returncode(self):
        result = types.SimpleNamespace(returncode=1, stdout="", stderr="encoder missing")
        with patch.object(vd.subprocess, "run", return_value=result):
            self.assertFalse(vd._ffmpeg_encoder_exists("/f/ffmpeg", "h264_nvenc"))

    def test_ffmpeg_encoder_exists_returns_true_when_codec_in_stdout(self):
        result = types.SimpleNamespace(
            returncode=0, stdout="h264_nvenc ..... NVIDIA NVENC H.264 encoder", stderr=""
        )
        with patch.object(vd.subprocess, "run", return_value=result):
            self.assertTrue(vd._ffmpeg_encoder_exists("/f/ffmpeg", "h264_nvenc"))

    def test_get_ffmpeg_binary_wrapper_delegates_to_utils(self):
        with patch.object(
            vd.utils, "get_ffmpeg_binary", return_value="/bin/ffmpeg"
        ) as utils_mock:
            self.assertEqual(vd.get_ffmpeg_binary(), "/bin/ffmpeg")
        utils_mock.assert_called_once()

    def test_sub_clipped_video_clip_str_includes_fields(self):
        item = vd.SubClippedVideoClip(
            file_path="a.mp4",
            start_time=1.0,
            end_time=3.0,
            width=100,
            height=200,
        )
        rendered = str(item)
        self.assertIn("file_path=a.mp4", rendered)
        self.assertIn("width=100", rendered)
        self.assertIn("height=200", rendered)

    def test_get_configured_video_codec_warns_on_unsupported_codec(self):
        config.app["video_codec"] = "vp8compat"
        with patch.object(vd.logger, "warning") as warning:
            codec = vd._get_configured_video_codec()
            self.assertIn(codec, vd._SUPPORTED_VIDEO_CODECS)
        warning.assert_called_once()

    def test_concat_video_clips_reraises_when_default_codec_fails(self):
        config.app["video_codec"] = "libx264"

        def fake_run(command, capture_output, text, check):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

        with tempfile.TemporaryDirectory() as temp_dir:
            clip_file = os.path.join(temp_dir, "clip.mp4")
            output_file = os.path.join(temp_dir, "combined.mp4")
            Path(clip_file).write_bytes(b"fake")
            with patch.object(vd.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    vd.concat_video_clips_with_ffmpeg(
                        clip_files=[clip_file],
                        output_file=output_file,
                        threads=1,
                        output_dir=temp_dir,
                    )


class TestGetBgmFileBranches(unittest.TestCase):
    def test_get_bgm_file_disabled_when_type_empty(self):
        self.assertEqual(vd.get_bgm_file(bgm_type=""), "")

    def test_get_bgm_file_random_falls_back_when_no_files(self):
        with (
            patch.object(vd.bgm_service, "list_bgm_files", return_value=[]),
            patch.object(vd.logger, "warning") as warning,
        ):
            self.assertEqual(vd.get_bgm_file(bgm_type="random"), "")
        warning.assert_called_once()

    def test_get_bgm_file_random_picks_existing_file(self):
        with patch.object(
            vd.bgm_service, "list_bgm_files", return_value=["a.mp3", "b.mp3"]
        ):
            result = vd.get_bgm_file(bgm_type="random")
        self.assertIn(result, ["a.mp3", "b.mp3"])

    def test_get_bgm_file_returns_empty_for_unknown_type(self):
        self.assertEqual(vd.get_bgm_file(bgm_type="silent"), "")


class TestImageAndClipHelpers(unittest.TestCase):
    def test_open_image_clip_with_fallback_uses_original_when_direct_opens(self):
        fake_clip = object()
        with patch.object(vd, "ImageClip", return_value=fake_clip):
            clip, path = vd._open_image_clip_with_fallback("img.png")
        self.assertIs(clip, fake_clip)
        self.assertEqual(path, "img.png")

    def test_open_image_clip_with_fallback_tries_sanitized_copy(self):
        fake_clip = object()
        with (
            patch.object(
                vd, "ImageClip", side_effect=[RuntimeError("bad exif"), fake_clip]
            ),
            patch.object(vd, "_sanitize_image_file", return_value="cleaned.png") as sanitize,
        ):
            clip, path = vd._open_image_clip_with_fallback("img.png")
        sanitize.assert_called_once_with("img.png")
        self.assertIs(clip, fake_clip)
        self.assertEqual(path, "cleaned.png")

    def test_open_video_clip_quietly_logs_suppressed_stdout(self):
        class _NoisyClip:
            def __init__(self, path, audio=False):
                print("audio_found: False")

            def close(self):
                pass

        with (
            patch.object(vd, "VideoFileClip", side_effect=_NoisyClip),
            patch.object(vd.logger, "debug") as log_debug,
        ):
            vd._open_video_clip_quietly("noisy.mp4")
        log_debug.assert_called_once()

    def test_close_clip_none_is_noop(self):
        self.assertIsNone(vd.close_clip(None))

    def test_close_clip_logs_error_when_reader_close_raises(self):
        class _BadReader:
            def close(self):
                raise RuntimeError("disk error")

        class _BadClip:
            reader = _BadReader()
            audio = None
            mask = None
            clips = []

        with patch.object(vd.logger, "error") as log_error:
            vd.close_clip(_BadClip())
        log_error.assert_called_once()

    def test_close_clip_handles_audio_and_mask_without_reader(self):
        class _ReaderLike:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Clip:
            def __init__(self):
                self.reader = _ReaderLike()
                self.audio = types.SimpleNamespace(reader=None)
                self.mask = types.SimpleNamespace(reader=None)
                self.clips = []

        clip = _Clip()
        vd.close_clip(clip)
        self.assertTrue(clip.reader.closed)
        self.assertFalse(hasattr(clip, "audio"))
        self.assertFalse(hasattr(clip, "mask"))

    def test_sanitize_image_file_strips_source_metadata(self):
        from PIL import Image as PILImage

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, "source.png")
            PILImage.new("RGB", (4, 4), color=(255, 0, 0)).save(source_path)
            sanitized_path = vd._sanitize_image_file(source_path)
            self.assertTrue(os.path.exists(sanitized_path))
            with PILImage.open(sanitized_path) as opened:
                self.assertEqual(opened.size, (4, 4))

    def test_close_clip_releases_nested_reader_resources(self):
        class _ReaderLike:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Child:
            def __init__(self):
                self.reader = _ReaderLike()
                self.audio = types.SimpleNamespace(reader=_ReaderLike())
                self.mask = None
                self.clips = []

        class _RichClip:
            def __init__(self):
                self.reader = _ReaderLike()
                self.audio = types.SimpleNamespace(reader=_ReaderLike())
                self.mask = types.SimpleNamespace(reader=_ReaderLike())
                self.child = _Child()
                self.clips = [self.child, self]

        clip = _RichClip()
        audio_reader = clip.audio.reader
        mask_reader = clip.mask.reader
        child_reader = clip.child.reader
        child_audio_reader = clip.child.audio.reader
        vd.close_clip(clip)

        self.assertTrue(clip.reader.closed)
        self.assertTrue(audio_reader.closed)
        self.assertTrue(mask_reader.closed)
        self.assertTrue(child_reader.closed)
        self.assertTrue(child_audio_reader.closed)
        self.assertFalse(hasattr(clip, "audio"))
        self.assertFalse(hasattr(clip, "mask"))
        self.assertEqual(clip.clips, [])
        self.assertEqual(clip.child.clips, [])


class TestCombineVideosBranches(unittest.TestCase):
    def _run_combine_with_clip(
        self,
        *,
        size=(1080, 1920),
        duration=3.0,
        transition=None,
        max_clip_duration=5,
        audio_duration=2.0,
        speed=1.0,
        fail_subclipped=False,
        choice_fn=lambda seq: seq[0],
    ):
        combined_written = []

        def _open(path):
            clip = _RichFakeClip(duration=duration, size=size)
            clip.fail_subclipped = fail_subclipped
            return clip

        def _capture_written(clip, *args, **kwargs):
            combined_written.append(clip)

        def _make_composite(clips):
            return clips[1] if len(clips) > 1 else clips[0]

        with tempfile.TemporaryDirectory() as tmp:
            combined_video_path = os.path.join(tmp, "combined.mp4")
            with (
                patch.object(
                    vd,
                    "AudioFileClip",
                    return_value=types.SimpleNamespace(
                        duration=audio_duration, close=lambda: None
                    ),
                ),
                patch.object(vd, "_open_video_clip_quietly", side_effect=_open),
                patch.object(vd, "_write_videofile_with_codec_fallback", side_effect=_capture_written),
                patch.object(
                    vd,
                    "_prioritize_unique_source_clips",
                    side_effect=lambda subclipped_items, concat_mode: subclipped_items,
                ),
                patch.object(vd, "concat_video_clips_with_ffmpeg") as concat,
                patch.object(vd, "delete_files"),
                patch.object(vd.random, "choice", side_effect=choice_fn),
                patch.object(vd, "ColorClip", return_value=_FakeBgClip()),
                patch.object(vd, "CompositeVideoClip", side_effect=_make_composite) as composite,
                patch.object(vd.video_effects, "fadein_transition", side_effect=lambda c, *a, **k: c) as fadein,
                patch.object(vd.video_effects, "fadeout_transition", side_effect=lambda c, *a, **k: c) as fadeout,
                patch.object(vd.video_effects, "slidein_transition", side_effect=lambda c, *a, **k: c) as slidein,
                patch.object(vd.video_effects, "slideout_transition", side_effect=lambda c, *a, **k: c) as slideout,
                patch.object(vd.video_effects, "zoomin_transition", side_effect=lambda c, *a, **k: c) as zoomin,
                patch.object(vd.video_effects, "zoomout_transition", side_effect=lambda c, *a, **k: c) as zoomout,
            ):
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["source.mp4"],
                    audio_file="audio.mp3",
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    video_transition_mode=transition,
                    max_clip_duration=max_clip_duration,
                    clip_speed=speed,
                )

        return {
            "result": result,
            "written": combined_written,
            "concat": concat,
            "composite": composite,
            "transition_mocks": {
                "fadein": fadein,
                "fadeout": fadeout,
                "slidein": slidein,
                "slideout": slideout,
                "zoomin": zoomin,
                "zoomout": zoomout,
            },
        }

    def test_combine_videos_resizes_matching_ratio_to_fill_frame(self):
        outcome = self._run_combine_with_clip(size=(540, 960))
        clip = outcome["written"][0]
        resized = [e for e in clip.events if e[0] == "resized"]
        self.assertEqual(len(resized), 1)
        self.assertEqual(resized[0][2], {"new_size": (1080, 1920)})
        outcome["composite"].assert_not_called()

    def test_combine_videos_letterboxes_different_ratio_clip(self):
        outcome = self._run_combine_with_clip(size=(1920, 1080))
        clip = outcome["written"][0]
        resized = [e for e in clip.events if e[0] == "resized"]
        self.assertEqual(len(resized), 1)
        self.assertEqual(resized[0][2], {"new_size": (1080, 607)})
        self.assertIn(("position", "center"), clip.events)
        outcome["composite"].assert_called_once()
        self.assertEqual(
            outcome["composite"].call_args.args[0][0],
            "left" if False else outcome["composite"].call_args.args[0][0],
        )

    def test_combine_videos_applies_every_transition_mode(self):
        expected_calls = {
            None: [],
            vd.VideoTransitionMode.none: [],
            vd.VideoTransitionMode.fade_in: ["fadein"],
            vd.VideoTransitionMode.fade_out: ["fadeout"],
            vd.VideoTransitionMode.slide_in: ["slidein"],
            vd.VideoTransitionMode.slide_out: ["slideout"],
            vd.VideoTransitionMode.zoom_in: ["zoomin"],
            vd.VideoTransitionMode.zoom_out: ["zoomout"],
            vd.VideoTransitionMode.shuffle: ["fadein"],
        }
        for transition, expected in expected_calls.items():
            with self.subTest(transition=transition):
                outcome = self._run_combine_with_clip(transition=transition)
                for name, mock in outcome["transition_mocks"].items():
                    if name in expected:
                        mock.assert_called_once()
                    else:
                        mock.assert_not_called()

    def test_combine_videos_unknown_transition_value_passes_clip_through(self):
        outcome = self._run_combine_with_clip(transition="spin")
        self.assertTrue(outcome["result"])
        self.assertEqual(len(outcome["written"]), 1)
        for name, mock in outcome["transition_mocks"].items():
            mock.assert_not_called()

    def test_combine_videos_shuffle_picks_every_transition(self):
        transition_names = list(self._run_combine_with_clip()["transition_mocks"])
        for k in range(len(transition_names)):
            with self.subTest(shuffle_index=k):

                def _choice(seq, k=k):
                    if seq and isinstance(seq[0], str):
                        return "left"
                    return seq[k]

                outcome = self._run_combine_with_clip(
                    transition=vd.VideoTransitionMode.shuffle, choice_fn=_choice
                )
                for name, mock in outcome["transition_mocks"].items():
                    if name == transition_names[k]:
                        mock.assert_called_once()
                    else:
                        mock.assert_not_called()

    def test_combine_videos_letterboxes_tall_ratio_by_height(self):
        outcome = self._run_combine_with_clip(size=(500, 1500))
        clip = outcome["written"][0]
        resized = [e for e in clip.events if e[0] == "resized"]
        self.assertEqual(len(resized), 1)
        self.assertEqual(resized[0][2], {"new_size": (640, 1920)})
        self.assertIn(("position", "center"), clip.events)
        outcome["composite"].assert_called_once()

    def test_combine_videos_trims_oversized_transition_clip(self):
        class _InflatedClip(_RichFakeClip):
            def __init__(self, size=(1080, 1920)):
                super().__init__(duration=6.0, size=size)

        inflated = _InflatedClip()
        with tempfile.TemporaryDirectory() as tmp:
            combined_video_path = os.path.join(tmp, "combined.mp4")
            with (
                patch.object(
                    vd,
                    "AudioFileClip",
                    return_value=types.SimpleNamespace(
                        duration=2.0, close=lambda: None
                    ),
                ),
                patch.object(
                    vd,
                    "_open_video_clip_quietly",
                    side_effect=lambda p: _RichFakeClip(duration=3.0, size=(1080, 1920)),
                ),
                patch.object(
                    vd,
                    "_write_videofile_with_codec_fallback",
                ) as writer,
                patch.object(
                    vd,
                    "_prioritize_unique_source_clips",
                    side_effect=lambda subclipped_items, concat_mode: subclipped_items,
                ),
                patch.object(vd, "concat_video_clips_with_ffmpeg"),
                patch.object(vd, "delete_files"),
                patch.object(
                    vd.random, "choice", side_effect=lambda seq: seq[0]
                ),
                patch.object(
                    vd.video_effects, "fadein_transition", return_value=inflated
                ),
            ):
                vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["source.mp4"],
                    audio_file="audio.mp3",
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    video_transition_mode=vd.VideoTransitionMode.fade_in,
                    max_clip_duration=3,
                    clip_speed=1.0,
                )

        self.assertEqual(inflated.subclipped_to, (0, 3))
        self.assertEqual(writer.call_args.args[0].duration, 3.0)

    def test_combine_videos_loops_processed_clips_to_cover_audio(self):
        looped_written = []

        def _capture_written(clip, *args, **kwargs):
            looped_written.append(clip)

        with tempfile.TemporaryDirectory() as tmp:
            combined_video_path = os.path.join(tmp, "combined.mp4")
            with (
                patch.object(
                    vd,
                    "AudioFileClip",
                    return_value=types.SimpleNamespace(
                        duration=3.0, close=lambda: None
                    ),
                ),
                patch.object(
                    vd,
                    "_open_video_clip_quietly",
                    side_effect=lambda p: _RichFakeClip(
                        duration=3.0, size=(1080, 1920)
                    ),
                ),
                patch.object(
                    vd, "_write_videofile_with_codec_fallback", side_effect=_capture_written
                ),
                patch.object(
                    vd,
                    "_prioritize_unique_source_clips",
                    side_effect=lambda subclipped_items, concat_mode: subclipped_items,
                ),
                patch.object(vd, "concat_video_clips_with_ffmpeg") as concat,
                patch.object(vd, "delete_files"),
            ):
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["source.mp4"],
                    audio_file="audio.mp3",
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    max_clip_duration=10,
                )

        self.assertEqual(result, combined_video_path)
        self.assertEqual(len(looped_written), 1)
        self.assertEqual(len(concat.call_args.kwargs["clip_files"]), 2)
        self.assertEqual(concat.call_args.kwargs["max_duration"], 3.0)

    def test_combine_videos_recovers_when_clip_processing_fails(self):
        def _failing_open(path):
            clip = _RichFakeClip(duration=3.0, size=(1080, 1920))
            clip.fail_subclipped = True
            return clip

        with tempfile.TemporaryDirectory() as tmp:
            combined_video_path = os.path.join(tmp, "combined.mp4")
            with (
                patch.object(
                    vd,
                    "AudioFileClip",
                    return_value=types.SimpleNamespace(
                        duration=2.0, close=lambda: None
                    ),
                ),
                patch.object(
                    vd, "_open_video_clip_quietly", side_effect=_failing_open
                ),
                patch.object(vd, "_write_videofile_with_codec_fallback"),
                patch.object(
                    vd,
                    "_prioritize_unique_source_clips",
                    side_effect=lambda subclipped_items, concat_mode: subclipped_items,
                ),
                patch.object(vd, "concat_video_clips_with_ffmpeg") as concat,
                patch.object(vd, "delete_files"),
                patch.object(vd.logger, "error") as log_error,
            ):
                result = vd.combine_videos(
                    combined_video_path=combined_video_path,
                    video_paths=["source.mp4"],
                    audio_file="audio.mp3",
                    video_aspect=vd.VideoAspect.portrait,
                    video_concat_mode=vd.VideoConcatMode.sequential,
                    max_clip_duration=10,
                )

        self.assertEqual(result, combined_video_path)
        log_error.assert_called_once()
        concat.assert_not_called()


class TestPreprocessVideoBranches(unittest.TestCase):
    def _run_preprocess(
        self,
        materials,
        *,
        ext="mp4",
        video_size=(1080, 1920),
        video_raises=False,
        image_size=(1080, 1920),
        image_raises=False,
        image_path="/tmp/local_videos/images/image.png",
    ):
        def _video_open(path):
            if video_raises:
                raise RuntimeError("cannot probe video")
            return _FakeSizedClip(video_size)

        def _image_open(path):
            if image_raises:
                raise RuntimeError("cannot read image")
            return _FakeSizedClip(image_size), image_path

        with (
            patch.object(vd.utils, "storage_dir", return_value="/tmp/local_videos"),
            patch.object(
                vd.file_security,
                "resolve_path_within_directory",
                return_value="/tmp/local_videos/source.mp4",
            ),
            patch.object(vd.utils, "parse_extension", return_value=ext),
            patch.object(vd, "_open_video_clip_quietly", side_effect=_video_open),
            patch.object(vd, "_open_image_clip_with_fallback", side_effect=_image_open),
            patch.object(vd, "close_clip") as close_clip,
            patch.object(vd, "ImageClip") as image_clip,
            patch.object(vd, "CompositeVideoClip") as composite_clip,
            patch.object(vd.logger, "warning") as warning,
        ):
            result = vd.preprocess_video(materials, clip_duration=4)
        return {
            "result": result,
            "close_clip": close_clip,
            "image_clip": image_clip,
            "composite_clip": composite_clip,
            "warning": warning,
        }

    def test_preprocess_video_returns_empty_for_no_materials(self):
        self.assertEqual(vd.preprocess_video([], clip_duration=4), [])

    def test_preprocess_video_skips_material_without_url(self):
        material = vd.MaterialInfo(provider="local", url="")
        outcome = self._run_preprocess([material])
        self.assertEqual(outcome["result"], [])
        outcome["image_clip"].assert_not_called()

    def test_preprocess_video_accepts_standard_video_material(self):
        material = vd.MaterialInfo(provider="local", url="source.mp4")
        outcome = self._run_preprocess([material], ext="mp4")
        self.assertEqual(outcome["result"], [material])
        self.assertEqual(material.url, "/tmp/local_videos/source.mp4")
        outcome["close_clip"].assert_called_once()
        outcome["image_clip"].assert_not_called()

    def test_preprocess_video_falls_back_to_image_when_video_probe_fails(self):
        material = vd.MaterialInfo(provider="local", url="source.mp4")
        outcome = self._run_preprocess(
            [material],
            ext="mp4",
            video_raises=True,
            image_path="/tmp/local_videos/source.mp4",
        )
        self.assertEqual(outcome["result"], [material])
        self.assertEqual(material.url, "/tmp/local_videos/source.mp4")

    def test_preprocess_video_skips_unreadable_material(self):
        material = vd.MaterialInfo(provider="local", url="source.mp4")
        outcome = self._run_preprocess(
            [material], ext="mp4", video_raises=True, image_raises=True
        )
        self.assertEqual(outcome["result"], [])
        outcome["warning"].assert_called_once()

    def test_preprocess_video_rejects_low_resolution_material(self):
        material = vd.MaterialInfo(provider="local", url="source.mp4")
        outcome = self._run_preprocess([material], ext="mp4", video_size=(100, 100))
        self.assertEqual(outcome["result"], [])
        outcome["close_clip"].assert_called_once()
        outcome["warning"].assert_called_once()

    def test_preprocess_video_rethrows_after_closing_image_export_failure(self):
        material = vd.MaterialInfo(provider="local", url="source.png")
        with (
            patch.object(vd.utils, "storage_dir", return_value="/tmp/local_videos"),
            patch.object(
                vd.file_security,
                "resolve_path_within_directory",
                return_value="/tmp/local_videos/source.png",
            ),
            patch.object(vd.utils, "parse_extension", return_value="png"),
            patch.object(vd, "_open_image_clip_with_fallback") as open_image,
            patch.object(vd, "close_clip") as close_clip,
            patch.object(vd, "ImageClip", side_effect=RuntimeError("export failed")),
            patch.object(vd, "CompositeVideoClip"),
        ):
            open_image.return_value = (
                _FakeSizedClip((1080, 1920)),
                "/tmp/local_videos/source.png",
            )
            with self.assertRaises(RuntimeError):
                vd.preprocess_video([material], clip_duration=4)
        self.assertGreaterEqual(close_clip.call_count, 2)


if __name__ == "__main__":
    unittest.main()
