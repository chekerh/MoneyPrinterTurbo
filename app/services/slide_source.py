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
import dataclasses
import html
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from loguru import logger

__all__ = [
    "SLIDE_SOURCE",
    "Slide",
    "SlideClipList",
    "SlideImageList",
    "is_slide_source",
    "plan_slides",
    "render_slides",
    "coerce_slides",
    "normalize_slide",
    "slides_to_video_clips",
    "cards_for_duration",
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

#: 一张幻灯片的目标停留时长。这是长视频观感的核心参数：一屏画面停留超过
#: 20 秒，观众就会开始怀疑视频卡住了。15 秒既能读完要点，又足够跟上口播。
TARGET_CARD_SECONDS = 15.0
#: 单段最多拆几张卡。再密就是短视频节奏，长视频会显得躁。
MAX_CARDS_PER_SECTION = 8
#: 单张卡的最长停留时长。超过这个长度，说明段落划分或卡片拆分出了问题，
#: 截断比让一张静止图停一分钟更安全。
MAX_CARD_SECONDS = 60.0
#: 卡片运动的帧率。和成片保持一致，避免拼接阶段再转一次帧率。
CARD_FPS = 30


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
    #: 归属的分段下标（``Section.index``，从 1 开始）。0 表示未归属。
    #: 素材分配靠它把卡片按脚本顺序分回各自段落，而不是靠"前 N 张给前 N 段"。
    section_index: int = 0

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
            "section_index": int(self.section_index or 0),
        }


def is_slide_source(video_source: object) -> bool:
    return str(video_source or "").strip().lower() == SLIDE_SOURCE


# --------------------------------------------------------------------------
# 版式
# --------------------------------------------------------------------------

#: 版式表。刻意用深色底 + 高对比文字：长视频的幻灯片通常要烧进画面下半部分，
#: 深底能让字幕（默认白色带描边）在上面保持可读。
#:
#: 视觉系统只有三件事，其余全部留白：左侧一条随段变色强调边、右上角的卡片计数、
#: 底部一条整片进度线。三者都不承载信息，只负责让画面"看起来在往前走"——静态
#: 文字卡最大的问题不是不好看，而是看不出视频还在播放。
_SLIDE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
  width: 100%; height: 100%; overflow: hidden;
  background: #07090d; color: #f0f6fc;
  font-family: "__FONT_FAMILY__", "PingFang SC", "Hiragino Sans GB",
               "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.slide {
  position: relative; width: 100%; height: 100%;
  display: flex; flex-direction: column;
  justify-content: center; align-items: stretch;
  padding: 8% 9% 10%;
  background:
    radial-gradient(120% 90% at 6% 0%, rgba(88,166,255,.16) 0%, rgba(7,9,13,0) 58%),
    radial-gradient(90% 90% at 100% 100%, rgba(88,166,255,.08) 0%, rgba(7,9,13,0) 60%),
    #0b0f16;
}
/* 左侧强调边：颜色按段落轮转，切换段落时观众能感知到"换章了" */
.accent {
  position: absolute; left: 0; top: 0; bottom: 0; width: 12px;
  background: linear-gradient(180deg, var(--accent) 0%, rgba(0,0,0,0) 92%);
}
.topbar {
  position: absolute; top: 6.4%; left: 9%; right: 9%;
  display: flex; align-items: center; justify-content: space-between;
}
.kicker {
  font-size: 2.6vh; letter-spacing: .2em; text-transform: uppercase;
  color: var(--accent); font-weight: 600;
}
.counter {
  font-size: 2.6vh; letter-spacing: .16em; color: #5f7186;
  font-variant-numeric: tabular-nums;
}
.title {
  font-size: 6.6vh; line-height: 1.16; font-weight: 700;
  color: #ffffff; margin-bottom: 2.2vh; text-wrap: balance;
  max-width: 92%;
}
.rule { width: 12vh; height: .5vh; background: var(--accent); margin-bottom: 3.4vh; opacity: .9; }
.bullets { list-style: none; display: flex; flex-direction: column; gap: 1.9vh; }
.bullets li {
  font-size: 3.7vh; line-height: 1.4; color: #c3ccd8; max-width: 88%;
}
.stat {
  font-size: 16vh; font-weight: 800; line-height: 1;
  color: var(--accent); letter-spacing: -.02em; margin-bottom: 2.4vh;
}
.stat-caption { font-size: 4.2vh; line-height: 1.4; color: #c3ccd8; max-width: 84%; }
.quote {
  font-size: 6.0vh; line-height: 1.3; font-weight: 600;
  color: #ffffff; margin-bottom: 3vh; text-wrap: balance; max-width: 92%;
}
.quote::before { content: "\\201C"; color: var(--accent); }
.quote::after { content: "\\201D"; color: var(--accent); }
.attribution { font-size: 3.2vh; color: #8b949e; }
.compare { display: grid; grid-template-columns: 1fr 1fr; gap: 5%; width: 100%; }
.compare > div {
  padding: 3.2vh 2.8vh; border-radius: 1.2vh;
  background: rgba(255,255,255,.045); border-top: 3px solid var(--accent);
  font-size: 3.5vh; line-height: 1.4; color: #c3ccd8;
}
.compare .label {
  display: block; font-size: 2.6vh; letter-spacing: .14em;
  text-transform: uppercase; color: var(--accent); margin-bottom: 1.6vh;
}
.compare .line + .line { margin-top: 1.1vh; }
.progress {
  position: absolute; left: 0; right: 0; bottom: 0; height: .8vh;
  background: rgba(255,255,255,.07);
}
.progress > span { display: block; height: 100%; background: var(--accent); }
"""

#: 段落强调色轮转表。同一段内的多张卡共用一个颜色，跨段才换色，避免每张卡
#: 都在换色导致"闪烁感"。
_ACCENTS = (
    "#58a6ff",  # blue
    "#3fb950",  # green
    "#d29922",  # amber
    "#bc8cff",  # purple
    "#39c5cf",  # cyan
    "#f778ba",  # pink
)

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


def _coerce_int(value: object) -> int:
    """把模型可能给出的 "1" / "section 1" / 1 都收敛成正整数，失败返回 0。"""
    if value is None or isinstance(value, bool):
        return 0
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else 0


def _lines(value: object) -> list[str]:
    """按行拆分多行文本，丢掉空行。"""
    return [
        line.strip()
        for line in re.split(r"[\r\n]+", str(value or ""))
        if line.strip()
    ]


def _bullet_lines(slide: Slide) -> list[str]:
    return [str(item).strip() for item in slide.bullets if str(item or "").strip()]


def _compare_panels(slide: Slide) -> tuple[list[str], list[str]]:
    """``compare`` 卡的两栏。

    模型经常把两侧内容整段塞进 ``bullets``，而 ``left``/``right`` 留空。照版式
    硬取字段会得到两个只剩 "A" / "B" 标签的空框，所以这里按顺序对半拆开——
    这是模型实际给出的语义顺序，比丢掉内容安全得多。
    """
    left = _lines(slide.left)
    right = _lines(slide.right)
    if left and right:
        return left, right
    lines = left + right + _bullet_lines(slide)
    if len(lines) < 2:
        return lines, []
    middle = (len(lines) + 1) // 2
    return lines[:middle], lines[middle:]


def normalize_slide(slide: Slide) -> Slide:
    """让一张卡的字段与它声明的版式一致。

    模型返回的 JSON 不总守版式：``compare`` 卡把内容塞进 ``bullets``、``stat``
    卡缺 ``stat_caption``。渲染层若照版式取字段，就会输出一张只看得见角落标签
    的卡——这条卡还要在画面里停十几秒，观众只会以为视频坏了。

    规则只有一条：内容能挪到该版式真正读取的字段就挪过去，挪不过去就降级为
    ``section`` 版式。函数返回新的 ``Slide``，不改调用方手上的对象。
    """
    kind = str(slide.kind or "section").strip().lower()
    if kind not in ("stat", "quote", "compare"):
        kind = "section"

    if kind == "stat" and not str(slide.stat or "").strip():
        kind = "section"
    elif kind == "quote" and not str(slide.quote or "").strip():
        kind = "section"
    elif kind == "compare":
        panels = _compare_panels(slide)
        if len(panels[0]) + len(panels[1]) < 2:
            # 只有一条内容，撑不起两栏对比：降级成要点卡，至少文字还在。
            kind = "section"
            slide = dataclasses.replace(slide, bullets=panels[0] or slide.bullets)
        else:
            slide = dataclasses.replace(
                slide,
                left="\n".join(panels[0]),
                right="\n".join(panels[1]),
            )
    return dataclasses.replace(slide, kind=kind)


def _lines_html(lines: Sequence[str]) -> str:
    return "".join(
        f'<div class="line">{_esc(_clamp(line, MAX_BODY_CHARS))}</div>'
        for line in lines
        if str(line or "").strip()
    )


def slide_html(
    slide: Slide,
    video_aspect: str = "16:9",
    *,
    position: int = 0,
    total: int = 0,
) -> str:
    """把一张幻灯片渲染成完整的 HTML 文档。

    ``position`` / ``total`` 只用于页面家具（右上角计数、底部进度线）。它们
    不是内容，缺省时整块不渲染——短视频路径只有一张卡，显示 "1/1" 毫无意义。
    """
    font_stack = _FONT_STACKS.get(str(video_aspect), _FONT_STACKS["16:9"])
    css = _SLIDE_CSS.replace("__FONT_FAMILY__", font_stack)

    slide = normalize_slide(slide)
    accent = _ACCENTS[max(int(slide.section_index or 1) - 1, 0) % len(_ACCENTS)]

    kind = slide.kind
    if kind == "stat":
        caption = _clamp(slide.stat_caption, 160)
        body = f'<div class="stat">{_esc(_clamp(slide.stat, 24))}</div>'
        if caption:
            body += f'<div class="stat-caption">{_esc(caption)}</div>'
    elif kind == "quote":
        attribution = _clamp(slide.attribution, MAX_KICKER_CHARS)
        body = f'<div class="quote">{_esc(_clamp(slide.quote, MAX_BODY_CHARS))}</div>'
        if attribution:
            body += f'<div class="attribution">{_esc(attribution)}</div>'
    elif kind == "compare":
        left_lines, right_lines = _compare_panels(slide)
        body = (
            '<div class="compare">'
            f'<div><span class="label">A</span>{_lines_html(left_lines)}</div>'
            f'<div><span class="label">B</span>{_lines_html(right_lines)}</div>'
            "</div>"
        )
    else:
        bullets = [
            _clamp(item, MAX_BULLET_CHARS)
            for item in _bullet_lines(slide)[:MAX_BULLETS]
        ]
        body = (
            "<ul class='bullets'>"
            + "".join(f"<li>{_esc(item)}</li>" for item in bullets)
            + "</ul>"
            if bullets
            else ""
        )

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
    counter = (
        f'<div class="counter">{int(position):02d} / {int(total):02d}</div>'
        if position and total
        else ""
    )
    progress = ""
    if position and total:
        percent = min(max(position / float(total), 0.0), 1.0) * 100.0
        progress = f'<div class="progress"><span style="width:{percent:.2f}%"></span></div>'

    rule = '<div class="rule"></div>' if heading else ""

    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        f"<style>{css}</style></head>"
        f'<body><div class="slide" style="--accent:{accent}">'
        '<div class="accent"></div>'
        f'<div class="topbar">{kicker}{counter}</div>'
        f"{heading}{rule}{body}"
        f"{progress}"
        "</div></body></html>"
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
            section_index=_coerce_int(
                entry.get("section") or entry.get("section_index") or entry.get("chapter")
            ),
        )
        slide = normalize_slide(slide)
        if _slide_is_empty(slide):
            continue
        slides.append(slide)

    if not slides and minimum > 0:
        logger.warning("slide planning produced no usable slides")
    return slides


def _slide_is_empty(slide: Slide) -> bool:
    """按版式判断这张卡渲染出来到底有没有正文。

    只判 ``bullets`` 是不够的：一张 ``stat`` 卡的内容在 ``stat`` 字段里，把
    别的字段当正文会让空白卡混进成片。
    """
    kind = str(slide.kind or "section").strip().lower()
    if kind == "stat":
        return not str(slide.stat or "").strip()
    if kind == "quote":
        return not str(slide.quote or "").strip()
    if kind == "compare":
        left, right = _compare_panels(slide)
        return len(left) + len(right) < 2
    return not (
        str(slide.title or "").strip()
        or any(_bullet_lines(slide))
    )


def cards_for_duration(seconds: float) -> int:
    """一段口播该拆成几张卡。

    这是长视频观感的第一个决定性参数。旧实现固定"一段一张卡"，于是一张静态
    图要顶 100 秒；观众看到的是一个静止画面加一条在变的字幕。按 15 秒一张
    拆分后，同一段里画面会随着讲述推进而换页。
    """
    try:
        value = float(seconds or 0.0)
    except (TypeError, ValueError):
        return 1
    if value <= 0:
        return 1
    return max(1, min(MAX_CARDS_PER_SECTION, int(round(value / TARGET_CARD_SECONDS))))


def _split_sentences(text: str) -> list[str]:
    return [
        line.strip()
        for line in re.split(r"(?<=[。！？.!?])\s*|\n+", text or "")
        if line.strip()
    ]


def _distribute(items: Sequence[str], count: int) -> list[list[str]]:
    """把一组句子尽量均匀地分成 ``count`` 组，且保证每组至少有一条。"""
    if count <= 0:
        return []
    if not items:
        return [[] for _ in range(count)]
    groups: list[list[str]] = []
    size = max(1, len(items) // count)
    for step in range(count):
        start = step * size
        chunk = list(items[start : start + size])
        if not chunk:
            # 句子数少于卡片数时循环取用，宁可重复也不能出现空白卡。
            chunk = [items[step % len(items)]]
        groups.append(chunk)
    # 余下的句子并入最后一组，避免它们直接消失。
    consumed = size * count
    if consumed < len(items):
        groups[-1].extend(items[consumed:])
    return groups


def _expand_segments(items: Sequence[str], count: int) -> list[str]:
    """把句子切成更多段，直到够 ``count`` 张卡或无法再切。

    一段文案只有一句话时，硬拆成三张卡会让三张卡都显示同一句话。这里改成
    在词边界把最长的一句切开：切出来的两半是**不同**的文字，既能跟上讲述
    推进，也不会出现重复内容。
    """
    segments = [str(item).strip() for item in items if str(item or "").strip()]
    while len(segments) < count:
        target = max(range(len(segments)), key=lambda index: len(segments[index].split()))
        words = segments[target].split()
        if len(words) < 4:
            break
        middle = len(words) // 2
        left = " ".join(words[:middle])
        right = " ".join(words[middle:])
        segments[target : target + 1] = [left, right]
    return segments


def plan_slides(
    sections: Sequence[Any],
    *,
    target_seconds: int,
    per_section: int | None = None,
) -> list[Slide]:
    """
    不调 LLM 兜底规划：按每段时长拆卡，标题用段标题，要点用段文案里的句子。

    ``per_section=None`` 表示按段时长自动决定张数（见 :func:`cards_for_duration`），
    这是长视频的正确行为；显式传整数时所有段用同一个张数，供测试和小脚本使用。
    """
    if not sections:
        return []

    from app.services.longform import Section, plan_section_count

    section_count = plan_section_count(target_seconds) or len(sections)

    slides: list[Slide] = []
    for raw in sections:
        section = raw if isinstance(raw, Section) else Section(index=len(slides) + 1)
        duration = max(float(getattr(section, "duration", 0.0) or 0.0), 0.0)
        body_source = section.text or section.intent or ""
        sentences = _split_sentences(body_source)
        count = (
            max(1, min(int(per_section or 1), MAX_CARDS_PER_SECTION))
            if per_section is not None
            else cards_for_duration(duration)
        )
        # 卡片数不该超过可用内容：句子不够时先把最长的一句在词边界切开。
        segments = _expand_segments(sentences, count)
        count = max(1, min(count, len(segments))) if segments else count
        share = (
            duration / count
            if duration > 0
            else max(1.0, target_seconds / max(len(sections), 1) / count)
        )
        groups = _distribute(segments, count)

        for step, group in enumerate(groups):
            first = step == 0
            slides.append(
                Slide(
                    kind="section",
                    # 首张承担章节标题，后续卡片只保留小标签，避免同一个大标题
                    # 在屏幕上重复六遍。
                    title=section.title if first else "",
                    kicker="" if first else (section.title or ""),
                    bullets=group[:3] or ([section.title] if section.title else []),
                    duration=share,
                    section_index=int(getattr(section, "index", 0) or 0),
                )
            )
    logger.info(
        f"slide fallback plan: {len(slides)} slides for {section_count} sections "
        f"({len(slides) / max(len(sections), 1):.1f} per section)"
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


def _slide_mp4_target(output_dir: str, position: int, seconds: float) -> str:
    """片段文件名里带上时长。

    不这么做的话，续跑会直接复用上一版（每卡 5 秒、无运镜）的旧片段：文件名
    一样、大小也大于 1KB，缓存判定会把它当成"已经渲染过"。时长写进名字以后，
    改版幻灯片布局或改停留时长都会自动重新生成。
    """
    return os.path.join(output_dir, f"slide-{position:03d}-{seconds:05.1f}s.mp4")


class SlideClipList(list):
    """幻灯片片段清单，附带每张卡归属的分段下标。

    素材分配必须知道"这张卡属于第几段"，否则 36 张卡会被"前 6 张给前 6 段、
    其余全塞给最后一段"。清单本身保持 ``list`` 语义，短视频路径和既有调用方
    不需要改动。
    """

    section_owners: list[int]

    def __init__(self, clips=(), section_owners=None):
        super().__init__(clips)
        self.section_owners = list(section_owners or [])


def _motion_filter(width: int, height: int, seconds: float, variant: int) -> str:
    """给静态截图加上缓慢运镜（Ken Burns）。

    静态图停留十几秒会让人以为视频卡住了。真正的解法不是把图换成视频，而是
    让画面在这十几秒里一直在缓慢变化：推、拉、平移三者轮换，幅度小到不抢
    注意力，但足以让眼睛确认"还在播放"。
    """
    frames = max(1, int(round(seconds * CARD_FPS)))
    # 先放大 2 倍再做 zoompan：直接在原分辨率上缩放，1px 的位移会明显发糊。
    pre = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2}"
    )
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    if variant == 1:
        zoom = f"'max(1.14-{0.14 / frames:.8f}*on,1.0)'"
        x, y = center_x, center_y
    elif variant == 2:
        zoom = "'1.12'"
        x, y = f"(iw-iw/zoom)*(on/{frames})", center_y
    elif variant == 3:
        zoom = "'1.12'"
        x, y = f"(iw-iw/zoom)*(1-on/{frames})", center_y
    else:
        zoom = f"'min(1.0+{0.14 / frames:.8f}*on,1.14)'"
        x, y = center_x, center_y
    return (
        f"{pre},zoompan=z={zoom}:x={x}:y={y}"
        f":d={frames}:s={width}x{height}:fps={CARD_FPS},format=yuv420p"
    )


def slides_to_video_clips(
    image_paths: Sequence[str],
    output_dir: str,
    video_aspect: str = "16:9",
    clip_seconds: float = 5.0,
    durations: Sequence[float] | None = None,
    section_indices: Sequence[int] | None = None,
) -> SlideClipList:
    """
    把幻灯片 PNG 转成视频片段，每张卡的时长由它自己覆盖的口播时长决定。

    为什么必须有这一步：``video.combine_videos`` 只接受**视频**文件，图片路径
    会被 MoviePy 当成视频去探测，直接抛
    ``Error passing `ffmpeg -i` command output: ... At least one output file
    must be specified``，整条任务卡在最后一阶段。项目里原有的图片→视频转换
    只存在于 ``video.preprocess_video``，而那条路径只处理
    ``storage/local_videos`` 下的本地素材，任务目录里的幻灯片截图够不着，
    所以短视频配幻灯片源其实一直是坏的，只是没人跑到过。

    ``durations`` 给出每张卡的真实停留时长，此时片段长度就等于该卡覆盖的
    口播长度，拼接阶段不会再把同一张图循环十几遍（那是长视频变成"静态图 +
    字幕"的直接原因）。缺省或为 0 时退回固定 ``clip_seconds``，保持短视频
    行为不变。``section_indices`` 与 ``image_paths`` 等长，用来告诉下游每张
    卡属于第几段；转换失败被跳过的卡片会连同归属一起跳过。

    返回顺序与 ``image_paths`` 一致，断点续跑时直接复用已转换的片段。
    """
    if not image_paths:
        return SlideClipList()

    target_dir = os.path.join(output_dir, "slides-mp4")
    os.makedirs(target_dir, exist_ok=True)

    width, height = _resolution(video_aspect)
    fallback_seconds = max(float(clip_seconds or 0.0), 1.0)

    ffmpeg_binary = ""
    clips: list[str] = []
    owners: list[int] = []
    for position, image_path in enumerate(image_paths, start=1):
        seconds = fallback_seconds
        if durations is not None and position - 1 < len(durations):
            try:
                candidate = float(durations[position - 1] or 0.0)
            except (TypeError, ValueError):
                candidate = 0.0
            # 0 表示"这张卡没有独立时长"（短视频路径），而不是"停一秒"。
            if candidate > 0:
                seconds = candidate
        seconds = min(max(seconds, 1.0), MAX_CARD_SECONDS)

        target = _slide_mp4_target(target_dir, position, seconds)
        owner = 0
        if section_indices is not None and position - 1 < len(section_indices):
            owner = int(section_indices[position - 1] or 0)

        if os.path.exists(target) and os.path.getsize(target) > 1024:
            clips.append(target)
            owners.append(owner)
            continue

        if not ffmpeg_binary:
            # 延迟导入：本模块只做渲染，不该在导入期把 config/utils 拉起来。
            from app.utils import utils

            ffmpeg_binary = utils.get_ffmpeg_binary()

        command = [
            ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            image_path,
            "-frames:v",
            str(max(1, int(round(seconds * CARD_FPS)))),
            "-vf",
            _motion_filter(width, height, seconds, position - 1),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            target,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if (
            result.returncode != 0
            or not os.path.exists(target)
            or os.path.getsize(target) <= 1024
        ):
            logger.error(
                f"failed to convert slide to video clip: {image_path}, "
                f"returncode={result.returncode}, stderr={(result.stderr or '')[-400:]}"
            )
            # 半成品文件会让下次续跑误判为已完成，必须删掉。
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
            continue

        clips.append(target)
        owners.append(owner)

    if len(clips) != len(image_paths):
        logger.warning(
            f"only {len(clips)}/{len(image_paths)} slides converted to video clips"
        )
    return SlideClipList(clips, section_owners=owners)


class SlideImageList(list):
    """渲染成功的幻灯片路径清单，附带对应的 ``Slide`` 对象。

    渲染可能单张失败并跳过，返回的路径数与 ``slides`` 不再一一对应。把
    成功的那几张连同它们的 ``Slide`` 一起带出来，下游才能拿到每张卡真正
    该停多久、属于哪一段，而不是按下标去猜。清单本身仍是 ``list[str]``。
    """

    slides: list["Slide"]

    def __init__(self, paths=(), slides=None):
        super().__init__(paths)
        self.slides = list(slides or [])


def render_slides(
    slides: Sequence[Slide],
    output_dir: str,
    video_aspect: str = "16:9",
) -> SlideImageList:
    """
    把幻灯片渲染成 PNG 列表。失败返回空列表。

    整批失败（浏览器起不来、没有可用 Chromium）才返回空列表；单张失败只丢那一张，
    因为丢掉一张会留一个几秒的画面空档，丢整批则直接让任务失败。
    """
    if not slides:
        return SlideImageList()

    output_dir = os.path.join(output_dir, "slides")
    os.makedirs(output_dir, exist_ok=True)

    width, height = _resolution(video_aspect)
    pages: list[str] = []
    paths: list[str] = []
    total = len(slides)
    for position, slide in enumerate(slides, start=1):
        target = os.path.join(output_dir, f"slide-{position:03d}.png")
        pages.append(
            slide_html(slide, video_aspect, position=position, total=total)
        )
        paths.append(target)

    logger.info(f"rendering {len(slides)} slides at {width}x{height} into {output_dir}")
    try:
        asyncio.run(_screenshot_all(pages, paths, width, height))
    except Exception as exc:  # noqa: BLE001 - 失败必须带可操作信息
        logger.error(
            f"failed to render slides with Playwright: {type(exc).__name__}: {exc}. "
            "Run `playwright install chromium` if the browser binary is missing."
        )
        return SlideImageList()

    pairs = [
        (path, slide)
        for path, slide in zip(paths, slides)
        if os.path.exists(path) and os.path.getsize(path) > 1024
    ]
    if len(pairs) != len(slides):
        logger.warning(
            f"only {len(pairs)}/{len(slides)} slides rendered successfully"
        )
    return SlideImageList(
        [path for path, _ in pairs],
        slides=[slide for _, slide in pairs],
    )
