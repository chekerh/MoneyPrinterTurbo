"""后台发布作业。

发布一条成片是一次 ``subprocess.run``，超时上限 900 秒——上传 54 MB 视频、加上
浏览器的登录与上传流程，几分钟是常态。而它原先是**直接在 Streamlit 的渲染路径里
同步跑的**，于是整个页面在上传期间完全冻结：不能滚动、不能切视图、连关标签页之前
都得等。进度条转的是 Streamlit 自己的 spinner，而那恰恰是最没有信息量的等待。

这里把它改成后台作业：

* 按钮只负责**启动**作业，立刻返回；
* 一个 ``@st.fragment`` 每两秒轮询作业状态，只重画那一小块；
* 作业完成后触发一次整页刷新，让计划行的「已发布」标记与成片库状态立刻反映出来。

作业登记放在模块级而不是 session_state：页面刷新、切视图、重开浏览器都不该让一次
正在进行的上传凭空"消失"——那会让用户以为没点上，从而再点一次，于是同一条片子被
发布两遍。而 ``youtube_uploader`` 的可见性是硬编码 public，重复发布无法撤回。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

_LOCK = threading.RLock()
_JOBS: dict[str, "PublishJob"] = {}


@dataclass
class PublishJob:
    """一次发布作业的状态。``result`` 在 ``done`` 之前是 ``None``。"""

    key: str
    label: str
    status: str = "running"  # running | done | failed
    result: dict | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def done(self) -> bool:
        return self.status in ("done", "failed")

    def snapshot(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "result": self.result,
            "elapsed": int((self.finished_at or time.time()) - self.started_at),
        }


def job_key(*parts: Any) -> str:
    return "␟".join(str(part) for part in parts)


def start_publish_job(key: str, label: str, work: Callable[[], dict]) -> PublishJob:
    """启动（或复用）一次发布作业。

    已经在跑就直接返回那一个，而不是再开一条线程：重复启动意味着同一条片子被
    发布两遍，而发布是公开可见且无法撤回的。
    """
    with _LOCK:
        existing = _JOBS.get(key)
        if existing is not None and not existing.done:
            return existing
        job = PublishJob(key=key, label=label)
        _JOBS[key] = job

    def runner():
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - 作业线程必须自己兜住
            result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        with _LOCK:
            job.result = result
            job.status = "done" if result.get("success") else "failed"
            job.finished_at = time.time()

    threading.Thread(
        target=runner, name=f"publish-{key[:40]}", daemon=True
    ).start()
    return job


def get_job(key: str) -> PublishJob | None:
    with _LOCK:
        return _JOBS.get(key)


def is_running(key: str) -> bool:
    job = get_job(key)
    return job is not None and not job.done


def clear_finished_jobs() -> None:
    """丢掉已结束的作业，只留正在跑的。

    界面上不再显示的作业继续留在表里会慢慢堆成内存泄漏。
    """
    with _LOCK:
        for key in [k for k, job in _JOBS.items() if job.done]:
            _JOBS.pop(key, None)


def all_jobs() -> list[PublishJob]:
    with _LOCK:
        return list(_JOBS.values())
