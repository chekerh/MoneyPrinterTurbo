"""计划元数据在任务目录里的文件名与读写。

曾经这里两侧各写各的字符串：``webui/Main.py`` 写 ``plan_meta.json``，
而读取赛道的那段代码找 ``ln.json``。结果是读不到、也不报错，
只是每条视频都悄悄退回通用写法——功能看着"开了"却没有任何品牌句。
这类静默失效最难发现，所以文件名收敛到这一个常量。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["PLAN_META_FILENAME", "plan_meta_path", "read_plan_meta"]

#: 任务目录里记录 channel / topic 的文件名。
PLAN_META_FILENAME = "plan_meta.json"


def plan_meta_path(task_dir: str | Path) -> Path:
    return Path(task_dir) / PLAN_META_FILENAME


def read_plan_meta(task_dir: str | Path) -> dict:
    """
    读回计划元数据。

    文件不存在、JSON 损坏、顶层不是对象——都返回空 dict。调用方是要拿它
    决定"这条视频属于哪个赛道"的，附加信息缺失不该让出片失败。
    """
    path = plan_meta_path(task_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}
