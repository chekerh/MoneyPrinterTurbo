"""工具台：一屏总览，以及每一张卡此刻到底能不能用。

工具台是落地页，因此它是**唯一一个用户不看导航就会看到**的界面。它要回答两个问题，
而且两个都必须是真的：

* 这里能做什么；
* 哪几项现在就差一点点。

第二问决定了它跟参考设计的分野。参考设计里七张卡完全一样，因为那是一个封闭产品；
本应用的能力要靠本地模型、免费额度的 key、乃至一条已完成的任务才可能跑起来。
一张在用户点下去之后才发现"其实不能用"的卡片，比一张明说"还差一个 key"的卡片更糟——
后者只浪费一次点击，前者浪费的是对整个界面的信任。

所以这些用例盯的不是"卡片渲染了没有"，而是**状态与它跳转的目标是否都跟着真实配置
变化**。左边改配置、右边卡片不动，是这类指示器最典型的失效方式。
"""

import ast
import json
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N = ROOT_DIR / "webui" / "i18n"

# 本模块要测"真正的落地页是什么"，所以不让 conftest 替它选屏——默认屏必须是
# 产品自己算出来的那个，而不是测试 harness 塞进来的。
WEBUI_DEFAULT_VIEW = None

BENCH_CARDS = [
    "script",
    "footage",
    "voice",
    "subtitles",
    "assembly",
    "thumbnails",
    "campaign",
    "plan",
    "library",
]

BENCH_KEYS = [
    "bench_go_script",
    "bench_go_footage",
    "bench_go_voice",
    "bench_go_subtitles",
    "bench_go_assembly",
    "bench_go_thumbnails",
    "bench_go_campaign",
    "bench_go_plan",
    "bench_go_library",
]


def _new_bench_app():
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
    app.session_state["ui_language"] = "en"
    app.session_state["nav_view"] = "bench"
    app.run()
    assert [str(item.value) for item in app.exception] == [], app.exception
    return app


def _main_source() -> str:
    return WEBUI_MAIN.read_text(encoding="utf-8")


# ── 渲染 ────────────────────────────────────────────────────────────────


class TestBenchRenders:
    def test_the_login_screen_is_the_landing_view(self):
        """落地页必须是登录屏。

        先连好各频道的 YouTube 账号，发布链路才是通的——之前落地在工具台，
        用户在媒体库点发布时才发现没登录。连过的 token 存在本机，之后打开
        直接显示已连接，不需要重复操作。
        """
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == [], app.exception
        assert app.session_state["nav_view"] == "login"

    def test_every_capability_has_a_card(self):
        """九项能力各一张卡。少一张就是少一个入口，而这类遗漏不会有任何报错。"""
        app = _new_bench_app()
        keys = {str(button.key) for button in app.button}
        missing = [key for key in BENCH_KEYS if key not in keys]
        assert not missing, f"工具台少了卡片：{missing}"

    def test_a_card_with_no_setup_still_renders_and_says_what_is_missing(self):
        """
        未就绪的卡必须出现，并且写清缺什么。

        把未就绪的卡藏起来是常见做法，理由是"不想让用户看到半成品"。但这里的
        半成品是**用户自己的配置缺口**，不是产品的缺陷——藏起来只会让他在别处
        撞上同一个问题，而且不知道该去哪里解决。
        """
        app = _new_bench_app()
        body = " ".join(
            [str(m.value) for m in app.markdown]
            + [str(c.value) for c in app.caption]
        )
        # 本机 config.toml 里可能配了 key，所以不能断言"一定有未就绪的卡"；
        # 断言的是：只要有卡标着待配置，它就一定同时给出了缺口的说明。
        if "Needs setup" in body:
            assert "Needs a free API key" in body, "标了待配置却没说缺什么"

    def test_the_bench_never_triggers_a_second_submission_path(self):
        """
        工具台不得绕过生成按钮的校验。

        提交前有约 280 行校验（素材源有效性、各源密钥、本地素材是否满足条数）。
        任何"从工具台直接触发生成"的做法都会跳过它们，于是任务在服务端才失败——
        而用户看到的只是一次成功的点击。
        """
        source = _main_source()
        tree = ast.parse(source)
        bench_fns = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and ("bench" in node.name)
        }
        assert bench_fns, "找不到任何工具台相关的函数"
        forbidden = ("_prepare_generation_task", "_render_generation_controls",
                     "start_publish_job", "_publish_via_browser")
        for name in bench_fns:
            body = ast.get_source_segment(source, next(
                node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name
            )) or ""
            for callee in forbidden:
                assert callee not in body, (
                    f"{name} 直接调了 {callee}；工具台只能导航，不能自己提交任务"
                )


# ── 状态 ────────────────────────────────────────────────────────────────


class TestBenchStateFollowsRealConfiguration:
    """卡片状态必须跟着真实配置走。

    左边改配置、右边卡片不动，是这类指示器最典型的失效方式：它还在，只是不再
    说真话。因此这里逐项验证**读的是哪个键**——不是"渲染出了 ready"，而是"配好
    之后它真的会变"。
    """

    def test_material_readiness_accepts_the_plural_config_keys(self):
        """
        素材就绪度必须认复数键。

        设置页写入的是 ``pexels_api_keys``（复数，与生成前的校验一致），而就绪度
        原先只读 ``pexels_api_key``。结果是：用户真的配好了 Pexels，侧栏与工具台
        仍然显示"未就绪"——一个专门用来回答"到底能不能用"的指示器在最需要它的
        时候说谎。键名现在按选中源拼出（``f"{source}_api_keys"``），不断言字面量，
        "配好真的变就绪"由下面的行为用例覆盖。
        """
        source = _main_source()
        tree = ast.parse(source)
        readiness = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_selected_media_state"
        )
        body = ast.get_source_segment(source, readiness) or ""
        assert "_api_keys" in body, (
            "就绪度没有读复数形式的图库密钥；配好密钥的用户会被永久显示成未就绪"
        )

    def test_material_readiness_follows_the_selected_source(self):
        """
        素材卡必须跟着选中的源走，而不是"配过任意一家就算就绪"。

        配了 Coverr 的 key、源选的却是 Pexels：旧逻辑显示 Ready，点生成时才被
        "Please Enter the Pexels API Key" 拦下。这里两个方向都断言——只配非选中
        源还是待配置，配好选中源才变就绪。
        """
        app = _new_bench_app()
        saved_source = config.app.get("video_source", "")
        saved_keys = {
            k: config.app.get(k, "")
            for k in ("pexels_api_keys", "pixabay_api_keys", "coverr_api_keys")
        }
        try:
            config.app["video_source"] = "pexels"
            for key in saved_keys:
                config.app[key] = ""
            app.run()
            assert _media_badge(app) == "needs_setup"

            config.app["coverr_api_keys"] = "test-key-not-a-real-credential"
            app.run()
            assert _media_badge(app) == "needs_setup", (
                "只配了 Coverr、源却是 Pexels，素材卡却显示就绪"
            )

            config.app["pexels_api_keys"] = "test-key-not-a-real-credential"
            app.run()
            assert _media_badge(app) == "ready", (
                "配好选中的 Pexels 之后素材卡仍不是就绪"
            )
        finally:
            if saved_source:
                config.app["video_source"] = saved_source
            else:
                config.app.pop("video_source", None)
            for key, value in saved_keys.items():
                if value:
                    config.app[key] = value
                else:
                    config.app.pop(key, None)

    def test_voice_readiness_reads_the_azure_section(self):
        """
        Azure 凭据存在 ``[azure]`` 段，不在 ``[app]`` 段。

        从 ``config.app`` 取 ``azure_speech_key`` 永远取不到，于是配好 Azure 的
        用户依然被告知"配音未就绪"。
        """
        source = _main_source()
        tree = ast.parse(source)
        readiness = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_selected_tts_state"
        )
        body = ast.get_source_segment(source, readiness) or ""
        assert "config.azure" in body, "就绪度没有读 [azure] 段里的凭据"

    @pytest.mark.parametrize(
        "config_key",
        ["pexels_api_keys", "pixabay_api_keys", "coverr_api_keys"],
    )
    def test_media_readiness_tracks_the_configured_key(self, config_key):
        """
        素材卡的状态必须跟着真实配置来回变。

        只验证"配好了显示就绪"是不够的：一个恒为"就绪"的假指示器也能通过它。所以
        两个方向都断言——**清空选中源的密钥必须变成待配置，配上之后必须变回
        就绪**。选中源也要在用例里显式固定，否则结果取决于本机 config 里恰好
        写的是哪个源——而"恰好"正是这类用例最常见的假阳性来源。

        这条正是原先那个 bug 的形状：就绪度只读单数键，于是配好 Pexels 的用户
        永远看到"未就绪"，而清空之后它显示"未就绪"反倒碰巧是对的——一个只在
        "没配"时正确的指示器。
        """
        source_for_key = {
            "pexels_api_keys": "pexels",
            "pixabay_api_keys": "pixabay",
            "coverr_api_keys": "coverr",
        }
        app = _new_bench_app()
        providers = ["pexels_api_keys", "pixabay_api_keys", "coverr_api_keys"]
        saved = {k: config.app.get(k, "") for k in providers}
        saved_source = config.app.get("video_source", "")
        try:
            config.app["video_source"] = source_for_key[config_key]
            for key in providers:
                config.app[key] = ""
            app.run()
            assert _media_badge(app) == "needs_setup", (
                "选中源的密钥清空了，素材卡却显示就绪"
            )

            config.app[config_key] = "test-key-not-a-real-credential"
            app.run()
            assert _media_badge(app) == "ready", (
                f"配好选中的 {config_key} 之后素材卡仍是 {_media_badge(app)!r}；"
                "就绪度没有读复数键"
            )
        finally:
            if saved_source:
                config.app["video_source"] = saved_source
            else:
                config.app.pop("video_source", None)
            for key, value in saved.items():
                if value:
                    config.app[key] = value
                else:
                    config.app.pop(key, None)

    def test_voice_readiness_tracks_the_azure_section(self):
        """
        Azure 凭据在 ``[azure]`` 段里，配上它配音必须变成就绪。

        原先从 ``config.app`` 取 ``azure_speech_key``——那个键根本不存在，于是
        配好 Azure 的用户永远被告知"配音未就绪"。选中的服务与配音模式同样要在
        用例里固定：就绪度看的是选中项，不是"配过任意一家"。

        key 与 region 必须一起配——运行时缺一个就抛错，只配 key 的"就绪"
        和没配没有区别。
        """
        app = _new_bench_app()
        others = [
            "elevenlabs_api_key", "azure_speech_key", "siliconflow_api_key",
            "minimax_api_key", "fish_api_key", "gemini_api_key",
        ]
        saved_app = {k: config.app.get(k, "") for k in others}
        saved_azure = config.azure.get("speech_key", "")
        saved_region = config.azure.get("speech_region", "")
        saved_mode = config.ui.get("voice_mode", "")
        saved_server = config.ui.get("tts_server", "")
        try:
            config.ui["voice_mode"] = "tts"
            config.ui["tts_server"] = "azure-tts-v1"
            for key in others:
                config.app[key] = ""
            config.azure["speech_key"] = ""
            config.azure["speech_region"] = ""
            app.run()
            assert _voice_badge(app) == "needs_setup", (
                "选中 Azure 且凭据都清空了，配音卡却显示就绪"
            )

            config.azure["speech_key"] = "test-key-not-a-real-credential"
            app.run()
            assert _voice_badge(app) == "needs_setup", (
                "只配了 key 没配 region，配音卡却显示就绪——运行时同样会失败"
            )

            config.azure["speech_region"] = "test-region"
            app.run()
            assert _voice_badge(app) == "ready", (
                f"在 [azure] 段配好凭据之后配音卡仍是 {_voice_badge(app)!r}；"
                "就绪度没有读 [azure] 段"
            )
        finally:
            for key, value in saved_app.items():
                if value:
                    config.app[key] = value
                else:
                    config.app.pop(key, None)
            if saved_azure:
                config.azure["speech_key"] = saved_azure
            else:
                config.azure.pop("speech_key", None)
            if saved_region:
                config.azure["speech_region"] = saved_region
            else:
                config.azure.pop("speech_region", None)
            if saved_mode:
                config.ui["voice_mode"] = saved_mode
            else:
                config.ui.pop("voice_mode", None)
            if saved_server:
                config.ui["tts_server"] = saved_server
            else:
                config.ui.pop("tts_server", None)

    def test_script_readiness_follows_the_selected_provider(self):
        """
        脚本卡必须跟着选中的 Provider 走，而不是"配过任意一家就算就绪"。

        选了 OpenAI 却只配了 Moonshot 的 key：旧逻辑显示 Ready，任务在运行时
        以 "openai: api_key is not set" 失败。这里配错一家、对上一家，以及一个
        注册表里不存在的 id，三种情况分别断言。
        """
        from app.models.llm_provider import get_llm_provider

        assert get_llm_provider("openai") is not None, "测试需要注册表里有 openai"
        assert get_llm_provider("moonshot") is not None, "测试需要注册表里有 moonshot"

        app = _new_bench_app()
        openai_key = get_llm_provider("openai").config_key("api_key")
        moonshot_key = get_llm_provider("moonshot").config_key("api_key")
        saved_provider = config.app.get("llm_provider", "")
        saved_openai = config.app.get(openai_key, "")
        saved_moonshot = config.app.get(moonshot_key, "")
        try:
            config.app["llm_provider"] = "openai"
            config.app[openai_key] = ""
            config.app[moonshot_key] = "test-key-not-a-real-credential"
            app.run()
            assert _card_state(app, "script") == "needs_setup", (
                "选了 OpenAI、只配了别家的 key，脚本卡却显示就绪"
            )

            config.app[openai_key] = "test-key-not-a-real-credential"
            app.run()
            assert _card_state(app, "script") == "ready", (
                "配好选中的 OpenAI 之后脚本卡仍不是就绪"
            )

            config.app["llm_provider"] = "no-such-provider"
            app.run()
            assert _card_state(app, "script") == "needs_setup", (
                "Provider id 不存在，脚本卡却显示就绪"
            )
        finally:
            if saved_provider:
                config.app["llm_provider"] = saved_provider
            else:
                config.app.pop("llm_provider", None)
            if saved_openai:
                config.app[openai_key] = saved_openai
            else:
                config.app.pop(openai_key, None)
            if saved_moonshot:
                config.app[moonshot_key] = saved_moonshot
            else:
                config.app.pop(moonshot_key, None)

    def test_a_never_needs_setup_card_stays_ready_without_any_credential(self):
        """
        字幕排版完全在本地跑，因此清空所有凭据之后它必须仍然是就绪。

        这是"永远就绪"那一档存在的理由。把它错标成待配置，会让用户以为字幕功能
        也需要密钥——然后在设置页里找一个并不存在的开关。
        """
        app = _new_bench_app()
        others = [
            "elevenlabs_api_key", "azure_speech_key", "siliconflow_api_key",
            "minimax_api_key", "fish_api_key", "gemini_api_key",
        ]
        saved = {k: config.app.get(k, "") for k in others}
        try:
            for key in others:
                config.app[key] = ""
            app.run()
            assert _card_state(app, "subtitles") == "ready"
        finally:
            for key, value in saved.items():
                if value:
                    config.app[key] = value
                else:
                    config.app.pop(key, None)


def _card_state(app, card: str) -> str:
    """读某张卡此刻的状态。

    状态写在 ``data-state`` 上，测试按 ``data-card`` 精确定位那一张。早先的版本
    在整页文字里搜状态词，返回的其实是**第一张**卡的旗标——断言于是会在真正坏掉的
    时候仍然通过，而在无关改动后突然失败。这类"看起来在工作"的辅助函数比没有更糟。
    """
    for element in app.get(element_type="markdown"):
        found = re.search(
            rf'data-card="{re.escape(card)}"\s+data-state="([^"]+)"', str(element.value)
        )
        if found:
            return found.group(1)
    present = [c for c in BENCH_CARDS if _card_present(app, c)]
    raise AssertionError(f"页面上找不到卡片 {card!r} 的状态标记；已渲染：{present}")


def _card_present(app, card: str) -> bool:
    return any(
        re.search(rf'data-card="{re.escape(card)}"', str(element.value))
        for element in app.get(element_type="markdown")
    )


def _media_badge(app) -> str:
    return _card_state(app, "footage")


def _voice_badge(app) -> str:
    return _card_state(app, "voice")


# ── 跳转 ────────────────────────────────────────────────────────────────


class TestBenchNavigation:
    def test_a_ready_card_navigates_to_its_own_view(self):
        """
        点"开始使用"要真的换屏。

        按钮存在不等于它接了线：``_NAV_REQUEST_KEY`` 是旁路键，写错一个字母就
        静默地什么都不发生，而卡片看上去完全正常。
        """
        app = _new_bench_app()
        target = next(
            b for b in app.button if str(b.key) == "bench_go_plan"
        )
        target.click().run()
        assert [str(item.value) for item in app.exception] == [], app.exception
        assert app.session_state["nav_view"] == "plan"

    def test_a_card_needing_setup_goes_to_settings_not_to_the_tool(self):
        """
        未就绪的卡跳设置页，而不是跳到那个用不了的控件前。

        跳过去只会让人对着一个空的控件发呆——而缺的是密钥，那个控件里根本没有
        填密钥的地方。

        这里先把凭据清空并把素材源固定为 Pexels，好让"待配置"这个状态真的出现。
        写成"如果本机恰好没配就跳过"的话，这条用例会在最需要它的开发机上安静地
        什么都不检查；不固定源的话，结果取决于本机 config 里恰好写的是哪个源。
        """
        app = _new_bench_app()
        providers = [
            "pexels_api_keys", "pixabay_api_keys", "coverr_api_keys",
            "elevenlabs_api_key", "siliconflow_api_key", "minimax_api_key",
            "fish_api_key", "gemini_api_key", "azure_speech_key",
        ]
        saved = {k: config.app.get(k, "") for k in providers}
        saved_azure = config.azure.get("speech_key", "")
        saved_source = config.app.get("video_source", "")
        try:
            config.app["video_source"] = "pexels"
            for key in providers:
                config.app[key] = ""
            config.azure["speech_key"] = ""
            app.run()

            targets = [
                str(b.label).strip()
                for b in app.button
                if str(b.key).startswith("bench_go_")
            ]
            assert "Set up" in targets, (
                f"清空全部凭据之后没有任何卡片切到待配置：{targets}"
            )

            button = next(
                b for b in app.button
                if str(b.key) == "bench_go_footage"
            )
            assert str(button.label).strip() == "Set up"
            button.click().run()
            assert [str(item.value) for item in app.exception] == [], app.exception
            assert app.session_state["nav_view"] == "settings", (
                "标着待配置的素材卡把用户送去了 "
                f"{app.session_state['nav_view']}，而不是设置页"
            )
        finally:
            for key, value in saved.items():
                if value:
                    config.app[key] = value
                else:
                    config.app.pop(key, None)
            if saved_source:
                config.app["video_source"] = saved_source
            else:
                config.app.pop("video_source", None)
            if saved_azure:
                config.azure["speech_key"] = saved_azure
            else:
                config.azure.pop("speech_key", None)

    def test_every_view_still_renders_after_the_bench_was_added(self):
        """加了落地页之后，原来的每一屏都还必须能打开。"""
        for view in ("login", "generate", "plan", "library", "copyright", "campaign", "settings", "bench"):
            app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
            app.session_state["ui_language"] = "en"
            app.session_state["nav_view"] = view
            app.run()
            assert [str(item.value) for item in app.exception] == [], (
                f"视图 {view} 渲染失败：{app.exception}"
            )


# ── 文案 ────────────────────────────────────────────────────────────────


class TestBenchCopyIsTranslated:
    """
    工具台的文案必须真的翻译。

    i18n 的 AST 测试只认 ``tr("字面量")``，而卡片网格是用 ``tr(tool["title"])``
    动态取键的——那两条 locale 测试**看不见这里的任何一个键**。缺键不会报错，
    界面上会直接显示 "Bench Script" 这种原始键名。只能自己核对。
    """

    @pytest.mark.parametrize("locale", ["en", "zh"])
    def test_every_bench_key_exists_in_both_maintained_locales(self, locale):
        source = _main_source()
        table = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        available = set(table["Translation"])

        referenced = set()
        for match in __import__("re").finditer(r'"((?:Bench|Nav Bench)[^"]*)"', source):
            referenced.add(match.group(1))
        referenced.add("Nav Group Bench")

        missing = sorted(referenced - available)
        assert not missing, f"{locale}.json 缺少工具台文案：{missing}"

    @pytest.mark.parametrize("locale", ["en", "zh"])
    def test_no_bench_copy_leaks_a_raw_key(self, locale):
        """渲染出来的文字里不能出现 "Bench ..." 这种原始键名。"""
        app = _new_bench_app()
        app.session_state["ui_language"] = locale
        app.run()
        blob = " ".join(
            [str(m.value) for m in app.markdown] + [str(c.value) for c in app.caption]
        )
        assert "Bench " not in blob, f"{locale} 下有文案回落成了原始键名：{blob[:200]}"

    def test_the_bench_does_not_invent_a_credit_system(self):
        """
        工具台不许出现 credits / credits 余额这类词。

        参考设计用积分计费，本应用是免费额度或纯本地能力。界面上出现"积分"会
        直接让人以为这里要花钱——而这正是免费工具卡一直在回答的问题。
        """
        source = _main_source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and "bench" in node.name:
                body = ast.get_source_segment(source, node) or ""
                for word in ("credit", "Credit"):
                    assert word not in body, (
                        f"{node.name} 里出现了 {word}；本应用不按积分计费"
                    )
