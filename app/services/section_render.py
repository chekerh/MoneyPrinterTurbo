"""分段渲染：逐段出片再拼接，而不是一次 MoviePy pass 扛完整条时间线。

短视频的 ``combine_videos`` + ``generate_video`` 是一次性 pass：把全部素材
拼成一条静音轨，再叠上完整旁白和整份字幕。对 90 秒的片子这是对的；对 25 分钟
的片子有三个问题：

1. **不可中断**。一次 pass 跑 2~3 小时，中途 OOM、ffmpeg 崩溃或机器重启，
   三个小时全部作废。
2. **不可见**。渲染期间没有任何产物落盘，运维既看不到进度，也无法确认
   已经完成的那部分到底有没有写对。
3. **风险集中**。MoviePy 全程持有解码后的帧缓冲，时长线性推高内存占用；
   失败往往发生在最后一步，即所有成本都已付出却拿不到成片。

所以这里按段渲染：每段出自己的素材轨、旁白切片和字幕切片，单独渲染成一条
几 MB 到几十 MB 的小片，最后用 ffmpeg concat 拼起来（``-c:v copy`` 可用的
话连重编码都省了）。每段渲染完成即落盘并记账，失败只重跑失败的那一段。

拼接优先用 ``concat_video_clips_with_ffmpeg``——它是项目里既有的编码器回退
实现，会在 libx264 不可用时自动换编码器并记住结果。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from loguru import logger

from app.services import shorts_cutter
from app.services import video as video_service
from app.services.longform import Section
from app.utils import utils

__all__ = [
    "SrtEntry",
    "parse_srt",
    "render_sections",
    "concat_section_videos",
]

#: SRT 时间码：``00:01:02,345``。
_SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)
#: 时间码行：``start --> end``，容忍尾部残留的定位参数（``X1:... Y1:...``）。
_SRT_TIMECODE_RE = re.compile(
    r"^\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


class SrtEntry:
    """一条字幕。``start``/``end`` 为秒。"""

    __slots__ = ("start", "end", "text")

    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


def _parse_srt_time(value: str) -> float:
    """``00:01:02,345`` → 62.345 秒。"""
    match = _SRT_TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"unrecognized SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def _format_srt_time(seconds: float) -> str:
    """秒 → ``00:01:02,345``。负值夹到 0，避免出现负时间码。"""
    total = max(int(round(seconds * 1000)), 0)
    millis = total % 1000
    total_seconds = total // 1000
    return (
        f"{total_seconds // 3600:02d}:"
        f"{(total_seconds % 3600) // 60:02d}:"
        f"{total_seconds % 60:02d},{millis:03d}"
    )


def parse_srt(srt_file: str) -> list[SrtEntry]:
    """
    读入 SRT。解析不了就返回空列表。

    按行做状态机而不是一条大正则：SRT 的"空行分隔字幕块"用正则表达时，
    文本组一定会跨过空行把后面的块一起吞掉（`[^\r\n]*` 匹配空串后可以吃掉
    那个换行），排查起来非常费时间。状态机把这个歧义从语法里去掉。

    顺带容忍若干真实世界里的脏格式：序号行缺失、BOM、多余空行、
    时间码行后带定位参数。

    返回空列表而不是抛异常：字幕是附加能力，解析失败时应该退化成无字幕成片，
    而不是让整条视频失败。
    """
    try:
        raw = Path(srt_file).read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning(f"failed to read subtitle file {srt_file}: {exc}")
        return []

    entries: list[SrtEntry] = []
    start: float | None = None
    end: float | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal start, end, buffer
        text = "\n".join(line for line in buffer if line.strip()).strip()
        if text and start is not None and end is not None and end > start:
            entries.append(SrtEntry(start, end, text))
        start = end = None
        buffer = []

    for line in raw.splitlines():
        timecode = _SRT_TIMECODE_RE.match(line)
        if timecode:
            # 上一块还在累积正文却先遇到时间码：这份 SRT 没有空行分隔。
            # 先结算再开新块，否则两段正文会被拼成一条超长字幕。
            flush()
            try:
                start = _parse_srt_time(timecode.group(1))
                end = _parse_srt_time(timecode.group(2))
            except ValueError:
                start = end = None
            continue

        if not line.strip():
            # 空行是块边界，但只在已经开始累积时才结算——文件开头的空行不算。
            if start is not None or buffer:
                flush()
            continue

        if start is None:
            # 还没有时间码，正文不可能属于任何一块；当序号行跳过。
            continue
        buffer.append(line)

    flush()

    if not entries:
        logger.warning(f"subtitle file {srt_file} contained no parsable entries")
    return entries


def write_srt(entries: Sequence[SrtEntry], output_file: str) -> bool:
    """写出 SRT，序号从 1 连续编号。"""
    try:
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        blocks = [
            f"{index}\n"
            f"{_format_srt_time(entry.start)} --> {_format_srt_time(entry.end)}\n"
            f"{entry.text}\n"
            for index, entry in enumerate(entries, start=1)
        ]
        Path(output_file).write_text("\n".join(blocks), encoding="utf-8")
        return True
    except (OSError, ValueError) as exc:
        logger.warning(f"failed to write section subtitle {output_file}: {exc}")
        return False


def slice_entries(
    entries: Sequence[SrtEntry],
    window_start: float,
    window_end: float,
) -> list[SrtEntry]:
    """
    截取时间窗内的字幕，并把时间轴平移到以 0 开头。

    跨越窗口边界的字幕按窗口裁剪而不是整条保留：一条 6 秒的字幕在 2 秒处被
    切开时，两侧各留一半比整条塞进某一边更符合实际观看。
    """
    sliced: list[SrtEntry] = []
    for entry in entries:
        if entry.end <= window_start or entry.start >= window_end:
            continue
        start = max(entry.start, window_start) - window_start
        end = min(entry.end, window_end) - window_start
        if end - start < 0.05:
            continue
        sliced.append(SrtEntry(start, end, entry.text))
    return sliced


# --------------------------------------------------------------------------
# 媒体切片
# --------------------------------------------------------------------------


def _slice_audio(
    audio_file: str, start: float, duration: float, output_file: str
) -> bool:
    """
    用 ffmpeg 从整段旁白里切出一段。

    ``-c:a copy`` 在 MP3 上通常可行且无损，但切点不落在帧边界时会产生几十
    毫秒的偏差，导致该段字幕整体错位。因此切完统一用 ffprobe 读回真实时长，
    由调用方决定是否需要补偿。
    """
    result = subprocess.run(
        [
            utils.get_ffmpeg_binary(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            audio_file,
            "-t",
            f"{max(duration, 0.1):.3f}",
            "-c:a",
            "copy",
            output_file,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # copy 切点失败时退回重编码。几十毫秒的编码噪声远好过没有这一段。
        logger.warning(
            f"lossless audio slice failed at {start:.2f}s, re-encoding: "
            f"{(result.stderr or '')[-300:]}"
        )
        result = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                "-i",
                audio_file,
                "-t",
                f"{max(duration, 0.1):.3f}",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                output_file,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        logger.error(
            f"failed to slice narration [{start:.2f}, +{duration:.2f}]: "
            f"{(result.stderr or '')[-300:]}"
        )
        return False
    return os.path.exists(output_file)


def concat_section_videos(section_videos: Sequence[str], output_file: str) -> str:
    """
    把各段成片拼成整条。

    优先走项目既有的 ``concat_video_clips_with_ffmpeg``，因为它带有编码器
    回退和运行时探测；它失败时再退回一次裸 concat，两次都失败才报错。
    """
    usable = [item for item in section_videos if item and os.path.exists(item)]
    if not usable:
        raise RuntimeError("no section videos available to concatenate")
    if len(usable) == 1:
        os.replace(usable[0], output_file)
        return output_file

    output_dir = os.path.dirname(output_file) or "."
    os.makedirs(output_dir, exist_ok=True)

    try:
        video_service.concat_video_clips_with_ffmpeg(
            clip_files=list(usable),
            output_file=output_file,
            threads=2,
            output_dir=output_dir,
        )
        if os.path.exists(output_file):
            return output_file
        logger.warning("ffmpeg concat reported success but produced no file")
    except Exception as exc:  # noqa: BLE001 - 下面还有一次裸 concat 兜底
        logger.warning(f"ffmpeg concat helper failed: {exc}")

    list_file = os.path.join(output_dir, "sections-concat.txt")
    try:
        with open(list_file, "w", encoding="utf-8") as handle:
            for item in usable:
                handle.write(f"file '{item}'\n")
        result = subprocess.run(
            [
                utils.get_ffmpeg_binary(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                output_file,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"section concat failed: {(result.stderr or '')[-400:]}"
            )
    finally:
        video_service.delete_files(list_file)

    return output_file


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def render_sections(
    task_id: str,
    params: Any,
    sections: Sequence[Section],
    section_materials: Sequence[Sequence[str]],
    audio_file: str,
    subtitle_path: str,
    *,
    on_progress: Callable[[int, str], None] | None = None,
    resume: bool = True,
) -> str | None:
    """
    逐段渲染并拼接，返回成片路径。任一段失败返回 ``None``。

    Args:
        sections: 已带回填时长的分段列表。
        section_materials: 与 ``sections`` 等长的素材列表。
        resume: 为 ``True`` 时跳过已经渲染完成的段（凭文件存在且非空判断）。
    """
    task_dir = utils.task_dir(task_id)
    render_dir = os.path.join(task_dir, "sections")
    os.makedirs(render_dir, exist_ok=True)

    entries = parse_srt(subtitle_path) if subtitle_path else []
    if subtitle_path and not entries:
        logger.warning(
            "long-form render will produce section videos without subtitles: "
            f"{subtitle_path}"
        )

    section_videos: list[str] = []
    cursor = 0.0

    for position, section in enumerate(sections, start=1):
        duration = max(float(section.duration or 0.0), 0.0)
        if duration <= 0:
            logger.error(
                f"section {position}/{len(sections)} has zero duration; "
                "cannot determine its media window"
            )
            return None

        section_file = os.path.join(render_dir, f"section-{position:03d}.mp4")
        if resume and _is_complete(section_file, duration):
            logger.info(
                f"section {position}/{len(sections)} already rendered; reusing"
            )
            section_videos.append(section_file)
            cursor += duration
            if on_progress:
                on_progress(_progress(position, len(sections)), f"section {position} cached")
            continue

        if on_progress:
            on_progress(
                _progress(position, len(sections)),
                f"rendering section {position}/{len(sections)}",
            )

        try:
            rendered = _render_one_section(
                task_id=task_id,
                params=params,
                position=position,
                materials=list(section_materials[position - 1])
                if position - 1 < len(section_materials)
                else [],
                audio_file=audio_file,
                audio_start=cursor,
                audio_duration=duration,
                entries=entries,
                render_dir=render_dir,
                section_file=section_file,
            )
        except Exception as exc:  # noqa: BLE001 - 编排层统一转任务失败
            logger.error(
                f"section {position}/{len(sections)} render failed: {exc}"
            )
            return None

        if not rendered:
            logger.error(
                f"section {position}/{len(sections)} produced no output; "
                "aborting instead of concatenating an incomplete video"
            )
            return None

        section_videos.append(section_file)
        cursor += duration

    if not section_videos:
        logger.error("long-form render produced no section videos")
        return None

    logger.info(f"concatenating {len(section_videos)} section videos")
    final_output = os.path.join(task_dir, "longform-sections.mp4")
    try:
        return concat_section_videos(section_videos, final_output)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"failed to concatenate long-form sections: {exc}")
        return None


def _progress(position: int, total: int) -> int:
    """渲染阶段占 50%→100%。"""
    if total <= 0:
        return 50
    return 50 + int(50 * position / total)


def _is_complete(section_file: str, expected_duration: float) -> bool:
    """
    复用已有分段视频前，先确认它真的完整。

    只判"文件存在"是不够的：上一次运行可能正好死在写文件的过程中，留下一个
    几 KB 的残片，复用它会让最终成片在这一段突然黑屏或截断。这里读真实时长，
    并要求它至少覆盖目标时长的大部分。

    复用 ``shorts_cutter.probe_video`` 而不是 ffprobe：本项目只保证解析出
    ffmpeg 二进制，不保证每个平台都有配套的 ffprobe（该模块的 docstring 里
    记了这个理由），直接复用它就不再引入一个新的外部依赖假设。
    """
    if not os.path.exists(section_file):
        return False
    try:
        if os.path.getsize(section_file) < 1024:
            return False
    except OSError:
        return False

    try:
        meta = shorts_cutter.probe_video(
            utils.get_ffmpeg_binary(), section_file
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败按"不可复用"处理
        logger.debug(f"could not probe cached section {section_file}: {exc}")
        return False

    # 容忍 1 秒误差：分段视频的末帧时长受容器时间基影响会略有出入。
    return meta.duration >= max(expected_duration - 1.0, 0.5)


def _render_one_section(
    *,
    task_id: str,
    params: Any,
    position: int,
    materials: list[str],
    audio_file: str,
    audio_start: float,
    audio_duration: float,
    entries: Sequence[SrtEntry],
    render_dir: str,
    section_file: str,
) -> bool:
    """渲染单段：切片旁白与字幕 → 拼素材轨 → 混音出片。"""
    if not materials:
        logger.error(f"section {position} has no materials assigned")
        return False

    section_audio = os.path.join(render_dir, f"section-{position:03d}.mp3")
    if not _slice_audio(
        audio_file, audio_start, audio_duration, section_audio
    ):
        return False

    section_subtitle = ""
    window = slice_entries(entries, audio_start, audio_start + audio_duration)
    if window:
        candidate = os.path.join(render_dir, f"section-{position:03d}.srt")
        if write_srt(window, candidate):
            section_subtitle = candidate

    combined = os.path.join(render_dir, f"section-{position:03d}-combined.mp4")
    # combine_videos 会按传入音频的时长决定素材轨长度，所以必须喂切片后的
    # 旁白：喂整段会让这一段尝试拼出 25 分钟的素材轨。
    video_service.combine_videos(
        combined_video_path=combined,
        video_paths=materials,
        audio_file=section_audio,
        video_aspect=params.video_aspect,
        video_concat_mode=params.video_concat_mode,
        video_transition_mode=params.video_transition_mode,
        max_clip_duration=params.video_clip_duration,
        threads=params.n_threads,
        clip_speed=params.video_clip_speed,
    )

    section_params = _section_params(params, bool(window))
    ok = video_service.generate_video(
        video_path=combined,
        audio_path=section_audio,
        subtitle_path=section_subtitle,
        output_file=section_file,
        params=section_params,
        # BGM 在整条成片上统一处理一次。分段渲染时每段都叠一遍 BGM，会让
        # 相邻两段的音乐在拼接处出现相位跳变。
        bgm_file_override="",
    )

    # 中间产物：素材轨和旁白切片在确认成片后可删，但保留在 resume 需要的
    # 失败现场里。这里只清掉体积最大的素材轨。
    video_service.delete_files(combined)
    return bool(ok) and os.path.exists(section_file)


def _section_params(params: Any, has_subtitles: bool) -> Any:
    """
    复制一份 params，并按分段实际情况修正字幕开关。

    不能直接改原对象：``params`` 是任务级共享状态，改了会让归档、缩略图、
    发布文案等后续环节都以为整条片子都有（或都没有）字幕。
    """
    try:
        from app.models.schema import VideoParams

        clone = VideoParams(**{**params.model_dump()})
    except Exception:  # noqa: BLE001 - 老任务可能是任意对象，退回浅拷贝
        import copy

        try:
            clone = copy.copy(params)
        except Exception:  # noqa: BLE001
            logger.warning("could not clone params for section render")
            return params

    clone.subtitle_enabled = has_subtitles
    return clone
