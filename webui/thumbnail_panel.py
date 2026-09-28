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

import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.services.result_descriptor import ResultDescriptor
from app.services.thumbnail import (
    MAX_OVERLAY_CHARS,
    ThumbnailError,
)
from app.services.thumbnail_files import (
    MAX_LIVE_ARTIFACTS_PER_VIDEO,
    ThumbnailFilesError,
    archive_artifact,
    load_manifest,
    select_artifact,
)
from app.services.thumbnail import CANDIDATE_FRAME_COUNT as FRAME_CANDIDATE_COUNT

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


def _pending_job_for(task_id: str, video_index: int):
    """返回该视频当前未结束的作业记录；没有则返回 ``None``。只读。"""
    from app.services.thumbnail_jobs import TERMINAL_STATES, get_job_manager

    try:
        manager = get_job_manager(Path(_job_root_for(task_id)))
    except Exception:  # noqa: BLE001 - 状态查询失败不应让面板崩掉
        return None
    candidates = [
        job
        for job in manager.list_jobs(task_id)
        if job["video_index"] == video_index and job["state"] not in TERMINAL_STATES
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda job: job["created_at"])


def _job_summary(tr: Callable[[str], str], job) -> None:
    """把作业状态渲染成一行提示。只读，不触发任何工作。"""
    import streamlit as st

    if job["state"] == "queued":
        st.info(tr("Thumbnail Queued"))
    elif job["state"] == "running":
        with st.spinner(tr("Thumbnail Generating")):
            time.sleep(0.2)


_JOB_ROOT_CACHE: dict[str, Path] = {}


def _should_request_refresh(records) -> bool:
    """
    是否应当触发一次整页刷新。

    只有当**确实有过作业**并且**现在全部结束**时才刷新。空列表返回 False
    是关键：否则一个没有任何待办任务的结果页会不停地自我刷新，把 CPU 和
    浏览器电量吃光。
    """
    from app.services.thumbnail_jobs import TERMINAL_STATES

    if not records:
        return False
    return all(record["state"] in TERMINAL_STATES for record in records)


def watch_for_completion(task_id: str, thumbnail_root: Path) -> bool:
    """
    检查该任务是否还有在途缩略图作业；有则返回 ``True``，交给调用方挂轮询。

    只读：不触发任何渲染。
    """
    from app.services.thumbnail_jobs import get_job_manager

    _JOB_ROOT_HOLDER[task_id] = str(thumbnail_root)
    try:
        manager = get_job_manager(Path(thumbnail_root))
        return manager.has_pending(task_id)
    except Exception:  # noqa: BLE001 - 状态查询失败不应让面板崩掉
        return False


def _job_root_for(task_id: str) -> str:
    return _JOB_ROOT_HOLDER.get(task_id, "")


_JOB_ROOT_HOLDER: dict[str, str] = {}


def render_thumbnail_panel(
    descriptor: ResultDescriptor,
    manifest_path: Path,
    thumbnail_root: Path,
    source_root: Path,
    tr: Callable[[str], str],
    font_path: Path | None = None,
) -> None:
    """
    渲染面板。

    点击"生成"只登记一个后台作业就返回，FFmpeg 跑在
    :mod:`app.services.thumbnail_jobs` 的工作线程上。这样请求线程不会被最长
    30 秒的抽帧冻住，界面靠轮询作业状态更新。

    :param source_root: 成片所在目录（通常是 ``storage/tasks/<task_id>``）。
        描述符是纯数据，不持有文件系统句柄，因此源路径由调用方给出，
        相对名仍然只取自描述符。

    这里刻意不接受工具链参数：渲染已经移到后台线程，工具链由
    :mod:`app.services.thumbnail_jobs` 在进程内共享解析一次，调用方再传一份
    只会造成"面板以为解析好了、后台其实没解析"的分歧。
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

        job = _pending_job_for(task_id, entry.video_index)
        if job is not None:
            _job_summary(tr, job)
            st.divider()
            continue

        button_label = tr(
            "Thumbnail Regenerate" if row.selected_artifact_id else "Thumbnail Generate"
        )
        if st.button(
            button_label,
            key=f"thumbnail_generate_{task_id}_{entry.video_index}",
            disabled=not row.can_generate,
        ):
            _submit_one(
                descriptor=descriptor,
                entry=entry,
                root=root,
                source_root=Path(source_root),
                font=font,
                overlay=overlay,
                error_key=error_key,
            )
            # 提交失败时要立刻显示。上面的检查发生在点击之前，只看它的话
            # 消息要等到下一次重跑才出现——正好是用户最想看到它的时刻。
            if st.session_state.get(error_key):
                st.error(tr(st.session_state[error_key]))

        # 选帧：一次生成多张，让用户挑最好看的那一个。
        # YouTube 允许上传前指定缩略图，所以"哪张最好看"必须是人的决定，
        # 而不是"最后生成的那张"自动胜出。
        if row.selected_artifact_id and len(row.archivable_ids) + 1 > 1:
            options = [row.selected_artifact_id, *row.archivable_ids]
            labels = {
                aid: tr("Thumbnail Candidate {index}").format(index=pos + 1)
                for pos, aid in enumerate(options)
            }
            chosen = st.radio(
                tr("Choose Thumbnail"),
                options=options,
                index=0,
                format_func=lambda aid: labels.get(aid, aid[:8]),
                key=f"thumbnail_pick_{task_id}_{entry.video_index}",
                horizontal=True,
            )
            if chosen and chosen != row.selected_artifact_id:
                _select_one(
                    manifest_path=Path(manifest_path),
                    task_id=task_id,
                    video_index=entry.video_index,
                    artifact_id=chosen,
                    error_key=error_key,
                )
                st.rerun()

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


def _submit_one(
    descriptor: ResultDescriptor,
    entry,
    root: Path,
    source_root: Path,
    font: Path,
    overlay: str,
    error_key: str,
) -> None:
    """登记一个后台生成作业。立刻返回，不做任何渲染。"""
    import hashlib

    import streamlit as st

    from app.services.thumbnail_jobs import get_job_manager

    try:
        text = normalize_overlay_text(overlay)
    except ValueError as exc:
        st.session_state[error_key] = str(exc)
        return

    request = {
        "source": str(source_root / entry.relative_name),
        "overlay_text": text or "",
        "font_path": str(font),
        "video_index": entry.video_index,
        "source_sha256": entry.sha256,
        "overlay_sha256": hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
        # 一次渲染多个时刻，让用户挑最好看的那张。只给一个时刻的话，
        # "选帧"这个最关键的创作决定就被省掉了。
        "frame_candidates": FRAME_CANDIDATE_COUNT,
    }

    _JOB_ROOT_HOLDER[descriptor.task_id] = str(root)
    try:
        get_job_manager(root).submit(descriptor.task_id, request)
    except (RuntimeError, OSError):
        st.session_state[error_key] = "Thumbnail Queue Unavailable"
        return
    st.session_state.pop(error_key, None)


def _archive_one(
    manifest_path: Path, task_id: str, video_index: int, artifact_id: str, error_key: str
) -> None:
    import streamlit as st

    try:
        archive_artifact(manifest_path, task_id, video_index, artifact_id)
        st.session_state.pop(error_key, None)
    except (ThumbnailFilesError, OSError):
        st.session_state[error_key] = "Thumbnail Archive Failed"


def _select_one(
    manifest_path: Path, task_id: str, video_index: int, artifact_id: str, error_key: str
) -> None:
    import streamlit as st

    try:
        select_artifact(manifest_path, video_index, artifact_id)
        st.session_state.pop(error_key, None)
    except (ThumbnailFilesError, OSError):
        st.session_state[error_key] = "Thumbnail Select Failed"


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
