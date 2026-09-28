"""赛道品牌与产出账本。

这里守住三件事：

1. **品牌句必须逐字一致。** 频道识别度来自"每条都一样"，一旦模型自己改写
   就失效了，所以要有测试盯着。
2. **钩子必须在品牌句之前。** 反过来就成了开场套话，正好是 humanizer 里
   点名的 staged run-up。
3. **账本不能跟着清理一起消失。** 上一轮的教训：产出统计只存在会被删掉的
   地方，于是"历史赛道做了几条"再也答不上来。
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.services.niche_brand import (
    DEFAULT_NICHE,
    NICHES,
    known_channels,
    resolve_niche,
    script_directives,
)
from app.services.niche_stats import NicheLedger


class TestNicheProfiles(unittest.TestCase):
    def test_the_four_niches_are_configured(self):
        self.assertEqual(
            set(known_channels()),
            {"history-mysteries", "mind-psychology", "money-finance", "space-science"},
        )

    def test_every_niche_has_a_brand_line_and_a_display_name(self):
        for channel, profile in NICHES.items():
            with self.subTest(channel=channel):
                self.assertTrue(profile.brand_line.strip(), "品牌句不能为空")
                self.assertTrue(profile.display_name.strip())
                self.assertTrue(profile.teaser_shapes)

    def test_brand_lines_share_one_skeleton(self):
        """
        四个品牌句必须同一副骨架：都以 "every day we" 开头。

        这是识别度的一部分。如果一个赛道改成 "we explore..."，观众听到的
        就是四个不同的频道，而不是一个频道的四个选题。
        """
        for channel, profile in NICHES.items():
            with self.subTest(channel=channel):
                self.assertTrue(
                    profile.brand_line.startswith("every day we"),
                    f"{channel} 的品牌句偏离了统一骨架：{profile.brand_line!r}",
                )

    def test_brand_lines_are_distinct_from_each_other(self):
        """骨架一致不代表内容雷同。"""
        lines = [p.brand_line for p in NICHES.values()]
        self.assertEqual(len(set(lines)), len(lines), "有赛道的品牌句重复了")

    def test_history_brand_line_matches_the_requested_wording(self):
        """
        历史赛道的品牌句是用户明确给定的措辞，不能被悄悄改写。

        允许的开头是频道名（调用方负责），从句部分必须逐字一致。
        """
        self.assertEqual(
            NICHES["history-mysteries"].brand_line,
            "every day we look at the kind of story you will end up retelling "
            "at dinner",
        )

    def test_unknown_channel_falls_back_without_raising(self):
        """频道名可能来自用户手输的 ln.json，拼错不该让生成失败。"""
        profile = resolve_niche("histoy-mysteries")
        self.assertEqual(profile.channel, DEFAULT_NICHE)
        self.assertEqual(profile.brand_line, "")

    def test_channel_matching_tolerates_spacing_and_case(self):
        for variant in ("History-Mysteries", "history_mysteries", " History Mysteries "):
            with self.subTest(variant=variant):
                self.assertEqual(
                    resolve_niche(variant).channel, "history-mysteries"
                )


class TestScriptDirectives(unittest.TestCase):
    def test_known_niche_produces_directives(self):
        text = script_directives("history-mysteries")
        self.assertIn("Channel voice and structure", text)
        self.assertIn("Hook", text)
        self.assertIn("Brand line", text)
        self.assertIn(NICHES["history-mysteries"].brand_line, text)

    def test_directives_put_the_hook_before_the_brand_line(self):
        """
        钩子必须排在品牌句前面。

        反过来的话，观众第一秒听到的是频道名，钩子作用就没了——这正是
        humanizer 点名的 staged run-up。
        """
        text = script_directives("history-mysteries")
        self.assertLess(
            text.index("1. **Hook"),
            text.index("2. **Brand line"),
        )

    def test_directives_forbid_a_greeting_before_the_hook(self):
        """模型最容易在这里偷懒，先写 welcome 再进正文。"""
        text = script_directives("money-finance")
        self.assertIn("No greeting", text)

    def test_directives_carry_the_niche_length_target(self):
        profile = NICHES["space-science"]
        text = script_directives("space-science")
        self.assertIn(str(profile.target_words), text)
        self.assertIn("go longer", text, "内容需要时应该允许更长")

    def test_directives_include_the_niche_specific_avoid_list(self):
        text = script_directives("money-finance")
        self.assertIn("Avoid in Money and Finance", text)
        self.assertIn("market predictions", text)

    def test_general_channel_yields_no_directives(self):
        """通用画像没有品牌句可给，调用方应当能无条件拼接空串。"""
        self.assertEqual(script_directives(""), "")
        self.assertEqual(script_directives("random-thing"), "")


class TestNicheLedgerSurvivesCleanup(unittest.TestCase):
    """账本必须写在清理流程够不到的地方。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "niche_stats.json"
        self.ledger = NicheLedger(path=self.path)

    def test_records_and_counts_per_channel(self):
        self.ledger.record("history-mysteries", "Silk Road", task_id="t1")
        self.ledger.record("history-mysteries", "Rope bridges", task_id="t2")
        self.ledger.record("money-finance", "Inflation", task_id="t3")

        self.assertEqual(self.ledger.video_count("history-mysteries"), 2)
        self.assertEqual(self.ledger.video_count("money-finance"), 1)
        self.assertEqual(self.ledger.total_videos(), 3)

    def test_multi_video_runs_count_every_video(self):
        """一次任务出 3 条就要记 3 条，否则统计会系统性偏低。"""
        self.ledger.record("space-science", "Venus", videos=3)
        self.assertEqual(self.ledger.video_count("space-science"), 3)

    def test_repeated_topic_is_counted_but_listed_once(self):
        self.ledger.record("mind-psychology", "Loss aversion", videos=1)
        self.ledger.record("mind-psychology", "Loss aversion", videos=1)
        summary = self.ledger.summaries()["mind-psychology"]
        self.assertEqual(summary.videos, 2)
        self.assertEqual(summary.unique_topics, 1)

    def test_data_is_readable_from_a_fresh_ledger_instance(self):
        self.ledger.record("history-mysteries", "Silk Road")
        reopened = NicheLedger(path=self.path)
        self.assertEqual(reopened.video_count("history-mysteries"), 1)

    def test_ledger_path_is_not_inside_the_scratch_dirs(self):
        """
        账本绝不能落在 tasks/ 或 cache_videos/ 里。

        那些现在是内存盘，进程退出就没了——放进去就等于回到老问题。
        """
        from app.services.niche_stats import ledger_path

        resolved = ledger_path()
        self.assertFalse(
            any(
                part in resolved.parts
                for part in ("tasks", "cache_videos", "temp")
            ),
            f"账本路径落在会被清理的目录里：{resolved}",
        )

    def test_corrupt_ledger_raises_instead_of_silently_overwriting(self):
        """
        账本坏了要报错，不能默默用空账本覆盖——那会把仅存的历史抹掉。
        """
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(Exception):
            self.ledger.summaries()

    def test_unrecognised_structure_is_preserved_on_disk(self):
        self.path.write_text('{"unexpected": 1}', encoding="utf-8")
        with self.assertRaises(Exception):
            self.ledger.record("history-mysteries", "Silk Road")
        # 原文件必须还在
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {"unexpected": 1},
        )

    def test_seed_backfills_history_without_inventing_videos(self):
        """补录历史时可以给条数，默认不虚增。"""
        self.ledger.seed("history-mysteries", ["Silk Road", "Rope bridges"])
        summary = self.ledger.summaries()["history-mysteries"]
        self.assertEqual(summary.videos, 0)
        self.assertEqual(summary.unique_topics, 2)

    def test_unclassified_channel_is_still_recorded(self):
        """没有赛道归属的产出也要计数，不能悄悄丢掉。"""
        self.ledger.record("", "some topic")
        self.assertEqual(self.ledger.video_count("unclassified"), 1)


if __name__ == "__main__":
    unittest.main()


class TestChannelResolutionFromTaskDirectory(unittest.TestCase):
    """
    赛道必须能从任务目录里读出来，否则整个品牌句功能形同虚设。

    这里曾经有个真 bug：解析函数找的是 ``ln.json``，而磁盘上实际的文件叫
    ``plan_meta.json``。读不到就静默退回通用写法——界面不报错、功能不生效，
    只是每条视频都没有品牌句。非常难发现。
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.task_id = "task-x"
        self.task_dir = self.base / self.task_id
        self.task_dir.mkdir()

    def _params(self, channel: str = ""):
        from app.models.schema import VideoParams

        return VideoParams(video_subject="t", channel=channel)

    def test_reads_the_channel_from_plan_meta_json(self):
        (self.task_dir / "plan_meta.json").write_text(
            json.dumps({"channel": "history-mysteries", "topic": "Silk Road"}),
            encoding="utf-8",
        )
        from unittest.mock import patch

        from app.services.task import resolve_task_channel

        with patch("app.utils.utils.task_dir", return_value=str(self.task_dir)):
            self.assertEqual(
                resolve_task_channel(self.task_id, self._params()),
                "history-mysteries",
            )

    def test_explicit_channel_wins_over_the_directory(self):
        (self.task_dir / "plan_meta.json").write_text(
            json.dumps({"channel": "history-mysteries"}), encoding="utf-8"
        )
        from unittest.mock import patch

        from app.services.task import resolve_task_channel

        with patch("app.utils.utils.task_dir", return_value=str(self.task_dir)):
            self.assertEqual(
                resolve_task_channel(self.task_id, self._params("space-science")),
                "space-science",
            )

    def test_missing_file_yields_an_empty_channel_without_raising(self):
        from unittest.mock import patch

        from app.services.task import resolve_task_channel

        with patch("app.utils.utils.task_dir", return_value=str(self.task_dir)):
            self.assertEqual(resolve_task_channel(self.task_id, self._params()), "")

    def test_corrupt_plan_meta_does_not_break_generation(self):
        """
        plan_meta.json 坏掉时必须安静退回通用写法，不能让整条视频生成失败。
        """
        (self.task_dir / "plan_meta.json").write_text(
            '{"channel": "history-mysteries"}\nnonsense second line',
            encoding="utf-8",
        )
        from unittest.mock import patch

        from app.services.task import resolve_task_channel

        with patch("app.utils.utils.task_dir", return_value=str(self.task_dir)):
            self.assertEqual(resolve_task_channel(self.task_id, self._params()), "")

    def test_the_filename_constant_matches_what_the_ui_writes(self):
        """
        写入方和读取方必须用同一个常量。

        之前两边各写各的字符串（``ln.json`` vs ``plan_meta.json``），
        才出现了"读不到但也不报错"的静默失效。
        """
        from app.services.plan_meta import PLAN_META_FILENAME

        main_source = (
            Path(__file__).resolve().parents[2] / "webui" / "Main.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            f'"{PLAN_META_FILENAME}"' if PLAN_META_FILENAME != "plan_meta.json" else '"ln.json"',
            main_source,
            "webui 侧不应再硬编码另一个文件名",
        )
