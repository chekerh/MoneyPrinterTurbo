import ast
import json
import re
import unittest
from pathlib import Path

from app.models.llm_provider import get_llm_provider
from app.utils import utils


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
LLM_PROVIDER_TIPS_PREFIX = "llm_provider_tips."
TTS_PROVIDER_TIPS_PREFIX = "tts_provider_tips."
SECONDARY_LOCALES = ("de", "es", "fr", "id", "it", "ko", "pt", "ru", "tr", "vi")
PROVIDER_TIPS_PREFIXES = (
    LLM_PROVIDER_TIPS_PREFIX,
    TTS_PROVIDER_TIPS_PREFIX,
)
# 合作 Provider 的品牌名和长说明只维护中英文。次要 locale 统一回退英文，
# 避免把完全相同的品牌名复制十份，也避免长说明后续只更新部分语言。
ENGLISH_FALLBACK_KEYS = frozenset(
    {
        # Posting tools maintain English and Chinese; other locales use tr() fallback.
        "Posting Package",
        "Posting Title",
        "Posting Description",
        "Posting Hashtags",
        "Posting Comment",
        "No Comment Draft",
        "Posting Copy Fallback",
        "Download Posting Text",
        "Posting Outcome Unknown",
        "Posting Receipt Invalid",
        "YouTube Connection Required",
        "YouTube API Publishing",
        "YouTube API Setup Help",
        "YouTube OAuth Client ID",
        "YouTube OAuth Client Secret",
        "YouTube Auto Publish",
        "AI Video Quote Required",
        "AI Video Quote Retained For Retry",
        "AI Video Quote Summary",
        "AI Video Quote Summary Singular",
        "AI Video Scene Count",
        "Confirm AI Video Charge",
        "Confirm AI Video Charge Help",
        "Confirm AI Video Charge Required",
        "Custom API Endpoint",
        "API Platform",
        "llm_provider_endpoint_selector.moonshot",
        "llm_provider_endpoint_selector_help.moonshot",
        "llm_provider_endpoint.moonshot.china",
        "llm_provider_endpoint.moonshot.global",
        "llm_provider_authentication_error.moonshot",
        "Local LLM Script Generation",
        "llm_provider_label.apimart",
        "llm_provider_label.openrouter",
        "llm_provider_label.shengsuanyun",
        "LoomLoom Poll Retry Pending",
        "LoomLoom Poll Retry Warning",
        "Resume LoomLoom Status Check",
        "LoomLoom Quote Summary Singular",
        "LoomLoom Video Terms Reuse Help",
        "Script Generation Method",
        "Script Generation Method Help",
        "Shengsuan Cloud AI Video",
        "Shengsuan Cloud AI Video Help",
        "Shengsuan Cloud API Key",
        "Shengsuan Cloud API Key Help",
        "Shengsuan Cloud API Key Placeholder",
        "Shengsuan Cloud API Key Required",
        "Shengsuan Cloud API Key Reused",
        "Shengsuan Cloud Batch Script Generation",
        "Stop Tracking LoomLoom Run",
        "Stop Tracking LoomLoom Run Help",
        "LoomLoom Video Quote Help",
        "LoomLoom Script Quote Help",
        "LoomLoom Run Help",
        "Clean Cache Now Help",
        # 活动内容工作台界面文案：新功能优先维护中英文，次要 locale 回退英文。
        "Audience",
        "Brand",
        "Build Campaign Brief",
        "Building Campaign Brief",
        "Campaign Brief",
        "Campaign Brief Built",
        "Campaign Brief Failed",
        "Campaign Content Help",
        "Campaign Content Studio",
        "Campaign Fetch Failed",
        "Campaign Generate Failed",
        "Campaign Link Empty",
        "Campaign Link Or Text",
        "Campaign Link Placeholder",
        "Campaign Media References",
        "Campaign Name",
        "Campaign Posts Count",
        "Campaign Posts Empty",
        "Campaign Posts Generated",
        "Campaign Source",
        "Campaign Source Empty",
        "Campaign Source Fetched",
        "Campaign Source Text",
        "Campaign Subject Loaded",
        "Content Pieces",
        "CTA",
        "Download Campaign Markdown",
        "Fetch And Understand",
        "Fetching Campaign Source",
        "Generate Campaign Posts",
        "Generating Campaign Posts",
        "Generate Hero Badge",
        "Generate Hero Sub",
        "Generate Hero Title",
        "Goal",
        "Hashtags",
        "Hook",
        "Kind Drive Docs",
        "Kind Drive File",
        "Kind Drive Folder",
        "Kind Notion",
        "Kind Text",
        "Kind Url",
        "No Campaign Source Yet",
        "On Screen Text",
        "Ready To Post",
        "Requirements",
        "Script",
        "Source Materials",
        "Tone",
        "Untitled Post",
        "Use Title As Video Subject",
        # 接受/重载已保存活动：次要 locale 回退英文。
        "Accept Campaign",
        "Campaign Accepted",
        "Campaign Load Failed",
        "Campaign Loaded",
        "Campaign Save Failed",
        "Campaign Saved As",
        "Load Saved Campaign",
        "Saved Campaigns",
        # 工具台文案：同样只维护中英文，次要 locale 回退英文。理由与上面一致——
        # 十一份复制品必然随时间失同步，而失同步的那一份正好是最难被发现的。
        "Bench Assembly",
        "Bench Assembly Desc",
        "Bench Campaign",
        "Bench Campaign Desc",
        "Bench Configure",
        "Bench Eyebrow",
        "Bench Footage",
        "Bench Footage Desc",
        "Bench Library",
        "Bench Library Desc",
        "Bench Needs Finished Task",
        "Bench Needs Key",
        "Bench Needs Key For",
        "Bench Needs Local Files",
        "Bench Needs Upload Audio",
        "Bench Plan",
        "Bench Plan Desc",
        "Bench Script",
        "Bench Script Desc",
        "Bench State Needs Setup",
        "Bench State Ready",
        "Bench State Waiting",
        "Bench Subtitle",
        "Bench Subtitles",
        "Bench Subtitles Desc",
        "Bench Thumbnails",
        "Bench Thumbnails Desc",
        "Bench Title",
        "Bench Try Now",
        "Bench Voice",
        "Bench Voice Desc",
        "Nav Bench",
        "Nav Bench Hint",
        "Nav Copyright",
        "Nav Copyright Hint",
        "Nav Group Account",
        "Nav Group Bench",
        "Nav Login",
        "Nav Login Hint",
        "New Video",
        "Login Hero Badge",
        "Login Hero Title",
        "Login Hero Sub",
        "Login Client Missing",
        "Login Client Setup",
        "Login Remembered Note",
        "Login No Channels",
        "Copyright Hero Badge",
        "Copyright Hero Title",
        "Copyright Hero Sub",
        "Copyright Pick Video",
        "Copyright Check",
        "Copyright No Video",
        "Copyright Verdict Low",
        "Copyright Verdict Medium",
        "Copyright Verdict High",
        "Copyright Disclaimer",
        "Copyright Asset Footage",
        "Copyright Asset BGM",
        "Copyright Asset Voice",
        "Copyright Note StockFootage",
        "Copyright Note AIVideo",
        "Copyright Note LocalFiles",
        "Copyright Note LocalUnknown",
        "Copyright Note UnknownSource",
        "Copyright Note CustomBGM",
        "Copyright Note AIMusic",
        "Copyright Note BundledMusic",
        "Copyright Note CustomAudio",
        "Copyright Note SyntheticVoice",
        "All Topics Recorded Next Step",
        "App Brand",
        "Confirm Delete",
        "Confirm Delete Task Caption",
        "Dismiss",
        "Draft Preview Needs Settings",
        "Draft Preview Note",
        "Draft Preview Unavailable",
        "Free Tool Media",
        "Free Tool Script",
        "Free Tool Voice",
        "Free Tools",
        "Free Tools Note",
        "Free Tools Partial",
        "Free Tools Ready",
        "Navigation Hint",
        "No Recorded Topics Yet",
        "Open Output Folder On Completion",
        "Open Output Folder On Completion Help",
        "Pipeline Health",
        "Pipeline Last Run",
        "Pipeline Last Run Never",
        "Pipeline Publish Connected",
        "Pipeline Publish Missing",
        "Pipeline Run Failed",
        "Pipeline Run Ok",
        "Pipeline Run Unknown",
        "Pipeline Scheduler Partial",
        "Pipeline Scheduler Running",
        "Pipeline Scheduler Stopped",
        "Pipeline Scheduler Unknown",
        "Plan Number",
        "Plan Overrides From Channel",
        "Plan Unknown Env Keys",
        "Publish Elapsed",
        "Publish Videos From The Library",
        "Publishing",
        "Recorded Topics",
        "Replay Onboarding Tour",
        "Replay Onboarding Tour Help",
        "Roadmap Ad Variants",
        "Roadmap Ad Variants Desc",
        "Roadmap Ad Variants Needs",
        "Roadmap AI Image",
        "Roadmap AI Image Desc",
        "Roadmap AI Image Needs",
        "Roadmap Badge",
        "Roadmap Bench",
        "Roadmap Bench Note",
        "Roadmap Not Yet",
        "Roadmap Prompt Builder",
        "Roadmap Prompt Builder Desc",
        "Roadmap Prompt Builder Needs",
        "Roadmap Search",
        "Roadmap Search Desc",
        "Roadmap Search Needs",
        "Roadmap Skit Builder",
        "Roadmap Skit Builder Desc",
        "Roadmap Skit Builder Needs",
        "Roadmap Timeline Editor",
        "Roadmap Timeline Editor Desc",
        "Roadmap Timeline Editor Needs",
        "Showing Newest Of Tasks",
        "Task Delete Kept Video No Layout",
        "Task Delete Kept Video Unarchived",
        "Task Delete Removed Video",
        "Thumbnail Generation Cancelled",
        "Thumbnail Generation Failed",
        "Channel Connect Help",
        "Channel Mismatch Warning",
        "Draft Preview Subtitles Off",
    }
)
FORMAT_PLACEHOLDER_PATTERN = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")
MARKDOWN_URL_PATTERN = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")


class _TrKeyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.keys = set()

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.keys.add(node.args[0].value)
        self.generic_visit(node)


def _load_translation(locale):
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data.get("Translation", {})


def _required_translation_keys(translations):
    """返回二级语言必须维护的 key，Provider 长说明统一回退英文。"""
    return {
        key
        for key in translations
        if key not in ENGLISH_FALLBACK_KEYS
        and not key.startswith(PROVIDER_TIPS_PREFIXES)
    }


def _format_placeholders(value):
    """提取运行时格式化变量，防止翻译遗漏或误改变量名。"""
    return set(FORMAT_PLACEHOLDER_PATTERN.findall(value))


def _markdown_urls(value):
    """提取 Markdown 链接目标，允许翻译链接文字但不允许改坏地址。"""
    return set(MARKDOWN_URL_PATTERN.findall(value))


class TestWebuiI18n(unittest.TestCase):
    def test_saved_ui_language_takes_priority_over_browser_locale(self):
        language = utils.resolve_ui_language(
            saved_language="de",
            browser_locale="zh-CN",
            supported_languages=["zh", "en", "de"],
        )

        self.assertEqual(language, "de")

    def test_browser_locale_is_normalized_to_supported_base_language(self):
        self.assertEqual(
            utils.resolve_ui_language("", "zh-CN", ["zh", "en"]),
            "zh",
        )
        self.assertEqual(
            utils.resolve_ui_language(None, "pt_BR", ["en", "pt"]),
            "pt",
        )

    def test_unsupported_browser_locale_falls_back_to_english(self):
        language = utils.resolve_ui_language(
            saved_language="",
            browser_locale="fr-FR",
            supported_languages=["zh", "en"],
        )

        self.assertEqual(language, "en")

    def test_english_locale_covers_static_webui_labels(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        en_keys = set(_load_translation("en"))

        self.assertEqual(sorted(visitor.keys - en_keys), [])

    def test_shengsuanyun_provider_tips_keep_registration_and_model_links(self):
        """合作入口和模型目录属于产品配置，避免后续改文案时误删追踪链接。"""
        expected_urls = {
            "https://www.shengsuanyun.com/?from=CH_XUQ4OTSK",
            "https://global.modelmesh.info/model",
        }

        for locale in ("zh", "en"):
            with self.subTest(locale=locale):
                tips = _load_translation(locale)["llm_provider_tips.shengsuanyun"]
                provider = get_llm_provider("shengsuanyun")
                rendered = tips.format(
                    api_key_url=provider.effective_api_key_url(),
                    default_base_url=provider.effective_default_base_url,
                    default_model=provider.default_model,
                )
                self.assertEqual(_markdown_urls(rendered), expected_urls)

    def test_secondary_locales_cover_english_locale(self):
        en_translations = _load_translation("en")
        required_en_keys = _required_translation_keys(en_translations)

        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(sorted(required_en_keys - locale_keys), [])

    def test_secondary_locales_do_not_duplicate_provider_tips(self):
        # Provider 配置长说明只维护中英文，其它语言运行时回退英文。
        # 禁止复制这些 key，避免出现不会持续维护的半翻译内容。
        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                duplicated_keys = sorted(
                    key for key in locale_keys if key.startswith(PROVIDER_TIPS_PREFIXES)
                )
                self.assertEqual(duplicated_keys, [])

    def test_secondary_locales_do_not_duplicate_english_fallback_keys(self):
        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(sorted(ENGLISH_FALLBACK_KEYS & locale_keys), [])

    def test_secondary_locales_cover_static_webui_labels(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(
                    sorted(visitor.keys - locale_keys - ENGLISH_FALLBACK_KEYS),
                    [],
                )

    def test_secondary_locales_preserve_format_placeholders(self):
        en_translations = _load_translation("en")

        for locale in SECONDARY_LOCALES:
            locale_translations = _load_translation(locale)
            for key in _required_translation_keys(en_translations):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _format_placeholders(locale_translations[key]),
                        _format_placeholders(en_translations[key]),
                    )

    def test_secondary_locales_preserve_markdown_urls(self):
        en_translations = _load_translation("en")

        for locale in SECONDARY_LOCALES:
            locale_translations = _load_translation(locale)
            for key in _required_translation_keys(en_translations):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _markdown_urls(locale_translations[key]),
                        _markdown_urls(en_translations[key]),
                    )

    def test_script_language_options_include_russian(self):
        tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
        support_locales = None

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "support_locales"
                for target in node.targets
            ):
                support_locales = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(support_locales)
        self.assertIn("ru-RU", support_locales)
