"""Rule matrix for the finished-video copyright-risk audit.

The audit is provenance-based: it can only judge what the task record says
about each asset family. Every case below pins one rule so a future edit
that silently weakens a verdict (e.g. "unknown means safe") fails loudly.
"""

from app.services.copyright_audit import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    audit_video_assets,
)


def _params(**overrides):
    base = {
        "video_source": "pexels",
        "bgm_type": "random",
        "bgm_volume": 0.2,
        "voice_name": "en-US-AvaMultilingualNeural",
    }
    base.update(overrides)
    return base


class TestFootageRules:
    def test_stock_footage_is_low_risk(self):
        report = audit_video_assets(_params(video_source="pexels"))
        footage = next(i for i in report["items"] if i["kind"] == "footage")
        assert footage["risk"] == RISK_LOW

    def test_ai_generated_footage_is_low_risk(self):
        report = audit_video_assets(_params(video_source="wavespeed"))
        footage = next(i for i in report["items"] if i["kind"] == "footage")
        assert footage["risk"] == RISK_LOW

    def test_user_supplied_files_are_high_risk(self):
        report = audit_video_assets(
            _params(
                video_source="local",
                video_materials=[{"provider": "local", "url": "/tmp/a.mp4"}],
            )
        )
        footage = next(i for i in report["items"] if i["kind"] == "footage")
        assert footage["risk"] == RISK_HIGH

    def test_local_source_without_file_list_is_not_called_safe(self):
        report = audit_video_assets(
            _params(video_source="local", video_materials=[])
        )
        footage = next(i for i in report["items"] if i["kind"] == "footage")
        assert footage["risk"] == RISK_MEDIUM

    def test_unknown_source_is_not_called_safe(self):
        report = audit_video_assets(_params(video_source="mystery"))
        footage = next(i for i in report["items"] if i["kind"] == "footage")
        assert footage["risk"] == RISK_MEDIUM


class TestBgmRules:
    def test_silent_video_has_no_bgm_item(self):
        report = audit_video_assets(_params(bgm_volume=0))
        assert [i for i in report["items"] if i["kind"] == "bgm"] == []

    def test_bundled_music_is_low_risk(self):
        report = audit_video_assets(_params(bgm_type="random", bgm_volume=0.2))
        bgm = next(i for i in report["items"] if i["kind"] == "bgm")
        assert bgm["risk"] == RISK_LOW

    def test_ai_music_is_medium_risk(self):
        """Generated does not mean unclaimable — Content ID flags AI tracks."""
        report = audit_video_assets(_params(bgm_type="sonilo", bgm_volume=0.2))
        bgm = next(i for i in report["items"] if i["kind"] == "bgm")
        assert bgm["risk"] == RISK_MEDIUM

    def test_uploaded_music_file_is_high_risk(self):
        report = audit_video_assets(
            _params(bgm_type="random", bgm_volume=0.2, bgm_file="mysong.mp3")
        )
        bgm = next(i for i in report["items"] if i["kind"] == "bgm")
        assert bgm["risk"] == RISK_HIGH


class TestVoiceRules:
    def test_synthetic_voice_is_low_risk(self):
        report = audit_video_assets(_params())
        voice = next(i for i in report["items"] if i["kind"] == "voice")
        assert voice["risk"] == RISK_LOW

    def test_full_custom_audio_is_high_risk(self):
        report = audit_video_assets(_params(custom_audio_file="/tmp/v.mp3"))
        voice = next(i for i in report["items"] if i["kind"] == "voice")
        assert voice["risk"] == RISK_HIGH

    def test_no_voiceover_has_no_voice_item(self):
        report = audit_video_assets(_params(voice_name="no-voice"))
        assert [i for i in report["items"] if i["kind"] == "voice"] == []


class TestVerdict:
    def test_verdict_is_the_highest_item_risk(self):
        report = audit_video_assets(
            _params(
                video_source="local",
                video_materials=[{"provider": "local", "url": "/tmp/a.mp4"}],
            )
        )
        assert report["verdict"] == RISK_HIGH

    def test_clean_synthetic_video_is_low_risk(self):
        assert audit_video_assets(_params())["verdict"] == RISK_LOW

    def test_empty_params_are_not_called_safe(self):
        assert audit_video_assets({})["verdict"] == RISK_MEDIUM
        assert audit_video_assets(None)["verdict"] == RISK_MEDIUM
