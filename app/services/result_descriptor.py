"""在任务完成时固定成片身份，让下游只认服务器写下的记录。

此前"这个任务产出了哪些视频"由各个消费方各自回答：有的列目录，有的按调用方
传来的索引拼路径。前者会在文件被删或索引错位时静默指向别的对象，后者把路径
决定权交给了调用方。本模块把这件事收敛成一处：任务完成时枚举 ``final-<index>.mp4``，
记录索引、相对名、大小、哈希与几何信息，写进 ``storage/result_descriptors/``。

两个刻意的边界：

* 描述符落在 ``storage/result_descriptors/`` 而不是 ``storage/tasks/``——后者
  会被静态挂载到 ``/tasks``，任务元数据不该跟着公开暴露。
* 写失败或读失败都抛 :class:`ResultDescriptorError`。调用方（任务完成路径）把
  它降级为"本任务不支持缩略图"，而不是让一个成功的视频任务因此失败。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.services.project_layout import ProjectLayout
from app.utils.strict_media_tools import MediaTools

__all__ = [
    "ResultDescriptor",
    "ResultDescriptorError",
    "ResultVideoEntry",
    "SCHEMA_VERSION",
    "build_result_descriptor",
    "read_result_descriptor",
    "write_result_descriptor",
]

SCHEMA_VERSION = 1

#: 成片命名：final-<index>.mp4。索引必须是无前导零的正整数。
_FINAL_VIDEO = re.compile(r"^final-(\d+)\.mp4$")

#: task_id 会进入文件路径，只接受不含路径分隔符的不透明标识符。
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: ffprobe 单次调用的超时。探测应当是毫秒级的，超时即视为坏文件。
_PROBE_TIMEOUT_SECONDS = 20

_DESCRIPTOR_FILENAME = "result_descriptor.json"


class ResultDescriptorError(RuntimeError):
    """成片无法被安全枚举、探测，或描述符无法被写出/读入。"""


@dataclass(frozen=True)
class ResultVideoEntry:
    """一段成片的固定身份。"""

    video_index: int
    relative_name: str
    bytes: int
    sha256: str
    width: int
    height: int
    rotation: int
    duration_ms: int

    def to_json(self) -> dict:
        return {
            "video_index": self.video_index,
            "relative_name": self.relative_name,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_json(cls, raw: object) -> "ResultVideoEntry":
        _require_exact_keys(raw, {
            "video_index", "relative_name", "bytes", "sha256",
            "width", "height", "rotation", "duration_ms",
        }, "videos[]")
        return cls(
            video_index=_require_int(raw["video_index"], "video_index", minimum=0),
            relative_name=_require_safe_relative_name(raw["relative_name"]),
            bytes=_require_int(raw["bytes"], "bytes", minimum=0),
            sha256=_require_digest(raw["sha256"], "sha256"),
            width=_require_int(raw["width"], "width", minimum=1),
            height=_require_int(raw["height"], "height", minimum=1),
            rotation=_require_int(raw["rotation"], "rotation", minimum=0),
            duration_ms=_require_int(raw["duration_ms"], "duration_ms", minimum=0),
        )


@dataclass(frozen=True)
class ResultDescriptor:
    """一个任务全部成片的身份记录。"""

    schema_version: int
    task_id: str
    videos: tuple[ResultVideoEntry, ...]
    toolchain: dict

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "videos": [entry.to_json() for entry in self.videos],
            "toolchain": self.toolchain,
        }

    @classmethod
    def from_json(cls, raw: object) -> "ResultDescriptor":
        _require_exact_keys(
            raw, {"schema_version", "task_id", "videos", "toolchain"}, "descriptor"
        )
        schema_version = _require_int(raw["schema_version"], "schema_version", minimum=1)
        if schema_version != SCHEMA_VERSION:
            raise ResultDescriptorError(
                f"不支持的 schema_version {schema_version}，当前实现只支持 {SCHEMA_VERSION}"
            )

        raw_videos = raw["videos"]
        if not isinstance(raw_videos, list) or not raw_videos:
            raise ResultDescriptorError("videos 必须是非空数组")
        videos = tuple(ResultVideoEntry.from_json(item) for item in raw_videos)

        indices = [entry.video_index for entry in videos]
        if len(set(indices)) != len(indices):
            raise ResultDescriptorError(f"videos 中存在重复的 video_index：{sorted(indices)}")

        return cls(
            schema_version=schema_version,
            task_id=_require_safe_task_id(raw["task_id"]),
            videos=videos,
            toolchain=_require_json_object(raw["toolchain"], "toolchain"),
        )


def _require_exact_keys(raw: object, expected: set[str], where: str) -> None:
    """字段集合必须完全一致：缺字段和多字段都要显式失败。"""
    if not isinstance(raw, dict):
        raise ResultDescriptorError(f"{where} 必须是对象，收到 {type(raw).__name__}")
    actual = set(raw)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ResultDescriptorError(f"{where} 缺少字段：{sorted(missing)}")
    if unknown:
        raise ResultDescriptorError(f"{where} 含未知字段：{sorted(unknown)}")


def _require_int(value: object, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultDescriptorError(f"{field} 必须是整数，收到 {value!r}")
    if value < minimum:
        raise ResultDescriptorError(f"{field} 不能小于 {minimum}，收到 {value}")
    return value


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ResultDescriptorError(f"{field} 必须是 64 位十六进制摘要")
    try:
        int(value, 16)
    except ValueError:
        raise ResultDescriptorError(f"{field} 不是合法的十六进制摘要") from None
    return value


def _require_json_object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ResultDescriptorError(f"{field} 必须是对象")
    return value


def _require_safe_task_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_TASK_ID.match(value):
        raise ResultDescriptorError(f"非法的 task_id：{value!r}")
    return value


def _require_safe_relative_name(value: object) -> str:
    """只接受不含分隔符、不含上级引用的单个文件名。"""
    if not isinstance(value, str) or not value:
        raise ResultDescriptorError("relative_name 必须是非空字符串")
    if os.sep in value or "/" in value or value in (".", ".."):
        raise ResultDescriptorError(f"relative_name 不能包含路径分隔符：{value!r}")
    if not _FINAL_VIDEO.match(value):
        raise ResultDescriptorError(f"relative_name 不符合 final-<index>.mp4 约定：{value!r}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: dict) -> bytes:
    """固定序列化规则：描述符 ID 与摘要都基于它，因此不能随意改动。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _ffprobe_stream(path: Path, ffprobe: Path) -> dict:
    """读取第一条视频流的几何信息。"""
    try:
        completed = subprocess.run(
            [
                str(ffprobe),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:stream_tags=rotate:side_data=rotation",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResultDescriptorError(f"ffprobe 无法读取 {path}：{exc}") from exc

    if completed.returncode != 0:
        raise ResultDescriptorError(
            f"ffprobe 读取 {path.name} 失败（返回码 {completed.returncode}）："
            f"{completed.stderr.strip()[:200]}"
        )

    try:
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams") or []
    except json.JSONDecodeError as exc:
        raise ResultDescriptorError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

    if not streams:
        raise ResultDescriptorError(f"{path.name} 没有视频流")
    return streams[0]


def _duration_ms(stream: dict, name: str) -> int:
    """时长来自容器或流；两者都缺就拒绝，未知时长无法安全抽帧。"""
    for source in (stream, stream.get("tags") or {}):
        raw = source.get("duration")
        if raw in (None, "", "N/A"):
            continue
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        if seconds <= 0:
            raise ResultDescriptorError(f"{name} 的时长非正数：{raw}")
        return int(round(seconds * 1000))
    raise ResultDescriptorError(f"{name} 的时长未知，无法安全抽取帧")


def _rotation(stream: dict) -> int:
    """
    归一化旋转角到 0/90/180/270。

    ffprobe 不同版本把旋转放在 ``tags.rotate`` 或 ``side_data_list`` 的
    ``rotation`` 里，两处都要认，并处理负值。
    """
    raw = (stream.get("tags") or {}).get("rotate")
    if raw in (None, "", "N/A"):
        for item in stream.get("side_data_list") or []:
            if isinstance(item, dict) and "rotation" in item:
                raw = item["rotation"]
                break
    if raw in (None, "", "N/A"):
        return 0
    try:
        degrees = int(round(float(raw))) % 360
    except (TypeError, ValueError):
        return 0
    if degrees < 0:
        degrees += 360
    return degrees


def _enumerate_final_videos(task_dir: Path) -> list[tuple[int, Path]]:
    """列出 ``final-<index>.mp4``，并对索引冲突和非常规文件失败关闭。"""
    found: dict[int, Path] = {}
    for child in sorted(task_dir.iterdir()):
        match = _FINAL_VIDEO.match(child.name)
        if not match:
            continue
        if child.is_symlink():
            raise ResultDescriptorError(
                f"成片 {child.name} 是符号链接：任务目录之外的来源无法被信任"
            )
        if not child.is_file():
            raise ResultDescriptorError(f"成片 {child.name} 不是普通文件")
        index = int(match.group(1))
        if index in found:
            raise ResultDescriptorError(
                f"成片索引重复：final-{index}.mp4 与 {found[index].name} 规范化后相同"
            )
        found[index] = child

    if not found:
        raise ResultDescriptorError(f"任务目录中没有成片：{task_dir}")
    return sorted(found.items())


def build_result_descriptor(
    layout: ProjectLayout,
    task_id: object,
    tools: MediaTools,
) -> ResultDescriptor:
    """枚举并探测一个任务的全部成片，返回描述符。不写盘。"""
    safe_task_id = _require_safe_task_id(task_id)
    task_dir = layout.storage_root / "tasks" / safe_task_id
    if not task_dir.is_dir():
        raise ResultDescriptorError(f"任务目录不存在：{safe_task_id}")

    entries: list[ResultVideoEntry] = []
    for index, path in _enumerate_final_videos(task_dir):
        stream = _ffprobe_stream(path, tools.ffprobe)
        entries.append(
            ResultVideoEntry(
                video_index=index,
                relative_name=path.name,
                bytes=path.stat().st_size,
                sha256=_sha256_file(path),
                width=_require_int(stream.get("width"), "width", minimum=1),
                height=_require_int(stream.get("height"), "height", minimum=1),
                rotation=_rotation(stream),
                duration_ms=_duration_ms(stream, path.name),
            )
        )

    return ResultDescriptor(
        schema_version=SCHEMA_VERSION,
        task_id=safe_task_id,
        videos=tuple(entries),
        toolchain=tools.to_manifest(),
    )


def write_result_descriptor(
    layout: ProjectLayout,
    descriptor: ResultDescriptor,
) -> tuple[Path, str, str]:
    """把描述符原子写入 ``descriptor_root``。

    :returns: ``(路径, descriptor_id, 规范化内容的 sha256)``。
    """
    target_dir = layout.descriptor_root / descriptor.task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)

    payload = descriptor.to_json()
    canonical = _canonical_json(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    descriptor_id = f"rd_{digest[:32]}"

    # 同目录临时文件 + 原子改名：读者要么看到旧内容，要么看到完整新内容。
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=target_dir, prefix=".result_descriptor-", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target_dir / _DESCRIPTOR_FILENAME)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    _fsync_dir(target_dir)
    return target_dir / _DESCRIPTOR_FILENAME, descriptor_id, digest


def _fsync_dir(path: Path) -> None:
    """把目录项本身刷盘，否则改名在崩溃后可能不可见。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:
        # 某些文件系统不允许对目录 fsync；此时改名仍已原子生效。
        pass
    finally:
        os.close(fd)


def read_result_descriptor(path: Path) -> ResultDescriptor:
    """读回并完整校验一份描述符。任何不符合约定的内容都显式失败。"""
    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ResultDescriptorError(f"无法读取描述符 {path}：{exc}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ResultDescriptorError(f"描述符不是合法 JSON：{exc}") from exc

    return ResultDescriptor.from_json(payload)
