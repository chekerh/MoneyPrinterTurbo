"""草稿预览必须跟着设置走，而且不能挡住正常生成。

界面上的预览最容易出的错是"看着像那么回事，实际不跟手"：改了字号位置，预览纹丝不动，
用户就以为设置没生效。这里用真实的 AppTest 跑一遍整页，验证联动与降级。
"""

import io
import os
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import plans
from app.config import config
from app.services import state as sm
from app.utils import utils

WEBUI_MAIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "webui",
    "Main.py",
)


def _preview_png(app: AppTest) -> bytes | None:
    """取出预览 PNG。

    预览把结果缓存在 session_state 里，测试就走这个缓存：AppTest 并不把 st.image
    暴露成可断言的元素，绕道去猜元素类型只会让测试跟着 Streamlit 的内部结构晃。
    """
    try:
        return app.session_state["draft_preview_cache"][1]
    except (KeyError, IndexError, TypeError):
        return None


def _ink(app: AppTest):
    """预览图的墨迹包围盒；没有预览时返回 None。"""
    raw = _preview_png(app)
    if raw is None:
        return None
    arr = np.array(Image.open(io.BytesIO(raw)).convert("RGB")).astype(int)
    mask = np.abs(arr - np.array((24, 24, 27))).sum(axis=2) > 40
    if not mask.any():
        return None
    ys, _xs = np.where(mask)
    return {"top": int(ys.min()), "bottom": int(ys.max()), "pixels": int(mask.sum())}


@pytest.fixture
def plan_app(tmp_path, monkeypatch):
    """跑起来、并且有一个待办主题的计划面板。"""
    monkeypatch.setattr(utils, "task_dir", lambda: str(tmp_path / "storage" / "tasks"))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))
    monkeypatch.setattr(
        plans, "list_channels", lambda: ["effch"]
    )
    monkeypatch.setattr(
        plans, "first_pending_topic", lambda ch: (1, "Why do Roman roads still work?")
    )
    monkeypatch.setattr(plans, "read_automation_env", lambda ch: {})
    monkeypatch.setattr(plans, "read_file_text", lambda ch, *a: "")
    monkeypatch.setattr(
        plans, "load_monthly_plan", lambda ch: [{"index": 1, "topic": "t", "recorded": True, "posted": ""}]
    )
    monkeypatch.setattr(plans, "find_video_path", lambda ch, i: None)
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(WEBUI_MAIN, default_timeout=90)
        # 生成视图负责写 form_params_snapshot；草稿预览在计划视图里读它，
        # 所以这里先跑默认视图把快照准备好，再切到计划视图。
        app.run()
        assert not app.exception, app.exception
        app.session_state["nav_view"] = "plan"
        app.run()
        assert not app.exception, app.exception
        yield app


def test_preview_appears_next_to_a_pending_topic(plan_app):
    """
    有待办主题时必须给出预览。

    这是"改设置前先看一眼"的前提：没有预览，用户就只能先花钱生成再发现问题。
    """
    ink = _ink(plan_app)
    assert ink is not None, "计划面板没有渲染草稿预览"
    assert ink["pixels"] > 500, "预览是空白的，没有画出字幕"


def test_no_preview_without_a_topic(monkeypatch, tmp_path):
    """没有待办主题时不硬凑预览。"""
    monkeypatch.setattr(utils, "task_dir", lambda: str(tmp_path / "storage" / "tasks"))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))
    monkeypatch.setattr(plans, "list_channels", lambda: ["effch"])
    monkeypatch.setattr(plans, "first_pending_topic", lambda ch: None)
    monkeypatch.setattr(plans, "read_automation_env", lambda ch: {})
    monkeypatch.setattr(plans, "read_file_text", lambda ch, *a: "")
    monkeypatch.setattr(plans, "load_monthly_plan", lambda ch: [])
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(WEBUI_MAIN, default_timeout=90)
        app.run()
        app.session_state["nav_view"] = "plan"
        app.run()
    assert not app.exception, app.exception
    assert _ink(app) is None, "没有主题时不该显示字幕预览"


def test_preview_works_without_visiting_generate_first(monkeypatch, tmp_path):
    """没进过生成页就直接点计划页，预览照样要出来。

    ``form_params_snapshot`` 只有生成页会写，而左侧导航把工具台排在最前面，
    于是"一次都没进过生成页"是完全正常的路径。曾经这里直接显示"请先去生成页设置
    字幕"，把界面自己的顺序问题说成了用户的操作失误——用户没做错任何事。
    """
    monkeypatch.setattr(utils, "task_dir", lambda: str(tmp_path / "storage" / "tasks"))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))
    monkeypatch.setattr(plans, "list_channels", lambda: ["effch"])
    monkeypatch.setattr(
        plans, "first_pending_topic", lambda ch: (1, "Why do Roman roads still work?")
    )
    monkeypatch.setattr(plans, "read_automation_env", lambda ch: {})
    monkeypatch.setattr(plans, "read_file_text", lambda ch, *a: "")
    monkeypatch.setattr(plans, "load_monthly_plan", lambda ch: [])
    monkeypatch.setattr(plans, "find_video_path", lambda ch, i: None)
    saved = dict(config.ui)
    try:
        config.ui["subtitle_enabled"] = False
        with patch.object(config, "try_save_config", return_value=True):
            app = AppTest.from_file(WEBUI_MAIN, default_timeout=90)
            # 首屏固定成 generate（conftest 的默认），所以显式改成计划页并且
            # 不先跑生成页：快照必须不存在。
            app.session_state["nav_view"] = "plan"
            app.run()

        assert not app.exception, app.exception
        state = app.session_state._state._old_state
        assert "form_params_snapshot" not in state, "前提不成立：快照不该存在"
        ink = _ink(app)
        assert ink is not None, "没进过生成页时预览没有出现"
        assert ink["pixels"] > 500, "预览是空白的"

        # 退化参数必须走 config.ui，而不是 schema 里写死的默认值
        # （subtitle_enabled 的默认值是 True）。否则预览会替用户宣称字幕开着。
        captions = [str(c.value) for c in app.get("caption")]
        assert any(
            "Subtitles are off" in c for c in captions
        ), f"退化预览没有如实说明字幕已关闭: {captions}"
        assert ink["pixels"] > 500

        # 再验证字号也真的读自 config.ui：把保存值调到很大，墨迹必须跟着变大。
        config.ui["font_size"] = 140
        app.run()
        large = _ink(app)
        assert large is not None
        assert large["pixels"] > ink["pixels"], (
            f"退化预览没有采用 config.ui 里的 font_size=140: "
            f"{ink['pixels']} -> {large['pixels']}"
        )
    finally:
        config.ui.clear()
        config.ui.update(saved)


def test_disclosure_is_absent_when_subtitles_are_enabled(monkeypatch, tmp_path):
    """字幕开着时不该显示"字幕已关闭"——那句话会变成谎报。"""
    monkeypatch.setattr(utils, "task_dir", lambda: str(tmp_path / "storage" / "tasks"))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))
    monkeypatch.setattr(plans, "list_channels", lambda: ["effch"])
    monkeypatch.setattr(
        plans, "first_pending_topic", lambda ch: (1, "Why do Roman roads still work?")
    )
    monkeypatch.setattr(plans, "read_automation_env", lambda ch: {})
    monkeypatch.setattr(plans, "read_file_text", lambda ch, *a: "")
    monkeypatch.setattr(plans, "load_monthly_plan", lambda ch: [])
    monkeypatch.setattr(plans, "find_video_path", lambda ch, i: None)
    saved = dict(config.ui)
    try:
        config.ui["subtitle_enabled"] = True
        with patch.object(config, "try_save_config", return_value=True):
            app = AppTest.from_file(WEBUI_MAIN, default_timeout=90)
            app.session_state["nav_view"] = "plan"
            app.run()
        assert not app.exception, app.exception
        captions = [str(c.value) for c in app.get("caption")]
        assert not any("Subtitles are off" in c for c in captions), (
            "字幕已开启时仍然显示了关闭提示"
        )
    finally:
        config.ui.clear()
        config.ui.update(saved)


def test_preview_follows_the_subtitle_position(plan_app, switch_view):
    """
    改位置，预览必须跟着动。

    位置错了但字幕照常出现，肉眼扫一眼未必发现——所以这里比对的是像素位置，不是
    "函数有没有被调用"。
    """
    before = _ink(plan_app)
    assert before is not None

    from webui.Main import localized_widget_key  # noqa: PLC0415

    # 字幕设置控件住在生成视图上，所以先切回去改，再切回计划视图看预览。
    switch_view(plan_app, "generate")
    plan_app.session_state["subtitle_enabled_checkbox"] = True
    plan_app.session_state[localized_widget_key("subtitle_position_select")] = "top"
    plan_app.run()
    assert not plan_app.exception, plan_app.exception
    switch_view(plan_app, "plan")

    after = _ink(plan_app)
    assert after is not None, "改了位置之后预览消失了"
    assert after["top"] < 400, f"位置改成 top 之后字幕仍在 y={after['top']}，预览没跟上"
    assert before["bottom"] > 1000, "改动前的默认位置就不是底部，测试前提不成立"


def test_preview_follows_the_font_size(plan_app, switch_view):
    # 字号控件同样在生成视图上：改设置、切回计划、读预览。
    switch_view(plan_app, "generate")
    plan_app.session_state["font_size_slider"] = 100
    plan_app.run()
    assert not plan_app.exception, plan_app.exception
    switch_view(plan_app, "plan")
    large = _ink(plan_app)
    assert large is not None

    switch_view(plan_app, "generate")
    plan_app.session_state["font_size_slider"] = 30
    plan_app.run()
    switch_view(plan_app, "plan")
    small = _ink(plan_app)
    assert small is not None

    assert large["pixels"] > small["pixels"] * 1.8, "字号改了，预览没跟着变"
    assert large["pixels"] > small["pixels"], "字号预览没变化"


def test_a_broken_preview_never_blocks_generation(plan_app, monkeypatch):
    """
    预览失败必须静默退场，不能挡住出片。

    字体缺失、参数异常都不该让"生成今日视频"点不动——那是一个纯辅助功能。
    """
    from app.services import subtitle_preview as sp

    def boom(*_a, **_k):
        raise RuntimeError("font missing")

    monkeypatch.setattr(sp, "render_subtitle_draft_png", boom)
    # 清掉上一轮渲染留下的缓存，否则会命中缓存、根本不会调用到上面这个会炸的函数，
    # 测出来的"预览还在"是缓存的功劳，不是降级逻辑的功劳。
    if "draft_preview_cache" in plan_app.session_state:
        del plan_app.session_state["draft_preview_cache"]
    plan_app.run()
    assert not plan_app.exception, plan_app.exception

    labels = [str(b.label) for b in plan_app.button]
    assert any("Generate" in x for x in labels), "预览炸了之后生成按钮消失了"
    assert _ink(plan_app) is None, "预览渲染失败后仍然留下了图像"
    assert any(
        "preview" in str(c.value).lower() or "预览" in str(c.value)
        for c in plan_app.get("caption")
    ), "预览失败时没有给出任何提示"


def test_preview_uses_the_channel_env_overrides(plan_app, monkeypatch):
    """
    预览必须反映频道的 env 覆盖，而不只是表单值。

    四个频道都靠 automation.env 锁品牌配色；只显示表单值，用户看到的就不是真正会
    出片的那份设置。
    """
    monkeypatch.setattr(
        plans, "read_automation_env", lambda ch: {"VIDEO_ASPECT": "16:9"}
    )
    plan_app.run()
    assert not plan_app.exception, plan_app.exception

    png = _preview_png(plan_app)
    assert png is not None, "没有预览图"
    # 16:9 的宽高比必须与 9:16 不同，否则 env 覆盖根本没进预览
    size = Image.open(io.BytesIO(png)).size
    assert size[0] > size[1], f"env 指定了 16:9，预览却是 {size}"
