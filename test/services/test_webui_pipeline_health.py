"""计划页自动化健康条：调度器、最近运行、发布连接只读可见。

背景：定时任务静默停了 11 天、发布从未成功过，而界面上没有任何地方能看出
来——健康状态只存在于 logs/ 目录和 launchctl 里。这个健康条把三件事摆到
计划页上：调度器是否在跑、该频道上次定时跑的结果、YouTube 是否已连接。

安全性质（重点）：健康条是只读的。它只查 launchctl 状态、读日志尾部、读
发布连接状态；绝不提交任务、绝不启停调度、绝不发布。用 AST 钉住这一点，
而不是靠"这次没触发"的运气。
"""

import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
LOGS_DIR = ROOT_DIR.parent / "logs"


def _main_source() -> str:
    return WEBUI_MAIN.read_text(encoding="utf-8")


def _function_body(source: str, name: str) -> str:
    tree = ast.parse(source)
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    return ast.get_source_segment(source, node) or ""


def _seed_channel(monkeypatch, tmp_path, name="healthch"):
    """把 plans 根隔离到临时目录并造一个频道，避免污染仓库里的真实计划。"""
    import plans

    plans_root = tmp_path / "plans"
    channel_dir = plans_root / name
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "topics_30day.txt").write_text(
        "Topic One\nTopic Two\n", encoding="utf-8"
    )
    monkeypatch.setattr(plans, "PLANS_DIR", str(plans_root))
    return name


def _plan_blob(app) -> str:
    parts = []
    for collection in ("markdown", "caption", "success", "warning", "error", "info"):
        try:
            elements = getattr(app, collection)
        except Exception:  # noqa: BLE001 - AppTest 版本差异时跳过该类元素
            continue
        parts.extend(str(e.value) for e in elements)
    return "\n".join(parts)


def test_health_strip_renders_on_plan_view(monkeypatch, tmp_path, switch_view):
    """计划页必须画出健康条，且不抛异常。"""
    _seed_channel(monkeypatch, tmp_path)
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
    app.session_state["ui_language"] = "en"
    switch_view(app, "plan")
    assert [str(e.value) for e in app.exception] == [], app.exception
    assert "Automation health" in _plan_blob(app), "计划页上没有健康条"


def test_health_strip_is_read_only():
    """健康条只能看不能动：函数体内不允许出现任何触发动作。"""
    body = _function_body(_main_source(), "_render_pipeline_health_strip")
    for forbidden in (
        "kickstart",
        "bootstrap",
        "bootout",
        "submit",
        "publish_video",
        "mark-posted",
        "check_output",
        "Popen",
    ):
        assert forbidden not in body, (
            f"健康条里出现了 {forbidden}；只读展示不得触发任何动作"
        )
    assert "_cached_youtube_channel_info" in body, "健康条没有读发布连接状态"
    assert "_pipeline_last_run" in body, "健康条没有读最近运行记录"


def test_scheduler_check_only_shells_out_on_macos():
    """调度器检查只在 macOS 上调 launchctl； elsewhere 报 unknown 而不是炸。"""
    body = _function_body(_main_source(), "_pipeline_schedule_counts")
    assert "launchctl" in body, "没有用 launchctl 查询调度器状态"
    assert "darwin" in body, "非 macOS 平台没有降级为 unknown 的保护"


def test_last_run_outcome_markers_are_parsed():
    """最近运行的成败按日志里的标记判断，而不是按文件存在。"""
    body = _function_body(_main_source(), "_parse_schedule_run_outcome")
    assert "SUCCESS on attempt" in body, "成功标记识别丢了"
    assert "attempts failed" in body, "失败标记识别丢了"


def test_last_run_success_shows_on_the_strip(monkeypatch, tmp_path, switch_view):
    """伪造一条成功的定时日志，健康条必须显示成功（用完即删，不留垃圾）。"""
    channel = _seed_channel(monkeypatch, tmp_path)
    fake_log = LOGS_DIR / f"daily_{channel}_20990101_000000.log"
    assert not fake_log.exists(), "假日志文件名撞上了真实文件，换一个再测"
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fake_log.write_text(
            "channel=test\ntopic=Topic One\n"
            "=== attempt 1/3 ===\n"
            "SUCCESS on attempt 1\n",
            encoding="utf-8",
        )
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.session_state["ui_language"] = "en"
        switch_view(app, "plan")
        assert [str(e.value) for e in app.exception] == [], app.exception
        blob = _plan_blob(app)
        assert "succeeded" in blob, f"成功的定时运行没有显示出来：{blob[:300]}"
    finally:
        fake_log.unlink(missing_ok=True)


def test_unconfigured_channel_shows_not_connected(monkeypatch, tmp_path, switch_view):
    """没连 YouTube 的频道必须明说没连，而不是留白让人猜。"""
    _seed_channel(monkeypatch, tmp_path)
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
    app.session_state["ui_language"] = "en"
    switch_view(app, "plan")
    assert [str(e.value) for e in app.exception] == [], app.exception
    assert "YouTube not connected" in _plan_blob(app)
