"""
Smart long-form-to-shorts cutter.

The tool splits one long video into several standalone short clips, selecting
the most "viral" moments using a light-weight virality scorer built on
heuristics that do not require a second LLM call or re-TTS:

* speech activity -- a short whose window is mostly silent feels dead, so
  long silence inside the window is penalized;
* visual variety -- a single locked-off camera angle rarely keeps retention,
  while a burst of wild cuts feels chaotic; moderate scene-change density is
  rewarded;
* hook power -- the best moments for a short usually live near the start of
  the source (after credits/logos), so windows that are too deep into the
  video get a smaller "hook" credit.

The module keeps the scoring math pure (no ffmpeg dependency) so it can be
unit-tested; all I/O lives in the probe/detect/cut functions which shell out
to the same ffmpeg binary resolved by ``app.utils.utils.get_ffmpeg_binary``.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.services.video import _write_videofile_with_codec_fallback
from app.utils import utils

DEFAULT_WINDOW_SECONDS = 30.0
DEFAULT_MIN_SPEECH_RATIO = 0.55
DEFAULT_SCENE_THRESHOLD = 0.35
DEFAULT_SILENCE_NOISE_DB = -30.0
DEFAULT_SILENCE_MIN_SECONDS = 0.5
DEFAULT_MAX_SHORTS = 5
DEFAULT_MIN_GAP_SECONDS = 1.0
DEFAULT_VERTICAL = True

# 打分权重。总和为 1.0；三个维度都落在 0..1 区间，再放缩到 0..100。
WEIGHT_SPEECH = 0.45
WEIGHT_DENSITY = 0.35
WEIGHT_HOOK = 0.20

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_VIDEO_STREAM_RE = re.compile(r"Stream\s+#\d+:\d+.*?Video:.*?(\d{2,5})x(\d{2,5})")
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
_PTS_TIME_RE = re.compile(r"pts_time:([0-9.]+)")


@dataclass
class VideoMeta:
    duration: float
    width: int | None = None
    height: int | None = None


@dataclass
class ShortsPlan:
    start: float
    end: float
    score: float
    speech_ratio: float
    scene_density_per_min: float


def probe_video(ffmpeg_binary: str, video_path: str) -> VideoMeta:
    """
    Probe duration and resolution using ``ffmpeg -i`` output.

    Uses ffmpeg rather than ffprobe because the project resolves an ffmpeg
    binary (config / PATH / imageio-ffmpeg) and does not guarantee a matching
    ffprobe on every platform.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"video not found: {video_path}")
    proc = subprocess.run(
        [ffmpeg_binary, "-hide_banner", "-i", video_path],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    duration = None
    for line in text.splitlines():
        match = _DURATION_RE.search(line)
        if match:
            hours, minutes, seconds, frac = match.groups()
            duration = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(frac) / (10 ** len(frac))
            break
    if duration is None:
        raise RuntimeError(
            "could not read duration from {path}; ffmpeg returned nothing usable: {err}".format(
                path=video_path,
                err=text.strip()[-200:] or "<empty>",
            )
        )
    width = height = None
    for line in text.splitlines():
        match = _VIDEO_STREAM_RE.search(line)
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            break
    return VideoMeta(duration=duration, width=width, height=height)


def detect_silences(
    ffmpeg_binary: str,
    video_path: str,
    noise_db: float = DEFAULT_SILENCE_NOISE_DB,
    min_duration: float = DEFAULT_SILENCE_MIN_SECONDS,
) -> list[tuple[float, float]]:
    """Return [(start, end), ...] silence intervals via ffmpeg silencedetect."""
    proc = subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-nostats",
            "-i",
            video_path,
            "-af",
            f"silencedetect=n={noise_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    return parse_silences((proc.stderr or "") + "\n" + (proc.stdout or ""))


def detect_scene_changes(
    ffmpeg_binary: str,
    video_path: str,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
) -> list[float]:
    """
    Return scene-change timestamps via ffmpeg ``select='gt(scene,T)'``.

    Threshold roughly matches ffmpeg's document intent: 0.3-0.4 catches hard
    cuts without flagging every subtle brightness change.
    """
    filter_expr = f"select='gt(scene,{threshold})',showinfo"
    proc = subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-nostats",
            "-i",
            video_path,
            "-vf",
            filter_expr,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    return parse_scene_times((proc.stderr or "") + "\n" + (proc.stdout or ""))


def parse_silences(text: str) -> list[tuple[float, float]]:
    """Parse ffmpeg silencedetect output into [(start, end), ...]."""
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(text)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(text)]
    intervals: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else None
        if end is not None and end > start:
            intervals.append((start, end))
    return intervals


def parse_scene_times(text: str) -> list[float]:
    """Parse ffmpeg showinfo output into a sorted list of scene-change times."""
    times = sorted({float(m.group(1)) for m in _PTS_TIME_RE.finditer(text)})
    return times


def speech_ratio(start: float, end: float, silences: list[tuple[float, float]]) -> float:
    """Fraction of the window that is not covered by a silence interval."""
    window_len = end - start
    if window_len <= 0:
        return 0.0
    covered = 0.0
    for (s, e) in silences:
        overlap = max(0.0, min(e, end) - max(s, start))
        covered += overlap
    return max(0.0, min(1.0, 1.0 - covered / window_len))


def scene_density_per_min(
    start: float, end: float, scene_times: list[float]
) -> float:
    window_len = end - start
    if window_len <= 0:
        return 0.0
    count = sum(1 for t in scene_times if start < t < end)
    return count * 60.0 / window_len


def density_score(density: float) -> float:
    """
    视觉变化打分：零次切换过于静态，过高密度又显杂乱。

    经验区间：每 60 秒 2~12 次场景切换对 30 秒竖屏短视频最友好，落在
    区间内给高分，两侧线性衰减。
    """
    if density <= 0:
        return 0.0
    if density < 2.0:
        return density / 2.0
    if density <= 12.0:
        return 1.0
    return max(0.0, 1.0 - (density - 12.0) / 20.0)


def hook_score(start: float, end: float, duration: float) -> float:
    """Hook 权重：前 25% 最强，越靠后越弱；完全不看结束时间。"""
    if duration <= 0:
        return 0.0
    position = start / duration
    if position <= 0.25:
        return 1.0
    return max(0.0, 1.0 - (position - 0.25) / 0.75)


def plan_virality(
    start: float,
    end: float,
    duration: float,
    scene_times: list[float],
    silences: list[tuple[float, float]],
) -> ShortsPlan:
    speech = speech_ratio(start, end, silences)
    density = scene_density_per_min(start, end, scene_times)
    score = 100.0 * (
        WEIGHT_SPEECH * speech
        + WEIGHT_DENSITY * density_score(density)
        + WEIGHT_HOOK * hook_score(start, end, duration)
    )
    return ShortsPlan(
        start=start,
        end=end,
        score=round(score, 1),
        speech_ratio=round(speech, 3),
        scene_density_per_min=round(density, 2),
    )


def _overlaps(start: float, end: float, chosen: list[tuple[float, float]], gap: float) -> bool:
    return any(
        start < chosen_end + gap and end > chosen_start - gap
        for (chosen_start, chosen_end) in chosen
    )


def select_short_plans(
    duration: float,
    scene_times: list[float],
    silences: list[tuple[float, float]],
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_shorts: int = DEFAULT_MAX_SHORTS,
    min_speech_ratio: float = DEFAULT_MIN_SPEECH_RATIO,
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS,
    candidates_per_second: float = 1.0,
) -> list[ShortsPlan]:
    """
    Greedy top-score selection of non-overlapping short windows.

    Windows are evaluated on a dense grid (``candidates_per_second`` windows
    per second of video), dropped when speech falls below ``min_speech_ratio``,
    then sorted by score and greedily accepted as long as they do not overlap
    an already-picked window (with ``min_gap_seconds`` breathing room).
    """
    if duration <= 0:
        return []
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")

    candidates: list[ShortsPlan] = []
    step = max(0.1, 1.0 / max(1.0, candidates_per_second))
    start = 0.0
    while start + window_seconds <= duration:
        end = start + window_seconds
        plan = plan_virality(start, end, duration, scene_times, silences)
        if plan.speech_ratio >= min_speech_ratio:
            candidates.append(plan)
        start += step

    candidates.sort(key=lambda p: p.score, reverse=True)
    chosen: list[ShortsPlan] = []
    picked_window: list[tuple[float, float]] = []
    for plan in candidates:
        if len(chosen) >= max_shorts:
            break
        if _overlaps(plan.start, plan.end, picked_window, min_gap_seconds):
            continue
        chosen.append(plan)
        picked_window.append((plan.start, plan.end))
    chosen.sort(key=lambda p: p.start)
    return chosen


def cut_short(
    ffmpeg_binary: str,
    video_path: str,
    output_path: str,
    start: float,
    end: float,
    vertical: bool = DEFAULT_VERTICAL,
    target_height: int = 1920,
    target_width: int = 1080,
) -> None:
    """
    Cut a single standalone short with moviepy.

    Source frames are subclipped exactly to [start, end). When ``vertical`` is
    set the clip is center-cropped to 9:16 and resized to target_width x
    target_height, matching typical short-form platforms.
    """
    from moviepy import VideoFileClip  # 延后导入，避免 CLI 工具无 headless 环境下拖慢

    clip = VideoFileClip(video_path)
    try:
        sub = clip.subclipped(start, end)
        if vertical:
            source_w, source_h = sub.size
            target_aspect = target_width / target_height
            source_aspect = source_w / source_h
            if source_aspect > target_aspect:
                crop_width = int(source_h * target_aspect)
                crop_height = source_h
            else:
                crop_width = source_w
                crop_height = int(source_w / target_aspect)
            sub = sub.cropped(
                x_center=sub.size[0] / 2,
                y_center=sub.size[1] / 2,
                width=crop_width,
                height=crop_height,
            )
            sub = sub.resized(width=target_width, height=target_height)
        _write_videofile_with_codec_fallback(
            sub,
            output_path,
            codec="libx264",
            audio_codec="aac",
            logger=None,
            write_logfile=False,
            threads=2,
        )
    finally:
        clip.close()
    logger.info(f"short written: {output_path}")


def cut_viral_shorts(
    video_path: str,
    output_dir: str,
    *,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    max_shorts: int = DEFAULT_MAX_SHORTS,
    min_speech_ratio: float = DEFAULT_MIN_SPEECH_RATIO,
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    vertical: bool = DEFAULT_VERTICAL,
    base_name: str | None = None,
    ffmpeg_binary: str | None = None,
) -> list[dict]:
    """
    Full pipeline: probe -> detect scenes/silences -> score -> cut.

    Returns a list of results, each ``{"file": str, "start": .., "end": ..,
    "score": .., "speech_ratio": .., "scene_density_per_min": ..}``.
    """
    binary = ffmpeg_binary or utils.get_ffmpeg_binary()
    meta = probe_video(binary, video_path)
    logger.info(
        f"probed {video_path}: duration={meta.duration:.1f}s "
        f"resolution={meta.width}x{meta.height}"
    )
    scene_times = detect_scene_changes(binary, video_path, threshold=scene_threshold)
    silences = detect_silences(binary, video_path)
    logger.info(f"scene changes: {len(scene_times)}, silence intervals: {len(silences)}")

    plans = select_short_plans(
        duration=meta.duration,
        scene_times=scene_times,
        silences=silences,
        window_seconds=window_seconds,
        max_shorts=max_shorts,
        min_speech_ratio=min_speech_ratio,
        min_gap_seconds=min_gap_seconds,
    )
    logger.info(f"selected {len(plans)} short windows")

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    stem = base_name or Path(video_path).stem
    results: list[dict] = []
    for index, plan in enumerate(plans, start=1):
        output_path = os.path.join(output_dir, f"{stem}_short_{index:02d}.mp4")
        cut_short(
            binary,
            video_path,
            output_path,
            start=plan.start,
            end=plan.end,
            vertical=vertical,
        )
        results.append(
            {
                "file": output_path,
                "start": round(plan.start, 2),
                "end": round(plan.end, 2),
                "score": plan.score,
                "speech_ratio": plan.speech_ratio,
                "scene_density_per_min": plan.scene_density_per_min,
            }
        )
    return results