"""已完成任务的缩略图面板。

三条约束决定了这里的写法：

* **身份来自描述符。** 视频行只按 :class:`ResultDescriptor` 里的条目渲染，
  不遍历目录、不接受调用方传来的索引。清单里多出来的条目会被忽略。
* **重跑不干活。** FFmpeg 只在用户显式点击"生成"时执行，且渲染发生在按钮
  回调里。普通的 Streamlit 重跑只读清单，不会启动任何子进程。
* **失败不覆盖成功。** 生成失败时保留上一个产物并显示原因，绝不把已有
  预览换成空白。

已知的取舍：这是同步渲染，30 秒的 FFmpeg 上限会占用这一次回调。异步任务
运行时属于后续步骤；此处先保证"重跑不干活"和"失败不覆盖"这两条更重要的性质。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.services.result_descriptor import ResultDescriptor
from app.services.thumbnail import (
    MAX_OVERLAY_CHARS,
    ThumbnailError,
    ThumbnailSpec,
    render_thumbnail,
)
from app.services.thumbnail_files import (
    MAX_LIVE_ARTIFACTS_PER_VIDEO,
    ThumbnailFilesError,
    append_artifact,
    archive_artifact,
    load_manifest,
    next_artifact_id,
)
from app.utils.strict_media_tools import MediaTools

__all__ = [
    "MAX_OVERLAY_CHARS",
    "PanelDecision",
    "VideoRow",
    "build_panel_plan",
    "normalize_overlay_text",
    "plan_for_video",
    "render_thumbnail_panel",
]


@dataclass(frozen=True)
class VideoRow:
    """一个视频在面板上的状态。"""

    video_index: int
    selected_artifact_id: str | None
    thumbnail_path: Path | None
    can_generate: bool
    needs_archive: bool
    live_count: int
    archivable_ids: tuple[str, ...]
    messages: tuple[str, ...] = ()

    @property
    def has_thumbnail(self) -> bool:
        return self.thumbnail_path is not None


@dataclass(frozen=True)
class PanelDecision:
    """整个面板的渲染计划。纯数据，可独立测试。"""

    rows: tuple[VideoRow, ...] = ()
    messages: tuple[str, ...] = field(default_factory=tuple)


def normalize_overlay_text(value: object) -> str | None:
    """校验并归一化叠加文字；不合格时返回 ``None`` 之外的可读原因。

    :returns: 归一化后的文字，或 ``None`` 表示"不叠加"。文字不合法时抛出
        :class:`ValueError`，消息可直接展示给用户。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Thumbnail Overlay Invalid")

    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > MAX_OVERLAY_CHARS:
        # 抛出的都是可翻译的 key，不是内部实现细节：这些消息会直接显示给用户。
        raise ValueError("Thumbnail Overlay Too Long")

    # 控制字符与双向覆盖字符的判定复用渲染层的规则，避免两处实现漂移。
    from app.services.thumbnail import _validate_text

    try:
        return _validate_text(stripped)
    except ThumbnailError:
        raise ValueError("Thumbnail Overlay Invalid") from None


def _resolve_selected_file(root: Path, manifest: dict, video_index: int) -> tuple[str | None, Path | None, tuple[str, ...]]:
    """返回 (选中的产物 ID, 文件路径, 提示消息)。"""
    messages: list[str] = []
    for video in manifest["videos"]:
        if video["video_index"] != video_index:
            continue
        selected = video["selected_artifact_id"]
        if selected is None:
            return None, None, ()
        for entry in video["artifacts"]:
            if entry["artifact_id"] != selected:
                continue
            if entry["artifact_state"] != "live":
                return selected, None, ("Thumbnail Artifact Missing",)
            candidate = root / entry["relative_name"]
            if candidate.is_symlink() or not candidate.is_file():
                messages.append("Thumbnail Artifact Missing")
                return selected, None, tuple(messages)
            return selected, candidate, ()
    return None, None, ()


def plan_for_video(
    video_index: int,
    manifest: dict | None,
    thumbnail_root: Path,
    task_id: str | None = None,
) -> VideoRow:
    """根据清单推导单个视频的面板状态。"""
    root = Path(thumbnail_root)
    messages: list[str] = []

    if manifest is None:
        return VideoRow(
            video_index=video_index,
            selected_artifact_id=None,
            thumbnail_path=None,
            can_generate=True,
            needs_archive=False,
            live_count=0,
            archivable_ids=(),
        )

    if task_id is not None and manifest.get("task_id") != task_id:
        raise ThumbnailFilesError(
            f"清单属于任务 {manifest.get('task_id')}，与 {task_id} 不一致"
        )

    selected_id, selected_path, found_messages = _resolve_selected_file(
        root, manifest, video_index
    )
    messages.extend(found_messages)

    live = [
        entry
        for video in manifest["videos"]
        if video["video_index"] == video_index
        for entry in video["artifacts"]
        if entry["artifact_state"] == "live"
    ]
    live_count = len(live)
    archivable = tuple(
        entry["artifact_id"] for entry in live if entry["artifact_id"] != selected_id
    )
    at_cap = live_count >= MAX_LIVE_ARTIFACTS_PER_VIDEO

    return VideoRow(
        video_index=video_index,
        selected_artifact_id=selected_id,
        thumbnail_path=selected_path,
        can_generate=not at_cap and not messages,
        needs_archive=at_cap,
        live_count=live_count,
        archivable_ids=archivable,
        messages=tuple(messages),
    )


def build_panel_plan(
    descriptor: ResultDescriptor,
    manifest_path: Path,
    task_id: str,
    thumbnail_root: Path | None = None,
) -> PanelDecision:
    """从描述符与清单推导出完整面板计划。

    清单缺失或损坏都不会让面板崩掉：前者是"还没有缩略图"，后者会显示一条
    明确的消息，因为那意味着磁盘状态与预期不符。
    """
    root = Path(thumbnail_root) if thumbnail_root is not None else Path(manifest_path).parent
    messages: list[str] = []

    # 描述符也必须属于当前任务。少了这一条，调用方把别的任务的描述符传进来
    # 时，面板会拿本任务的清单去解释那份描述符，展示出错的视频。
    if descriptor.task_id != task_id:
        return PanelDecision(
            rows=(),
            messages=("Thumbnail Descriptor Mismatch",),
        )

    manifest: dict | None = None
    path = Path(manifest_path)
    if path.is_file():
        try:
            manifest = load_manifest(path, expected_task_id=task_id)
        except ThumbnailFilesError:
            manifest = None
            messages.append("Thumbnail Manifest Unreadable")

    rows = tuple(
        plan_for_video(entry.video_index, manifest, root, task_id=task_id)
        for entry in descriptor.videos
    )
    return PanelDecision(rows=rows, messages=tuple(messages))


def _default_font() -> Path:
    from app.utils.utils import resource_dir

    return Path(resource_dir("fonts")) / "Charm-Regular.ttf"


def render_thumbnail_panel(
    descriptor: ResultDescriptor,
    manifest_path: Path,
    thumbnail_root: Path,
    source_root: Path,
    tools: MediaTools,
    tr: Callable[[str], str],
    font_path: Path | None = None,
) -> None:
    """
    渲染面板。仅在按钮回调里才会调用 :func:`render_thumbnail`。

    :param source_root: 成片所在目录（通常是 ``storage/tasks/<task_id>``）。
        描述符是纯数据，不持有文件系统句柄，因此源路径由调用方给出，
        相对名仍然只取自描述符。
    """
    import streamlit as st

    task_id = descriptor.task_id
    root = Path(thumbnail_root)
    font = Path(font_path) if font_path is not None else _default_font()

    st.subheader(tr("Thumbnail"))

    for entry in descriptor.videos:
        plan = build_panel_plan(descriptor, Path(manifest_path), task_id, root)
        row = next(r for r in plan.rows if r.video_index == entry.video_index)

        st.markdown(f"**{tr('Thumbnail Video {index}').format(index=entry.video_index)}**")

        for message in row.messages:
            st.warning(tr(message))

        if row.thumbnail_path is not None:
            # 清单校验保证文件存在且非符号链接，但不保证它还能被解码。
            # 直接把异常抛给 st.image 会中断整个结果页的渲染，因此这里降级为提示。
            try:
                st.image(str(row.thumbnail_path), width=280)
            except Exception:  # noqa: BLE001 - 任何解码/读取失败都只影响这一张预览
                st.warning(tr("Thumbnail Preview Unavailable"))
        else:
            st.caption(tr("Thumbnail No Preview Yet"))

        text_key = f"thumbnail_overlay_{task_id}_{entry.video_index}"
        overlay = st.text_input(
            tr("Thumbnail Overlay"),
            key=text_key,
            max_chars=MAX_OVERLAY_CHARS,
        )

        error_key = f"thumbnail_error_{task_id}_{entry.video_index}"
        if st.session_state.get(error_key):
            st.error(tr(st.session_state[error_key]))

        button_label = tr("Thumbnail Regenerate" if row.selected_artifact_id else "Thumbnail Generate")
        if st.button(
            button_label,
            key=f"thumbnail_generate_{task_id}_{entry.video_index}",
            disabled=not row.can_generate,
        ):
            _generate_one(
                descriptor=descriptor,
                entry=entry,
                root=root,
                source_root=Path(source_root),
                manifest_path=Path(manifest_path),
                tools=tools,
                font=font,
                overlay=overlay,
                error_key=error_key,
            )

        if row.needs_archive and row.archivable_ids:
            st.caption(tr("Thumbnail Storage Full"))
            for candidate in row.archivable_ids:
                if st.button(
                    tr("Thumbnail Archive {index}").format(index=candidate[:8]),
                    key=f"thumbnail_archive_{task_id}_{entry.video_index}_{candidate[:8]}",
                ):
                    _archive_one(
                        manifest_path=Path(manifest_path),
                        task_id=task_id,
                        video_index=entry.video_index,
                        artifact_id=candidate,
                        error_key=error_key,
                    )

        st.divider()


def _generate_one(
    descriptor: ResultDescriptor,
    entry,
    root: Path,
    source_root: Path,
    manifest_path: Path,
    tools: MediaTools,
    font: Path,
    overlay: str,
    error_key: str,
) -> None:
    """执行一次生成，并把结果追加进清单。失败时保留错误消息。"""
    import streamlit as st

    try:
        text = normalize_overlay_text(overlay)
    except ValueError as exc:
        st.session_state[error_key] = str(exc)
        return


    artifact_id = next_artifact_id()
    staged = root / f".pending-{artifact_id}.jpg"
    try:
        result = render_thumbnail(
            ThumbnailSpec(
                source=source_root / entry.relative_name,
                output=staged,
                overlay_text=text or "",
                font_path=font,
                duration_ms=entry.duration_ms,
            ),
            tools=tools,
        )
        final = root / f"thumbnail-{artifact_id}.jpg"
        staged.replace(final)

        manifest = load_manifest(manifest_path, expected_task_id=descriptor.task_id) \
            if manifest_path.is_file() else _empty_manifest(descriptor.task_id)
        append_artifact(
            root,
            descriptor.task_id,
            {
                "artifact_id": artifact_id,
                "video_index": entry.video_index,
                "parent_artifact_id": None,
                "previous_artifact_id": None,
                "relative_name": final.name,
                "source_sha256": entry.sha256,
                "output_sha256": result.output_sha256,
                "overlay_sha256": _text_digest(text or ""),
                "font_id": result.font_id,
                "frame_at_seconds": result.frame_at_seconds,
                "width": result.width,
                "height": result.height,
                "bytes": result.output_bytes,
                "created_at": _now(),
                "artifact_state": "live",
                "purged_at": None,
            },
            expected_revision=manifest["manifest_revision"],
        )
        st.session_state.pop(error_key, None)
    except ThumbnailError:
        staged.unlink(missing_ok=True)
        st.session_state[error_key] = "Thumbnail Generate Failed"
    except (ThumbnailFilesError, OSError):
        staged.unlink(missing_ok=True)
        st.session_state[error_key] = "Thumbnail Record Failed"


def _archive_one(
    manifest_path: Path, task_id: str, video_index: int, artifact_id: str, error_key: str
) -> None:
    import streamlit as st

    try:
        archive_artifact(manifest_path, task_id, video_index, artifact_id)
        st.session_state.pop(error_key, None)
    except (ThumbnailFilesError, OSError):
        st.session_state[error_key] = "Thumbnail Archive Failed"


def _empty_manifest(task_id: str) -> dict:
    return {
        "schema_version": 2,
        "task_id": task_id,
        "manifest_revision": 0,
        "videos": [],
    }


def _text_digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
