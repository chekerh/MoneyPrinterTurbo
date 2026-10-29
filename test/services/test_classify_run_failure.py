"""``tools/classify_run_failure.py`` 的判定表。

这些字符串不是编的：每一条都取自 ``logs/2026-10/`` 里真实失败的那一行。重试
策略（``daily_youtube_short.sh``）按这里的结论决定"快速重试 / 消耗一次完整
尝试 / 停手"，判错一类就等于又回到盲重试。
"""

import importlib.util
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = APP_ROOT.parent
TOOL = REPO_ROOT / "tools" / "classify_run_failure.py"


def _load():
    spec = importlib.util.spec_from_file_location("classify_run_failure", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestClassifyRunFailure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def classify(self, text):
        edge = self.mod.classify(text)
        return edge.as_dict()

    def test_tool_exists(self):
        self.assertTrue(TOOL.is_file(), f"缺少重试判定脚本: {TOOL}")

    def test_dns_failure_is_a_quick_retry_not_a_wasted_attempt(self):
        """真实日志：stage=script, error=nodename nor servname provided。"""
        result = self.classify(
            '2026-10-11 08:00:31 | ERROR | "./cli.py:1601": run_cli - CLI task failed: '
            "task_id=892b3b83-66d7-44a6-b15b-692b90a675b5, stop_at=video, stage=script, "
            "error=nodename nor servname provided, or not known"
        )
        self.assertEqual(result["stage"], "script")
        self.assertEqual(result["kind"], "dns")
        self.assertEqual(result["retryable"], "yes")
        self.assertEqual(result["early"], "yes")

    def test_tts_stall_is_transient_and_retried_early(self):
        """结构化行只写 failed to synthesize audio，原因在上一行。"""
        result = self.classify(
            '2026-10-28 20:09:06 | ERROR | "./app/services/voice.py:873": azure_tts_v1 '
            "- failed, error: edge_tts stream stalled for 30s\n"
            '2026-10-28 20:09:06 | ERROR | "./cli.py:1685": run_cli - CLI task failed: '
            "task_id=3eb3b317-c94b-4c12-89dc-db2b7208c907, stop_at=video, stage=audio, "
            "error=failed to synthesize audio"
        )
        self.assertEqual(result["stage"], "audio")
        self.assertEqual(result["kind"], "transient")
        self.assertEqual(result["retryable"], "yes")
        self.assertEqual(result["early"], "yes")

    def test_publish_auth_failure_stops_retrying(self):
        """重试解决不了未授权：需要人去控制台。"""
        result = self.classify(
            '2026-10-19 08:51:08 | ERROR | "./app/services/youtube_upload.py:436": '
            "channel_info - failed to load YouTube channel info for 'history-mysteries'\n"
            "youtube is not connected for niche 'history-mysteries'"
        )
        self.assertEqual(result["kind"], "permanent")
        self.assertEqual(result["retryable"], "no")

    def test_api_disabled_stops_retrying(self):
        result = self.classify(
            '{"error": "<HttpError 403 ... returned \\"YouTube Data API v3 has not been '
            'used in project 314980975331 before or it is disabled.\\""}'
        )
        self.assertEqual(result["kind"], "permanent")
        self.assertEqual(result["retryable"], "no")

    def test_watchdog_is_not_treated_as_transient(self):
        """watchdog 杀掉不是瞬时故障：再跑一次大概率又跑满 MAX_RUNTIME。"""
        result = self.classify(
            "watchdog: exceeded 2700s, terminating\nattempt 3 failed\nall 3 attempts failed"
        )
        self.assertEqual(result["kind"], "watchdog")
        self.assertEqual(result["stage"], "watchdog")
        self.assertEqual(result["retryable"], "no")
        self.assertIn("watchdog", result["reason"])

    def test_late_unknown_failure_is_retryable_but_not_early(self):
        result = self.classify(
            '2026-10-12 16:20:00 | ERROR | "./cli.py:1543": run_cli - CLI task failed: '
            "task_id=00000000-0000-0000-0000-000000000000, stop_at=video, stage=video, "
            "error=ValueError: unexpected render state"
        )
        self.assertEqual(result["stage"], "video")
        self.assertEqual(result["kind"], "unknown")
        self.assertEqual(result["retryable"], "yes")
        self.assertEqual(result["early"], "no")

    def test_ansi_colour_codes_do_not_hide_the_failure(self):
        result = self.classify(
            "\x1b[31m2026-10-28 20:09:06\x1b[0m | \x1b[31mERROR\x1b[0m | "
            '"./cli.py:1685": run_cli - CLI task failed: task_id=x, stop_at=video, '
            "stage=script, error=nodename nor servname provided"
        )
        self.assertEqual(result["kind"], "dns")
        self.assertEqual(result["stage"], "script")

    def test_multiple_attempts_are_reported_in_order(self):
        text = (
            "=== attempt 1/3 ===\n"
            '"./cli.py:1601": run_cli - CLI task failed: task_id=a, stop_at=video, '
            "stage=script, error=nodename nor servname provided\n"
            "=== attempt 2/3 ===\n"
            '"./cli.py:1685": run_cli - CLI task failed: task_id=b, stop_at=video, '
            "stage=video, error=Connection reset by peer\n"
        )
        edges = self.mod.classify_edges(text)
        self.assertEqual([e.stage for e in edges], ["script", "video"])
        self.assertEqual([e.kind for e in edges], ["dns", "transient"])

    def test_runner_still_exits_zero_on_unparseable_input(self):
        """分类器挂了不能把定时任务带崩：永远返回 0。"""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write("nothing useful here\n")
            path = handle.name
        try:
            self.assertEqual(self.mod.main(["--json", "--file", path]), 0)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
