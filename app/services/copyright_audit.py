"""Finished-video copyright-risk audit: provenance in, verdict out.

What this is: for one finished video, list every asset family (footage,
background music, voiceover), say where each came from and which license or
provenance that implies, and roll it up into a single risk verdict.

What this is not: a Content ID pre-check. YouTube runs Content ID only
*after* upload — no local tool, API, or model can clear a video in advance.
The UI must say exactly that (see ``Copyright Disclaimer``), otherwise this
panel would be selling certainty it cannot deliver.

Rules (deliberately coarse — provenance is what we can actually know):

* Stock footage (Pexels / Pixabay / Coverr) — low risk. Royalty-free and
  cleared for YouTube; the note reminds about visible brands and logos.
* AI-generated footage — low risk, with a note to re-check the provider's
  current terms before monetizing.
* Anything the user supplied from disk (local clips, uploaded BGM, full
  custom audio) — high risk. We cannot see a license, so the only honest
  answer is "safe only if you own or licensed it".
* AI-generated music — medium risk. The track is generated, but Content ID
  does flag AI music often enough that "generated" must not read as "safe".
* Missing information (unknown source id, local source with no surviving
  file list) — medium risk. Unknown is not safe; it is unverified.

Pure function, no I/O: ``params`` is the task's ``script.json`` params dict
(or any mapping with the same keys), which already embeds the
``video_materials`` list. Every human sentence lives behind an i18n key;
note templates take at most a ``{name}`` placeholder so every locale formats
with the same call.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    "audit_video_assets",
]

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

_STOCK_SOURCES = frozenset({"pexels", "pixabay", "coverr"})
_AI_VIDEO_SOURCES = frozenset({"wavespeed", "volcengine_seedance", "loomloom"})
_AI_MUSIC_TYPES = frozenset({"sonilo", "elevenlabs"})
_NO_VOICE_NAMES = frozenset({"", "no-voice", "none"})

_RISK_ORDER = {RISK_LOW: 0, RISK_MEDIUM: 1, RISK_HIGH: 2}


def _item(kind, title_key, source_name, risk, note_key):
    return {
        "kind": kind,
        "title_key": title_key,
        "source_name": source_name,
        "risk": risk,
        "note_key": note_key,
    }


def _audit_footage(params: Mapping) -> dict:
    source = str(params.get("video_source", "") or "").strip().lower()
    materials = params.get("video_materials") or []
    local_files = [
        m
        for m in materials
        if isinstance(m, Mapping)
        and str(m.get("provider", "") or "").strip().lower() == "local"
    ]
    if source in _STOCK_SOURCES:
        return _item(
            "footage",
            "Copyright Asset Footage",
            source.capitalize(),
            RISK_LOW,
            "Copyright Note StockFootage",
        )
    if source in _AI_VIDEO_SOURCES:
        return _item(
            "footage",
            "Copyright Asset Footage",
            source,
            RISK_LOW,
            "Copyright Note AIVideo",
        )
    if source == "local":
        if local_files:
            return _item(
                "footage",
                "Copyright Asset Footage",
                "Local files",
                RISK_HIGH,
                "Copyright Note LocalFiles",
            )
        return _item(
            "footage",
            "Copyright Asset Footage",
            "Local files",
            RISK_MEDIUM,
            "Copyright Note LocalUnknown",
        )
    return _item(
        "footage",
        "Copyright Asset Footage",
        source or "Unknown",
        RISK_MEDIUM,
        "Copyright Note UnknownSource",
    )


def _bgm_in_use(params: Mapping) -> bool:
    bgm_type = str(params.get("bgm_type", "") or "").strip().lower()
    if bgm_type in ("", "none"):
        return False
    try:
        return float(params.get("bgm_volume", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _audit_bgm(params: Mapping) -> dict | None:
    if not _bgm_in_use(params):
        return None
    if params.get("bgm_file"):
        return _item(
            "bgm",
            "Copyright Asset BGM",
            "Uploaded file",
            RISK_HIGH,
            "Copyright Note CustomBGM",
        )
    bgm_type = str(params.get("bgm_type", "") or "").strip().lower()
    if bgm_type in _AI_MUSIC_TYPES:
        return _item(
            "bgm",
            "Copyright Asset BGM",
            bgm_type,
            RISK_MEDIUM,
            "Copyright Note AIMusic",
        )
    return _item(
        "bgm",
        "Copyright Asset BGM",
        "Bundled library",
        RISK_LOW,
        "Copyright Note BundledMusic",
    )


def _audit_voice(params: Mapping) -> dict | None:
    if params.get("custom_audio_file"):
        return _item(
            "voice",
            "Copyright Asset Voice",
            "Uploaded file",
            RISK_HIGH,
            "Copyright Note CustomAudio",
        )
    voice_name = str(params.get("voice_name", "") or "").strip().lower()
    if voice_name in _NO_VOICE_NAMES:
        return None
    return _item(
        "voice",
        "Copyright Asset Voice",
        "Synthetic voice",
        RISK_LOW,
        "Copyright Note SyntheticVoice",
    )


def audit_video_assets(params: Mapping | None) -> dict:
    """Audit one finished video's asset provenance.

    :param params: task ``script.json`` params (any mapping). Missing keys
        are treated as unknown, never as safe. Non-mapping input is treated
        as empty for the same reason.
    :returns: ``{"verdict": "low"|"medium"|"high", "items": [...]}``.
    """
    if not isinstance(params, Mapping):
        params = {}
    items = [_audit_footage(params)]
    bgm = _audit_bgm(params)
    if bgm is not None:
        items.append(bgm)
    voice = _audit_voice(params)
    if voice is not None:
        items.append(voice)
    verdict = RISK_LOW
    for item in items:
        if _RISK_ORDER[item["risk"]] > _RISK_ORDER[verdict]:
            verdict = item["risk"]
    return {"verdict": verdict, "items": items}
