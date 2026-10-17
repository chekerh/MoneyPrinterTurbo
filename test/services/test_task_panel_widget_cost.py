"""任务表只在用户展开时才渲染。

任务面板每两秒刷新一次。旧实现把整张表塞在顶栏的 ``st.popover`` 里，于是每轮
都重建 12 行 × 每行五个操作控件 + 状态页签，上百个控件一秒重建 30 遍，而绝大
多数轮次用户根本没在看这张表。

现在表在主区域，由 ``task_manager_open`` 这个自有开关控制：关闭时一行都不产生。

曾经试过"弹层关闭时不渲染弹层内容"（靠 popover 的展开状态当闸门），**走不通**：

1. 内容为空时 Streamlit 会直接禁用这个 popover——任务管理器和每行的删除按钮同时
   变灰，删除功能整个消失；
2. 换 ``st.empty()`` 占位后按钮恢复，但 popover 的展开状态不写进
   ``session_state``，闸门在真实浏览器里永远读到 False。AppTest 里能过是因为测试
   自己把键设成了 True，与真实交互不是一回事。

所以闸门换成了自己控制的开关。这组用例盯的就是"关闭时真的什么都不渲染"。
"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import state as sm
from app.utils import utils

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"

TASK_ROWS = 12


def _seed_tasks(root: Path, count: int) -> None:
    for index in range(count):
        task_dir = root / f"task{index:02d}"
        task_dir.mkdir(parents=True)
        (task_dir / "final-1.mp4").write_bytes(b"x" * 512)


@pytest.fixture
def seeded_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    _seed_tasks(tasks_dir, TASK_ROWS)
    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_dir))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))
    return tasks_dir


def _app() -> AppTest:
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
    app.run()
    assert not app.exception, app.exception
    return app


def test_closed_task_manager_renders_no_task_rows(seeded_tasks):
    """
    关闭状态下不得出现任何任务行按钮。

    播放、打开目录、删除、重新生成这些按钮只可能来自任务表。它们出现就说明
    闸门没生效。
    """
    with patch.object(config, "try_save_config", return_value=True):
        app = _app()

    row_buttons = [
        b
        for b in app.button
        if str(b.key or "").startswith(
            ("play_task_", "open_task_", "restore_task_", "delete_task_")
        )
    ]
    assert row_buttons == [], (
        f"任务管理器关闭时仍然渲染了 {len(row_buttons)} 个任务行按钮："
        f"{[b.key for b in row_buttons][:5]}"
    )


def test_task_count_on_the_toggle_stays_live_while_closed(seeded_tasks):
    """
    关闭时计数仍要更新。

    闸门是为了省掉表格的重建，不是为了让用户看不到"还有几个任务在跑"——那正是
    他想不开表的原因。
    """
    with patch.object(config, "try_save_config", return_value=True):
        app = _app()
    toggles = [b for b in app.button if b.key == "task_manager_toggle"]
    assert len(toggles) == 1, "顶栏没有任务管理器开关"
    assert "Task Manager" in str(toggles[0].label)


def test_opening_the_task_manager_renders_the_rows(seeded_tasks):
    """闸门不能过头：展开之后必须真的画出任务行。"""
    with patch.object(config, "try_save_config", return_value=True):
        app = _app()
        app.session_state["task_manager_open"] = True
        app.run()
        assert not app.exception, app.exception

    row_buttons = [
        b
        for b in app.button
        if str(b.key or "").startswith(("play_task_", "open_task_"))
    ]
    assert row_buttons, "展开之后仍然没有任务行按钮"


def test_closing_it_again_stops_rendering(seeded_tasks):
    """来回切换必须干净，不能越开越多。"""
    with patch.object(config, "try_save_config", return_value=True):
        app = _app()
        app.session_state["task_manager_open"] = True
        app.run()
        opened = len(app.button)
        app.session_state["task_manager_open"] = False
        app.run()
        closed = len(app.button)
    assert closed < opened, f"关闭后控件数没有回落：{opened} -> {closed}"


def test_a_full_rerun_stays_responsive_with_the_panel_closed(seeded_tasks):
    """留出余量，别让这一轮悄悄退化到秒级。"""

    with patch.object(config, "try_save_config", return_value=True):
        app = _app()
        start = time.perf_counter()
        app.run()
        elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 9000, f"关闭状态下一次 rerun 耗时 {elapsed_ms:.0f}ms"
