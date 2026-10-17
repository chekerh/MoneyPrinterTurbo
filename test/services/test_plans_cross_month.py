import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# 用 pyproject.toml 定位仓库根，而不是数 dirname()：原写法多套了一层，
# 在本机恰好指到"仓库的上一级"。按标记文件找根不会数错层数。
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
while REPO_ROOT != os.path.dirname(REPO_ROOT) and not os.path.isfile(
    os.path.join(REPO_ROOT, "pyproject.toml")
):
    REPO_ROOT = os.path.dirname(REPO_ROOT)
sys.path.insert(0, REPO_ROOT)
# tools/ 也必须在 sys.path 上，这样下面的 ``import plans`` 拿到的是**生产代码用的
# 同一个模块对象**。生产侧（webui/Main.py、tools/archive_video.py）一律用
# ``import plans``；若这里改用 ``from tools import plans``，同一份文件会被加载成
# 两个模块，monkeypatch 只对其中一个生效，render_plan 去读另一个模块的 PLANS_DIR
# ——症状是"归档看不到自己刚写的数据"，而不是任何报错。
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import archive_video  # noqa: E402
import plans  # noqa: E402


@contextmanager
def _frozen_period(case, period):
    """把 plans 模块认定的"当前月份"钉死。

    跨月行为必须与运行日期无关。早先的用例直接写死 "2026-08" 断言
    ``current_plan_dir`` 返回它，因此在八月能过、九月也能过（因为旧实现返回的是
    磁盘上最新的月份），却完全测不到"跨月"这件事本身。
    """
    with mock.patch.object(plans, "_current_period", return_value=period):
        yield case


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

    def test_current_plan_dir_is_the_current_month_not_the_newest_on_disk(self):
        """
        必须按"今天是哪个月"取，而不是磁盘上最新的那个月份。

        夹具里最新的月份是 2026-08。把当前月份钉在 2026-08 时两者一致，因此这个
        用例单独跑**抓不到**回归；真正的风险出现在跨月：命令行用
        ``$(date +%Y-%m)`` 建当月目录，WebUI 却继续往上一个月的目录写，同一批
        视频被拆进两个月的计划里。见 test_month_rollover_*。
        """
        with _frozen_period(self, "2026-09"):
            self.assertEqual(
                os.path.basename(plans.current_plan_dir(self.channel)), "2026-09"
            )

    def test_month_rollover_writes_to_the_new_month_not_the_last_one(self):
        """
        跨月回归：磁盘上只剩 2026-08，而今天是 2026-09。

        旧实现返回"最新的已有月份"，于是 9 月生成的新片会被归档进 8 月的计划，
        与命令行流水线分家。断言必须看**新月份目录被建出来**。
        """
        with _frozen_period(self, "2026-09"):
            plan_dir = plans.current_plan_dir(self.channel)
        self.assertEqual(os.path.basename(plan_dir), "2026-09")
        self.assertTrue(os.path.isdir(plan_dir))

    def test_mark_posted_can_target_the_new_month_explicitly(self):
        """
        归档按计划目录写入时，可以显式指定目录，不受"当前月"影响。

        这条钉住的是接口契约而不是日期：库里的历史月份必须仍然可写，否则补录
        往期成片就无处可去。
        """
        self.assertTrue(
            plans.mark_posted(
                self.channel,
                1,
                "https://youtu.be/NEW",
                plan_dir=self.old_dir,
            )
        )
        old_row = plans.find_plan_row_by_video(
            self.channel, "1-2026-07-01-old.mp4"
        )[1]
        self.assertEqual(old_row["posted"], "https://youtu.be/NEW")

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

    def test_recorded_topic_is_never_offered_again(self):
        """
        已经录过的主题不会再被给出，包括上个月录的。

        这条**改写了旧的要求**。旧行为是"如果剩下的都算近期重复，就退回第一条待办
        继续做"，于是每月 1 号都会把上月做完的 #1 再渲一遍——两个多小时、真实花钱，
        然后归档侧因为跨月查到已录制而把它丢掉。

        现在的要求是：录过就是录过，30 条用完就返回 None，让界面提示去写新主题，
        而不是悄悄重烧一次渲染换来"有进展"的假象。
        ``skip_recent_days`` 仍然只在**未录制**的主题之间排序，它不复活已录制的主题。
        """
        self._write_plan(
            self.old_dir,
            [(1, "Old topic", "✓ [%s](videos/%s)" % (self.old_video, self.old_video))],
        )
        self._write_plan(self.new_dir, [(1, "Old topic", "pending")])
        self._touch_video(self.old_dir, self.old_video)
        self._write_topics(["Old topic"])

        self.assertIsNone(
            plans.first_pending_topic(self.channel, skip_recent_days=6000),
            "已录制的主题被重新给出了；这会重烧一次完整渲染然后被归档丢弃",
        )

    def test_new_topic_after_a_recorded_one_is_still_offered(self):
        """
        近期重复的"退回"逻辑仍然有效，只是只作用于未录制的主题。

        少了这条，修复就会顺手把 skip_recent_days 这个"别太早重复"的启发也一起
        废掉：两个都在最近做过的未录制主题，应该回退到第一条。
        """
        self._write_plan(
            self.old_dir,
            [(1, "Used up", "✓ [%s](videos/%s)" % (self.old_video, self.old_video))],
        )
        self._write_plan(
            self.new_dir,
            [
                (1, "Used up", "pending"),
                (2, "Fresh A", "pending"),
                (3, "Fresh B", "pending"),
            ],
        )
        self._touch_video(self.old_dir, self.old_video)
        self._write_topics(["Used up", "Fresh A", "Fresh B"])

        # "Used up" 已录制被跳过；Fresh A / Fresh B 未录制，按顺序给出第一个。
        self.assertEqual(
            plans.first_pending_topic(self.channel, skip_recent_days=6000),
            (2, "Fresh A"),
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

class TestTopicCursorSurvivesMonthRollover(unittest.TestCase):
    """
    跨月时"下一个主题"不能重置。

    ``topics_30day.txt`` 是一份 30 条的滚动列表，不是每月一份，所以 10 月的 #1
    和 9 月的 #1 是同一条主题。早先 ``first_pending_topic`` 只读当月的计划文件，
    于是每月 1 号新月计划全 pending，按钮重新给出 #1；渲染跑完（两个多小时、
    真金白银）之后，��归档侧的跨月查重又认为 #1 已做过而把它丢掉。按钮从此再也不会
    前进，整个月每天都白烧一次渲染。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.topics = [f"Topic {i}" for i in range(1, 31)]
        channel_dir = os.path.join(self._tmp, "plans", "rollch")
        os.makedirs(channel_dir)
        with open(
            os.path.join(channel_dir, "topics_30day.txt"), "w", encoding="utf-8"
        ) as handle:
            handle.write("\n".join(self.topics))
        self.patchers = [
            mock.patch.object(plans, "PLANS_DIR", os.path.join(self._tmp, "plans")),
            mock.patch.object(plans, "OUTPUTS_DIR", os.path.join(self._tmp, "outputs")),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        shutil.rmtree(self._tmp)

    def _write_plan(self, out_dir):
        Path(out_dir, "monthly-plan.md").write_text(
            archive_video.render_plan("rollch", self.topics, Path(out_dir)),
            encoding="utf-8",
        )

    def _archive_through(self, plan_dir: Path, last_index: int, period: str):
        videos = plan_dir / "videos"
        videos.mkdir(parents=True, exist_ok=True)
        for i in range(1, last_index + 1):
            name = f"{i:02d}-{period}-topic-{i}"
            (videos / f"{name}.mp4").write_bytes(b"x")
            (videos / f"{name}.txt").write_text(
                f"task_id: t{i}\ntopic: Topic {i}\nrecorded: {period}\n"
                f"video: videos/{name}.mp4\n",
                encoding="utf-8",
            )
        self._write_plan(plan_dir)

    def test_new_month_does_not_reoffer_topic_one(self):
        """10 月的第一条待办必须是 11，而不是 1。"""
        with _frozen_period(None, "2026-09"):
            september = Path(plans.current_plan_dir("rollch"))
            self._archive_through(september, 10, "2026-09")
            self.assertEqual(plans.first_pending_topic("rollch"), (11, "Topic 11"))

        with _frozen_period(None, "2026-10"):
            october = Path(plans.current_plan_dir("rollch"))
            october.mkdir(parents=True, exist_ok=True)
            (october / "videos").mkdir(parents=True, exist_ok=True)
            self._write_plan(october)

            pending = plans.first_pending_topic("rollch")
            self.assertEqual(
                pending,
                (11, "Topic 11"),
                "新月重新给出了 #1；渲染会跑完再被归档侧丢弃，每天白烧一次",
            )

    def test_new_month_plan_carries_previous_progress(self):
        """
        新月的计划文件必须体现上月已完成的进度。

        否则界面上的计数退回 0/30，计划表也整张空掉，用户会以为十月从头开始，
        而成片其实好好地躺在上个月的目录里。
        """
        with _frozen_period(None, "2026-09"):
            september = Path(plans.current_plan_dir("rollch"))
            self._archive_through(september, 10, "2026-09")

        with _frozen_period(None, "2026-10"):
            october = Path(plans.current_plan_dir("rollch"))
            october.mkdir(parents=True, exist_ok=True)
            (october / "videos").mkdir(parents=True, exist_ok=True)
            self._write_plan(october)
            text = (october / "monthly-plan.md").read_text(encoding="utf-8")

        self.assertIn("10/30 topics recorded", text)
        self.assertIn("../2026-09/videos/", text)

    def test_progress_keeps_accumulating_across_three_months(self):
        """连续三个月都应单调推进，永不重置。"""
        seen = []
        for month, last in (("2026-09", 10), ("2026-10", 14), ("2026-11", 20)):
            with _frozen_period(None, month):
                plan_dir = Path(plans.current_plan_dir("rollch"))
                self._archive_through(plan_dir, last, month)
                seen.append(plans.first_pending_topic("rollch")[0])
        self.assertEqual(seen, [11, 15, 21])
