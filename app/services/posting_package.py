"""Persistent posting copy shared by generation, the library and upload tools."""

from __future__ import annotations

import json
import re
from pathlib import Path

from loguru import logger

from app.services.task_artifacts import _write_json_atomic


def fallback(
    subject: str, script: str = "", channel: str = "", task_id: str = ""
) -> dict:
    return {
        "version": 1,
        "task_id": task_id,
        "channel": channel,
        "title": str(subject).strip()[:100],
        "caption": str(script or subject).strip()[:5000],
        "hashtags": ["#Shorts"],
        "comment": "",
        "copy_source": "fallback",
    }


def build(
    subject: str,
    script: str,
    language: str,
    channel: str,
    task_id: str,
    *,
    sources: list | None = None,
) -> dict:
    """Create copy even when publishing is disconnected or the LLM is unavailable."""
    from app.services import llm

    package = fallback(subject, script, channel, task_id)
    try:
        metadata = llm.generate_social_metadata(
            video_subject=subject,
            video_script=script,
            language=language,
            platform="youtube_shorts",
        )
        if isinstance(metadata, dict):
            title = metadata.get("title")
            caption = metadata.get("caption")
            if isinstance(title, str) and title.strip():
                package["title"] = title.strip()[:100]
            if isinstance(caption, str) and caption.strip():
                package["caption"] = caption.strip()[:5000]
            tags = metadata.get("hashtags")
            if isinstance(tags, list):
                package["hashtags"] = list(
                    dict.fromkeys(
                        "#" + tag.strip().lstrip("#")
                        for tag in tags
                        if isinstance(tag, str) and re.fullmatch(r"#?\w+", tag.strip())
                    )
                )[:15]
            package["copy_source"] = "generated_or_provider_fallback"
    except Exception as exc:
        logger.warning(f"posting copy unavailable; using script: {type(exc).__name__}")

    urls = list(
        dict.fromkeys(
            str(source.get("source_page", "")).strip()
            for source in (sources or [])
            if isinstance(source, dict)
            and str(source.get("source_page", "")).startswith(("https://", "http://"))
        )
    )
    if urls:
        package["comment"] = "Footage sources:\n" + "\n".join(urls)
    return package


def render_text(package: dict) -> str:
    return (
        f"TITLE\n{package['title']}\n\n"
        f"DESCRIPTION / CAPTION\n{package['caption']}\n\n"
        f"HASHTAGS\n{' '.join(package['hashtags'])}\n\n"
        f"COMMENT DRAFT (post and pin manually if wanted)\n{package['comment']}\n"
    )


def save_for_video(video: str | Path, package: dict) -> None:
    video = Path(video)
    _write_json_atomic(video.with_suffix(".posting.json"), package)
    video.with_suffix(".posting.txt").write_text(render_text(package), encoding="utf-8")


def load_for_video(video: str | Path, channel: str = "", subject: str = "") -> dict:
    """Read saved copy; old archives get an offline fallback from their script."""
    video = Path(video)
    saved = video.with_suffix(".posting.json")
    if saved.exists():
        try:
            package = json.loads(saved.read_text(encoding="utf-8"))
            if not isinstance(package, dict) or package.get("version") != 1:
                raise ValueError("unsupported version")
            for key in ("title", "caption", "comment", "channel", "task_id"):
                if not isinstance(package.get(key), str):
                    raise ValueError(f"invalid {key}")
            if not isinstance(package.get("hashtags"), list) or any(
                not isinstance(tag, str) for tag in package["hashtags"]
            ):
                raise ValueError("invalid hashtags")
            return package
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid posting package: {saved.name}") from exc

    script = ""
    task_id = ""
    try:
        legacy = video.with_suffix(".txt").read_text(encoding="utf-8")
        topic = re.search(r"^topic: (.+)$", legacy, re.MULTILINE)
        identifier = re.search(r"^task_id: (.+)$", legacy, re.MULTILINE)
        narration = re.search(
            r"\nscript:\n(.*?)(?:\n\nsearch terms:|\Z)", legacy, re.DOTALL
        )
        subject = topic.group(1) if topic else subject
        task_id = identifier.group(1) if identifier else ""
        script = narration.group(1).strip() if narration else ""
    except FileNotFoundError:
        pass
    return fallback(subject or video.stem, script, channel, task_id)
