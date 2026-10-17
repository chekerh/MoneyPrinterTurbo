"""分段 TTS：把一条长脚本切成若干次请求，拼接音频并合并字幕时间轴。

短视频路径上一次 ``voice.tts()`` 就出整段旁白。长视频不行，原因有三个，
而且是硬限制不是调参能绕过的：

1. **服务商上限**。Azure v2 TTS 单次请求的合成时长和请求体大小都有上限，
   25 分钟的脚本远超任何一条。
2. **超时**。本项目 ``app.edge_tts_timeout`` 默认 30 秒，是按短视频量级设的；
   一次合成 25 分钟音频不可能在 30 秒内完成，超时后拿不到音频也拿不到字幕。
3. **失败成本**。单次调用失败意味着整条 25 分钟的片子全部作废。分段之后
   只有失败的那一段需要重试。

拼接音频用 ffmpeg 的 concat demuxer + ``-c copy``：不重编码、不引入二次有损
压缩、也不把 25 分钟音频整个读进内存。字幕时间轴则必须手工平移——分段 TTS
返回的每份 SubMaker 的时间轴都从 0 开始，直接串起来会得到一堆互相重叠的
0~N 秒字幕。
"""

from __future__ import annotations

import os
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from app.services import voice
from app.utils import utils

__all__ = [
    "MAX_CHUNK_CHARS",
    "MIN_CHUNK_CHARS",
    "split_for_tts",
    "tts_chunked",
    "merge_sub_makers",
]

#: 单次 TTS 请求的字符上限。取 1500 是因为：Azure v2 的 SSML 请求体在
#: 约 4000 字节时就接近上限，而中文 1500 字大约 4500 字节——所以中文实际
#: 会更早触发这里的切分。留足余量，宁可多请求几次也不要撞服务商的硬上限。
MAX_CHUNK_CHARS = 1500
#: 单段下限。一句话被硬切成两半会破坏 TTS 的韵律，宁可和相邻句子合并。
MIN_CHUNK_CHARS = 120

#: 100 纳秒单位。项目里 edge_tts SubMaker 和 legacy offset 都用这个单位。
HUNDRED_NANOS_PER_SECOND = 10_000_000


# --------------------------------------------------------------------------
# 切分
# --------------------------------------------------------------------------


def _split_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """
    把一个自然段切成若干不超过 ``max_chars`` 的块。

    先按标点断句（和 ``voice._normalize_arabic`` / ``create_subtitle`` 用的是
    同一套断句逻辑，因此断句点与后续字幕匹配点天然对齐），再贪心装块。
    装不满的块会尝试和下一个块合并，避免产生大量碎片。
    """
    sentences = utils.split_string_by_punctuations(paragraph) or [paragraph]

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current}{sentence}" if current else sentence
        if len(candidate) <= max_chars or not current:
            # 单句本身就超长时也照收。硬切一个长句得到的两半语调都不对，
            # 比超限更糟——真正的兜底交给服务商报错，那条路径已有重试。
            current = candidate
            if len(current) >= max_chars:
                chunks.append(current.strip())
                current = ""
        else:
            chunks.append(current.strip())
            current = sentence
    if current.strip():
        chunks.append(current.strip())

    return [chunk for chunk in chunks if chunk]


def split_for_tts(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """
    按自然段优先的顺序切分整篇脚本。

    段落边界优先于句子边界，因为段落边界同时也是字幕链路的边界：切在这里，
    每份 SubMaker 就正好覆盖若干个完整脚本段，字幕逐段匹配不会跨界。
    """
    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    limit = max(max_chars, MIN_CHUNK_CHARS)
    chunks: list[str] = []
    for paragraph in normalized.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            chunks.append(paragraph)
        else:
            chunks.extend(_split_paragraph(paragraph, limit))

    # 相邻碎片合并：太短的块单独发一次请求纯属浪费，而且会打断韵律。
    merged: list[str] = []
    for chunk in chunks:
        if merged and len(merged[-1]) + len(chunk) + 1 <= limit:
            merged[-1] = f"{merged[-1]}\n{chunk}"
        else:
            merged.append(chunk)

    if not merged:
        return [normalized[:limit]]
    return merged


# --------------------------------------------------------------------------
# SubMaker 合并
# --------------------------------------------------------------------------


def _shift_cues(cues: Iterable[Any], offset: timedelta, first_index: int) -> list[Any]:
    """
    把 ``edge_tts.srt_composer.Subtitle`` 列表整体平移 ``offset``。

    ``Subtitle`` 是普通类而不是 NamedTuple，构造签名是
    ``(index, start, end, content)``——少传 ``index`` 会直接 TypeError。
    合并多份 SubMaker 后各段的 SRT 序号会重复，所以这里按 ``first_index``
    连续重排，保证下游 ``get_srt()`` 产出的是合法 SRT。

    用 ``getattr`` 逐字段取值而不是 ``type(cue)(...)``：不同 TTS 路径可能
    返回 cue 的子类，直接按基类签名构造会丢字段。
    """
    shifted = []
    for position, cue in enumerate(cues):
        shifted.append(
            type(cue)(
                first_index + position,
                cue.start + offset,
                cue.end + offset,
                cue.content,
            )
        )
    return shifted


def merge_sub_makers(
    parts: list[tuple[Any, float]],
) -> Any:
    """
    把多份 SubMaker 按累计音频时长合并成一份。

    Args:
        parts: ``[(sub_maker, 该段的音频秒数), ...]``，顺序即拼接顺序。

    Returns:
        一份时间轴覆盖全片的 SubMaker。``subs``/``offset`` 这套 legacy 字段
        一并平移，因为本项目里 Azure v2、Gemini、SiliconFlow 等路径仍然直接
        读写它们（见 ``voice.ensure_legacy_submaker_fields``）。

    合并对象沿用第一份的 SubMaker 实例而不是新建 ``SubMaker()``：不同 TTS
    服务商返回的对象类型和附带状态不同，复用第一份能保证下游拿到的类型与
    短视频路径一致。
    """
    usable = [(maker, seconds) for maker, seconds in parts if maker is not None]
    if not usable:
        return None

    merged = voice.ensure_legacy_submaker_fields(usable[0][0])

    all_cues: list[Any] = []
    all_subs: list[str] = []
    all_offsets: list[tuple[int, int]] = []
    cursor = 0.0
    #: edge_tts 的 SRT 序号是每份 SubMaker 各自从 0 开始的。合并后必须全局重排，
    #: 否则 get_srt() 会写出 "1, 1, 2, 2" 这种重复序号的非法 SRT。
    cue_cursor = 1

    for maker, seconds in usable:
        piece_offset = timedelta(seconds=cursor)
        cursor_ns = int(cursor * HUNDRED_NANOS_PER_SECOND)

        piece_cues = _shift_cues(
            getattr(maker, "cues", None) or [], piece_offset, cue_cursor
        )
        all_cues.extend(piece_cues)
        cue_cursor += len(piece_cues)

        subs = getattr(maker, "subs", None) or []
        offsets = getattr(maker, "offset", None) or []
        for position, sub_text in enumerate(subs):
            if position >= len(offsets):
                break
            start, end = offsets[position]
            all_subs.append(sub_text)
            all_offsets.append((start + cursor_ns, end + cursor_ns))

        cursor += max(float(seconds or 0.0), 0.0)

    merged.cues = all_cues
    merged.subs = all_subs
    merged.offset = all_offsets
    return merged


# --------------------------------------------------------------------------
# 音频拼接
# --------------------------------------------------------------------------


def _concat_audio_files(sources: list[Path], output_file: str) -> bool:
    """
    用 ffmpeg concat demuxer 无损拼接音频。

    ``-c copy`` 是关键：重编码 25 分钟音频既慢又会在每次分段拼接时叠加一次
    有损压缩。concat demuxer 直接复制码流，代价是一次文件级复制。
    """
    if not sources:
        return False
    if len(sources) == 1:
        try:
            if Path(sources[0]).resolve() == Path(output_file).resolve():
                return True
        except OSError:
            pass
        return _copy_file(sources[0], output_file)

    list_file = Path(output_file).parent / f".{Path(output_file).stem}-concat.txt"
    try:
        list_file.parent.mkdir(parents=True, exist_ok=True)
        # concat demuxer 的语法要求单引号包裹路径，内部的单引号要转义。
        lines = [
            "file '{}'".format(str(source.resolve()).replace("'", "'\\''"))
            for source in sources
        ]
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
                str(list_file),
                "-c",
                "copy",
                output_file,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                "failed to concatenate narration chunks: "
                f"returncode={result.returncode}, stderr={result.stderr[-600:]}"
            )
            return False
        return os.path.exists(output_file)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(f"failed to concatenate narration chunks: {exc}")
        return False
    finally:
        list_file.unlink(missing_ok=True)


def _copy_file(source: Path, destination: str) -> bool:
    try:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(source).replace(destination)
        return True
    except OSError:
        try:
            Path(destination).write_bytes(Path(source).read_bytes())
            return True
        except OSError as exc:
            logger.error(f"failed to copy narration file {source} -> {destination}: {exc}")
            return False


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def tts_chunked(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    *,
    chunk_dir: str | None = None,
    max_chars: int = MAX_CHUNK_CHARS,
    on_chunk_done=None,
) -> tuple[Any, str] | None:
    """
    分段合成整篇旁白，返回 ``(合并后的 SubMaker, 音频文件路径)``。

    与 ``voice.tts()`` 的契约保持一致：成功返回 ``(sub_maker, voice_file)``，
    失败返回 ``None``，调用方只需判空。

    ``on_chunk_done(index, total, duration)`` 用于进度回报——长视频合成十几分钟
    没有任何日志会让运维以为任务卡死。

    分段音频先落在 ``chunk_dir``（默认音频文件同级目录），全部成功后才拼接。
    任何一段失败就整体放弃并返回 None：半条旁白配半条素材比整条失败更难排查。
    """
    chunks = split_for_tts(text, max_chars=max_chars)
    if not chunks:
        return None
    if not chunk_dir:
        chunk_dir = str(Path(voice_file).parent)
    Path(chunk_dir).mkdir(parents=True, exist_ok=True)

    # 单段脚本走原来的单次路径，避免给短视频增加任何额外开销。
    if len(chunks) == 1:
        sub_maker = voice.tts(
            text=chunks[0],
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=voice_file,
        )
        return (sub_maker, voice_file) if sub_maker is not None else None

    logger.info(
        f"long-form narration: {len(chunks)} TTS chunks "
        f"({sum(len(chunk) for chunk in chunks)} chars total)"
    )

    parts: list[tuple[Any, float]] = []
    chunk_files: list[Path] = []

    for position, chunk in enumerate(chunks, start=1):
        chunk_file = Path(chunk_dir) / f"narration-{position:03d}.mp3"
        sub_maker = voice.tts(
            text=chunk,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=str(chunk_file),
        )
        if sub_maker is None:
            logger.error(
                f"narration chunk {position}/{len(chunks)} failed; "
                "abandoning the whole narration"
            )
            return None

        # 用真实落盘文件测时长，不信 SubMaker 的最后一个词边界：TTS 在词边界
        # 之后还会留一段固定尾巴（Edge TTS 约 0.88 秒），用错时长会让下一段
        # 的字幕整体前移或后移。理由与 task.generate_audio 里的注释一致。
        seconds = voice.get_audio_duration(str(chunk_file))
        if seconds <= 0:
            logger.error(
                f"narration chunk {position}/{len(chunks)} produced zero-length "
                "audio; abandoning the whole narration"
            )
            return None

        parts.append((sub_maker, seconds))
        chunk_files.append(chunk_file)
        if callable(on_chunk_done):
            on_chunk_done(position, len(chunks), seconds)

    if not _concat_audio_files(chunk_files, voice_file):
        return None

    merged = merge_sub_makers(parts)
    if merged is None:
        return None

    # 分段音频是中间产物。删掉它们，但保留最后一份——某些 TTS 路径在返回的
    # SubMaker 里不携带完整文本，排查字幕问题时需要能重听。
    for chunk_file in chunk_files:
        try:
            chunk_file.unlink(missing_ok=True)
        except OSError:
            pass

    return merged, voice_file
