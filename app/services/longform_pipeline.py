"""长视频流水线编排：把分段规划接到 ``task.py`` 的六个阶段上。

``task._run_pipeline`` 的六个阶段本身不用改写。这里的每一个函数都只是**同
一阶段的另一种实现**，由 ``task.py`` 在 ``content_length != short`` 时改调：

    阶段            短视频                    长视频
    ------------    -----------------------    ---------------------------------
    1 script        llm.generate_script       build_longform_script
    2 terms         llm.generate_terms        terms_for_longform_sections
    3 audio         voice.tts                 tts_chunking.tts_chunked
    4 subtitle      subtitle.create           不变（合并后的 SubMaker 照常可用）
    5 materials     download_videos           不变（关键词变成按段展开的长列表）
    6 render        combine_videos            section_render.render_sections

阶段 4、5 之所以不用改，是因为分段 TTS 产出的 SubMaker 时间轴已经覆盖全片、
素材下载本来就按 ``audio_duration`` 缩放——它们对"长"这件事天然免疫。真正
需要重写的只有产出脚本、配音和渲染这三段。

进度回报是一个刻意的设计：长视频单个阶段会跑十几分钟到几小时，如果只有
阶段级的 10%→20%→30% 跳变，运维看到的是一台卡死的机器。因此每个长视频
阶段都会回调 ``on_progress``，由调用方映射到任务的 progress 字段。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Sequence

from loguru import logger

from app.services import longform, niche_brand, tts_chunking
from app.services.longform import Section, SectionPlan

__all__ = [
    "LongformResult",
    "resolve_longform_request",
    "build_longform_script",
    "tts_longform_narration",
    "terms_for_longform_sections",
    "assign_materials_to_sections",
    "make_section_neighbours",
]

#: 每段向模型请求多少个素材关键词。4 个是平衡点：少于 3 个下载轮询会重复
#: 命中同一批素材，多于 5 个绝大多数关键词在 Pexels/Pixabay 上没有结果，
#: 反而把下载时间浪费在空搜索上。
TERMS_PER_SECTION = 4


class LongformResult:
    """长视频规划阶段的产物集合。"""

    def __init__(
        self,
        plan: SectionPlan,
        script: str = "",
        terms: Sequence[str] = (),
    ) -> None:
        self.plan = plan
        self.script = script
        self.terms = list(terms)

    @property
    def sections(self) -> list[Section]:
        return self.plan.outline

    @property
    def target_seconds(self) -> int:
        return self.plan.target_seconds

    def to_dict(self) -> dict[str, Any]:
        """写进 ``script.json`` 的结构，便于失败后人工接手。"""
        return {
            "content_length": self.plan.band.key,
            "target_seconds": self.plan.target_seconds,
            "language": self.plan.language,
            "sections": [section.to_dict() for section in self.plan.outline],
        }


def resolve_longform_request(params, channel: str) -> tuple[longform.LengthBand, int]:
    """
    定出这条任务的档位和目标秒数。

    档位来源顺序：请求里的 ``content_length`` > 赛道画像的 ``longform_band``。
    这样"给某个频道整体切到长视频"只需要改赛道画像一次，不必在每个请求里
    重复声明；而单条任务仍可临时覆盖。
    """
    requested = str(getattr(params, "content_length", "") or "").strip().lower()
    band_key, channel_target = niche_brand.longform_preference(channel)

    if requested not in longform.LENGTH_BANDS:
        requested = band_key or longform.DEFAULT_BAND_KEY
    band = longform.LENGTH_BANDS.get(requested, longform.LENGTH_BANDS["short"])

    target = longform.resolve_target_seconds(
        band.key,
        getattr(params, "content_target_seconds", None),
        channel_target or None,
    )
    return band, target


# --------------------------------------------------------------------------
# 阶段 1：脚本
# --------------------------------------------------------------------------


def make_section_neighbours(section: Section, sections: Sequence[Section]) -> str:
    """
    给单段提示词带上前后段的意图，避免段落之间重复或断裂。

    长视频最常见的两种退化——段与段换句话重复、段与段之间断掉——都是因为
    模型在写第 N 段时看不到第 N-1 段说了什么。把邻居的**意图**（而不是成稿）
    交给模型，约束力足够又不至于让它照抄邻居的句子。
    """
    index = section.index - 1
    previous = sections[index - 1] if index > 0 else None
    following = sections[index + 1] if index + 1 < len(sections) else None

    lines: list[str] = []
    if previous:
        lines.append(
            f"- Previous section ({previous.index}) was: "
            f"{previous.title or '(untitled)'} — {previous.intent or '(unspecified)'}"
        )
    if following:
        lines.append(
            f"- Next section ({following.index}) will be: "
            f"{following.title or '(untitled)'} — {following.intent or '(unspecified)'}"
        )
    if not lines:
        return ""
    # 明确写出方向性要求，否则"不要重复"很容易被理解成"别写这段"。
    lines.append(
        "- Do not restate the previous section and do not set up the next one; "
        "deliver this section's own claim."
    )
    return "\n".join(lines)


def build_longform_script(
    task_id: str,
    params,
    channel: str,
    *,
    on_progress: Callable[[int, str], None] | None = None,
) -> LongformResult | None:
    """
    阶段 1：生成大纲 + 逐段文案。失败返回 ``None``。

    大纲和文案分两步是刻意的：大纲一次成型保证叙事连贯；文案逐段写，
    任何一段失败只重试那段，不会推翻已经写好的十几段。
    """
    band, target_seconds = resolve_longform_request(params, channel)
    if not band.sectioned:
        return None

    section_count = longform.plan_section_count(target_seconds)
    language = str(getattr(params, "video_language", "") or "")

    if on_progress:
        on_progress(6, f"planning {section_count}-section outline")

    try:
        outline_payload = _call_with_retry(
            "outline",
            lambda: _generate_outline(params, channel, section_count, target_seconds),
        )
    except Exception as exc:  # noqa: BLE001 - 编排层负责转成任务失败
        logger.error(f"failed to plan long-form outline: {exc}")
        return None

    if outline_payload is None:
        logger.error("long-form outline planning gave up after all retries")
        return None

    plan = longform.build_plan(
        band=band,
        target_seconds=target_seconds,
        outline_payload=outline_payload,
        language=language,
    )
    sections = plan.outline
    if not sections:
        logger.error("long-form outline produced no sections")
        return None

    # 空标题说明模型没给出可用结构，但仍可能写得出文案；这种情况补一句
    # 占位意图，让逐段提示词至少有一个方向。
    for position, section in enumerate(sections, start=1):
        if not section.title and not section.intent:
            section.title = f"Section {position}"

    cjk = longform._is_cjk_language(language)
    max_chars = longform.chars_per_section(target_seconds, section_count, cjk)

    written = 0
    for position, section in enumerate(sections, start=1):
        if on_progress:
            # 脚本阶段在总进度里占 10%→20%，按段均分。
            span = 10 / max(len(sections), 1)
            on_progress(
                10 + int(span * (position - 1)),
                f"writing section {position}/{len(sections)}",
            )
        text = _call_with_retry(
            f"section {position}",
            lambda section=section: _generate_section(
                params, channel, section, sections, target_seconds, max_chars
            ),
        )
        if text is None:
            logger.error(
                f"long-form section {position}/{len(sections)} gave up after "
                "all retries; aborting the task instead of shipping a gap"
            )
            return None
        section.text = text
        written += 1

    if written == 0:
        return None

    script = longform.render_sections_as_script(sections)
    if not script.strip():
        logger.error("long-form script assembled to empty text")
        return None

    logger.info(
        f"long-form script ready: {len(sections)} sections, "
        f"{len(script)} chars, target {target_seconds}s "
        f"({band.key}, {band.duration_label})"
    )
    return LongformResult(plan=plan, script=script)


def _generate_outline(
    params, channel: str, section_count: int, target_seconds: int
) -> Any:
    from app.services import llm

    return llm.generate_outline(
        video_subject=params.video_subject,
        section_count=section_count,
        language=str(getattr(params, "video_language", "") or ""),
        target_seconds=target_seconds,
        channel=channel,
        video_script_prompt=str(getattr(params, "video_script_prompt", "") or ""),
        custom_system_prompt=str(getattr(params, "custom_system_prompt", "") or ""),
    )


def _generate_section(
    params,
    channel: str,
    section: Section,
    sections: Sequence[Section],
    target_seconds: int,
    max_chars: int,
) -> str:
    from app.services import llm

    return llm.generate_section_text(
        section_title=section.title,
        section_intent=section.intent,
        section_index=section.index,
        section_count=len(sections),
        video_subject=params.video_subject,
        max_chars=max_chars,
        language=str(getattr(params, "video_language", "") or ""),
        channel=channel,
        neighbours=make_section_neighbours(section, sections),
        video_script_prompt=str(getattr(params, "video_script_prompt", "") or ""),
        custom_system_prompt=str(getattr(params, "custom_system_prompt", "") or ""),
    )


def _call_with_retry(label: str, action: Callable[[], Any], attempts: int = 3):
    """
    同一段工作重试若干次。

    LLM 服务层已经自带退避重试，这里是第二层：长视频的每一段文案都是一次
    独立的付费请求，把它和"整条大纲"��为同一个失败单位来重试，会在最后一段
    偶发失败时把前 19 段的调用费用全部重来一遍。第二层把失败范围收窄到单段。
    """
    last_error = ""
    for attempt in range(attempts):
        try:
            result = action()
            if result:
                return result
            last_error = "empty result"
        except Exception as exc:  # noqa: BLE001 - 统一收敛成重试
            last_error = str(exc)
        if attempt < attempts - 1:
            logger.warning(
                f"long-form {label} attempt {attempt + 1}/{attempts} failed: "
                f"{last_error}"
            )
    logger.error(f"long-form {label} failed after {attempts} attempts: {last_error}")
    return None


# --------------------------------------------------------------------------
# 阶段 2：关键词
# --------------------------------------------------------------------------


def terms_for_longform_sections(
    plan: SectionPlan,
    video_subject: str,
    *,
    on_progress: Callable[[int, str], None] | None = None,
) -> list[str]:
    """
    阶段 2：为每一段生成关键词，再按叙事顺序压平。

    模型不可用时降级为"按段标题当关键词"。这不会让视频变好，但素材下载仍
    然拿得到东西，总比整个任务在阶段 2 死掉强——关键词质量只影响画面。
    """
    from app.services import llm, section_materials

    sections = plan.outline
    if not sections:
        return []

    summaries = [
        {
            "index": section.index,
            "title": section.title,
            "intent": section.intent,
            "preview": section.text,
        }
        for section in sections
    ]

    if on_progress:
        on_progress(18, f"planning search terms for {len(sections)} sections")

    buckets = _call_with_retry(
        "section terms",
        lambda: llm.generate_section_terms(
            section_summaries=summaries,
            video_subject=video_subject,
            terms_per_section=TERMS_PER_SECTION,
        ),
    )

    if not buckets or not any(buckets):
        logger.warning(
            "section terms unavailable; falling back to section titles as "
            "search terms"
        )
        buckets = [[section.title] if section.title else [] for section in sections]

    for section, bucket in zip(sections, buckets):
        section.terms = [str(term).strip() for term in bucket if str(term).strip()]

    flat = section_materials.flatten_terms(sections)
    logger.info(
        f"long-form terms: {len(flat)} terms across {len(sections)} sections"
    )
    return flat


# --------------------------------------------------------------------------
# 阶段 3：旁白
# --------------------------------------------------------------------------


def tts_longform_narration(
    task_id: str,
    params,
    plan: SectionPlan,
    voice_file: str,
    *,
    on_progress: Callable[[int, str], None] | None = None,
) -> tuple[Any, float] | None:
    """
    阶段 3：分段合成旁白，回填每段时长。

    返回 ``(sub_maker, total_seconds)``。分段 TTS 产出的 SubMaker 时间轴已经
    覆盖全片，所以下游 ``subtitle.create_subtitle`` 不需要任何改动。

    每段时长在这里回填是有用的副产品：素材额度按各段时长成比例分配，没有
    它就只能平均分，长短不均的段落会因此反复播放同一批素材。
    """
    from app.services import voice

    script = longform.render_sections_as_script(plan.outline)
    if not script.strip():
        logger.error("long-form narration requested with an empty script")
        return None

    voice_name = voice.parse_voice_name(params.voice_name)
    voice_rate = float(getattr(params, "voice_rate", 1.0) or 1.0)

    def _chunk_done(index: int, total: int, _seconds: float) -> None:
        if not on_progress:
            return
        # 音频阶段在总进度里占 20%→30%。
        span = 10 / max(total, 1)
        on_progress(
            20 + int(span * index),
            f"narrating chunk {index}/{total}",
        )

    result = tts_chunking.tts_chunked(
        text=script,
        voice_name=voice_name,
        voice_rate=voice_rate,
        voice_file=voice_file,
        on_chunk_done=_chunk_done,
    )
    if result is None:
        return None

    sub_maker, resolved_file = result
    total_seconds = voice.get_audio_duration(resolved_file)
    if total_seconds <= 0:
        logger.error("long-form narration produced zero-length audio")
        return None

    _record_section_durations(plan, script, sub_maker, total_seconds)

    return sub_maker, total_seconds


def _record_section_durations(
    plan: SectionPlan,
    script: str,
    sub_maker: Any,
    total_seconds: float,
) -> None:
    """
    用合并后 SubMaker 的时间轴反推每段的起止，把时长回填到各段。

    段落和字幕 cue 是一一对应的（``split_for_tts`` 在段落边界切分，
    ``voice.create_subtitle`` 又按空行把脚本文本切段），所以某段最后一条 cue
    的结束时间就是这一段的结束时间。这比按字数均分准确得多。
    """
    sections = plan.outline
    cues = list(getattr(sub_maker, "cues", None) or [])
    if not cues:
        # 拿不到 cue 的 TTS 路径退化为按字数比例分配。宁可粗糙也不能让所有段
        # 的时长都是 0——那样素材额度会全部堆到第一段。
        _distribute_durations_by_weight(sections, script, total_seconds)
        return

    try:
        cursor = 0
        for section in sections:
            section_end = cursor
            # 逐条消费 cue，直到文本长度覆盖本段文案。
            consumed = 0
            target_length = len(section.text.strip())
            while cursor < len(cues) and consumed < target_length:
                content = getattr(cues[cursor], "content", "") or ""
                consumed += len(content)
                section_end = cues[cursor].end.total_seconds()
                cursor += 1
            section.duration = max(section_end, 0.0)
    except Exception as exc:  # noqa: BLE001 - 时长只是配额优化，不是必需项
        logger.warning(f"failed to attribute section durations: {exc}")
        _distribute_durations_by_weight(sections, script, total_seconds)
        return

    # 逐词 cue 可能因为断句对齐而略有出入，把最后一段兜底到总时长，
    # 避免累计时长比音频短，最后一段的素材因此被少分配。
    if sections and total_seconds > 0:
        tail = total_seconds - sections[-1].duration
        if tail > 0:
            sections[-1].duration += tail


def _distribute_durations_by_weight(
    sections: Sequence[Section], script: str, total_seconds: float
) -> None:
    lengths = [max(len(section.text.strip()), 1) for section in sections]
    total_weight = float(sum(lengths)) or 1.0
    cursor = 0.0
    for section, weight in zip(sections, lengths):
        span = total_seconds * (weight / total_weight)
        cursor += span
        section.duration = span


# --------------------------------------------------------------------------
# 阶段 5：素材分段
# --------------------------------------------------------------------------


def assign_materials_to_sections(
    plan: SectionPlan,
    downloaded_videos: Sequence[str],
    total_seconds: float,
    clip_duration: int,
) -> list[list[str]]:
    """
    把已下载的素材按段额度切分。

    切分必须保持 ``downloaded_videos`` 的原始顺序——那个顺序就是
    ``download_videos`` 在 ``match_materials_to_script=True`` 时按关键词轮询
    建立的叙事顺序，打乱它等于把"画面跟着文案走"这个特性弄丢。
    """
    from app.services import section_materials

    sections = plan.outline
    if not sections:
        return []

    budget = section_materials.clip_budget(
        sections=sections,
        total_seconds=total_seconds,
        clip_duration=clip_duration,
    )

    assigned: list[list[str]] = []
    cursor = 0
    for position, count in enumerate(budget):
        window = list(downloaded_videos[cursor : cursor + count])
        cursor += count
        assigned.append(window)
        if position == 0:
            logger.info(
                f"material split: {len(budget)} sections, "
                f"{len(downloaded_videos)} clips available, "
                f"first section gets {len(window)}"
            )

    # 素材多于额度时，把多出来的并入最后一段，而不是丢弃——丢弃会让
    # combine_videos 的素材池变小，长视频末尾更容易出现重复画面。
    leftover = list(downloaded_videos[cursor:])
    if leftover and assigned:
        assigned[-1].extend(leftover)
    elif leftover and not assigned:
        assigned = [leftover]

    return assigned
