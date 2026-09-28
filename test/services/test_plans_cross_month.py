import io
import os

import pytest
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, REPO_ROOT)

plans = pytest.importorskip("plans", reason="requires the out-of-repo tools/ helpers (plans, archive_video), which live outside the git repository. They run in a local workspace that has that directory; everywhere else the module is skipped rather than failing collection.")  # noqa: E402


class TestPlansCrossMonth(unittest.TestCase):
    """archived videos must match the plan row that originally recorded them,
    even after the channel has moved on to a new month."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.channel = "test-channel"
        self.old_dir = os.path.join(self.tmp, self.channel, "2026-07")
        self.new_dir = os.path.join(self.tmp, self.channel, "2026-08")
        os.makedirs(os.path.join(self.old_dir, "videos"), exist_ok=True)
        os.makedirs(os.path.join(self.new_dir, "videos"), exist_ok=True)
        with open(
            os.path.join(self.old_dir, "monthly-plan.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                "| 1 | Old topic | ✓ [1-2026-07-01-old.mp4]"
                "(videos/1-2026-07-01-old.mp4) - posted: https://youtu.be/OLD |\n"
            )
        with open(
            os.path.join(self.new_dir, "monthly-plan.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                "| 1 | New topic | ✓ [1-2026-08-01-new.mp4]"
                "(videos/1-2026-08-01-new.mp4) |\n"
            )
        for path in (
            os.path.join(self.old_dir, "videos", "1-2026-07-01-old.mp4"),
            os.path.join(self.new_dir, "videos", "1-2026-08-01-new.mp4"),
        ):
            with open(path, "wb") as handle:
                handle.write(b"x")
        self.patcher = mock.patch.object(plans, "OUTPUTS_DIR", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp)

    def test_newest_month_is_current(self):
        self.assertEqual(
            os.path.basename(plans.current_plan_dir(self.channel)), "2026-08"
        )

    def test_old_video_matches_original_month_row(self):
        match = plans.find_plan_row_by_video(self.channel, "1-2026-07-01-old.mp4")
        self.assertIsNotNone(match)
        plan_dir, row = match
        self.assertEqual(os.path.basename(plan_dir), "2026-07")
        self.assertEqual(row["index"], 1)
        self.assertEqual(row["posted"], "https://youtu.be/OLD")

    def test_new_video_matches_newest_month_row(self):
        match = plans.find_plan_row_by_video(self.channel, "1-2026-08-01-new.mp4")
        self.assertIsNotNone(match)
        plan_dir, row = match
        self.assertEqual(os.path.basename(plan_dir), "2026-08")
        self.assertFalse(row["posted"])

    def test_missing_video_has_no_row(self):
        self.assertIsNone(
            plans.find_plan_row_by_video(self.channel, "nonsense.mp4")
        )

    def test_mark_posted_old_month_does_not_touch_current(self):
        self.assertTrue(
            plans.mark_posted(
                self.channel,
                1,
                "https://youtu.be/OLD2",
                plan_dir=self.old_dir,
            )
        )
        old_row = plans.find_plan_row_by_video(
            self.channel, "1-2026-07-01-old.mp4"
        )[1]
        self.assertEqual(old_row["posted"], "https://youtu.be/OLD2")
        new_row = plans.find_plan_row_by_video(
            self.channel, "1-2026-08-01-new.mp4"
        )[1]
        self.assertFalse(new_row["posted"])

    def test_mark_posted_defaults_to_current_month(self):
        self.assertTrue(plans.mark_posted(self.channel, 1, "https://youtu.be/NEW"))
        new_row = plans.find_plan_row_by_video(
            self.channel, "1-2026-08-01-new.mp4"
        )[1]
        self.assertEqual(new_row["posted"], "https://youtu.be/NEW")

    def test_build_video_row_index_covers_all_months_once(self):
        index = plans.build_video_row_index(self.channel)
        self.assertEqual(
            {name: os.path.basename(dir_) for name, (dir_, _) in index.items()},
            {
                "1-2026-07-01-old.mp4": "2026-07",
                "1-2026-08-01-new.mp4": "2026-08",
            },
        )
        # 未记录的视频不应出现在索引里
        self.assertNotIn("nonsense.mp4", index)


class TestPlansDuplicateTopics(unittest.TestCase):
    """recent_topic_slugs / first_pending_topic(skip_recent_days=...) must keep
    already-produced topics from being scheduled again in a new month."""

    @staticmethod
    def _days_ago(days):
        return (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.channel = "test-channel"
        self.plans_dir = os.path.join(self.tmp, "plans")
        now = datetime.now()
        this_month = now.strftime("%Y-%m")
        last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        self.old_dir = os.path.join(self.tmp, self.channel, last_month)
        self.new_dir = os.path.join(self.tmp, self.channel, this_month)
        os.makedirs(os.path.join(self.plans_dir, self.channel), exist_ok=True)
        os.makedirs(os.path.join(self.old_dir, "videos"), exist_ok=True)
        os.makedirs(os.path.join(self.new_dir, "videos"), exist_ok=True)
        self.old_video = "1-%s-old.mp4" % self._days_ago(7)
        self.patchers = [
            mock.patch.object(plans, "PLANS_DIR", self.plans_dir),
            mock.patch.object(plans, "OUTPUTS_DIR", self.tmp),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        shutil.rmtree(self.tmp)

    def _write_topics(self, topics):
        path = os.path.join(self.plans_dir, self.channel, "topics_30day.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(topics) + "\n")

    def _write_plan(self, plan_dir, rows):
        path = os.path.join(plan_dir, "monthly-plan.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("| # | Topic | Status |\n")
            handle.write("|---|-------|--------|\n")
            for index, topic, status in rows:
                handle.write("| %d | %s | %s |\n" % (index, topic, status))

    def _touch_video(self, plan_dir, name):
        with open(os.path.join(plan_dir, "videos", name), "wb") as handle:
            handle.write(b"x")

    def test_recent_topic_slugs_includes_old_posted_and_excludes_pending_current(self):
        self._write_plan(
            self.old_dir,
            [
                (
                    1,
                    "Old topic",
                    "✓ [%s](videos/%s) - posted: https://youtu.be/OLD"
                    % (self.old_video, self.old_video),
                )
            ],
        )
        self._write_plan(self.new_dir, [(1, "New topic", "pending")])
        self._touch_video(self.old_dir, self.old_video)
        recent = plans.recent_topic_slugs(self.channel)
        self.assertIn("old topic", recent)
        self.assertNotIn("new topic", recent)

    def test_first_pending_topic_backward_compatible_without_duplicate(self):
        self._write_plan(
            self.old_dir,
            [(1, "Old topic", "✓ [%s](videos/%s)" % (self.old_video, self.old_video))],
        )
        self._write_plan(
            self.new_dir,
            [(1, "Fresh topic A", "pending"), (2, "Fresh topic B", "pending")],
        )
        self._touch_video(self.old_dir, self.old_video)
        self._write_topics(["Fresh topic A", "Fresh topic B"])
        expected = (1, "Fresh topic A")
        self.assertEqual(plans.first_pending_topic(self.channel), expected)
        self.assertEqual(
            plans.first_pending_topic(self.channel, skip_recent_days=6000), expected
        )

    def test_first_pending_topic_skips_recent_duplicate(self):
        self._write_plan(
            self.old_dir,
            [(1, "Old topic", "✓ [%s](videos/%s)" % (self.old_video, self.old_video))],
        )
        self._write_plan(
            self.new_dir,
            [(1, "Old topic", "pending"), (2, "Fresh topic", "pending")],
        )
        self._touch_video(self.old_dir, self.old_video)
        self._write_topics(["Old topic", "Fresh topic"])
        self.assertEqual(
            plans.first_pending_topic(self.channel, skip_recent_days=6000),
            (2, "Fresh topic"),
        )

    def test_first_pending_topic_falls_back_when_all_pending_are_duplicates(self):
        self._write_plan(
            self.old_dir,
            [(1, "Old topic", "✓ [%s](videos/%s)" % (self.old_video, self.old_video))],
        )
        self._write_plan(self.new_dir, [(1, "Old topic", "pending")])
        self._touch_video(self.old_dir, self.old_video)
        self._write_topics(["Old topic"])
        self.assertEqual(
            plans.first_pending_topic(self.channel, skip_recent_days=6000),
            (1, "Old topic"),
        )

    def test_check_duplicates_cli_reports_duplicate(self):
        self._write_plan(
            self.old_dir,
            [(1, "Old topic", "✓ [%s](videos/%s)" % (self.old_video, self.old_video))],
        )
        self._write_plan(
            self.new_dir,
            [(1, "Old topic", "pending"), (2, "Fresh topic", "pending")],
        )
        self._touch_video(self.old_dir, self.old_video)
        self._write_topics(["Old topic", "Fresh topic"])
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = plans.main(["--check-duplicates", self.channel])
        self.assertEqual(code, 1)
        output = stream.getvalue()
        self.assertIn("Old topic", output)
        self.assertIn("last used", output)

    def test_check_duplicates_cli_clean(self):
        self._write_plan(self.new_dir, [(1, "Fresh topic", "pending")])
        self._write_topics(["Fresh topic"])
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = plans.main(["--check-duplicates", self.channel])
        self.assertEqual(code, 0)
        self.assertEqual(stream.getvalue().strip(), "no duplicate topics")


if __name__ == "__main__":
    unittest.main()