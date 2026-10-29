"""字幕归一化：把 TTS 时间轴产出的碎片字幕修成可读的整句。

长视频的字幕直接来自 TTS 返回的逐词 cue 聚合。当断句匹配在某个连接词上
失败时，那个词会单独占一条 cue，而它的起止时间相同——成片里表现为字幕闪
一下（``00:00:13,562 --> 00:00:13,562  Yet``），观众什么都看不清。另一个
方向的问题是一条 cue 停 8 秒以上，阅读节奏和口播完全脱节。

这里只做三件事，全部是纯文本/时间轴变换，不碰音频也不改字数：

1. **合并过短**：时长不足 ``min_seconds`` 的碎片并入下一条（或上一条），
   让短词回到它所属的句子里。
2. **拆分过长**：超过 ``max_seconds`` 或字符数超过两行上限的 cue 按词数和
   字符比例切分，时间按字符占比分配——切出来的边界跟着语速走。
3. **折行**：每条 cue 最多 ``max_lines`` 行、每行 ``max_chars_per_line``
   字符，只在词边界断开。

刻意只作用于长视频路径。短视频的字幕观感已经被用户接受，任何改动都属于
未经请求的行为变化。
"""

from __future__ import annotations

import math
import os

from loguru import logger

from app.services.section_render import SrtEntry, parse_srt, write_srt

__all__ = [
    "DEFAULT_MIN_SECONDS",
    "DEFAULT_MAX_SECONDS",
    "DEFAULT_MAX_CHARS_PER_LINE",
    "DEFAULT_MAX_LINES",
    "normalize_entries",
    "normalize_srt_file",
]

DEFAULT_MIN_SECONDS = 1.2
DEFAULT_MAX_SECONDS = 7.0
DEFAULT_MAX_CHARS_PER_LINE = 42
DEFAULT_MAX_LINES = 2


def normalize_entries(
    entries: list[SrtEntry] | tuple[SrtEntry, ...],
    *,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[SrtEntry]:
    """返回归一化后的 cue 列表，输入顺序保持不变。"""
    merged = _merge_short(list(entries or []), min_seconds)
    split: list[SrtEntry] = []
    char_limit = max(max_chars_per_line * max_lines, 1)
    for entry in merged:
        split.extend(_split_long(entry, max_seconds, char_limit))
    wrapped = [
        SrtEntry(
            entry.start,
            entry.end,
            _wrap(entry.text, max_chars_per_line, max_lines),
        )
        for entry in split
    ]
    logger.info(
        f"subtitle normalization: {len(entries or [])} cues -> {len(wrapped)} cues"
    )
    return wrapped


def normalize_srt_file(
    subtitle_path: str,
    *,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE,
    max_lines: int = DEFAULT_MAX_LINES,
) -> bool:
    """
    原地归一化一个 SRT 文件。无法解析或写入失败时返回 ``False``，原文件不动。

    写临时文件再 ``os.replace``，避免中途失败留下半份字幕——半份字幕会让
    分段渲染的某一段直接没有字幕，而且很难从日志看出发生过截断。
    """
    entries = parse_srt(subtitle_path, allow_zero=True)
    if not entries:
        logger.warning(f"subtitle normalization skipped: {subtitle_path} unreadable")
        return False

    normalized = normalize_entries(
        entries,
        min_seconds=min_seconds,
        max_seconds=max_seconds,
        max_chars_per_line=max_chars_per_line,
        max_lines=max_lines,
    )
    if not normalized:
        return False

    temporary = f"{subtitle_path}.normalized"
    try:
        if not write_srt(normalized, temporary):
            return False
        os.replace(temporary, subtitle_path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass
    return True


def _merge_short(entries: list[SrtEntry], min_seconds: float) -> list[SrtEntry]:
    """把时长不足的碎片并入下一条；末尾碎片并入上一条。"""
    merged: list[SrtEntry] = []
    pending_start: float | None = None
    buffer: list[str] = []

    for entry in entries:
        text = str(entry.text or "").strip()
        if not text:
            continue
        start = max(float(entry.start), 0.0)
        end = max(float(entry.end), start)
        if pending_start is None:
            pending_start = start
        buffer.append(text)
        if end - pending_start >= min_seconds:
            merged.append(SrtEntry(pending_start, end, " ".join(buffer)))
            pending_start = None
            buffer = []

    if buffer:
        if merged:
            last = merged[-1]
            last.text = f"{last.text} {' '.join(buffer)}".strip()
        else:
            merged.append(
                SrtEntry(pending_start or 0.0, (pending_start or 0.0) + min_seconds, " ".join(buffer))
            )
    return merged


def _split_long(entry: SrtEntry, max_seconds: float, char_limit: int) -> list[SrtEntry]:
    """按时长和字数切分超长 cue，时间按字符占比分配。"""
    span = max(entry.end - entry.start, 0.0)
    words = str(entry.text or "").split()
    if span <= max_seconds and len(entry.text or "") <= char_limit:
        return [entry]
    if len(words) <= 1:
        # 无法按词切分时保留原样：切成半句话比稍微超时更难读。
        return [entry]

    by_time = math.ceil(span / max_seconds) if max_seconds > 0 else 1
    by_chars = math.ceil(len(entry.text) / char_limit)
    pieces = min(max(by_time, by_chars, 2), len(words))

    groups = _group_words(words, pieces)
    lengths = [sum(len(word) + 1 for word in group) for group in groups]
    total = float(sum(lengths)) or 1.0

    result: list[SrtEntry] = []
    cursor = entry.start
    for group, length in zip(groups, lengths):
        if not group:
            continue
        end = min(entry.end, cursor + span * (length / total))
        if end - cursor < 0.05 and result:
            # 浮点误差导致的零长片段并回上一块，不要产出新的零长 cue。
            result[-1].end = min(entry.end, cursor)
            cursor = end
            continue
        result.append(SrtEntry(cursor, end, " ".join(group)))
        cursor = end

    if result:
        result[-1].end = entry.end
    return result or [entry]


def _group_words(words: list[str], pieces: int) -> list[list[str]]:
    """按累计字符数把词均匀分成 ``pieces`` 组，且不留空组。"""
    pieces = max(1, min(pieces, len(words)))
    total = float(sum(len(word) + 1 for word in words)) or 1.0
    groups: list[list[str]] = [[] for _ in range(pieces)]
    accumulated = 0.0
    index = 0
    for word in words:
        groups[index].append(word)
        accumulated += len(word) + 1
        # 达到当前组的字符配额才换组，最后几组由剩余词自然填满。
        if index < pieces - 1 and accumulated >= total * (index + 1) / pieces:
            index += 1
    return groups


def _wrap(text: str, max_chars_per_line: int, max_lines: int) -> str:
    """在词边界折行；超出行数时把余下的并回最后一行，宁可溢出也不丢字。"""
    words = str(text or "").split()
    if not words:
        return ""

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars_per_line:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        head = lines[: max_lines - 1]
        tail = " ".join(lines[max_lines - 1 :])
        lines = head + [tail]
    return "\n".join(lines)
