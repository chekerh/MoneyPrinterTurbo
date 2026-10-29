"""``tools/publish_backfill.py`` 的只读发现逻辑。

这个脚本的真实行为是**上传**，所以测试全部落在 sandbox 上：临时目录里搭一棵
``outputs/<channel>/<YYYY-MM>/videos/``，断言它挑出哪些成片、跳过哪些、以及在
没给 ``--channel`` / ``--limit`` 时拒绝上传（返回 2）。任何测试都不允许走到
``subprocess`` 真正调 ``publish_video.py``。
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

APP_ROOT = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = APP_ROOT.parent
TOOL = REPO_ROOT / "tools" / "publish_backfill.py"


def _load():
    name = "publish_backfill_under_test"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    # 脚本里是 ``from __future__ import annotations`` + dataclass，dataclass 需要
    # 从 sys.modules 反查模块命名空间来解析字符串注解；不注册就直接 AttributeError。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _video(channel_dir: Path, month: str, index: int, slug: str = "a-topic") -> Path:
    videos = channel_dir / month / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    path = videos / f"{index}-2026-10-{index:02d}-{slug}.mp4"
    path.write_bytes(b"not a real video")
    return path


def _plan(channel_dir: Path, month: str, rows) -> None:
    directory = channel_dir / month
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["# plan", "", "| # | topic | status |", "|---|---|---|"]
    for index, topic, status in rows:
        lines.append(f"| {index} | {topic} | {status} |")
    (directory / "monthly-plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _receipt(video: Path, *, success: bool, error: str = "") -> None:
    payload = {
        "result": {
            "youtube": {
                "success": success,
                "error": error,
                "url": "https://youtu.be/x" if success else "",
            }
        }
    }
    video.with_suffix(".publish.json").write_text(json.dumps(payload), encoding="utf-8")


class TestDiscover(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.channel = self.root / "outputs" / "money-finance"

    def tearDown(self):
        self._tmp.cleanup()

    def test_tool_exists(self):
        self.assertTrue(TOOL.is_file(), f"缺少回填脚本: {TOOL}")

    def test_no_outputs_directory_returns_empty(self):
        self.assertEqual(self.mod.discover(self.root), [])

    def test_unposted_video_with_no_receipt_is_a_candidate(self):
        video = _video(self.channel, "2026-10", 1)
        _plan(self.channel, "2026-10", [(1, "What inflation does", "pending")])

        found = self.mod.discover(self.root)

        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].needs_publish)
        self.assertEqual(found[0].receipt, "missing")
        self.assertEqual(found[0].topic, "What inflation does")
        self.assertEqual(found[0].video, video)

    def test_failed_receipt_is_still_a_candidate_and_keeps_the_reason(self):
        video = _video(self.channel, "2026-10", 2)
        _receipt(video, success=False, error="youtube is not connected for niche 'x'")

        found = self.mod.discover(self.root)

        self.assertTrue(found[0].needs_publish)
        self.assertEqual(found[0].receipt, "failed")
        self.assertIn("not connected", found[0].receipt_error)

    def test_successful_receipt_is_skipped(self):
        video = _video(self.channel, "2026-10", 3)
        _receipt(video, success=True)

        found = self.mod.discover(self.root)

        self.assertFalse(found[0].needs_publish)
        self.assertEqual(found[0].receipt, "ok")
        self.assertIn("receipt", found[0].skipped_reason)

    def test_plan_row_with_posted_url_is_skipped(self):
        _video(self.channel, "2026-10", 4)
        _plan(
            self.channel,
            "2026-10",
            [(4, "Compound interest", "posted: https://youtu.be/abc")],
        )

        found = self.mod.discover(self.root)

        self.assertFalse(found[0].needs_publish)
        self.assertEqual(found[0].posted_url, "https://youtu.be/abc")
        self.assertIn("posted", found[0].skipped_reason)

    def test_receipt_that_cannot_be_parsed_counts_as_failed(self):
        video = _video(self.channel, "2026-10", 5)
        video.with_suffix(".publish.json").write_text("{not json", encoding="utf-8")

        found = self.mod.discover(self.root)

        self.assertTrue(found[0].needs_publish)
        self.assertEqual(found[0].receipt, "failed")
        self.assertIn("unreadable", found[0].receipt_error)

    def test_non_video_and_unnamed_files_are_ignored(self):
        videos = self.channel / "2026-10" / "videos"
        videos.mkdir(parents=True)
        (videos / "notes.txt").write_text("x", encoding="utf-8")
        (videos / "no-index-2026-10-01-x.mp4").write_bytes(b"x")

        self.assertEqual(self.mod.discover(self.root), [])

    def test_underscore_directories_are_ignored(self):
        _video(self.root / "outputs" / "_scratch", "2026-10", 1)
        self.assertEqual(self.mod.discover(self.root), [])

    def test_channel_filter_only_returns_the_named_channel(self):
        _video(self.channel, "2026-10", 1)
        _video(self.root / "outputs" / "space-science", "2026-10", 1)

        found = self.mod.discover(self.root, ["money-finance"])

        self.assertEqual([c.channel for c in found], ["money-finance"])

    def test_posting_sidecar_supplies_the_real_title(self):
        video = _video(self.channel, "2026-10", 6)
        video.with_suffix(".posting.json").write_text(
            json.dumps({"title": "The real packaged title"}), encoding="utf-8"
        )

        found = self.mod.discover(self.root)

        self.assertEqual(found[0].title, "The real packaged title")

    def test_multiple_months_report_their_own_plan_directory(self):
        _video(self.channel, "2026-09", 1)
        _video(self.channel, "2026-10", 1)
        _plan(self.channel, "2026-09", [(1, "September topic", "pending")])
        _plan(self.channel, "2026-10", [(1, "October topic", "pending")])

        found = {c.month: c for c in self.mod.discover(self.root)}

        self.assertEqual(found["2026-09"].topic, "September topic")
        self.assertEqual(found["2026-10"].topic, "October topic")
        self.assertEqual(found["2026-09"].plan_dir.name, "2026-09")

    def test_app_root_layout_is_supported(self):
        """作品集根也可能是 app 根：``MoneyPrinterTurbo/outputs``。"""
        video = self.root / "MoneyPrinterTurbo" / "outputs" / "money-finance"
        _video(video, "2026-10", 1)

        found = self.mod.discover(self.root)

        self.assertEqual(len(found), 1)


class TestRenderTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_summary_counts_unposted_and_published(self):
        candidates = [
            self.mod.Candidate(
                channel="a", month="2026-10", index=1, date="2026-10-01",
                topic="t", title="t", video=Path("a.mp4"), plan_dir=Path("."),
            ),
            self.mod.Candidate(
                channel="a", month="2026-10", index=2, date="2026-10-02",
                topic="t", title="t", video=Path("b.mp4"), plan_dir=Path("."),
                needs_publish=False,
            ),
        ]

        table = self.mod.render_table(candidates)

        self.assertIn("1 unposted of 2 archived", table)
        self.assertIn("(skip)", table)


class TestApplyGuards(unittest.TestCase):
    """``--apply`` 是公开且不可撤销的动作，三个条件缺一不可。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _video(self.root / "outputs" / "money-finance", "2026-10", 1)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.mod.main(argv + ["--portfolio-root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_apply_without_channel_exits_2(self):
        code, _, err = self._run(["--apply", "--limit", "1"])
        self.assertEqual(code, 2)
        self.assertIn("--channel", err)

    def test_apply_without_limit_exits_2(self):
        code, _, err = self._run(["--apply", "--channel", "money-finance"])
        self.assertEqual(code, 2)
        self.assertIn("--limit", err)

    def test_apply_with_zero_limit_exits_2(self):
        code, _, _ = self._run(["--apply", "--channel", "money-finance", "--limit", "0"])
        self.assertEqual(code, 2)

    def test_dry_run_never_uploads(self):
        with mock.patch.object(self.mod.subprocess, "run") as run:
            code, out, _ = self._run(["--channel", "money-finance"])
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("1 unposted of 1 archived", out)

    def test_apply_marks_the_plan_after_a_successful_upload(self):
        upload = {"success": True, "result": {"youtube": {"url": "https://youtu.be/new"}}}
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "publish_video.py" in argv[1]:
                return mock.Mock(
                    returncode=0, stdout=json.dumps(upload), stderr=""
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            code, out, _ = self._run(
                ["--apply", "--channel", "money-finance", "--limit", "1"]
            )

        self.assertEqual(code, 0)
        self.assertIn("published: https://youtu.be/new", out)
        mark = [c for c in calls if "mark-posted" in c]
        self.assertTrue(mark, "成功的上传必须回写计划行")
        self.assertIn("--plan-dir", mark[0])
        self.assertTrue(
            mark[0][-1].endswith("2026-10"),
            f"--plan-dir 必须指回成片所属月份，实际是 {mark[0][-1]}",
        )

    def test_apply_stops_a_channel_that_needs_a_human(self):
        """同一个 API 未启用/未登录，后面的片子重试也是同样的错。"""
        failed = {
            "success": False,
            "error": "youtube: youtube is not connected for niche 'money-finance'",
        }
        _video(self.root / "outputs" / "money-finance", "2026-10", 2)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout=json.dumps(failed), stderr="")

        with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            code, out, _ = self._run(
                ["--apply", "--channel", "money-finance", "--limit", "5"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1, "第一个就判定需要人工后不应再试第二个")
        self.assertIn("needs a human", out)


if __name__ == "__main__":
    unittest.main()
