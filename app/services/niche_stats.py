"""按赛道统计产出，写在**清理流程之外**的地方。

为什么需要这个：赛道产出数据原本只存在于 ``script.json`` 和任务状态里，
而那些正是清理时会被删掉的东西。上一轮清理之后，"历史赛道一共做了几条"
就再也答不出来了。这个账本放在 ``storage/niche_stats.json``——清理脚本
只清中间产物目录，不会碰它。

只追加、只统计，不做分析判断：把"做过几条、什么时候、什么题目"记准，
选什么题、怎么写，那是 :mod:`app.services.niche_brand` 的事。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "NicheStatsError",
    "NicheLedger",
    "record_generation",
    "niche_ledger",
    "ledger_path",
]

_LOCK = threading.RLock()
_FILENAME = "niche_stats.json"
_UNKNOWN = "unclassified"


class NicheStatsError(RuntimeError):
    """账本损坏或不可写。"""


@dataclass(frozen=True)
class NicheSummary:
    channel: str
    videos: int
    topics: tuple[str, ...]

    @property
    def unique_topics(self) -> int:
        return len(self.topics)


def ledger_path() -> Path:
    """
    账本位置：``storage/niche_stats.json``。

    刻意**不**放在 ``storage/tasks`` 里——那个目录现在是内存盘，进程退出就没了，
    放进去等于又回到"数据随清理一起消失"的老问题。
    """
    from app.utils.utils import storage_dir

    return Path(storage_dir()) / _FILENAME


class NicheLedger:
    """赛道产出账本。进程内单例，但数据落在磁盘上。"""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else ledger_path()
        self._lock = _LOCK

    # -- 读 ---------------------------------------------------------------

    def _load(self) -> dict:
        if not self._path.is_file():
            return {"version": 1, "channels": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NicheStatsError(f"无法读取赛道账本 {self._path}：{exc}") from exc
        if not isinstance(data, dict) or "channels" not in data:
            # 结构不认识时不要覆盖：原文件先留档，避免把别人的数据抹掉。
            raise NicheStatsError(f"赛道账本结构无法识别：{self._path}")
        return data

    def summaries(self) -> dict[str, NicheSummary]:
        data = self._load()
        out: dict[str, NicheSummary] = {}
        for channel, record in (data.get("channels") or {}).items():
            if not isinstance(record, dict):
                continue
            out[str(channel)] = NicheSummary(
                channel=str(channel),
                videos=int(record.get("videos", 0) or 0),
                topics=tuple(str(t) for t in (record.get("topics") or [])),
            )
        return out

    def total_videos(self) -> int:
        return sum(s.videos for s in self.summaries().values())

    def video_count(self, channel: str) -> int:
        key = _normalise(channel)
        found = self.summaries().get(key)
        return found.videos if found else 0

    # -- 写 ---------------------------------------------------------------

    def record(
        self,
        channel: str,
        topic: str,
        *,
        task_id: str = "",
        videos: int = 1,
        at: float | None = None,
    ) -> NicheSummary:
        """记一次产出。``videos`` 是这次生成的成片条数。"""
        key = _normalise(channel)
        when = time.time() if at is None else float(at)
        with self._lock:
            data = self._load()
            channels = data.setdefault("channels", {})
            record = channels.setdefault(
                key, {"videos": 0, "topics": [], "entries": []}
            )
            record["videos"] = int(record.get("videos", 0) or 0) + int(videos)
            clean_topic = str(topic or "").strip()
            topics = record.setdefault("topics", [])
            if clean_topic and clean_topic not in topics:
                topics.append(clean_topic)
            entries = record.setdefault("entries", [])
            entries.append(
                {
                    "task_id": str(task_id),
                    "topic": clean_topic,
                    "videos": int(videos),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)),
                }
            )
            self._write(data)
            return NicheSummary(
                channel=key,
                videos=int(record["videos"]),
                topics=tuple(str(t) for t in topics),
            )

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(f".{self._path.name}.tmp")
        try:
            temp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, self._path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise NicheStatsError(f"无法写入赛道账本 {self._path}：{exc}") from exc

    def seed(self, channel: str, topics, videos: int = 0) -> None:
        """补录历史数据（例如从别处导出的清单）。"""
        key = _normalise(channel)
        with self._lock:
            data = self._load()
            record = data.setdefault("channels", {}).setdefault(
                key, {"videos": 0, "topics": [], "entries": []}
            )
            record["videos"] = int(record.get("videos", 0) or 0) + int(videos)
            known = list(record.get("topics") or [])
            for topic in topics:
                clean = str(topic or "").strip()
                if clean and clean not in known:
                    known.append(clean)
            record["topics"] = known
            self._write(data)


def _normalise(channel: object) -> str:
    text = str(channel or "").strip().lower().replace(" ", "-").replace("_", "-")
    return text or _UNKNOWN


_ledger: NicheLedger | None = None


def niche_ledger() -> NicheLedger:
    global _ledger
    with _LOCK:
        if _ledger is None:
            _ledger = NicheLedger()
        return _ledger


@contextmanager
def record_generation(channel: object, topic: object, **kwargs):
    """上下文管理器：任务成功才记账，失败不污染统计。"""
    ledger = niche_ledger()
    try:
        yield ledger
    except Exception:
        raise
    ledger.record(channel, topic, **kwargs)
