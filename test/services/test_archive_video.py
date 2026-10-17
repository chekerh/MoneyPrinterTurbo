import json
import os

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

# tools/ 现在随仓库一起提供。仓库根用 pyproject.toml 定位，而不是数 dirname()：
# 原��写法多套了一层，在本机恰好指到"仓库的上一级"——那里也有个 tools/，
# 于是本机全绿，干净克隆却 import 不到。按标记文件找根不会数错层数。
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
while REPO_ROOT != os.path.dirname(REPO_ROOT) and not os.path.isfile(
    os.path.join(REPO_ROOT, "pyproject.toml")
):
    REPO_ROOT = os.path.dirname(REPO_ROOT)
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# tools 目录本身已在 sys.path（与 Main.py 一致），直接导入 tools 内的模块。
import archive_video  # noqa: E402
import plans  # noqa: E402


class TestArchiveChannelVideo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.channel = "testch"
        self.plans_root = self.tmp / "plans"
        self.out_dir = self.tmp / "outputs" / "testch" / date.today().strftime("%Y-%m")
        (self.plans_root / self.channel).mkdir(parents=True, exist_ok=True)
        (self.plans_root / self.channel / "topics_30day.txt").write_text(
            "Topic One\nTopic Two\n", encoding="utf-8"
        )
        self.task_dir = self.tmp / "tasks" / "task-abc"
        self.task_dir.mkdir(parents=True, exist_ok=True)

        # 本组用例只关心归档的写入顺序、命名和计划重写，payload 用的是占位字节
        # 而不是真实视频。成片完整性校验由 TestArchiveMediaValidation 单独覆盖，
        # 这里直接把探测结果固定为「有效」，避免每个用例都依赖真实 ffprobe。
        patcher = mock.patch.object(
            archive_video, "probe_dimensions", return_value=(1920, 1080)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_script(self, video_subject="Topic One"):
        (self.task_dir / "script.json").write_text(
            json.dumps(
                {
                    "params": {
                        "video_subject": video_subject,
                        "video_aspect": "16:9",
                        "voice_name": "mia",
                        "voice_rate": "1.0",
                        "subtitle_position": "bottom",
                    },
                    "script": "Hello world.\n\nSecond line.",
                    "search_terms": ["term one", "term two"],
                    "material_sources": [
                        {
                            "creator": {"name": "creator-one"},
                            "source_page": "https://example.com/page",
                            "search_term": "term one",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_final_video(self, content=b"video", subtitle=True):
        (self.task_dir / "final-1.mp4").write_bytes(content)
        if subtitle:
            (self.task_dir / "subtitle.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nhi\n", encoding="utf-8"
            )

    def _expected_base(self, idx, topic):
        return f"{idx:02d}-{date.today().isoformat()}-{archive_video.slugify(topic)}"

    def test_archive_without_copy_writes_txt_and_rewrites_plan(self):
        self._write_script()
        self._write_final_video()

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="ignored-fallback",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            duration="12.5",
        )

        expected = self._expected_base(1, "Topic One")
        self.assertEqual(base, expected)
        videos = self.out_dir / "videos"
        self.assertFalse(list(videos.glob("*.mp4")), "CLI 路径不复制媒体文件")

        txt = (videos / f"{base}.txt").read_text(encoding="utf-8")
        self.assertIn("topic: Topic One", txt)
        self.assertIn("video: videos/%s.mp4" % base, txt)
        self.assertIn("duration: 12.5s", txt)
        self.assertIn("task_id: task-abc", txt)
        self.assertIn("Hello world.", txt)

        plan = (self.out_dir / "monthly-plan.md").read_text(encoding="utf-8")
        self.assertIn("1/2 topics recorded", plan)
        self.assertIn("✓", plan)
        self.assertIn("pending", plan)

    def test_archive_with_copy_media_copies_mp4_and_srt(self):
        self._write_script()
        self._write_final_video(content=b"payload", subtitle=True)

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            copy_media=True,
        )

        videos = self.out_dir / "videos"
        self.assertEqual((videos / f"{base}.mp4").read_bytes(), b"payload")
        self.assertTrue((videos / f"{base}.srt").exists())

    def test_archive_keeps_posting_text_and_success_receipt(self):
        from app.services import posting_package

        self._write_script()
        self._write_final_video(content=b"payload", subtitle=True)
        source = self.task_dir / "final-1.mp4"
        package = posting_package.fallback("Specific title", "Specific description", self.channel, self.task_dir.name)
        posting_package.save_for_video(source, package)
        receipt = {"version": 1, "channel": self.channel, "result": {"youtube": {"success": True, "url": "url"}}, "pending": []}
        source.with_suffix(".publish.json").write_text(json.dumps(receipt))
        base = archive_video.archive_channel_video(
            self.channel, str(self.task_dir), "Topic One", str(self.out_dir), str(self.plans_root),
            duration="5", copy_media=True,
        )
        archived = self.out_dir / "videos" / f"{base}.mp4"
        self.assertEqual(posting_package.load_for_video(archived), package)
        self.assertEqual(json.loads(archived.with_suffix(".publish.json").read_text()), receipt)
        self.assertIn("Specific description", archived.with_suffix(".posting.txt").read_text())

    def test_archive_does_not_remove_video_during_upload(self):
        self._write_script()
        self._write_final_video(content=b"payload")
        source = self.task_dir / "final-1.mp4"
        source.with_suffix(".publish.lock").write_text("uploading")
        with self.assertRaisesRegex(archive_video.ArchiveError, "being published"):
            archive_video.archive_channel_video(
                self.channel, str(self.task_dir), "Topic One", str(self.out_dir), str(self.plans_root),
                duration="5", copy_media=True,
            )
        self.assertTrue(source.is_file())
        self.assertFalse((self.out_dir / "monthly-plan.md").exists())

    def test_copy_media_removes_the_working_copy(self):
        """归档成功后必须删掉 outputs/<task_id>/ 里的工作副本。

        成片先落在 outputs/<task_id>/final-1.mp4 再被归档。以前只复制不删除，
        而 outputs/<task_id>/ 又属于 PERSISTENT_SUBDIRS、不会被自动清理，于是
        outputs/ 顶层永久多出一个无名的 UUID 目录——那里攒了 9 个。
        """
        self._write_script()
        self._write_final_video(content=b"payload")

        archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            copy_media=True,
        )

        self.assertFalse(
            (self.task_dir / "final-1.mp4").exists(),
            "归档成功后工作副本仍在，outputs/ 会重新开始堆积 UUID 目录",
        )

    def test_without_copy_media_the_working_copy_survives(self):
        """copy_media=False（补 .txt / 计划表的修复路径）不能删源。

        那条路径根本没有复制成片，删掉就是毁掉唯一的成片。
        """
        self._write_script()
        self._write_final_video(content=b"payload")

        archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            copy_media=False,
        )

        self.assertTrue(
            (self.task_dir / "final-1.mp4").exists(),
            "未复制媒体时删掉了源文件——那是唯一一份成片",
        )

    def test_working_copy_survives_a_failed_copy(self):
        """复制失败必须留下源文件。"""
        self._write_script()
        self._write_final_video(content=b"payload")

        with mock.patch.object(
            archive_video, "_copy_verified", side_effect=archive_video.ArchiveError("boom")
        ):
            with self.assertRaises(archive_video.ArchiveError):
                archive_video.archive_channel_video(
                    channel=self.channel,
                    task_dir=str(self.task_dir),
                    topic="Topic One",
                    out_dir=str(self.out_dir),
                    plans_root=str(self.plans_root),
                    copy_media=True,
                )

        self.assertTrue(
            (self.task_dir / "final-1.mp4").exists(),
            "复制失败却删掉了源文件",
        )

    def test_final_video_dir_is_the_source_when_given(self):
        """成片在 outputs/<task_id>/ 时，成片来自那里而不是 task_dir。

        流水线（task._final_video_output_path）写的是 outputs/<task_id>/，
        早先命令行调用方却去 task_dir 找，于是每天的归档步骤必然失败。
        """
        self._write_script()
        self._write_final_video(content=b"wrong-place")
        working = self.out_dir.parent / "some-task-id"
        working.mkdir(parents=True, exist_ok=True)
        (working / "final-1.mp4").write_bytes(b"payload")

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            copy_media=True,
            final_video_dir=str(working),
        )

        self.assertEqual(
            (self.out_dir / "videos" / f"{base}.mp4").read_bytes(),
            b"payload",
            "归档的不是 final_video_dir 里的成片",
        )
        self.assertFalse(
            (working / "final-1.mp4").exists(),
            "final_video_dir 里的工作副本没有被清理",
        )

    def test_archive_with_copy_media_tolerates_missing_subtitle(self):
        self._write_script()
        self._write_final_video(subtitle=False)

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            copy_media=True,
        )

        videos = self.out_dir / "videos"
        self.assertTrue((videos / f"{base}.mp4").exists())
        self.assertFalse((videos / f"{base}.srt").exists())

    def test_unplanned_topic_falls_back_to_placeholder_index(self):
        self._write_script(video_subject="Mystery Topic")

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Mystery Topic",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )

        self.assertEqual(base, self._expected_base(1, "Mystery Topic"))
        plan = (self.out_dir / "monthly-plan.md").read_text(encoding="utf-8")
        self.assertIn("0/2 topics recorded", plan)
        self.assertIn("## Unplanned recordings", plan)
        self.assertIn("Mystery Topic", plan)

    def test_unplanned_index_counts_existing_videos(self):
        videos = self.out_dir / "videos"
        videos.mkdir(parents=True)
        (videos / "01-2026-09-01-other.mp4").write_bytes(b"old")
        self._write_script(video_subject="Mystery Topic")

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Mystery Topic",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )

        self.assertEqual(base, self._expected_base(2, "Mystery Topic"))

    def test_archive_calls_probe_duration_when_duration_missing(self):
        self._write_script()
        self._write_final_video()
        with mock.patch.object(archive_video, "probe_duration", return_value="77"):
            base = archive_video.archive_channel_video(
                channel=self.channel,
                task_dir=str(self.task_dir),
                topic="Topic One",
                out_dir=str(self.out_dir),
                plans_root=str(self.plans_root),
            )

        txt = (self.out_dir / "videos" / f"{base}.txt").read_text(encoding="utf-8")
        self.assertIn("duration: 77s", txt)

    def test_archive_pulls_topic_from_script_params(self):
        self._write_script(video_subject="Topic Two")

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )

        self.assertEqual(base, self._expected_base(2, "Topic Two"))

    def test_archive_matches_numbered_topic_lines(self):
        # plans.load_topics 支持 "1. Topic One" 编号前缀；归档必须解析出相同的话题，
        # 否则会把话题误判为「未计划录制」，plan_index 也拿不到正确的行号。
        (self.plans_root / self.channel / "topics_30day.txt").write_text(
            "1. Topic One\n2. Topic Two\n", encoding="utf-8"
        )
        self._write_script(video_subject="Topic One")

        base = archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )

        self.assertEqual(base, self._expected_base(1, "Topic One"))
        plan = (self.out_dir / "monthly-plan.md").read_text(encoding="utf-8")
        self.assertIn("1/2 topics recorded", plan)
        self.assertNotIn("Unplanned recordings", plan)

    def test_rearchive_preserves_posted_marker(self):
        self._write_script(video_subject="Topic One")
        archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )
        plan_file = self.out_dir / "monthly-plan.md"
        self.assertTrue(
            plans.mark_posted(self.channel, 1, "https://youtu.be/abc123", plan_dir=str(self.out_dir))
        )
        self.assertIn("posted: https://youtu.be/abc123", plan_file.read_text(encoding="utf-8"))

        self._write_script(video_subject="Topic Two")
        archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic Two",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )

        plan = plan_file.read_text(encoding="utf-8")
        self.assertIn("2/2 topics recorded", plan)
        self.assertIn("posted: https://youtu.be/abc123", plan)

    def test_rearchive_preserves_legacy_recorded_row_without_sidecar(self):
        # 早期归档的成片可能没有 videos/<base>.txt 侧写；重写计划时必须原样保留
        # 这些已录制行，而不是把它们退化回 pending。
        plan_file = self.out_dir / "monthly-plan.md"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(
            "# legacy - monthly plan (2026-09)\n\n"
            "1/2 topics recorded\n\n"
            "| # | Topic | Status |\n|---|-------|--------|\n"
            "| 1 | Topic One | ✓ 2026-09-20 - [99-2026-09-20-old.mp4](videos/99-2026-09-20-old.mp4) |\n"
            "| 2 | Topic Two | pending |\n",
            encoding="utf-8",
        )
        self._write_script(video_subject="Topic Two")

        archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic Two",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
        )

        plan = plan_file.read_text(encoding="utf-8")
        self.assertIn("✓ 2026-09-20 - [99-2026-09-20-old.mp4]", plan)
        self.assertIn("Topic Two", plan)


class TestProbeDuration(unittest.TestCase):
    def test_missing_video_returns_empty(self):
        self.assertEqual(archive_video.probe_duration("/nonexistent/video.mp4"), "")

    def test_subprocess_error_returns_empty(self):
        video = Path(tempfile.mkdtemp()) / "video.mp4"
        video.write_bytes(b"m")
        with mock.patch.object(
            archive_video.subprocess, "run", side_effect=OSError("no ffprobe")
        ):
            self.assertEqual(archive_video.probe_duration(str(video)), "")

    def test_probe_returns_stdout(self):
        video = Path(tempfile.mkdtemp()) / "video.mp4"
        video.write_bytes(b"m")
        with mock.patch.object(
            archive_video.subprocess,
            "run",
            return_value=mock.Mock(stdout="12.5\n", stderr=""),
        ):
            self.assertEqual(archive_video.probe_duration(str(video)), "12.5")

    def test_probe_timeout_returns_empty(self):
        # ffprobe 卡死时走 SubprocessError(TimeoutExpired) 分支，返回空而不是阻塞。
        video = Path(tempfile.mkdtemp()) / "video.mp4"
        video.write_bytes(b"m")
        with mock.patch.object(
            archive_video.subprocess,
            "run",
            side_effect=archive_video.subprocess.TimeoutExpired("ffprobe", 5),
        ):
            self.assertEqual(archive_video.probe_duration(str(video)), "")

    def test_probe_bounds_subprocess_timeout(self):
        # 归档在 WebUI 的 2 秒轮询 fragment 内触发，ffprobe 的超时必须压低，
        # 否则单次 30 秒超时会拖住整个轮询；这里固定断言上限 <= 5 秒。
        video = Path(tempfile.mkdtemp()) / "video.mp4"
        video.write_bytes(b"m")
        with mock.patch.object(
            archive_video.subprocess,
            "run",
            return_value=mock.Mock(stdout="", stderr=""),
        ) as run_mock:
            archive_video.probe_duration(str(video))
            _, kwargs = run_mock.call_args
            self.assertLessEqual(kwargs["timeout"], 5)


class TestArchiveMediaValidation(unittest.TestCase):
    """归档前的成片完整性校验。

    旧实现只判断 ``final-1.mp4`` 是否存在就复制，2 秒轮询的任务面板会在 ffmpeg
    刚创建目标文件时立刻触发归档，于是把半截文件写进媒体库并标记为已归档、
    永不重试。下面这些用例锁定新的拒绝条件与「失败不留记录」的行为。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.channel = "testch"
        self.plans_root = self.tmp / "plans"
        self.out_dir = self.tmp / "outputs" / "testch" / date.today().strftime("%Y-%m")
        (self.plans_root / self.channel).mkdir(parents=True, exist_ok=True)
        (self.plans_root / self.channel / "topics_30day.txt").write_text(
            "Topic One\n", encoding="utf-8"
        )
        self.task_dir = self.tmp / "tasks" / "task-abc"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        (self.task_dir / "script.json").write_text(
            json.dumps({"params": {"video_subject": "Topic One"}}), encoding="utf-8"
        )

    def _archive(self):
        return archive_video.archive_channel_video(
            channel=self.channel,
            task_dir=str(self.task_dir),
            topic="Topic One",
            out_dir=str(self.out_dir),
            plans_root=str(self.plans_root),
            copy_media=True,
        )

    def _videos(self):
        return self.out_dir / "videos"

    def test_rejects_missing_final_video(self):
        with self.assertRaises(archive_video.ArchiveError):
            self._archive()
        self.assertEqual(list(self._videos().glob("*")) if self._videos().exists() else [], [])

    def test_rejects_empty_final_video(self):
        (self.task_dir / "final-1.mp4").write_bytes(b"")
        with self.assertRaises(archive_video.ArchiveError):
            self._archive()

    def test_rejects_unparseable_final_video(self):
        (self.task_dir / "final-1.mp4").write_bytes(b"\x00" * 512)
        with self.assertRaises(archive_video.ArchiveError):
            self._archive()

    def test_failure_leaves_no_plan_record_and_no_sidecar(self):
        """失败时不得留下「已录制」记录，否则计划会指向不存在的成片。"""
        (self.task_dir / "final-1.mp4").write_bytes(b"\x00" * 512)
        with self.assertRaises(archive_video.ArchiveError):
            self._archive()

        self.assertFalse((self.out_dir / "monthly-plan.md").exists())
        self.assertEqual(list(self._videos().glob("*.txt")), [])
        self.assertEqual(list(self._videos().glob("*.mp4")), [])

    def test_no_partial_file_is_left_behind_on_failure(self):
        (self.task_dir / "final-1.mp4").write_bytes(b"\x00" * 512)
        with self.assertRaises(archive_video.ArchiveError):
            self._archive()
        self.assertEqual(list(self._videos().glob("*.partial")), [])

    def test_copy_verified_rejects_short_copy(self):
        """复制出的字节数少于源文件时必须判为失败并清理临时文件。

        这正是线上 8 份损坏归档的成因：ffmpeg 仍在写，``shutil.copy2`` 只读到
        一部分。这里让 copy2 只写一半，断言不留下目标文件也不留下 .partial。
        """
        src = self.tmp / "src.mp4"
        src.write_bytes(b"a" * 1000)
        dst = self.tmp / "dst.mp4"

        def short_copy(_src, dst_path):
            Path(dst_path).write_bytes(b"a" * 400)

        with mock.patch.object(archive_video.shutil, "copy2", side_effect=short_copy):
            with self.assertRaises(archive_video.ArchiveError):
                archive_video._copy_verified(src, dst)

        self.assertFalse(dst.exists())
        self.assertEqual(list(self.tmp.glob("*.partial")), [])

    def test_copy_verified_replaces_atomically(self):
        """成功时目标文件内容与源一致，且没有残留临时文件。"""
        src = self.tmp / "src.mp4"
        src.write_bytes(b"b" * 2048)
        dst = self.tmp / "dst.mp4"

        archive_video._copy_verified(src, dst)

        self.assertEqual(dst.read_bytes(), src.read_bytes())
        self.assertEqual(list(self.tmp.glob("*.partial")), [])

    def test_probe_dimensions_reports_unreadable_for_empty_file(self):
        empty = self.tmp / "empty.mp4"
        empty.write_bytes(b"")
        self.assertIsNone(archive_video.probe_dimensions(str(empty)))

    def test_probe_dimensions_parses_ffprobe_output(self):
        video = self.tmp / "video.mp4"
        video.write_bytes(b"m")
        completed = mock.Mock(stdout="1080x1920\n", stderr="")
        with mock.patch.object(archive_video.subprocess, "run", return_value=completed):
            self.assertEqual(archive_video.probe_dimensions(str(video)), (1080, 1920))

    def test_probe_dimensions_bounds_subprocess_timeout(self):
        video = self.tmp / "video.mp4"
        video.write_bytes(b"m")
        with mock.patch.object(
            archive_video.subprocess, "run", return_value=mock.Mock(stdout="1080x1920")
        ) as run_mock:
            archive_video.probe_dimensions(str(video))
            _, kwargs = run_mock.call_args
            self.assertLessEqual(kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
