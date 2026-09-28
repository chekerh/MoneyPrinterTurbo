import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import plans as repo_plans  # noqa: E402

from app.services import campaign as campaign_service  # noqa: E402

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


@pytest.fixture
def campaign_app(monkeypatch, tmp_path):
    applications_dir = ROOT_DIR / "test" / "test_data" / "media_library_app"
    plans_root = applications_dir / "plans"
    outputs_root = applications_dir / "outputs"
    (plans_root / "testch").mkdir(parents=True, exist_ok=True)
    (outputs_root / "testch").mkdir(parents=True, exist_ok=True)
    saved_root = tmp_path / "campaigns"
    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(plans_root))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(outputs_root))
    monkeypatch.setattr(
        campaign_service, "saved_campaigns_root", lambda create=False: str(saved_root)
    )

    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception
        return app


def _campaign_source_dict():
    return {
        "kind": "notion",
        "url": "https://mediamaxxing.notion.site/lovable-clipping",
        "title": "Lovable Clipping",
        "text": (
            "Main goal is tier1 views. Must show Anton and/or Lovable.\n"
            "Must use #LovablePartner. Submit within 10 minutes.\n"
        ),
        "refs": [{"title": "podcast", "url": "https://www.youtube.com/watch?v=jr9ESVOtyMo"}],
    }


def _campaign_brief_dict():
    return {
        "campaign_name": "Lovable Clipping",
        "brand": "Lovable",
        "goal": "tier 1 short-form views from podcast clips",
        "audience": "startup founders and AI builders",
        "tone": "authentic, upbeat",
        "platforms": ["youtube shorts"],
        "hashtags": ["LovablePartner", "AI"],
        "requirements": ["show Anton or Lovable", "use #LovablePartner"],
        "source_materials": ["https://www.youtube.com/watch?v=jr9ESVOtyMo"],
        "content_types": ["clip"],
        "posting_notes": "submit within 10 minutes of posting",
    }


def _campaign_posts_list():
    return [
        {
            "title": "Anton explains the flow",
            "hook": "This is how Lovable changed founder-build speed.",
            "script": "Intro.\n\nBody.\n\nOutro.",
            "on_screen_text": "Lovable",
            "description": "Short but real.",
            "hashtags": ["LovablePartner", "AI"],
            "cta": "Follow for more.",
            "ready_to_post": "Anton explains the flow\n\nShort but real.\n\nFollow for more.\n\n#LovablePartner #AI",
        }
    ]


def test_campaign_use_title_starts_generation(campaign_app):
    """The "Use Title As Video Subject" button must actually start generation."""
    from app.services import webui_task

    app = campaign_app
    source = _campaign_source_dict()
    brief = _campaign_brief_dict()
    posts = _campaign_posts_list()

    with patch.object(
        campaign_service, "fetch_campaign_source",
        return_value=campaign_service.CampaignSource.from_dict(source),
    ), patch.object(
        campaign_service, "understand_campaign", return_value=brief,
    ), patch.object(
        campaign_service, "generate_campaign_posts", return_value=posts,
    ), patch.object(config, "app", dict(config.app, video_source="pexels")), patch.object(
        webui_task, "submit_generation", return_value=None,
    ) as submit_mock:
        text_input = next(t for t in app.text_input if t.label == "Campaign link or text")
        text_input.set_value("https://mediamaxxing.notion.site/lovable-clipping")
        app.run()

        fetch_button = next(b for b in app.button if b.label == "Fetch source")
        fetch_button.click()
        app.run()
        assert not app.exception

        understand_button = next(b for b in app.button if b.label == "Understand campaign")
        understand_button.click()
        app.run()
        assert not app.exception

        generate_button = next(b for b in app.button if b.label == "Generate content")
        generate_button.click()
        app.run()
        assert not app.exception

        # 活动面板先于设置表单渲染；但视频源选择框会从既有会话状态取回上次
        # 的 “local”。这里显式切到 pexels，确保点击“用作视频主题”后会进入可
        # 正常提交的生成路径，而不是被本地素材校验拦截。
        source_select = next(s for s in app.selectbox if s.label == "Video Source")
        source_select.set_value("pexels")
        app.run()

        use_title_button = next(
            b for b in app.button if b.label == "Use as video subject"
        )
        use_title_button.click()
        app.run()
        assert not app.exception

        assert submit_mock.call_count == 1, "campaign button must submit generation"
        called_params = submit_mock.call_args.kwargs["params"]
        assert called_params.video_subject == "Anton explains the flow"
        assert "Intro." in called_params.video_script


def test_campaign_panel_renders_empty_state(campaign_app):
    app = campaign_app
    expander_labels = [e.label for e in app.expander]
    assert "Campaign Content Studio" in expander_labels
    text_inputs = [t.label for t in app.text_input]
    assert "Campaign link or text" in text_inputs
    markdown_text = " ".join(str(m.value) for m in app.markdown)
    assert "No campaign source loaded yet" in markdown_text
    assert not app.exception


def test_campaign_fetch_build_and_generate(campaign_app):
    app = campaign_app
    source = _campaign_source_dict()
    brief = _campaign_brief_dict()
    posts = _campaign_posts_list()

    with patch.object(
        campaign_service, "fetch_campaign_source",
        return_value=campaign_service.CampaignSource.from_dict(source),
    ), patch.object(
        campaign_service, "understand_campaign", return_value=brief,
    ), patch.object(
        campaign_service, "generate_campaign_posts", return_value=posts,
    ):
        text_input = next(t for t in app.text_input if t.label == "Campaign link or text")
        text_input.set_value("https://mediamaxxing.notion.site/lovable-clipping")

        fetch_button = next(b for b in app.button if b.label == "Fetch source")
        fetch_button.click()
        app.run()
        assert not app.exception
        markdown_text = " ".join(str(m.value) for m in app.markdown)
        assert "Campaign source" in markdown_text
        assert "podcast" in markdown_text
        caption_text = " ".join(str(c.value) for c in app.caption)
        assert "Lovable Clipping" in caption_text

        understand_button = next(b for b in app.button if b.label == "Understand campaign")
        understand_button.click()
        app.run()
        assert not app.exception
        # 简报与来源素材应展示
        brief_text = " ".join(str(m.value) for m in app.markdown)
        assert "Lovable Clipping" in brief_text
        assert "#LovablePartner" in brief_text

        generate_button = next(b for b in app.button if b.label == "Generate content")
        generate_button.click()
        app.run()
        assert not app.exception
        expander_labels = [e.label for e in app.expander]
        assert any("Anton explains the flow" in label for label in expander_labels)
        markdown_text = " ".join(str(m.value) for m in app.markdown)
        assert "Ready to post" in markdown_text
        code_blocks = [str(c.value) for c in app.code]
        assert any("#LovablePartner" in block for block in code_blocks)