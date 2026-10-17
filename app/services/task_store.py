"""任务状态落盘。

任务状态此前只存在内存里（``MemoryState``），于是：

* 进程一重启，**整张任务历史连同每条错误信息全部消失**。用户看到的不是"失败"，
  而是一条状态含糊的旧任务——``failed_stage`` 与 ``error`` 是排障时唯一能说明
  "到底卡在哪一步、为什么"的东西，而它们活不过一次重启；
* 通过 API 提交的异步任务尤其糟糕：调用方拿到的 ``task_id`` 在服务重启后查不到任何
  状态，既不能确认成功，也不能确认失败。

落盘的位置刻意选在 ``storage/state/`` 而不是任务目录里：任务目录会被清理脚本
按"选择性清除中间产物"的规则扫过，把状态放进去等于把它一起删掉。

写入用**先写临时文件再改名**。直接原地覆写在进程被 kill 的瞬间会留下半截 JSON，
而那比没有文件更糟：下次启动读到坏文件，整个状态就再也加载不出来了。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from app.utils import utils

logger = logging.getLogger(__name__)

_STATE_FILE = "tasks.json"
# 只保留这么多条，防止无节制增长。保留最近的即可：更老的任务在文件层面还在
# （成片、脚本、月度计划都在），丢的只是界面上的状态摘要。
_MAX_RECORDS = 500
_LOCK = threading.RLock()


def state_file_path() -> Path:
    return Path(utils.storage_dir("state", create=True)) / _STATE_FILE


def _serializable(task: dict) -> dict:
    """只保留能安全写进 JSON 的字段。

    任务记录里可能混进不可序列化的对象（异常、句柄）。写盘失败不该让生成流程
    崩掉——状态是辅助信息，不是主路径。
    """
    clean: dict = {}
    for key, value in dict(task).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        elif isinstance(value, (list, tuple)):
            clean[key] = [item for item in value if isinstance(item, (str, int, float, bool))]
        elif isinstance(value, dict):
            clean[key] = {
                k: v for k, v in value.items() if isinstance(v, (str, int, float, bool))
            }
        else:
            clean[key] = str(value)
    return clean


def save_tasks(tasks: dict, removed: set | None = None) -> None:
    """把任务状态写盘。失败只记日志，不抛出。

    本应用有两个进程（WebUI 与 API）各自持有一份 ``MemoryState``，而状态文件只有
    一份。若直接拿本进程的内存快照整份覆写，**另一个进程的任务会被整体抹掉**：
    WebUI 写完自己的 3 条之后，API 进程一次进度更新就会把文件重写成它启动时读到的
    旧内容，重启后 WebUI 那边的历史与每条 ``error`` 全部消失——正是本模块存在的理由
    所要防的那种故障。

    因此这里做"读-合并-写"：磁盘上的既有记录作为底，叠加本进程的更新，并扣掉本进程
    显式删除的条目。合并在 ``_LOCK`` 内完成，锁只保护单进程内的并发；两个进程真正
    的互斥交给操作系统写原子的 ``os.replace``，同名临时文件的竞争因此被排除。

    ``removed`` 必须由调用方显式传入删除集合：靠"内存里没有"来推断删除，会让磁盘上
    属于另一个进程、只是本进程没见过的记录被当成删除。
    """
    path = state_file_path()
    with _LOCK:
        merged: dict = {}
        if path.is_file():
            # 合并失败时退化成"只写本进程"，宁可少记也不丢别的进程的数据。
            try:
                with open(path, encoding="utf-8") as handle:
                    existing = json.load(handle)
                if isinstance(existing, dict) and isinstance(
                    existing.get("tasks"), dict
                ):
                    merged = {
                        task_id: dict(task)
                        for task_id, task in existing["tasks"].items()
                        if isinstance(task, dict)
                    }
            except (OSError, ValueError):
                logger.warning("unreadable task state while merging; writing own view")

        # 顺序也在这里定下来，而且必须由**本进程内存里的顺序**决定：``MemoryState``
        # 每次更新都会把任务挪到 dict 末尾，所以内存顺序 = 最近更新顺序。合并若沿用
        # 磁盘上的顺序，"本进程刚刚更新过的任务"这个信息就永远传不到文件里，上限
        # 依旧只会按最早的创建时间淘汰——而长跑任务恰恰是最早创建、也最不该丢的那条。
        # 于是先按内存顺序铺好本进程的任务，再把只存在于磁盘上的（属于另一个进程的）
        # 追加在后面。
        for task_id in removed or ():
            merged.pop(task_id, None)
        merged = {
            task_id: task
            for task_id, task in tasks.items()
        } | {
            task_id: task
            for task_id, task in merged.items()
            if task_id not in tasks
        }

        # 容量上限按"最近更新的"保留，而不是按创建顺序。``update_task`` 重新赋值
        # 不会改变 dict 里的位置，所以一条跑了几分钟、进度一直在变的任务会随着新任务
        # 涌入而被优先淘汰——它恰恰是最不该丢的那条。
        trimmed = list(merged.items())[-_MAX_RECORDS:]
        payload = {
            "version": 1,
            "tasks": {
                task_id: _serializable(task) for task_id, task in trimmed
            },
        }
        # 临时文件名必须唯一：固定的 tasks.tmp 会让两个进程同时写入同一个路径，
        # 交错写出的半截 JSON 会被 os.replace 原子地"发布"成正式文件，下次启动
        # 只能读出损坏并整体丢弃。
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            logger.warning(f"failed to persist task state: {exc}")
            try:
                temporary.unlink()
            except OSError:
                pass


def load_tasks() -> dict:
    """读回任务状态。文件不存在或损坏时返回空字典。

    坏文件必须**降级**而不是抛错：状态只是辅助信息，为了它让整个应用起不来是
    本末倒置。损坏的文件会被改名留档，而不是留在原地反复失败。
    """
    path = state_file_path()
    if not path.is_file():
        return {}
    with _LOCK:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(f"task state file unreadable, starting empty: {exc}")
            try:
                os.replace(path, path.with_suffix(".corrupt"))
            except OSError:
                pass
            return {}
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, dict):
            logger.warning("task state file has an unexpected shape; starting empty")
            return {}
    return {
        str(task_id): dict(task)
        for task_id, task in tasks.items()
        if isinstance(task, dict)
    }
