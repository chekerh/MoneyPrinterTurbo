"""在后台生成缩略图，让点击立刻返回。

为什么需要这一层：FFmpeg 抽帧最长 30 秒。直接跑在 Streamlit 的请求线程上会把
整个界面冻住，用户无法交互、看不到进度，只能刷新——而刷新又会把在途状态丢掉。
把工作移到一个**全进程唯一**的后台线程，并把状态落盘，界面就可以只做轮询。

几条不变量，改动时务必守住：

* **提交不阻塞。** :meth:`ThumbnailJobManager.submit` 只登记意图就返回。
* **轮询是只读的。** :meth:`status` 与 :meth:`list_jobs` 绝不启动任何子进程。
* **状态可恢复。** 状态写在 ``jobs.json``，新进程接手时仍能看到在途任务。
* **失败不覆盖成功。** 渲染失败只把任务标成 failed，绝不碰既有清单。
* **一个进程一个管理器。** 同一根目录反复取到同一实例；每个会话各起一个线程会
  和 Streamlit 的重跑语义打架，线程数也会随刷新无限膨胀。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from loguru import logger

from app.services.thumbnail import ThumbnailSpec, render_thumbnail
from app.services.thumbnail_files import (
    ThumbnailFilesError,
    append_artifact,
    load_manifest,
    next_artifact_id,
)

__all__ = [
    "JOB_STORE_FILENAME",
    "JobRecord",
    "JobState",
    "ThumbnailJobManager",
    "get_job_manager",
]

JOB_STORE_FILENAME = "jobs.json"

#: 队列深度。超过则拒绝提交而不是无限堆积——本地单机场景下堆积只会让
#: 用户以为卡死。
MAX_QUEUE_DEPTH = 4

#: 超过这个时长的任务在重启后视为未知：进程已经不在了，状态无从确认。
STALE_RUNNING_SECONDS = 15 * 60

_PERSIST_INTERVAL_SECONDS = 0.2


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_STATES = frozenset(
    {
        JobState.SUCCEEDED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        # unknown 表示"重启后没人知道结果了"。它不会再变化，因此必须算作
        # 终态：否则界面会一直等一个永远不会结束的作业。
        JobState.UNKNOWN.value,
    }
)


@dataclass
class JobRecord:
    """一个生成请求的完整状态。刻意保持可 JSON 序列化。"""

    job_id: str
    task_id: str
    state: str
    created_at: float
    updated_at: float
    video_index: int
    error: str = ""
    artifact_id: str = ""

    def to_json(self) -> dict:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "video_index": self.video_index,
            "error": self.error,
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_json(cls, raw: object) -> "JobRecord":
        if not isinstance(raw, dict):
            raise ValueError("job record must be an object")
        return cls(
            job_id=str(raw["job_id"]),
            task_id=str(raw["task_id"]),
            state=str(raw["state"]),
            created_at=float(raw["created_at"]),
            updated_at=float(raw["updated_at"]),
            video_index=int(raw["video_index"]),
            error=str(raw.get("error", "")),
            artifact_id=str(raw.get("artifact_id", "")),
        )


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _overlay_digest(request) -> str:
    """
    记录叠加文字的摘要。调用方给了就用它，没给就按文字本身算。

    早先这里写的是 ``request.get("overlay_sha256", "c" * 64)``——调用方没传就
    把 64 个 'c' 写进清单。面板那条路径自己会算摘要，所以问题只在 API/CLI/
    直接提交时暴露，但清单是长期留存的可信记录：写死一个假摘要，之后任何
    "这张图是不是这行文字生成的"核对都会得到错误答案，而且没人会发现。
    """
    supplied = request.get("overlay_sha256")
    if isinstance(supplied, str) and supplied.strip():
        return supplied
    text = request.get("overlay_text") or ""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


class ThumbnailJobManager:
    """一个根目录对应一个管理器，内含唯一的后台工作线程。"""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._cancelled: set[str] = set()
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_DEPTH)
        self._accepting = True
        self._last_persist = 0.0

        self._load()

        self._worker = threading.Thread(
            target=self._run, name="thumbnail-worker", daemon=True
        )
        self._worker.start()

        # 进程内其他实例（例如上一次 rerun 留下的）必须让位，否则会出现
        # 两个线程同时写同一个根目录下的清单。
        self._reap_previous()

    # -- 生命周期 ---------------------------------------------------------

    def _reap_previous(self) -> None:
        existing = _REGISTRY.get(str(self._root))
        if existing is not None and existing is not self:
            existing.shutdown(wait=False, timeout=1)
        _REGISTRY[str(self._root)] = self

    def shutdown(self, wait: bool = True, timeout: float = 5.0) -> None:
        """停止接收新任务，并让工作线程收尾。"""
        with self._lock:
            self._accepting = False
        if wait and self._worker.is_alive():
            self._worker.join(timeout=timeout)
        with self._lock:
            if _REGISTRY.get(str(self._root)) is self:
                _REGISTRY.pop(str(self._root), None)

    # -- 状态存取 ---------------------------------------------------------

    @property
    def store_path(self) -> Path:
        return self._root / JOB_STORE_FILENAME

    def _load(self) -> None:
        """
        读回上次留下的状态。文件损坏时从空状态开始——状态文件是可再生的缓存，
        为了它而拒绝服务没有意义。
        """
        if not self.store_path.is_file():
            return
        try:
            payload = json.loads(self.store_path.read_text(encoding="utf-8"))
            records = payload.get("jobs") or {}
            self._jobs = {
                key: JobRecord.from_json(value) for key, value in records.items()
            }
        except (OSError, ValueError, KeyError, TypeError):
            self._jobs = {}

        # 进程重启后，running 的任务其实已经没人执行了。能重新排队的就重新
        # 排队，让它真正跑完；只有超过阈值、已经无法判断结果时才标成
        # unknown，否则它会永远卡在"生成中"。
        cutoff = time.time() - STALE_RUNNING_SECONDS
        to_requeue = []
        for record in self._jobs.values():
            if record.state in (JobState.RUNNING.value, JobState.QUEUED.value):
                if record.updated_at < cutoff:
                    record.state = JobState.UNKNOWN.value
                    record.error = "任务在进程重启后失去执行者"
                else:
                    to_requeue.append(record)

        for record in to_requeue:
            try:
                self._queue.put_nowait(record.job_id)
            except queue.Full:
                # 队列已满：不能静默丢弃，先落地状态让用户看到排队中。
                self._persist(force=True)

    def _persist(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_persist < _PERSIST_INTERVAL_SECONDS:
            return
        self._last_persist = now
        payload = {
            "version": 1,
            "jobs": {key: rec.to_json() for key, rec in self._jobs.items()},
        }
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="wb", dir=self._root, prefix=".jobs-", suffix=".tmp", delete=False
            )
            temp_path = Path(handle.name)
            try:
                with handle:
                    handle.write(_canonical(payload))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, self.store_path)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        except OSError:
            # 状态落盘失败不应让生成失败：清单才是权威记录。
            return

    # -- 只读接口 ---------------------------------------------------------

    def status(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return record.to_json() if record else None

    def list_jobs(self, task_id: str) -> list[dict]:
        with self._lock:
            return [
                record.to_json()
                for record in self._jobs.values()
                if record.task_id == task_id
            ]

    def has_pending(self, task_id: str) -> bool:
        with self._lock:
            return any(
                record.task_id == task_id
                and record.state not in TERMINAL_STATES
                for record in self._jobs.values()
            )

    # -- 提交 -------------------------------------------------------------

    def submit(self, task_id: str, request: dict, tools=None) -> str:
        """登记一个生成请求并立刻返回 job_id。不等待渲染。"""
        with self._lock:
            if not self._accepting:
                raise RuntimeError("缩略图任务管理器已关闭，不再接受新任务")
            if self._queue.qsize() >= MAX_QUEUE_DEPTH:
                raise RuntimeError("缩略图队列已满，请稍后重试")

            now = time.time()
            job_id = uuid.uuid4().hex
            record = JobRecord(
                job_id=job_id,
                task_id=task_id,
                state=JobState.QUEUED.value,
                created_at=now,
                updated_at=now,
                video_index=int(request["video_index"]),
            )
            self._jobs[job_id] = record
            self._persist(force=True)

        self._queue.put((job_id, dict(request), tools))
        return job_id

    def cancel_all(self, task_id: str) -> int:
        """把某个任务下所有非终态的作业标记为已取消。"""
        cancelled = 0
        with self._lock:
            for record in self._jobs.values():
                if record.task_id != task_id or record.state in TERMINAL_STATES:
                    continue
                self._cancelled.add(record.job_id)
                if record.state == JobState.QUEUED.value:
                    record.state = JobState.CANCELLED.value
                    record.updated_at = time.time()
                    cancelled += 1
            self._persist(force=True)
        return cancelled

    # -- 工作线程 ---------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                with self._lock:
                    if not self._accepting:
                        return
                continue

            job_id, request, tools = item
            with self._lock:
                if job_id in self._cancelled:
                    continue
                record = self._jobs.get(job_id)
                if record is None or record.state in TERMINAL_STATES:
                    continue
                record.state = JobState.RUNNING.value
                record.updated_at = time.time()
                self._persist(force=True)

            try:
                artifact_id = self._execute(job_id, record.task_id, request, tools)
            except Exception as exc:  # noqa: BLE001 - 单个作业失败不影响队列
                with self._lock:
                    record = self._jobs.get(job_id)
                    if record is not None:
                        record.state = JobState.FAILED.value
                        record.error = str(exc)[:300]
                        record.updated_at = time.time()
                    self._persist(force=True)
            else:
                with self._lock:
                    record = self._jobs.get(job_id)
                    if record is not None:
                        record.state = JobState.SUCCEEDED.value
                        record.artifact_id = artifact_id
                        record.updated_at = time.time()
                    self._persist(force=True)
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str, task_id: str, request: dict, tools) -> str:
        """
        渲染一张缩略图并把它登记进清单。失败时不碰既有清单。

        ``task_id`` 由调用方从作业记录传入，而不是要求调用方在 request 里重复
        一次——两者不一致会让产物写进错误的清单。
        """
        root = self._root
        resolved_tools = tools if tools is not None else _shared_tools()

        # 候选帧：一次渲染多个时刻，让用户挑最好看的那张。
        # 只给一个时刻的话，"选帧"这个最关键的创作决定就被省掉了。
        requested = request.get("frame_candidates")
        moments = [None]
        if requested:
            from app.services.thumbnail import _probe_duration_ms, candidate_timestamps

            try:
                duration_ms = _probe_duration_ms(
                    Path(request["source"]), resolved_tools.ffprobe
                )
                moments = candidate_timestamps(duration_ms, int(requested))
            except Exception as exc:  # noqa: BLE001
                # 候选帧是增强，不是前提。探测时长失败就退回单帧自动抽帧：
                # 给用户一张能用的图，比直接报错强。
                logger.debug(
                    f"frame candidates unavailable, falling back to one frame: "
                    f"error={type(exc).__name__}: {exc}"
                )
                moments = [None]

        last_artifact_id = ""
        for moment in moments:
            last_artifact_id = self._render_one(
                task_id, request, resolved_tools, root, moment
            )
        return last_artifact_id

    def _render_one(
        self, task_id: str, request: dict, resolved_tools, root: Path, moment
    ) -> str:
        """渲染并登记一张。失败时清掉临时文件，不碰既有清单。"""
        artifact_id = next_artifact_id()
        staged = root / f".pending-{artifact_id}.jpg"

        try:
            result = render_thumbnail(
                ThumbnailSpec(
                    source=Path(request["source"]),
                    output=staged,
                    overlay_text=request.get("overlay_text", ""),
                    font_path=Path(request["font_path"]),
                    frame_at_seconds=moment,
                ),
                tools=resolved_tools,
            )
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

        final = root / f"thumbnail-{artifact_id}.jpg"
        os.replace(staged, final)
        os.chmod(final, 0o600)

        manifest_path = root / "manifest.json"
        if manifest_path.is_file():
            manifest = load_manifest(manifest_path, expected_task_id=task_id)
        else:
            manifest = {
                "schema_version": 2,
                "task_id": task_id,
                "manifest_revision": 0,
                "videos": [],
            }

        try:
            append_artifact(
                root,
                task_id,
                {
                    "artifact_id": artifact_id,
                    "video_index": int(request["video_index"]),
                    "parent_artifact_id": None,
                    "previous_artifact_id": None,
                    "relative_name": final.name,
                    "source_sha256": request["source_sha256"],
                    "output_sha256": result.output_sha256,
                    "overlay_sha256": _overlay_digest(request),
                    "font_id": result.font_id,
                    "frame_at_seconds": result.frame_at_seconds,
                    "width": result.width,
                    "height": result.height,
                    "bytes": result.output_bytes,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                    "artifact_state": "live",
                    "purged_at": None,
                },
                expected_revision=manifest["manifest_revision"],
            )
        except ThumbnailFilesError:
            # 清单写不进去就把 JPEG 撤掉，绝不留下"文件在、记录里没有"的孤儿。
            final.unlink(missing_ok=True)
            raise

        return artifact_id


_TOOLS_CACHE: dict = {}
_TOOLS_LOCK = threading.Lock()


def _shared_tools():
    """进程内共享的工具链探测结果。

    ``probe_media_tools()`` 要跑两次 ``-version`` 并给两个二进制做 SHA-256；
    每个作业探一次会让"提交后立刻返回"这件事在实际使用中变成可感知的等待。
    工具链在一个进程生命周期内不会变，因此探一次就够。
    """
    with _TOOLS_LOCK:
        if "tools" not in _TOOLS_CACHE:
            from app.utils.strict_media_tools import probe_media_tools

            _TOOLS_CACHE["tools"] = probe_media_tools()
        return _TOOLS_CACHE["tools"]


#: 根目录 -> 管理器。保证同一进程内同一个根只有一个工作线程。
_REGISTRY: dict[str, ThumbnailJobManager] = {}
_REGISTRY_LOCK = threading.Lock()


def get_job_manager(root: Path) -> ThumbnailJobManager:
    """取得（必要时创建）某个根目录对应的管理器。"""
    key = str(Path(root))
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is not None:
            return existing
    return ThumbnailJobManager(Path(root))
