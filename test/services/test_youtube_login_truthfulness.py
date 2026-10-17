"""登录屏必须说真话：区分「从未登录」「登录过但 token 过期」「登录过但查不了」。

回归背景（用户实际撞上的那一轮）：

1. 授权流程在浏览器里显示 "The authentication flow has completed"，
   ``storage/credentials/youtube/history-mysteries.json`` 里也确实写进了
   refresh_token——**OAuth 是成功的**。
2. 但项目当时还没启用 YouTube Data API，于是 ``channel_info()`` 返回
   ``accessNotConfigured``。
3. 登录屏只把这条路径渲染成 "⚠️ Channel is not signed in"。用户刚看到成功、
   转头就被告知没登录，于是反复重新登录——而重登永远解决不了接口没通的问题。

更糟的是第二个缺陷：``_remembered_youtube_login`` 在验证失败时返回 ``None``，
而 ``_render_youtube_login_detail`` 见到 ``None`` 就直接不画任何东西。于是这条
错误提示**只在刚点完按钮那一瞬间存在**，刷新页面后状态整条消失。

这些用例断言用户实际读到的**英文文案**，而不是内部字典的形状：文案是这个缺陷
唯一泄漏出去的地方。断言字典形状的测试会在文案被改成同样有害的另一种说法时
依然通过，而那正是这个缺陷的复发方式。
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.services import youtube_upload

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_EN = ROOT_DIR / "webui" / "i18n" / "en.json"

#: 这套测试自己声明频道，不读开发者真实的 ``plans/``。
#:
#: conftest 里有个 autouse 护栏把 ``plans.PLANS_DIR`` 指向一个空的临时目录，所以
#: 登录屏在测试里会显示"还没有频道"、一张卡片都不画。用真实的频道目录去补这个洞
#: 会让用例依赖本机状态：这台机器有 4 个频道、那台 1 个，断言随环境漂移。
#: 而且更糟的是——补了之后，"这个频道从来没登录过"这个状态就再也测不到了。
NICHE = "history-mysteries"
OTHER_NICHES = ("space-science", "mind-psychology", "money-finance")

# conftest 的 ``_pin_default_webui_view`` 会把 AppTest 默认钉在生成页；这里关心的是
# 登录屏，必须在第一次 run 之前声明（见该 fixture 的 docstring）。
WEBUI_DEFAULT_VIEW = "login"

_ACCESS_NOT_CONFIGURED = (
    "<HttpError 403 when requesting "
    "https://youtube.googleapis.com/youtube/v3/channels returned "
    '"YouTube Data API v3 has not been used in project 314980975331 before or '
    'it is disabled." Details: "[{\'reason\': \'accessNotConfigured\'}]">'
)


def _copy(key: str) -> str:
    """从en.json 取真实文案。

    直接引用翻译表而不是把句子抄进测试：文案改写时测试会跟着走，而不会变成
    一个永远匹配的僵尸断言——上一次这类缺陷就藏在"字符串看起来没变"的假设里。
    """
    return json.loads(I18N_EN.read_text(encoding="utf-8"))["Translation"][key]


UNVERIFIED_CARD = _copy("Channel Signed In Unverified")
UNVERIFIED_PLAN = _copy("Pipeline Publish Unverified")
EXPIRED_PLAN = _copy("Pipeline Publish Expired")
EXPIRED_CARD = _copy("Channel Login Expired")
MISSING_PLAN = _copy("Pipeline Publish Missing")
PREFLIGHT_USERS = _copy("Login Preflight TestUsers")
PREFLIGHT_PRODUCTION = _copy("Login Preflight Production")
NOT_LOGGED_IN = _copy("Channel Not Logged In")


def _visible_text(app: AppTest) -> str:
    """页面上的全部可见文本，剥掉 ``<style>`` 块并还原 HTML 实体。

    两处清理都是为了让断言能和翻译表**原文**比对：

    * 主题 CSS 通过 ``st.markdown`` 注入，块内全是中文注释。不剥掉的话任何
      "某句话不该出现"的否定断言都会被样式表里的字符命中。
    * 登录屏的引导文案用 ``unsafe_allow_html`` 渲染，里的引号会变成 ``&quot;``。
      不还原实体的话断言就得改成匹配转义后的文本——那等于把渲染细节抄进测试，
      改一个字就假失败，而真正要防的回归（文案改错了）反而测不出来。
    """
    chunks: list[str] = []
    for element in app.markdown:
        chunks.append(str(element.value or ""))
    for collection in (app.caption, app.info, app.warning, app.error):
        for element in collection:
            chunks.append(str(getattr(element, "body", "") or getattr(element, "value", "") or ""))
    for element in app.expander:
        chunks.append(str(getattr(element, "label", "") or ""))
    for element in app.button:
        chunks.append(str(getattr(element, "label", "") or ""))
    text = re.sub(r"<style.*?</style>", "", "\n".join(chunks), flags=re.S)
    return html.unescape(text)


@pytest.fixture
def niches(monkeypatch):
    """声明一组固定的频道，四个都在。

    同时打两份 ``plans``：``webui/Main.py`` 走 ``sys.path`` 插入的 top-level 那份，
    而 ``from tools import plans`` 的用例拿到的是包里的另一份。只打一份的话症状
    很隐蔽：登录屏安静地显示"还没有频道"，一张卡片都不画，断言里找不到任何状态
    文案——看起来像断言写错了，实际是打错了对象。
    """
    import plans as top_level_plans

    names = [NICHE, *OTHER_NICHES]
    modules = [top_level_plans]
    try:
        from tools import plans as tools_plans

        if tools_plans is not top_level_plans:
            modules.append(tools_plans)
    except Exception:  # noqa: BLE001 - tools 包不可导入时只打 top-level 那份
        pass
    for module in modules:
        monkeypatch.setattr(module, "list_channels", lambda: list(names))
    return names


@pytest.fixture
def signed_in_but_api_off(tmp_path, monkeypatch, niches):
    """授权已完成、凭据在磁盘上，但 YouTube Data API 还没启用。

    这正是用户报障时的状态：refresh_token 存在，``channel_info`` 却因为
    ``accessNotConfigured`` 失败。
    """
    monkeypatch.setattr(youtube_upload, "token_path", lambda ch: str(tmp_path / f"{ch}.json"))
    monkeypatch.setattr(youtube_upload, "connected_channels", lambda: [NICHE])
    monkeypatch.setattr(youtube_upload, "is_configured", lambda ch="": ch == NICHE)
    (tmp_path / f"{NICHE}.json").write_text('{"refresh_token": "x"}', encoding="utf-8")
    return patch.object(
        youtube_upload,
        "channel_info",
        return_value={"success": False, "error": _ACCESS_NOT_CONFIGURED},
    )


@pytest.fixture
def never_signed_in(monkeypatch, niches):
    """四个频道一个都没登录过——凭据目录是空的。"""

    def _token_path(channel: str) -> str:
        return f"/nonexistent-storage/credentials/youtube/{channel}.json"

    monkeypatch.setattr(youtube_upload, "token_path", _token_path)
    monkeypatch.setattr(youtube_upload, "connected_channels", lambda: [])
    monkeypatch.setattr(youtube_upload, "is_configured", lambda ch="": False)
    monkeypatch.setattr(
        youtube_upload,
        "channel_info",
        lambda ch="": {"success": False, "error": "not connected"},
    )
    return list(niches)


def _run_login_page() -> AppTest:
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
    # 必须清 ``cache_data``：``_cached_youtube_channel_info`` 是带 5 分钟 TTL 的
    # 缓存，而这个测试文件里**每个用例都在换 ``channel_info`` 的返回值**。缓存
    # 按代码哈希命中，同一个文件重新执行并不会得到新的缓存条目，于是上一个用例
    # 的连接状态会漏进下一个——表现是"过期"用例渲染出"暂时查不了"，断言失败
    # 却完全看不出哪里错了。conftest 的护栏只清了 ``cache_resource``。
    import streamlit as st

    st.cache_data.clear()
    app.run()
    assert not app.exception, app.exception
    return app


def test_saved_login_is_never_reported_as_missing(signed_in_but_api_off):
    """凭据在磁盘上、只是接口暂时查不了——不能说"未登录"。

    这条断言直接对着用户撞到的那句话。退化回去，用户就会进入
    "登录 → 还是说未登录" 的死循环，而真正的原因无人告知。
    """
    with signed_in_but_api_off:
        text = _visible_text(_run_login_page())

    assert NOT_LOGGED_IN not in text, (
        f"本机存着 refresh_token 却显示未登录。实际页面：\n{text[-1200:]}"
    )
    assert UNVERIFIED_CARD in text, "登录卡片没有说明凭据已保存、只是验证不了"
    assert UNVERIFIED_PLAN in text, "计划页没有说明原因是要启用 YouTube Data API"
    assert "YouTube Data API" in UNVERIFIED_PLAN, (
        "提示必须点名真正的原因，否则用户只知道'验证不了'，还是不知道该做什么"
    )


def test_unverified_state_survives_a_page_refresh(signed_in_but_api_off):
    """刷新之后这条状态必须还在。

    ``_remembered_youtube_login`` 早先在验证失败时返回 ``None``，渲染函数见
    到 ``None`` 就整段跳过——于是"已保存凭据"这个事实只在刚点完按钮的那一瞬间
    存在于页面上。AppTest 每次都是全新 session，正好复现刷新后的状态。
    """
    with signed_in_but_api_off:
        app = _run_login_page()
        text_before = _visible_text(app)
        app.run()  # 再跑一次，等价于用户刷新页面
        text_after = _visible_text(app)

    assert UNVERIFIED_CARD in text_before
    assert UNVERIFIED_CARD in text_after, (
        f"刷新后状态整条消失——用户会以为登录丢了。刷新后页面：\n{text_after[-800:]}"
    )
    assert NOT_LOGGED_IN not in text_after


def test_expired_login_is_not_confused_with_a_missing_one(signed_in_but_api_off):
    """过期凭据要说的是"要重新登录"，不是"你没登录过"。

    两者下一步动作不同：过期的根因通常是同意屏幕停在 Testing，重新授权之后
    7 天还会再坏一次；只说"未登录"会让人以为只是没配过，于是配完也不对。
    """
    with patch.object(
        youtube_upload,
        "channel_info",
        return_value={
            "success": False,
            "needs_reauth": True,
            "error": youtube_upload.REAUTH_MESSAGE,
        },
    ):
        text = _visible_text(_run_login_page())

    assert EXPIRED_PLAN in text, (
        f"过期登录没有被单独点名。实际页面：\n{text[-1200:]}"
    )
    assert EXPIRED_CARD in text, "登录卡片也没说这是过期而不是没配"
    assert UNVERIFIED_PLAN not in text, (
        "'暂时查不了'和'token 过期'是两回事，混成同一条提示会让人反复启用 API"
    )
    assert NOT_LOGGED_IN not in text


def test_preflight_prerequisites_are_visible_before_the_user_clicks(signed_in_but_api_off):
    """两个前置条件必须在点按钮**之前**就写出来。

    Google 把授权错误渲染在浏览器里，本地回调服务器看不到——只有超时回到应用。
    也就是说应用内的错误诊断对这一轮对话不可达，前置条件只能提前写在屏幕上，
    否则用户只能对着 Google 的错误页猜。
    """
    with signed_in_but_api_off:
        text = _visible_text(_run_login_page())

    assert PREFLIGHT_USERS in text, f"登录屏没有预告 Test users 前置条件。\n{text[-1200:]}"
    assert PREFLIGHT_PRODUCTION in text, "登录屏没有预告 Testing 模式 7 天失效"
    assert "7 days" in PREFLIGHT_PRODUCTION, (
        "必须写明 7 天这个具体数字，否则用户不知道这个问题会反复出现、也就不会去修"
    )


def test_never_signed_in_channel_still_says_missing(never_signed_in):
    """反向用例：真的没登录过，就必须老老实实说未连接。

    上一组用例把"已保存凭据"讲清楚了，但如果顺手把"从未登录"也改成安抚性文案，
    用户就失去了"这个频道还没配"的信号——而这是四个频道逐一接入时的关键状态。
    """
    text = _visible_text(_run_login_page())

    assert text.count(MISSING_PLAN) == len(never_signed_in), (
        f"每个未登录频道都应各有一条未连接提示。实际页面：\n{text[-1200:]}"
    )
    assert UNVERIFIED_PLAN not in text, "本机没有任何凭据，不该出现'已保存但未验证'"
    assert NOT_LOGGED_IN not in text, (
        "从未登录时不该弹出'再登一次'的警告——那张卡片只在登录尝试失败后才有意义"
    )


def test_one_signed_in_niche_does_not_hide_the_rest(never_signed_in, tmp_path, monkeypatch):
    """四个频道各自独立：一个"存了凭据但查不了"不能盖掉另外三个的未连接状态。

    这是"逐个接入四个频道"这个工作流的实际形态：用户按顺序连，第一个会先撞上
    API 未启用。若未验证态被渲染成整页的一条横幅，另外三个频道就会看起来像
    "也处理过了"，用户以为自己只剩一个频道要管。
    """
    monkeypatch.setattr(youtube_upload, "connected_channels", lambda: [NICHE])
    monkeypatch.setattr(youtube_upload, "is_configured", lambda ch="": ch == NICHE)
    # 必须连 token_path 一起改：``never_signed_in`` 把它指到不存在的目录，
    # 只改上面两项的话 has_saved_token() 仍然读不到凭据，卡片会退回"未连接"——
    # 也就是说这个用例会静默退化成上一个用例的复测，白绿。
    monkeypatch.setattr(youtube_upload, "token_path", lambda ch: str(tmp_path / f"{ch}.json"))
    (tmp_path / f"{NICHE}.json").write_text('{"refresh_token": "x"}', encoding="utf-8")

    text = _visible_text(_run_login_page())

    assert UNVERIFIED_PLAN in text, "已授权的频道没有显示未验证态"
    assert text.count(MISSING_PLAN) == len(OTHER_NICHES), (
        f"另外 {len(OTHER_NICHES)} 个频道各自的未连接提示消失了。实际页面：\n{text[-1200:]}"
    )


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))