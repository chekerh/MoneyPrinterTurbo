import json
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


def _campaign_source_dict():
    return {
        "kind": "notion",
        "url": "https://mediamaxxing.notion.site/lovable-clipping",
        "title": "Lovable Clipping",
        "text": "Main goal is tier1 views.\nMust show Anton and/or Lovable.\n",
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


@pytest.fixture
def campaign_save_app(tmp_path, monkeypatch):
    applications_dir = ROOT_DIR / "test" / "test_data" / "media_library_app"
    plans_root = applications_dir / "plans"
    outputs_root = applications_dir / "outputs"
    (plans_root / "testch").mkdir(parents=True, exist_ok=True)
    (outputs_root / "testch").mkdir(parents=True, exist_ok=True)
    saved_root = tmp_path / "campaigns"
    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(plans_root))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(outputs_root))
    # Main.py 经 `from app.services import campaign as campaign_service` 使用同一
    # 模块对象；patch 存档根目录函数，避免把测试活动写进开发者的真实 storage。
    monkeypatch.setattr(
        campaign_service, "saved_campaigns_root", lambda create=False: str(saved_root)
    )

    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception

    return app, saved_root


def test_campaign_accept_persists_source_brief_and_posts(campaign_save_app):
    app, saved_root = campaign_save_app
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
        app.run()

        next(b for b in app.button if b.label == "Fetch source").click()
        app.run()
        assert not app.exception

        next(b for b in app.button if b.label == "Understand campaign").click()
        app.run()
        assert not app.exception

        next(b for b in app.button if b.label == "Generate content").click()
        app.run()
        assert not app.exception

        accept_button = next(b for b in app.button if b.label == "Accept campaign")
        accept_button.click()
        app.run()

    assert not app.exception
    saved_files = sorted(saved_root.glob("*.json"))
    assert len(saved_files) == 1
    payload = json.loads(saved_files[0].read_text(encoding="utf-8"))
    campaign_id = payload["campaign_id"]
    assert payload["source"]["url"] == source["url"]
    assert payload["brief"]["campaign_name"] == "Lovable Clipping"
    assert len(payload["posts"]) == 1
    assert (saved_root / f"{campaign_id}.md").is_file()

    assert app.session_state["campaign_saved_last"]["campaign_id"] == campaign_id
    toasts = [str(t.value) for t in app.get("toast")]
    assert any("Campaign accepted." in toast for toast in toasts)


def test_campaign_loader_appears_after_accept(campaign_save_app):
    app, saved_root = campaign_save_app
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
        app.run()
        next(b for b in app.button if b.label == "Fetch source").click()
        app.run()
        next(b for b in app.button if b.label == "Understand campaign").click()
        app.run()
        next(b for b in app.button if b.label == "Generate content").click()
        app.run()
        next(b for b in app.button if b.label == "Accept campaign").click()
        app.run()
        # st.rerun() 后再跑一次，加载器应已列出刚接受的活动。
        app.run()

    assert not app.exception
    selectbox_labels = [s.label for s in app.selectbox]
    assert "Saved campaigns" in selectbox_labels
    caption = next(
        c for c in app.caption if "Saved as" in c.value and "lovable-clipping" in c.value
    )
    assert caption


def test_campaign_loader_restores_saved_campaign(campaign_save_app):
    app, saved_root = campaign_save_app
    campaign_service.save_campaign(
        campaign_service.CampaignSource.from_dict(_campaign_source_dict()),
        _campaign_brief_dict(),
        _campaign_posts_list(),
        campaign_id="love-clip",
        root=str(saved_root),
    )

    app.run()
    selectbox = next(s for s in app.selectbox if s.label == "Saved campaigns")
    # 选项值为 f"{name} ({saved_at})" 标签，选第一个（最新）即对应 love-clip。
    selectbox.set_value(selectbox.options[0])
    app.run()

    load_button = next(b for b in app.button if b.label == "Load saved campaign")
    load_button.click()
    app.run()
    # load 成功后 st.rerun()，重新渲染的活动面板应展示已保存的简报与文案。
    app.run()

    assert not app.exception
    assert app.session_state["campaign_source"]["title"] == "Lovable Clipping"
    assert app.session_state["campaign_brief"]["campaign_name"] == "Lovable Clipping"
    assert app.session_state["campaign_posts"][0]["title"] == "Anton explains the flow"
    markdown_text = " ".join(str(m.value) for m in app.markdown)
    assert "Ready to post" in markdown_text
    assert "Lovable Clipping" in markdown_text