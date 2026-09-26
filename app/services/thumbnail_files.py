"""缩略图清单：唯一的所有权与选择记录。

清单回答两个问题：现在选中的是哪个产物，以及磁盘上多出来的那个文件是一次
"已提交但没记账"的发布还是一次失败的残留。区分不了这两者，清理就只能二选一：
全删会丢掉真实结果，全留会让垃圾无限堆积。

写入采用"同目录临时文件 + 原子改名"，因此读者要么看到旧版本、要么看到完整新
版本，不会看到半个 JSON。并发写者通过 ``expected_revision`` 做乐观并发控制：
清单文件本身就是被替换的那个资源，所以版本检查必须发生在替换之前。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "MANIFEST_FILENAME",
    "QUARANTINE_DIRNAME",
    "ThumbnailFilesError",
    "append_artifact",
    "archive_artifact",
    "list_artifacts",
    "load_manifest",
    "manifest_digest",
    "next_artifact_id",
    "read_selection",
    "recover_orphans",
    "write_manifest",
]

SCHEMA_VERSION = 2
MANIFEST_FILENAME = "manifest.json"
QUARANTINE_DIRNAME = ".quarantine"

#: 源视频：final-<index>.mp4。清单恢复时不得把这些当成垃圾。
_SOURCE_VIDEO = re.compile(r"^final-\d+\.mp4$")

#: 产物文件名：thumbnail-<artifact-id>.jpg
_ARTIFACT_FILE = re.compile(r"^thumbnail-([A-Za-z0-9_-]{8,64})\.jpg$")

_MANIFEST_KEYS = {"schema_version", "task_id", "manifest_revision", "videos"}
_VIDEO_KEYS = {"video_index", "source_sha256", "artifacts", "selected_artifact_id"}
_ARTIFACT_KEYS = {
    "artifact_id", "video_index", "parent_artifact_id", "previous_artifact_id",
    "relative_name", "source_sha256", "output_sha256", "overlay_sha256",
    "font_id", "frame_at_seconds", "width", "height", "bytes",
    "created_at", "artifact_state", "purged_at",
}
_ARTIFACT_STATES = {"live", "purged"}

#: 每次任务的产物上限。达到上限后新的生成请求被拒绝，
#: 除非操作者显式归档一个未选中的产物。
MAX_LIVE_ARTIFACTS_PER_VIDEO = 20

_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ThumbnailFilesError(RuntimeError):
    """清单结构非法、并发版本不匹配，或产物文件不满足约束。"""


def next_artifact_id() -> str:
    """生成一个不透明产物 ID。"""
    return uuid.uuid4().hex


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(payload: dict) -> bytes:
    """固定序列化规则。摘要与并发判断都基于它，不能随意改动。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def manifest_digest(manifest: dict) -> str:
    """清单规范化内容的 SHA-256。"""
    return hashlib.sha256(_canonical_json(manifest)).hexdigest()


def _require_exact_keys(raw: object, expected: set[str], where: str) -> None:
    if not isinstance(raw, dict):
        raise ThumbnailFilesError(f"{where} 必须是对象，收到 {type(raw).__name__}")
    missing = expected - set(raw)
    unknown = set(raw) - expected
    if missing:
        raise ThumbnailFilesError(f"{where} 缺少字段：{sorted(missing)}")
    if unknown:
        raise ThumbnailFilesError(f"{where} 含未知字段：{sorted(unknown)}")


def _require_int(value: object, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ThumbnailFilesError(f"{field} 必须是整数，收到 {value!r}")
    if value < minimum:
        raise ThumbnailFilesError(f"{field} 不能小于 {minimum}")
    return value


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ThumbnailFilesError(f"{field} 必须是 64 位十六进制摘要")
    try:
        int(value, 16)
    except ValueError:
        raise ThumbnailFilesError(f"{field} 不是合法的十六进制摘要") from None
    return value


def _require_task_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_TASK_ID.match(value):
        raise ThumbnailFilesError(f"非法的 task_id：{value!r}")
    return value


def _validate_artifact(entry: object) -> dict:
    _require_exact_keys(entry, _ARTIFACT_KEYS, "artifacts[]")
    state = entry["artifact_state"]
    if state not in _ARTIFACT_STATES:
        raise ThumbnailFilesError(f"未知的 artifact_state：{state!r}")
    video_index = _require_int(entry["video_index"], "video_index", minimum=0)
    _require_digest(entry["source_sha256"], "source_sha256")
    _require_digest(entry["output_sha256"], "output_sha256")
    _require_digest(entry["overlay_sha256"], "overlay_sha256")
    _require_int(entry["width"], "width", minimum=1)
    _require_int(entry["height"], "height", minimum=1)
    _require_int(entry["bytes"], "bytes", minimum=0)
    if not isinstance(entry["artifact_id"], str) or not entry["artifact_id"]:
        raise ThumbnailFilesError("artifact_id 必须是非空字符串")
    if not isinstance(entry["relative_name"], str) or not entry["relative_name"]:
        raise ThumbnailFilesError("relative_name 必须是非空字符串")
    return {"video_index": video_index}


def _validate_manifest(raw: object, expected_task_id: str | None) -> dict:
    _require_exact_keys(raw, _MANIFEST_KEYS, "manifest")

    if raw["schema_version"] != SCHEMA_VERSION:
        raise ThumbnailFilesError(
            f"不支持的 schema_version {raw['schema_version']!r}，"
            f"当前实现只支持 {SCHEMA_VERSION}"
        )
    task_id = _require_task_id(raw["task_id"])
    if expected_task_id is not None and task_id != expected_task_id:
        raise ThumbnailFilesError(
            f"清单属于任务 {task_id}，与目录对应的 {expected_task_id} 不一致"
        )
    _require_int(raw["manifest_revision"], "manifest_revision", minimum=0)

    if not isinstance(raw["videos"], list):
        raise ThumbnailFilesError("videos 必须是数组")

    seen_indices: set[int] = set()
    for video in raw["videos"]:
        _require_exact_keys(video, _VIDEO_KEYS, "videos[]")
        index = _require_int(video["video_index"], "video_index", minimum=0)
        if index in seen_indices:
            raise ThumbnailFilesError(f"videos 中 video_index 重复：{index}")
        seen_indices.add(index)
        _require_digest(video["source_sha256"], "source_sha256")

        if not isinstance(video["artifacts"], list) or not video["artifacts"]:
            raise ThumbnailFilesError(f"video {index} 的 artifacts 必须是非空数组")

        seen_ids: set[str] = set()
        live_count = 0
        for entry in video["artifacts"]:
            info = _validate_artifact(entry)
            if info["video_index"] != index:
                raise ThumbnailFilesError(
                    f"产物 {entry['artifact_id']} 的 video_index 与所在条目不一致"
                )
            if entry["artifact_id"] in seen_ids:
                raise ThumbnailFilesError(
                    f"产物 ID 重复：{entry['artifact_id']}"
                )
            seen_ids.add(entry["artifact_id"])
            if entry["artifact_state"] == "live":
                live_count += 1

        if live_count > MAX_LIVE_ARTIFACTS_PER_VIDEO:
            raise ThumbnailFilesError(
                f"video {index} 的存活产物数 {live_count} 超过上限 "
                f"{MAX_LIVE_ARTIFACTS_PER_VIDEO}"
            )

        selected = video["selected_artifact_id"]
        if selected is not None:
            if not isinstance(selected, str) or selected not in seen_ids:
                raise ThumbnailFilesError(
                    f"video {index} 的 selected_artifact_id {selected!r} 不在产物列表中"
                )
    return raw


def load_manifest(path: Path, expected_task_id: str | None = None) -> dict:
    """读回并完整校验一份清单。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ThumbnailFilesError(f"无法读取清单 {path}：{exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ThumbnailFilesError(f"清单不是合法 JSON：{exc}") from exc
    return _validate_manifest(payload, expected_task_id)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_manifest(root: Path, manifest: dict) -> Path:
    """
    原子写入清单。同目录临时文件 + 改名，读者不会看到半个 JSON。

    临时文件与目标在同一目录，因此正常情况下不可能跨卷；但 macOS 上所有 APFS
    卷共用同一个 ``st_dev``，预检判断不出跨卷，所以真正的兜底是捕获 ``EXDEV``
    并转成类型化错误，而不是让它以底层 OSError 的形式漏出去。
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / MANIFEST_FILENAME
    canonical = _canonical_json(manifest)

    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=root, prefix=".manifest-", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, target)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        if exc.errno == errno.EXDEV:
            raise ThumbnailFilesError(
                f"清单目标与临时文件不在同一文件系统，无法原子替换：{root}"
            ) from exc
        raise
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    _fsync_dir(root)
    return target


def _resolve_inside(root: Path, relative_name: object) -> Path:
    """把清单里的相对名解析成根目录内的真实文件，拒绝任何形式的逃逸。

    符号链接检查必须在 ``resolve()`` **之前**：resolve 会跟随链接，
    指向根内的链接会被判为合法，指向根外的则报成"逃逸"——两种都掩盖了
    "产物文件本身是链接"这个真正的问题。
    """
    if not isinstance(relative_name, str) or not relative_name:
        raise ThumbnailFilesError("relative_name 必须是非空字符串")
    if os.path.isabs(relative_name) or relative_name.startswith("/"):
        raise ThumbnailFilesError(f"relative_name 不能是绝对路径：{relative_name!r}")
    if "\\" in relative_name or "/" in relative_name or ".." in relative_name:
        raise ThumbnailFilesError(f"relative_name 不能包含路径分隔符或上级引用：{relative_name!r}")

    candidate = root / relative_name
    if candidate.is_symlink():
        raise ThumbnailFilesError(f"产物文件是符号链接：{relative_name}")

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise ThumbnailFilesError(
            f"relative_name 指向根目录之外：{relative_name!r}"
        ) from None
    return candidate


def _require_artifact_file(root: Path, relative_name: str) -> Path:
    path = _resolve_inside(root, relative_name)
    if not path.is_file():
        raise ThumbnailFilesError(f"产物文件不存在：{relative_name}")
    return path


def _find_video(manifest: dict, video_index: int) -> dict:
    for video in manifest["videos"]:
        if video["video_index"] == video_index:
            return video
    raise ThumbnailFilesError(f"清单中没有 video_index {video_index}")


def append_artifact(
    root: Path,
    task_id: str,
    artifact: dict,
    expected_revision: int,
) -> dict:
    """把一个已渲染好的产物追加进清单，并把选择移到它身上。

    调用方必须先把 JPEG 写好。函数会校验文件真实存在、相对名不逃逸、
    产物 ID 不重复，并在替换清单前确认磁盘上的版本仍是自己读到的那一版。
    """
    root = Path(root)
    safe_task_id = _require_task_id(task_id)
    manifest_path = root / MANIFEST_FILENAME

    if manifest_path.is_file():
        manifest = load_manifest(manifest_path, expected_task_id=safe_task_id)
    else:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "task_id": safe_task_id,
            "manifest_revision": 0,
            "videos": [],
        }

    # 并发检查必须发生在任何写入之前。
    if manifest["manifest_revision"] != expected_revision:
        raise ThumbnailFilesError(
            f"清单 revision 不匹配：期望 {expected_revision}，"
            f"磁盘上是 {manifest['manifest_revision']}"
        )

    entry = dict(artifact)
    entry.setdefault("artifact_state", "live")
    entry.setdefault("purged_at", None)
    entry.setdefault("parent_artifact_id", None)
    entry.setdefault("previous_artifact_id", None)
    _validate_artifact(entry)
    _require_artifact_file(root, entry["relative_name"])

    video_index = entry["video_index"]
    if video_index in {v["video_index"] for v in manifest["videos"]}:
        video = _find_video(manifest, video_index)
    else:
        video = {
            "video_index": video_index,
            "source_sha256": entry["source_sha256"],
            "artifacts": [],
            "selected_artifact_id": None,
        }
        manifest["videos"].append(video)
        manifest["videos"].sort(key=lambda item: item["video_index"])

    if any(a["artifact_id"] == entry["artifact_id"] for a in video["artifacts"]):
        raise ThumbnailFilesError(f"产物 ID 已存在：{entry['artifact_id']}")

    live = sum(1 for a in video["artifacts"] if a["artifact_state"] == "live")
    if live >= MAX_LIVE_ARTIFACTS_PER_VIDEO:
        raise ThumbnailFilesError(
            f"video {video_index} 的存活产物已达上限 "
            f"{MAX_LIVE_ARTIFACTS_PER_VIDEO}：请先归档一个未选中的产物"
        )

    entry["previous_artifact_id"] = video["selected_artifact_id"]
    if video["selected_artifact_id"] is None:
        entry["parent_artifact_id"] = None
    video["artifacts"].append(entry)
    video["selected_artifact_id"] = entry["artifact_id"]

    manifest["manifest_revision"] = manifest["manifest_revision"] + 1
    write_manifest(root, _validate_manifest(manifest, safe_task_id))
    return manifest


def read_selection(manifest_path: Path, video_index: int) -> str | None:
    """返回某个视频当前选中的产物 ID；没有则返回 ``None``。"""
    try:
        manifest = load_manifest(manifest_path)
    except ThumbnailFilesError:
        return None
    for video in manifest["videos"]:
        if video["video_index"] == video_index:
            return video["selected_artifact_id"]
    return None


def list_artifacts(
    manifest_path: Path, video_index: int, live_only: bool = True
) -> list[dict]:
    """按时间顺序列出某个视频的产物。"""
    try:
        manifest = load_manifest(manifest_path)
    except ThumbnailFilesError:
        return []
    for video in manifest["videos"]:
        if video["video_index"] != video_index:
            continue
        entries = [
            entry
            for entry in video["artifacts"]
            if not live_only or entry["artifact_state"] == "live"
        ]
        return sorted(entries, key=lambda item: item["created_at"])
    return []


def recover_orphans(root: Path, manifest_path: Path) -> list[str]:
    """把清单未引用的 JPEG 移入隔离区，返回被处理的文件名。

    没有清单时不做任何事：此时可能正处在首次发布的中间，把整个目录当垃圾
    清空会毁掉正在进行的发布。源视频永远不参与这个判断。
    """
    root = Path(root)
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        return []

    referenced = set()
    try:
        manifest = load_manifest(manifest_path)
    except ThumbnailFilesError:
        # 清单本身坏了：宁可什么都不删，也不猜。
        return []
    for video in manifest["videos"]:
        for entry in video["artifacts"]:
            referenced.add(entry["relative_name"])

    quarantine = root / QUARANTINE_DIRNAME
    moved: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_file():
            continue
        if not _ARTIFACT_FILE.match(child.name):
            continue
        if child.name in referenced:
            continue
        quarantine.mkdir(parents=True, exist_ok=True)
        os.chmod(quarantine, 0o700)
        os.replace(child, quarantine / child.name)
        moved.append(child.name)

    if moved:
        _fsync_dir(root)
    return moved


def archive_artifact(
    manifest_path: Path, task_id: str, video_index: int, artifact_id: str
) -> dict:
    """把一个未选中的产物标记为已归档：删除文件，保留血缘记录。

    选中的产物永远不能被归档——它是当前 UI 展示的那一个。
    """
    safe_task_id = _require_task_id(task_id)
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path, expected_task_id=safe_task_id)
    video = _find_video(manifest, video_index)

    entry = next(
        (a for a in video["artifacts"] if a["artifact_id"] == artifact_id), None
    )
    if entry is None:
        raise ThumbnailFilesError(f"video {video_index} 中没有产物 {artifact_id}")
    if video["selected_artifact_id"] == artifact_id:
        raise ThumbnailFilesError(
            f"产物 {artifact_id} 当前处于选中状态，不能归档"
        )
    if entry["artifact_state"] == "purged":
        return manifest

    root = manifest_path.parent
    try:
        path = _resolve_inside(root, entry["relative_name"])
    except ThumbnailFilesError:
        path = None
    if path is not None and path.is_file() and not path.is_symlink():
        path.unlink()

    entry["artifact_state"] = "purged"
    entry["purged_at"] = _utcnow()
    manifest["manifest_revision"] = manifest["manifest_revision"] + 1
    write_manifest(root, _validate_manifest(manifest, safe_task_id))
    return manifest
