"""字幕逐词高亮：跟读式（karaoke）字幕。

一条视频里，**正在念的那个词**被点亮，其余词保持暗色——观众的视线
被引导到"现在读到哪"，这是短视频字幕最有效的做法之一。

难点不在画字，而在**时间轴**。SRT 只有整句的时间，词级时间来自
whisper 的 ``word_timestamps``。如果按位置在整句里均分词，
高亮会和语音明显错位，看起来比不做还糟。所以：

* 词级时间必须来自真实 ASR 输出，落盘成 sidecar；
* 拿不到词级时间时**整段退回原样渲染**，绝不猜测均分；
* 高亮窗口按词的实际时长分配，词之间的空隙不给任何词。

这一层只负责"什么时候点亮哪个词"，不碰绘制——绘制在
:mod:`app.services.caption_render`。这样时间轴逻辑可以脱离
MoviePy 与字体单独验证。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WordTiming",
    "CaptionCue",
    "parse_srt",
    "WORD_TIMING_SUFFIX",
    "word_timing_path",
    "write_word_timings",
    "read_word_timings",
    "build_highlight_timeline",
]

#: SRT 旁边存放词级时间的文件名。
WORD_TIMING_SUFFIX = ".words.json"

#: 词级时间里的最小可信时长（秒）。比这更短多半是 ASR 抖动。
_MIN_WORD_SECONDS = 0.04


@dataclass(frozen=True)
class WordTiming:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class CaptionCue:
    """一条字幕（一句），带它覆盖的词。"""

    start: float
    end: float
    text: str
    words: tuple[WordTiming, ...] = ()

    @property
    def can_highlight(self) -> bool:
        return len(self.words) >= 2


def word_timing_path(subtitle_path: str | Path) -> Path:
    """SRT 路径 → 词级时间 sidecar 路径。"""
    path = Path(subtitle_path)
    return path.with_name(path.stem + WORD_TIMING_SUFFIX)


def write_word_timings(subtitle_path: str | Path, timings) -> Path:
    """
    把词级时间写到 sidecar。

    写入是原子的：sidecar 损坏等于"没有高亮"，如果读到半个文件，
    词的下标会和字幕错位，高亮会串到别的句子上。
    """
    path = word_timing_path(subtitle_path)
    payload = {
        "version": 1,
        "words": [
            {"start": round(float(w.start), 3), "end": round(float(w.end), 3), "text": str(w.text)}
            for w in timings
        ],
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)
    return path


def read_word_timings(subtitle_path: str | Path) -> list[WordTiming]:
    """读回词级时间；没有、损坏或不合法时返回空列表。"""
    path = word_timing_path(subtitle_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(payload, dict):
        return []

    out: list[WordTiming] = []
    for item in payload.get("words") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if not text or end < start:
            continue
        out.append(WordTiming(start=start, end=end, text=text))
    out.sort(key=lambda w: w.start)
    return out


def build_highlight_timeline(cue: CaptionCue) -> list[tuple[float, float, int]]:
    """
    把一条字幕展开成 ``(start, end, active_word_index)`` 序列。

    每个词高亮一次，窗口就是这个词自己的时长。词与词之间的空隙不分配，
    否则会出现"还没念到就先亮"的错位。
    """
    if not cue.can_highlight:
        return []

    timeline: list[tuple[float, float, int]] = []
    for index, word in enumerate(cue.words):
        start = max(word.start, cue.start)
        end = min(word.end, cue.end)
        if end - start < _MIN_WORD_SECONDS:
            # 时长过短：并到前一个窗口里，宁可粗一点也不要闪一下
            if timeline:
                prev_start, _, prev_index = timeline[-1]
                timeline[-1] = (prev_start, end, prev_index)
                continue
            end = start + _MIN_WORD_SECONDS
        timeline.append((start, end, index))
    return timeline


_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _srt_seconds(hours, minutes, seconds, millis) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(str(millis).ljust(3, "0")) / 1000.0
    )


def parse_srt(subtitle_path: str | Path, words=None) -> list["CaptionCue"]:
    """
    解析 SRT，返回带时间的字幕条目。

    自己解析而不是复用 :func:`app.services.subtitle.file_to_subtitles`：
    后者返回的是**未解析**的原始元组 ``(序号, "起 --> 止", 文本)``，
    直接当字幕用会把时间戳当成台词画上去。
    """
    try:
        text = Path(subtitle_path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []

    timings = list(words or [])
    cues: list[CaptionCue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_line = lines[1] if len(lines) > 1 and "-->" in lines[1] else (
            lines[0] if "-->" in lines[0] else None
        )
        if time_line is None:
            continue
        match = _SRT_TIME.search(time_line)
        if not match:
            continue
        start = _srt_seconds(*match.group(1, 2, 3, 4))
        end = _srt_seconds(*match.group(5, 6, 7, 8))
        body_start = 2 if len(lines) > 1 and "-->" in lines[1] else 1
        body = " ".join(line.strip() for line in lines[body_start:]).strip()
        if not body or end <= start:
            continue
        cue_words = tuple(
            w for w in timings if start - 0.25 <= w.start < end
        )
        cues.append(CaptionCue(start=start, end=end, text=body, words=cue_words))
    return cues
