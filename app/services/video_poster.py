"""为归档成片抽一张封面帧，用在计划表与媒体库的行内缩略图。

归档进 ``outputs/<channel>/<月份>/videos/`` 的成片是**没有缩略图**的：缩略图功能
是按任务（``task_id`` + ``final-N.mp4``）存下来的，而计划表里的每一行指向的是归档
后的副本，两边对不上。列表里那一列"🔗 / —"因此既没有图也没有说明，用户无法一眼
判断哪条成片已经发出去。

这里按需抽帧，并且把结果落盘缓存：

* 抽帧时刻沿用缩略图服务的 25% 规则（``plan_frame_timestamp``），两处看到的是同一帧；
* 缓存键包含路径、mtime 与大小，改过的成片会自然失效，不会一直显示旧画面；
* 任何一步失败（没有 ffmpeg、视频损坏、权限问题）都返回 ``None``，由调用方画占位。

抽帧本身不便宜——每条约 0.14 秒，几乎全是进程启动开销。所以缓存是必须的：一批 11 条
的行会从 1.5 秒降到 0，而之后每次打开都是直接读盘。
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path

from app.utils import utils

logger = logging.getLogger(__name__)

# 封面宽度。行内缩略图只有几十像素宽，再大的图只是白白占内存。
POSTER_WIDTH = 320
# 抽帧超时。单条视频 0.14 秒，30 秒足够宽裕；超时说明 ffmpeg 卡住了。
_EXTRACT_TIMEOUT_SECONDS = 30

# 进程内备忘，避免 2 秒轮询的任务面板反复起子进程。键含 mtime 与大小，
# 因此被重写过的成片会自然重新抽帧，不会一直显示旧画面。
_MEMO: dict[tuple[str, int, int], Path | None] = {}
_MEMO_LIMIT = 512


def _poster_cache_dir() -> Path:
    """封面缓存目录：跟其他派生产物放在一起，不污染作品集。"""
    return Path(utils.storage_dir("posters", create=True))


def _cache_key(video_path: str) -> str:
    try:
        stat = os.stat(video_path)
        fingerprint = f"{os.path.abspath(video_path)}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        # 读不到 stat 就退化成只用路径：文件多半已经不在了，调用方会自行放弃。
        fingerprint = os.path.abspath(video_path)
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]


def _probe_duration_seconds(video_path: str) -> float | None:
    """用 ffprobe 读时长。

    不要用 ``ffmpeg -f null -`` 代替：那条命令会把整段视频解一遍。一条 50 秒的
    1080p 成片要花掉一秒，而我们要的只是头部那行 Duration——列表里 11 行就是
    11 秒白等。ffprobe 只读元数据，几十毫秒。
    """
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=_EXTRACT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("ffprobe failed for %s: %s", video_path, exc)
        return None

    try:
        seconds = float((completed.stdout or "").strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def poster_frame_path(video_path: str) -> Path | None:
    """返回封面帧的缓存路径；抽不了就返回 ``None``。

    调用方应当把 ``None`` 当成"这张还没有封面"，而不是错误——素材库里混着损坏文件、
    没装 ffmpeg 的机器、以及正在写入的成片，这些都不该让整个列表挂掉。
    """
    if not video_path or not os.path.isfile(video_path):
        return None

    try:
        stat = os.stat(video_path)
        memo_key = (os.path.abspath(video_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None
    if memo_key in _MEMO:
        return _MEMO[memo_key]

    resolved = _poster_frame_path_uncached(video_path)
    if len(_MEMO) >= _MEMO_LIMIT:
        _MEMO.clear()
    _MEMO[memo_key] = resolved
    return resolved


def _poster_frame_path_uncached(video_path: str) -> Path | None:
    cache_dir = _poster_cache_dir()
    cached = cache_dir / f"{_cache_key(video_path)}.jpg"
    if cached.is_file() and cached.stat().st_size > 0:
        return cached

    try:
        ffmpeg_binary = utils.get_ffmpeg_binary()
    except Exception as exc:  # noqa: BLE001 - 缺 ffmpeg 是可预期状态
        logger.debug("ffmpeg unavailable, skipping poster: %s", exc)
        return None

    duration = _probe_duration_seconds(video_path)
    if not duration or duration <= 0:
        return None

    # 与缩略图服务保持一致：25% 处，短视频再兜一个片尾保护。
    at = min(duration - 0.1, max(0.5, duration * 0.25))
    if at <= 0:
        return None

    # 先写临时文件再改名：中途失败不会留下半张图被后续读取当成有效缓存。
    temporary = cached.with_suffix(".tmp.jpg")
    try:
        completed = subprocess.run(
            [
                ffmpeg_binary,
                "-v",
                "error",
                "-ss",
                f"{at:.3f}",
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-vf",
                f"scale={POSTER_WIDTH}:-2",
                "-f",
                "image2",
                "-y",
                str(temporary),
            ],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("poster extraction failed for %s: %s", video_path, exc)
        _discard(temporary)
        return None

    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        logger.debug(
            "ffmpeg produced no frame for %s: %s",
            video_path,
            (completed.stderr or b"").decode("utf-8", "replace")[:200],
        )
        _discard(temporary)
        return None

    try:
        os.replace(temporary, cached)
    except OSError as exc:
        logger.debug("could not publish poster cache for %s: %s", video_path, exc)
        _discard(temporary)
        return None
    return cached
