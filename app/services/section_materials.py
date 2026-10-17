"""分段素材：按段分配搜索关键词和素材额度。

短视频路径把素材关键词写死成 5~8 个（``llm.generate_terms`` 的 ``amount``
参数），因为 90 秒的片子有 5~8 个镜头就够。25 分钟的片子需要上百个镜头，
继续用 8 个关键词轮询下载，结果就是同一批素材在整条时间线上反复出现——
这正是长视频看起来"很廉价"的主因：观众能看出素材库只有那么大。

这里改成按段分配：每一段有自己的关键词和自己的素材额度，段与段之间顺序衔接。
素材额度按各段口播时长成比例，而不是平均分——真实脚本的各段长短差别很大，
平均分会让长段落重复播放同一批素材。

本模块只做**规划**（算出每段该有几个镜头、关键词是什么），真正下载仍走
``material.download_videos``，因此素材源、去重、缓存、失败重试全部沿用既有
实现，不需要为长视频另写一套。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from loguru import logger

from app.services.longform import Section, plan_section_count

__all__ = [
    "MIN_CLIPS_PER_SECTION",
    "MAX_CLIPS_PER_SECTION",
    "clip_budget",
    "coerce_section_terms",
    "flatten_terms",
]

#: 每段素材数下限。低于 3 个镜头，观众会明显感到画面重复。
MIN_CLIPS_PER_SECTION = 3
#: 每段素材数上限。单段下载太多会让某一个关键词挤占整段时间线（这正是
#: ``download_videos`` 里 round-robin 逻辑要防的事），这里先卡一道。
MAX_CLIPS_PER_SECTION = 24


def clip_budget(
    sections: Sequence[Section],
    total_seconds: float,
    clip_duration: int,
) -> list[int]:
    """
    按各段口播时长成比例分配素材额度。

    Args:
        sections: 已写入文案和 ``duration`` 的分段列表。
        total_seconds: 全片口播总时长。
        clip_duration: 单个素材在成片里的播放秒数。

    Returns:
        与 ``sections`` 等长的额度列表。每项至少 ``MIN_CLIPS_PER_SECTION``，
        总和与按总时长算出的镜头数偏差控制在最小额度以内。
    """
    if not sections:
        return []

    clip_seconds = max(int(clip_duration or 5), 1)
    total = max(float(total_seconds or 0.0), 0.0)

    # 各段时长来自 TTS，是真实值；拿不到时退回平均分配。
    durations = [max(float(section.duration or 0.0), 0.0) for section in sections]
    if sum(durations) <= 0:
        durations = [1.0] * len(sections)

    weights = [duration / sum(durations) for duration in durations]
    ideal = [
        max(int(round(total * weight / clip_seconds)), MIN_CLIPS_PER_SECTION)
        for weight in weights
    ]
    ideal = [min(count, MAX_CLIPS_PER_SECTION) for count in ideal]

    # 段数变化后重新对齐总镜头数。不对齐的后果是：脚本比目标长时素材不够，
    # combine_videos 只能反复回放已有素材；脚本比目标短时素材闲置。
    target_total = max(
        int(math.ceil(total / clip_seconds)) if total > 0 else len(ideal),
        MIN_CLIPS_PER_SECTION * len(ideal),
    )
    # 上限约束可能让总数低于 target_total，此时不再补——宁可少下载，
    # 也不让 MAX_CLIPS_PER_SECTION 形同虚设。
    delta = target_total - sum(ideal)
    while delta > 0:
        grew = False
        for position, count in enumerate(ideal):
            if count >= MAX_CLIPS_PER_SECTION:
                continue
            ideal[position] += 1
            delta -= 1
            grew = True
            if delta == 0:
                break
        if not grew:
            break
    while delta < 0:
        shrunk = False
        for position in range(len(ideal) - 1, -1, -1):
            if ideal[position] <= MIN_CLIPS_PER_SECTION:
                continue
            ideal[position] -= 1
            delta += 1
            shrunk = True
            if delta == 0:
                break
        if not shrunk:
            break

    logger.info(
        f"section clip budget for {len(sections)} sections: "
        f"total={sum(ideal)} clips, per-section={ideal}"
    )
    return ideal


def coerce_section_terms(
    payload: Any,
    section_count: int,
) -> list[list[str]]:
    """
    把模型返回的"每段关键词"规整成 ``section_count`` 个列表。

    容忍三种模型行为：返回 dict（``{"sections": [...]}``）、返回二维数组、
    返回一维数组（一维时按顺序轮转填入各段，而不是全塞进第一段）。
    一维是常见的失败模式：模型把 20 个关键词平铺返回，直接当成二维用会
    让每一段拿到一个字符串。
    """
    if isinstance(payload, dict):
        for key in ("sections", "terms", "items", "keywords"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = []

    if not isinstance(payload, list):
        return [[] for _ in range(section_count)]

    nested = all(isinstance(entry, (list, tuple)) for entry in payload)
    if nested:
        buckets = [
            [str(term).strip() for term in entry if str(term).strip()]
            for entry in payload[:section_count]
        ]
    else:
        # 一维：先摊平可能存在的嵌套 dict 包装，再轮转分配。
        flat: list[str] = []
        for entry in payload:
            if isinstance(entry, dict):
                candidate = (
                    entry.get("terms") or entry.get("keywords") or entry.get("search_terms")
                )
                if isinstance(candidate, list):
                    flat.extend(str(term).strip() for term in candidate if str(term).strip())
                continue
            text = str(entry).strip()
            if text:
                flat.append(text)
        buckets = [[] for _ in range(min(section_count, max(len(flat), 1)))]
        if not buckets:
            buckets = [[] for _ in range(section_count)]
        for position, term in enumerate(flat):
            buckets[position % len(buckets)].append(term)

    while len(buckets) < section_count:
        buckets.append([])
    return buckets[:section_count]


def flatten_terms(sections: Sequence[Section]) -> list[str]:
    """
    把各段关键词按叙事顺序压平成一个列表。

    保持顺序是刻意的：``material.download_videos`` 在
    ``match_materials_to_script=True`` 时会严格按关键词顺序轮询下载，
    压平后的顺序就是最终时间线上素材出现的顺序。
    """
    flat: list[str] = []
    seen: set[str] = set()
    for section in sections:
        for term in section.terms:
            # 段内去重即可；跨段重复是有意保留的——同一素材库在整条片子里
            # 完全不重复反而会显得刻意，但同一个关键词在一段内出现两次
            # 会导致 download_videos 重复下载同一个搜索结果。
            key = term.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            flat.append(term.strip())
    return flat


def expected_section_count(target_seconds: int) -> int:
    """规划阶段的段数，供关键词生成时对齐。"""
    return plan_section_count(target_seconds)
