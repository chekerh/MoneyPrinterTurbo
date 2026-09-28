"""决定每一类产物落在哪里：内存盘、应用目录，还是最终成片目录。

规则来自实际需求——**SSD 上只留最终成片**：

============================  ==========================================
产物                          落点
============================  ==========================================
配音 / 字幕 / 脚本            内存盘，用完即删
下载的素材（cache_videos）     内存盘，用完即删
拼接片段 / combined-*.mp4     内存盘，用完即删
AI 生成的 BGM                 内存盘，用完即删
最终成片                      ``output_dir``（唯一长期留存的视频）
缩略图 / 结果描述符           应用 storage（KB 级，界面要用）
用户上传的素材、凭据、浏览器    应用 storage（用户自己的东西，不能删）
============================  ==========================================

把"哪些该删"集中在这里，而不是散落在三十个调用点里：新增一个中间产物时，
只要它落在 :data:`SCRATCH_SUBDIRS` 里的目录，就自动跟着进内存盘。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "SCRATCH_SUBDIRS",
    "PERSISTENT_SUBDIRS",
    "is_scratch_subdir",
    "scratch_path",
    "output_dir",
    "final_video_dir",
]

#: 这些 storage 子目录里的东西都是"用完即扔"的中间产物。
SCRATCH_SUBDIRS = frozenset(
    {
        "cache_videos",
        "cache_material_search",
        "temp",
        "tasks",
        "task_audio",
    }
)

#: 用户自己的东西或必须长期留存的状态，绝不能跟着中间产物一起被清掉。
PERSISTENT_SUBDIRS = frozenset(
    {
        "local_videos",
        "bgm",
        "credentials",
        "browser_profiles",
        "browser_debug",
        "campaigns",
        "thumbnails",
        "result_descriptors",
    }
)

_DEFAULT_OUTPUT_DIRNAME = "output"


def is_scratch_subdir(sub_dir: str) -> bool:
    return str(sub_dir or "").strip("/") in SCRATCH_SUBDIRS


def scratch_path(*parts: str) -> str:
    """
    把 ``storage/<parts>`` 映射到内存盘里的对应位置。

    内存盘不可用时会退回系统临时目录——此时中间产物仍会写盘，调用方应当
    通过 :func:`app.utils.scratch.scratch_space` 读到 ``on_ram_disk == False``
    并向用户告警，而不是假装一切正常。
    """
    from app.utils.scratch import scratch_space

    space = scratch_space()
    base = space.ensure_ready()
    if base is None:
        # scratch_mode = "off"：退回应用目录，保持旧行为。
        from app.utils.utils import storage_dir

        return os.path.join(storage_dir(), *parts) if parts else storage_dir()

    path = Path(base)
    for part in parts:
        if part:
            path = path / part
    return str(path)


def output_dir() -> str:
    """
    最终成片的长期存放目录。**这是唯一会一直留在磁盘上的视频。**

    正常情况用作品集里的 ``outputs``（``<portfolio_root>/outputs``）——这本来就是
    布局里定义好的位置。没配置作品集根时退回应用根下的 ``output``：出片不能
    因为缺少可选配置而失败。
    """
    try:
        from app.services.project_layout import resolve_project_layout

        return str(resolve_project_layout().outputs_root)
    except Exception:  # noqa: BLE001 - 作品集根是可选配置，不能因此挡住出片
        from app.utils.utils import root_dir

        return os.path.join(root_dir(), _DEFAULT_OUTPUT_DIRNAME)


def final_video_dir(task_id: str) -> str:
    """某个任务的成片目录：``<outputs_root>/<task_id>``。不存在则创建。"""
    target = os.path.join(output_dir(), str(task_id))
    os.makedirs(target, exist_ok=True)
    return target
