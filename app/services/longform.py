"""长视频规划：把"一次性"的短视频流水线扩成"按段推进"的长视频流水线。

短视频流水线的每一段都是一次性的：一次 LLM 调用出一份脚本、一次 TTS 出整段
旁白、固定 5~8 个搜索关键词。25 分钟的视频需要约 3500 词、约 25 次 TTS 请求、
上百个素材关键词——**"一次性"这个假设才是长视频跑不起来的真正原因**，不是
ffmpeg 也不是素材源。

本模块只负责规划：目标时长 → 分段大纲 → 每段文案。它不去改写 ``task.py``
的六个阶段，而是给每一阶段提供一个按段推进的版本；``short`` 时长度的旧路径
一行都不走，所以短视频的既有行为完全不变。

分段粒度的取舍：一段太长，单次 TTS 仍然可能撞上服务商上限，渲染也仍然是一次
巨大的 MoviePy pass；一段太短，LLM 调用次数线性增长，而且每段都要单独下载素材。
按约 75 秒一段切，medium 约 10 段、long 约 20 段，落在两边都能接受的位置。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

__all__ = [
    "LENGTH_BANDS",
    "LengthBand",
    "Section",
    "SectionPlan",
    "is_longform",
    "resolve_band",
    "resolve_target_seconds",
    "plan_section_count",
    "words_for_target",
    "chars_per_section",
    "build_plan",
    "render_sections_as_script",
]

#: 口播速度（英文口径，词/秒）。与 niche_brand.NicheProfile.target_words 的
#: 注释保持一致：约 2.5 词/秒。中文大约 4~5 字/秒，由 SECTION_WORDS_CN 换算。
WORDS_PER_SECOND_EN = 2.5
#: 中文字/秒。用于按语言估算每段字数上限，避免把英文词数直接套到中文上。
CHARS_PER_SECOND_CN = 4.5

#: 一段的理想口播秒数。决定分段数量，也决定单次 TTS 的请求大小。
SECONDS_PER_SECTION = 75

#: 分段数量上限。段数直接等于 LLM 调用次数和素材下载批次数，必须有界。
MAX_SECTIONS = 24
#: 下限。少于 3 段就退回"一次性"路径更划算，分段只会白白增加调用。
MIN_LONGFORM_SECTIONS = 3


@dataclass(frozen=True)
class LengthBand:
    """一个时长档位。"""

    #: 档位标识，与 ``VideoParams.content_length`` 对应。
    key: str
    #: 展示名，给 WebUI 和日志用。
    label: str
    #: 允许的秒数区间（含）。
    min_seconds: int
    max_seconds: int
    #: 落在区间内时默认取的秒数。
    target_seconds: int
    #: 该档位下是否启用分段。short 为 False，旧路径不变。
    sectioned: bool = False

    @property
    def duration_label(self) -> str:
        return f"{self.min_seconds // 60}-{self.max_seconds // 60} min"


LENGTH_BANDS: dict[str, LengthBand] = {
    "short": LengthBand(
        key="short",
        label="Short",
        min_seconds=60,
        max_seconds=300,
        target_seconds=90,
        sectioned=False,
    ),
    "medium": LengthBand(
        key="medium",
        label="Medium",
        min_seconds=480,
        max_seconds=900,
        target_seconds=720,
        sectioned=True,
    ),
    "long": LengthBand(
        key="long",
        label="Long",
        min_seconds=1200,
        max_seconds=1800,
        target_seconds=1500,
        sectioned=True,
    ),
}

DEFAULT_BAND_KEY = "short"


@dataclass
class Section:
    """长视频的一段。

    ``text`` 为空表示这一段还没写文案——大纲阶段先产出 ``title``/``intent``，
    写文案阶段再填 ``text``。两阶段分开是因为大纲要一次成型（保证叙事连贯），
    而每段文案可以独立重试而不推翻整条大纲。
    """

    index: int
    title: str = ""
    intent: str = ""
    text: str = ""
    #: 本段专属的素材搜索关键词，保持叙事顺序。
    terms: list[str] = field(default_factory=list)
    #: 本段实际口播秒数，TTS 之后回填。
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "intent": self.intent,
            "text": self.text,
            "terms": list(self.terms),
            "duration": round(self.duration, 3),
        }


@dataclass
class SectionPlan:
    """一次长视频规划的完整结果。"""

    band: LengthBand
    target_seconds: int
    language: str
    #: 大纲阶段的产物，即使写文案阶段失败也保留，便于人工接手。
    outline: list[Section] = field(default_factory=list)

    @property
    def sections(self) -> list[Section]:
        return [section for section in self.outline if section.text.strip()]

    @property
    def total_duration(self) -> float:
        return sum(section.duration for section in self.sections)


def is_longform(content_length: object) -> bool:
    """该时长是否走分段路径。"""
    return resolve_band(content_length).sectioned


def resolve_band(content_length: object) -> LengthBand:
    """
    把 ``content_length`` 解析成档位。认不出来就退回 short。

    故意不抛异常：这个值可能来自 WebUI 手选、API 请求体或历史任务记录，
    一个拼写错误不该让整条视频生成失败。
    """
    key = str(content_length or "").strip().lower()
    return LENGTH_BANDS.get(key, LENGTH_BANDS[DEFAULT_BAND_KEY])


def resolve_target_seconds(
    content_length: object,
    explicit_seconds: object = None,
    channel_target: int | None = None,
) -> int:
    """
    定出这条视频的目标秒数。

    优先级：显式 ``content_target_seconds`` > 赛道画像的长视频目标 > 档位默认值。
    显式值会被夹到档位区间内——允许"medium 档但只要 9 分钟"，不允许
    "medium 档却要 40 分钟"，因为那会按 medium 的分段数去生成 40 分钟的文案。
    """
    band = resolve_band(content_length)

    candidate = explicit_seconds
    if candidate is None and band.sectioned:
        candidate = channel_target
    if candidate is None:
        return band.target_seconds

    try:
        seconds = int(candidate)
    except (TypeError, ValueError):
        logger.warning(
            f"ignoring non-numeric content_target_seconds: {candidate!r}"
        )
        return band.target_seconds

    clamped = max(band.min_seconds, min(seconds, band.max_seconds))
    if clamped != seconds:
        logger.warning(
            f"content_target_seconds={seconds} is outside the {band.key} band "
            f"({band.min_seconds}-{band.max_seconds}s); clamped to {clamped}s"
        )
    return clamped


def plan_section_count(target_seconds: int) -> int:
    """按目标秒数决定分段数量，夹在有界区间内。"""
    raw = max(1, round(target_seconds / SECONDS_PER_SECTION))
    if raw < MIN_LONGFORM_SECTIONS:
        return MIN_LONGFORM_SECTIONS
    return min(raw, MAX_SECTIONS)


def _is_cjk_language(language: str | None, text: str = "") -> bool:
    """粗判是否需要按中文字数而不是英文词数估算。"""
    lang = (language or "").strip().lower()
    if lang.startswith(("zh", "ja", "ko")):
        return True
    if lang:
        return False
    return any("一" <= char <= "鿿" for char in text[:400])


def words_for_target(target_seconds: int, cjk: bool = False) -> int:
    """把目标秒数换算成目标词数（或中文字数）。"""
    if cjk:
        return int(target_seconds * CHARS_PER_SECOND_CN)
    return int(target_seconds * WORDS_PER_SECOND_EN)


def chars_per_section(target_seconds: int, section_count: int, cjk: bool) -> int:
    """单段文案的长度上限，用来约束单次 LLM 调用的输出规模。"""
    if section_count <= 0:
        return 0
    per_section_seconds = max(target_seconds / section_count, 1.0)
    words = words_for_target(per_section_seconds, cjk=cjk)
    # 中文一字即一"词"，英文一词约 6 字符（含空格），统一折算成字符上限传给模型。
    return max(int(words * 2.2) if cjk else int(words * 7), 200)


# --------------------------------------------------------------------------
# 大纲解析
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def parse_json_block(raw: str | None) -> Any:
    """
    从模型回复里尽最大努力取出 JSON。

    依次尝试：整体解析 → 去掉代码围栏 → 去掉 think 块 → 抓第一个 [...] 数组。
    全部失败才返回 ``None``，由调用方决定是重试还是降级。
    """
    if not raw:
        return None

    candidates = [raw]
    without_think = _THINK_RE.sub("", raw)
    if without_think != raw:
        candidates.append(without_think)
    candidates.append(_FENCE_RE.sub("", without_think))

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass

    # 最后兜底：抓第一个方括号数组。有些模型会在 JSON 前后加解释性文字。
    match = _ARRAY_RE.search(candidates[-1])
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _coerce_outline(payload: Any, section_count: int) -> list[Section]:
    """把模型返回的大纲规整成固定的段数，容忍缺字段和多余段。"""
    if isinstance(payload, dict):
        for key in ("sections", "outline", "items", "chapters"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = []
    if not isinstance(payload, list):
        return []

    sections: list[Section] = []
    for position, entry in enumerate(payload[:section_count], start=1):
        if isinstance(entry, str):
            sections.append(Section(index=position, title=entry.strip()))
            continue
        if not isinstance(entry, dict):
            continue
        title = str(
            entry.get("title") or entry.get("heading") or entry.get("name") or ""
        ).strip()
        intent = str(
            entry.get("intent")
            or entry.get("summary")
            or entry.get("description")
            or entry.get("beat")
            or ""
        ).strip()
        if not title and not intent:
            continue
        sections.append(Section(index=position, title=title, intent=intent))

    # 模型给少了就补空段，写文案阶段会逐段补齐；给多了就截断。
    while len(sections) < section_count:
        sections.append(Section(index=len(sections) + 1))
    return sections[:section_count]


def build_plan(
    *,
    band: LengthBand,
    target_seconds: int,
    outline_payload: Any,
    language: str = "",
) -> SectionPlan:
    """
    把模型产出的大纲组装成 :class:`SectionPlan`。

    这个函数不做任何 LLM 调用，因此可以脱离网络单独测试。
    """
    section_count = plan_section_count(target_seconds)
    outline = _coerce_outline(outline_payload, section_count)
    return SectionPlan(
        band=band,
        target_seconds=target_seconds,
        language=language or "",
        outline=outline,
    )


def render_sections_as_script(sections: list[Section]) -> str:
    """
    把各段文案拼成一份脚本文本。

    段与段之间用空行分隔——这是 ``llm``、``voice``、``subtitle`` 三条链路共同
    认定的段落边界（``utils.split_string_by_punctuations`` 按标点断句，
    TTS 的 ``_match_script_line`` 也按空行回溯脚本文本），所以拼接格式必须
    和短视频路径产出的脚本完全一致，否则字幕会整段匹配失败。
    """
    return "\n\n".join(
        section.text.strip() for section in sections if section.text.strip()
    )
