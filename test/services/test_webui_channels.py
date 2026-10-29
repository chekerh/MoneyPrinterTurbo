"""赛道页（Channels）的界面级回归测试。

这一屏的存在理由是"在应用里自己做完"：新增赛道、选主题、直接出片，都不该再
回到改代码或抄命令。所以测试也必须走界面，而不是直接调 ``niche_registry``：
真正会坏的地方一直是控件层——早期用哨兵值 + ``format_func`` 的 selectbox 在
第二次 rerun 时抛 ``ValueError``，页面白屏；"从赛道直接开一条片子"曾经只把
参数写进 session，却没有人真正提交，表现是"点了没反应"。

覆盖三件事：

1. 页面能连续渲染两次（哨兵选项回归点）。
2. 表单保存的赛道出现在注册表、``topics_for`` 与生成页的赛道下拉里。
3. "Create video now" 走的是与手动点击"生成视频"完全相同的提交路径：主题、
   赛道、长度档位、目标秒数都落到提交的 ``VideoParams`` 上。
4. 删除需要二次确认，确认前注册表不变。
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import niche_brand, niche_registry, webui_task

# tools/ 与本仓库根目录：WebUI 入口自己也会按同样方式补 sys.path。
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
TOOLS_DIR = ROOT_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def _widget(sequence, label, kind):
    """按界面文案取控件。找不到就把当前所有文案打出来，省得猜。"""
    matches = [widget for widget in sequence if widget.label == label]
    assert matches, (
        f"no {kind} labelled {label!r}; "
        f"available: {[widget.label for widget in sequence]}"
    )
    return matches[0]


def _fill_new_niche(app):
    _widget(app.text_input, "Niche slug", "text_input").set_value("odd-numbers")
    _widget(app.text_input, "Display name", "text_input").set_value("Odd Numbers")
    _widget(app.text_area, "Brand line", "text_area").set_value(
        "every day one number that refuses to behave"
    )
    _widget(app.text_area, "Teaser shapes (one per line)", "text_area").set_value(
        "open on the strangest number in the topic"
    )
    _widget(app.text_area, "Topics (one per line)", "text_area").set_value(
        "1. The number that broke a bank\n2. Why 7 feels lucky"
    )
    _widget(app.selectbox, "Long-form band", "selectbox").set_value("medium")
    _widget(app.number_input, "Long-form target seconds", "number_input").set_value(600)


@pytest.fixture
def channels_app(monkeypatch, tmp_path):
    """在临时注册表上跑整页，绝不碰开发者真实的 ``storage/niches.json``。"""
    monkeypatch.setenv("MPT_NICHE_STORE", str(tmp_path / "niches.json"))
    # 保存会同步写回 plans/<channel>/，删除会移动目录。两者都属于真实作品集数据，
    # 在用例里一律挡掉；这里要验证的是注册表与界面，而不是文件搬运。
    monkeypatch.setattr(niche_registry, "sync_plan_files", lambda *a, **k: None)
    monkeypatch.setattr(niche_registry, "_archive_plan_dir", lambda *a, **k: None)
    # 幻灯片画面源不依赖任何素材库 key，提交前的校验不会因为本机没 key 而拦截。
    monkeypatch.setattr(
        config, "app", dict(config.app, video_source="slides"), raising=False
    )

    saved_niches = dict(niche_brand.NICHES)
    niche_brand.refresh_niches()
    try:
        with patch.object(config, "try_save_config", return_value=True):
            app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=90)
            app.session_state["nav_view"] = "channels"
            app.run()
            assert not app.exception, app.exception
            yield app
    finally:
        niche_brand.NICHES.clear()
        niche_brand.NICHES.update(saved_niches)


def test_channels_page_renders_twice_and_creates_a_niche(channels_app, switch_view):
    app = channels_app

    # 曾经的崩溃点：页面第一次能开，第二次 rerun 时 selectbox 用展示文案回查
    # 一个不在选项里的哨兵值，直接白屏。
    app.run()
    assert not app.exception, app.exception

    _fill_new_niche(app)
    app.run()
    assert not app.exception, app.exception

    _widget(app.button, "Save niche", "button").click()
    app.run()
    assert not app.exception, app.exception

    profiles = niche_registry.load_profiles()
    assert "odd-numbers" in profiles
    assert profiles["odd-numbers"].display_name == "Odd Numbers"
    assert profiles["odd-numbers"].longform_band == "medium"
    assert profiles["odd-numbers"].longform_target_seconds == 600
    # 主题里的 "1." 序号必须被清掉，否则它会变成视频主题的一部分。
    assert niche_registry.topics_for("odd-numbers") == [
        "The number that broke a bank",
        "Why 7 feels lucky",
    ]

    # 新赛道必须出现在生成页的赛道下拉里，否则"在应用里新增"只完成了一半。
    switch_view(app, "generate")
    assert not app.exception, app.exception
    niche_select = app.selectbox(key="channel_en")
    assert "odd-numbers" in niche_select.options


def test_create_video_from_niche_prefills_and_submits(channels_app, switch_view):
    app = channels_app

    _fill_new_niche(app)
    app.run()
    _widget(app.button, "Save niche", "button").click()
    app.run()
    assert not app.exception, app.exception

    # 选中刚建好的赛道，再挑一个主题。
    app.selectbox(key="channels_selected").set_value("Odd Numbers")
    app.run()
    assert not app.exception, app.exception

    topic_key = next(
        key
        for key in app.session_state.filtered_state
        if str(key).startswith("channel_create_topic_")
    )
    app.selectbox(key=topic_key).set_value("The number that broke a bank")
    app.run()
    assert not app.exception, app.exception

    with patch.object(webui_task, "submit_generation", return_value=None) as submit_mock:
        _widget(app.button, "Create video now", "button").click()
        app.run()
    assert not app.exception, app.exception

    assert submit_mock.call_count == 1, "one click must submit exactly one task"
    params = submit_mock.call_args.kwargs["params"]
    assert params.video_subject == "The number that broke a bank"
    assert params.channel == "odd-numbers"
    assert params.content_length == "medium"
    assert params.content_target_seconds == 600
    # 提交走的是生成页，主题必须已经回填到界面上，而不是只存在于提交参数里。
    assert app.session_state["video_subject"] == "The number that broke a bank"


def test_editing_an_existing_niche_updates_its_profile(channels_app):
    """编辑也必须走同一个表单：内置赛道选中后 slug 只读，其余字段可改并落盘。"""
    app = channels_app

    app.selectbox(key="channels_selected").set_value("History and Mysteries")
    app.run()
    assert not app.exception, app.exception

    _widget(app.text_area, "Brand line", "text_area").set_value(
        "every day we retell one story that refuses to stay buried"
    )
    _widget(app.selectbox, "Long-form band", "selectbox").set_value("long")
    _widget(app.number_input, "Long-form target seconds", "number_input").set_value(1500)
    app.run()
    assert not app.exception, app.exception

    _widget(app.button, "Save niche", "button").click()
    app.run()
    assert not app.exception, app.exception

    profile = niche_registry.load_profiles()["history-mysteries"]
    assert profile.brand_line == (
        "every day we retell one story that refuses to stay buried"
    )
    assert profile.longform_band == "long"
    assert profile.longform_target_seconds == 1500


def test_plan_page_states_the_niche_length_band(channels_app, switch_view, monkeypatch):
    """
    计划页要说清楚"这条赛道默认出多长的片"。

    界面上的档位来自生成页表单，而计划页按天出片时用的是赛道画像里的档位。
    不写出来，用户会以为两者一致——直到拿到一条时长完全不同的成片。
    """
    from app.services import niche_registry as registry

    app = channels_app
    _fill_new_niche(app)
    app.run()
    _widget(app.button, "Save niche", "button").click()
    app.run()
    assert not app.exception, app.exception

    # 计划面板只在 plans/ 里真有频道时才画；这里手动让它认这条赛道。
    monkeypatch.setattr(registry, "sync_plan_files", lambda *a, **k: None)
    import plans as repo_plans

    monkeypatch.setattr(repo_plans, "list_channels", lambda: ["odd-numbers"])

    switch_view(app, "plan")
    assert not app.exception, app.exception

    captions = " ".join(str(c.value) for c in app.caption)
    assert "Medium" in captions, captions


def test_delete_requires_confirmation(channels_app):
    app = channels_app

    _fill_new_niche(app)
    app.run()
    _widget(app.button, "Save niche", "button").click()
    app.run()
    assert "odd-numbers" in niche_registry.load_profiles()

    app.selectbox(key="channels_selected").set_value("Odd Numbers")
    app.run()
    assert not app.exception, app.exception

    _widget(app.button, "Delete niche", "button").click()
    app.run()
    assert not app.exception, app.exception

    # 第一次点击只负责问一句，绝不能已经把赛道删掉。
    assert "odd-numbers" in niche_registry.load_profiles()
    warning_text = " ".join(str(w.value) for w in app.warning)
    assert "odd-numbers" in warning_text

    _widget(app.button, "Yes, delete niche", "button").click()
    app.run()
    assert not app.exception, app.exception
    assert "odd-numbers" not in niche_registry.load_profiles()
