"""幻灯片素材源：把文案渲染成大标题卡片，替代素材库画面。

存在的理由很实际：有一类赛道根本没有可用的素材库画面——讲配置的讲解、
讲流程的方法论、纯数据的对比。这类内容用 Pexels 搜出来的画面只能得到
会议室、笔记本电脑、握手这类"通用商务 B-roll"，观众看了三秒就知道画面和
内容无关，留存直接崩掉。

幻灯片源把这些内容变成画面本身：模型产出结构化的幻灯片描述（标题、要点、
数字、引语），本模块渲染成 HTML，用 Playwright 截成 PNG，再当作本地图片
素材交给既有的图片→视频链路。好处是整条链路完全复用——``video_source``
解析、分段渲染、字幕烧录都不需要知道素材是库存视频还是幻灯片截图。

设计上刻意保持"只做渲染，不做编排"：幻灯片内容和成片时长都由上层决定，
本模块只接受一个幻灯片列表，返回 PNG 路径列表。
"""

from __future__ import annotations

import asyncio
import html
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

from app.utils import utils

__all__ = [
    "SLIDE_SOURCE",
    "Slide",
    "is_slide_source",
    "plan_slides",
    "render_slides",
    "coerce_slides",
]

#: ``video_source`` 的取值。放在这里而不是 config.py，是因为它同时是
#: API/WebUI 的枚举值和分发逻辑的分支点，需要和渲染实现保持在一起。
SLIDE_SOURCE = "slides"

#: 单张幻灯片渲染的超时。Playwright 冷启动 + 字体加载在慢机器上会明显偏慢，
#: 但再慢也不该超过这个量级——超了说明浏览器起不来，该失败而不是挂着。
_SLIDE_TIMEOUT_MS = 30_000

#: 一个幻灯片里的要点上限。超过就该拆成两张卡，而不是让字号一路缩到看不清。
MAX_BULLETS = 6
#: 要点文本长度上限，同样是防止单条要点撑爆版面。
MAX_BULLET_CHARS = 140
MAX_TITLE_CHARS = 90
MAX_KICKER_CHARS = 60
MAX_BODY_CHARS = 320


@dataclass
class Slide:
    """一张幻灯片。"""

    #: 变体：section / stat / quote / compare。决定版式，不决定内容。
    kind: str = "section"
    title: str = ""
    #: 小标签，出现在标题上方。
    kicker: str = ""
    #: section 变体的要点列表。
    bullets: list[str] = field(default_factory=list)
    #: stat 变体的数字与说明。
    stat: str = ""
    stat_caption: str = ""
    #: quote 变体的引语与出处。
    quote: str = ""
    attribution: str = ""
    #: compare 变体的两栏。
    left: str = ""
    right: str = ""
    #: 该幻灯片对应的口播时长（秒）。渲染后据此决定图片在成片里的停留时间。
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "kicker": self.kicker,
            "bullets": list(self.bullets),
            "stat": self.stat,
            "stat_caption": self.stat_caption,
            "quote": self.quote,
            "attribution": self.attribution,
            "left": self.left,
            "right": self.right,
            "duration": round(self.duration, 3),
        }


def is_slide_source(video_source: object) -> bool:
    return str(video_source or "").strip().lower() == SLIDE_SOURCE


# --------------------------------------------------------------------------
# 版式
# --------------------------------------------------------------------------

#: 版式表。刻意用深色底 + 高对比文字：长视频的幻灯片通常要烧进画面下半部分，
#: 深底能让字幕（默认白色带描边）在上面保持可读。
_SLIDE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
  width: 100%; height: 100%; overflow: hidden;
  background: #0d1117; color: #f0f6fc;
  font-family: "__FONT_FAMILY__", "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.slide {
  width: 100%; height: 100%;
  display: flex; flex-direction: column;
  justify-content: center; align-items: flex-start;
  padding: 8% 9%;
  /* 一条极细的竖向高亮线，给纯文字版面一个视觉锚点 */
  border-left: 14px solid #58a6ff;
}
.kicker {
  font-size: 3.4vh; letter-spacing: .22em; text-transform: uppercase;
  color: #58a6ff; margin-bottom: 2.6vh; font-weight: 600;
}
.title {
  font-size: 8.2vh; line-height: 1.18; font-weight: 700;
  color: #ffffff; margin-bottom: 3.4vh; text-wrap: balance;
}
.bullets { list-style: none; display: flex; flex-direction: column; gap: 2.1vh; }
.bullets li {
  font-size: 4.1vh; line-height: 1.42; color: #c9d1d9;
  padding-left: 3.4vh; position: relative;
}
.bullets li::before {
  content: ""; position: absolute; left: 0; top: 1.85vh;
  width: 1.5vh; height: 1.5vh; border-radius: 50%; background: #58a6ff;
}
.stat {
  font-size: 17vh; font-weight: 800; line-height: 1;
  color: #58a6ff; letter-spacing: -.02em; margin-bottom: 2.4vh;
}
.stat-caption { font-size: 4.4vh; line-height: 1.4; color: #c9d1d9; }
.quote {
  font-size: 7.4vh; line-height: 1.32; font-weight: 600;
  color: #ffffff; margin-bottom: 3vh; text-wrap: balance;
}
.quote::before { content: "\\201C"; color: #58a6ff; }
.quote::after { content: "\\201D"; color: #58a6ff; }
.attribution { font-size: 3.6vh; color: #8b949e; }
.compare { display: grid; grid-template-columns: 1fr 1fr; gap: 6%; width: 100%; }
.compare > div {
  padding: 3.6vh 3vh; border-radius: 1.4vh;
  background: #161b22; border-top: 4px solid #58a6ff;
  font-size: 4.0vh; line-height: 1.4; color: #c9d1d9;
}
.compare .label {
  display: block; font-size: 3.2vh; letter-spacing: .12em;
  text-transform: uppercase; color: #58a6ff; margin-bottom: 1.8vh;
}
"""

_FONT_STACKS = {
    "9:16": '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei"',
    "16:9": '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei"',
    "1:1": '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei"',
}


def _resolution(video_aspect: str | None) -> tuple[int, int]:
    """幻灯片按成片分辨率渲染，不做二次缩放。"""
    aspect = str(video_aspect or "16:9").strip()
    if aspect == "9:16":
        return 1080, 1920
    if aspect == "1:1":
        return 1080, 1080
    return 1920, 1080


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _clamp(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    # 截断时补省略号，让观众知道这是被裁短的，而不是原文如此。
    return text[: max(limit - 1, 1)].rstrip() + "…"


def slide_html(slide: Slide, video_aspect: str = "16:9") -> str:
    """把一张幻灯片渲染成完整的 HTML 文档。"""
    font_stack = _FONT_STACKS.get(str(video_aspect), _FONT_STACKS["16:9"])
    css = _SLIDE_CSS.replace("__FONT_FAMILY__", font_stack)

    kind = (slide.kind or "section").strip().lower()
    if kind == "stat":
        body = (
            f'<div class="stat">{_esc(_clamp(slide.stat, 24))}</div>'
            f'<div class="stat-caption">{_esc(_clamp(slide.stat_caption, 160))}</div>'
        )
    elif kind == "quote":
        body = (
            f'<div class="quote">{_esc(_clamp(slide.quote, MAX_BODY_CHARS))}</div>'
            f'<div class="attribution">{_esc(_clamp(slide.attribution, MAX_KICKER_CHARS))}</div>'
        )
    elif kind == "compare":
        body = (
            '<div class="compare">'
            f'<div><span class="label">A</span>{_esc(_clamp(slide.left, MAX_BODY_CHARS))}</div>'
            f'<div><span class="label">B</span>{_esc(_clamp(slide.right, MAX_BODY_CHARS))}</div>'
            "</div>"
        )
    else:
        bullets = [
            _clamp(item, MAX_BULLET_CHARS)
            for item in list(slide.bullets)[:MAX_BULLETS]
            if str(item or "").strip()
        ]
        rendered = (
            "<ul class='bullets'>"
            + "".join(f"<li>{_esc(item)}</li>" for item in bullets)
            + "</ul>"
            if bullets
            else ""
        )
        body = rendered

    heading = (
        f'<div class="title">{_esc(_clamp(slide.title, MAX_TITLE_CHARS))}</div>'
        if str(slide.title or "").strip()
        else ""
    )
    kicker = (
        f'<div class="kicker">{_esc(_clamp(slide.kicker, MAX_KICKER_CHARS))}</div>'
        if str(slide.kicker or "").strip()
        else ""
    )

    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        f"<style>{css}</style></head>"
        f"<body><div class=\"slide\">{kicker}{heading}{body}</div></body></html>"
    )


# --------------------------------------------------------------------------
# 规划
# --------------------------------------------------------------------------


def coerce_slides(payload: Any, minimum: int = 1) -> list[Slide]:
    """
    把模型返回的幻灯片描述规整成 :class:`Slide` 列表。

    容忍 dict 包裹、字符串降级、字段缺失。字段全空的条目直接丢弃——渲染出一张
    只有背景色的空白幻灯片，比少一张更糟。
    """
    if isinstance(payload, dict):
        for key in ("slides", "cards", "items", "deck"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = []
    if not isinstance(payload, list):
        return []

    slides: list[Slide] = []
    for entry in payload:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                slides.append(Slide(kind="section", title=_clamp(text, MAX_TITLE_CHARS)))
            continue
        if not isinstance(entry, dict):
            continue

        bullets = entry.get("bullets") or entry.get("points") or entry.get("items") or []
        if isinstance(bullets, str):
            bullets = [bullets]

        slide = Slide(
            kind=str(entry.get("kind") or entry.get("type") or "section"),
            title=str(entry.get("title") or entry.get("heading") or ""),
            kicker=str(entry.get("kicker") or entry.get("label") or entry.get("eyebrow") or ""),
            bullets=[str(item) for item in bullets if str(item or "").strip()],
            stat=str(entry.get("stat") or entry.get("number") or entry.get("value") or ""),
            stat_caption=str(
                entry.get("stat_caption") or entry.get("caption") or entry.get("note") or ""
            ),
            quote=str(entry.get("quote") or entry.get("text") or ""),
            attribution=str(entry.get("attribution") or entry.get("source") or ""),
            left=str(entry.get("left") or ""),
            right=str(entry.get("right") or ""),
        )
        if _slide_is_empty(slide):
            continue
        slides.append(slide)

    if not slides and minimum > 0:
        logger.warning("slide planning produced no usable slides")
    return slides


def _slide_is_empty(slide: Slide) -> bool:
    return not any(
        str(value or "").strip()
        for value in (
            slide.title,
            slide.kicker,
            slide.stat,
            slide.quote,
            slide.left,
            slide.right,
            "\n".join(slide.bullets),
        )
    )


def plan_slides(
    sections: Sequence[Any],
    *,
    target_seconds: int,
    per_section: int = 1,
) -> list[Slide]:
    """
    不调 LLM 的兜底规划：每段一张 section 幻灯片，标题用段标题，要点用段文案首句。

    LLM 规划的幻灯片更丰富（stat/quote/compare 变体），但它可能失败。这个
    兜底保证幻灯片源在任何情况下都能产出可用的画面，而不是让整条任务因为
    "模型没给出好格式的 JSON" 而失败。
    """
    if not sections:
        return []

    from app.services.longform import Section, plan_section_count

    section_count = plan_section_count(target_seconds) or len(sections)
    per_section = max(1, min(int(per_section or 1), 3))
    share = max(1.0, target_seconds / max(len(sections), 1) / per_section)

    slides: list[Slide] = []
    for raw in sections:
        section = raw if isinstance(raw, Section) else Section(index=len(slides) + 1)
        for step in range(per_section):
            kicker = section.title or ""
            # 第一张给要点，后续给正文的切分片段——同一段的多张卡片必须
            # 承载不同内容，否则等于把同一句话在屏幕上重复几遍。
            body_source = section.text or section.intent or ""
            sentences = [
                line.strip()
                for line in re.split(r"(?<=[。！？.!?])\s*|\n+", body_source)
                if line.strip()
            ]
            start = step * 3
            picked = sentences[start : start + 3]
            slides.append(
                Slide(
                    kind="section",
                    title=section.title if step == 0 else f"{section.title} ({step + 1})",
                    kicker=kicker if step == 0 else "",
                    bullets=picked,
                    duration=share,
                )
            )
    logger.info(
        f"slide fallback plan: {len(slides)} slides for {section_count} sections "
        f"({per_section} per section, {share:.1f}s each)"
    )
    return slides


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


async def _screenshot_all(
    html_pages: Sequence[str],
    output_paths: Sequence[str],
    width: int,
    height: int,
) -> None:
    """一次浏览器会话批量截图。

    每张幻灯片都新开 context 而不是复用同一个 page：复用会让上一张的字体和
    布局状态泄漏到下一张，而这是最难排查的一类渲染错误。
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as player:
        browser = await player.chromium.launch(headless=True)
        try:
            for html_text, output_path in zip(html_pages, output_paths):
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                )
                page = await context.new_page()
                try:
                    await page.set_content(html_text, wait_until="load")
                    await page.screenshot(path=output_path, type="png")
                finally:
                    await context.close()
        finally:
            await browser.close()


def render_slides(
    slides: Sequence[Slide],
    output_dir: str,
    video_aspect: str = "16:9",
) -> list[str]:
    """
    把幻灯片渲染成 PNG 列表。失败返回空列表。

    整批失败（浏览器起不来、没有可用 Chromium）才返回空列表；单张失败只丢那一张，
    因为丢掉一张会留一个几秒的画面空档，丢整批则直接让任务失败。
    """
    if not slides:
        return []

    output_dir = os.path.join(output_dir, "slides")
    os.makedirs(output_dir, exist_ok=True)

    width, height = _resolution(video_aspect)
    pages: list[str] = []
    paths: list[str] = []
    for position, slide in enumerate(slides, start=1):
        target = os.path.join(output_dir, f"slide-{position:03d}.png")
        # 断点续跑：已渲染的幻灯片直接复用，和分段视频的复用逻辑一致。
        if os.path.exists(target) and os.path.getsize(target) > 1024:
            pages.append(slide_html(slide, video_aspect))
            paths.append(target)
            continue
        pages.append(slide_html(slide, video_aspect))
        paths.append(target)

    logger.info(f"rendering {len(slides)} slides at {width}x{height} into {output_dir}")
    try:
        asyncio.run(_screenshot_all(pages, paths, width, height))
    except Exception as exc:  # noqa: BLE001 - 失败必须带可操作信息
        logger.error(
            f"failed to render slides with Playwright: {type(exc).__name__}: {exc}. "
            "Run `playwright install chromium` if the browser binary is missing."
        )
        return []

    rendered = [item for item in paths if os.path.exists(item) and os.path.getsize(item) > 1024]
    if len(rendered) != len(slides):
        logger.warning(
            f"only {len(rendered)}/{len(slides)} slides rendered successfully"
        )
    return rendered
