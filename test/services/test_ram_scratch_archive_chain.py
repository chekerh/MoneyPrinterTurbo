"""``scratch_mode = "ram"``（出厂默认）下，归档链路必须是通的。

回归背景：``_purge_task_scratch`` 早先在任务结束时 ``rmtree`` 整个任务目录，
把 ``script.json`` / ``plan_meta.json`` / ``subtitle.srt`` 一起删掉。这三个文件
是下游唯一的输入：

* ``_auto_archive_plan_tasks`` 读 ``plan_meta.json`` 才知道成片属于哪个频道、
  哪个主题；
* ``archive_channel_video`` 读 ``script.json`` 取权威主题；
* WebUI 的"重新生成"从 ``script.json`` 恢复全部参数。

三者同时消失之后，任务面板仍然显示"已完成"、播放按钮仍然可用（成片在
``outputs/<task_id>/``），但成片永远进不了媒体库和月度计划，且没有任何报错。
当时 ``test_ssd_hygiene`` 甚至断言任务目录应当整体消失，等于把故障钉死。

这些用例直接调用真实的 ``_auto_archive_plan_tasks``，而不是只测清理函数本身，
因为故障发生在"清理之后、消费之前"这个接缝上，只测清理函数看不见它。
"""

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import project_layout
from app.services import task as task_service
from app.services.project_layout import ProjectLayout
from app.utils import utils


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
TOOLS_DIR = ROOT_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import archive_video  # noqa: E402
import plans as repo_plans  # noqa: E402

_REAL_SUBPROCESS_RUN = subprocess.run


def _stub_ffprobe(monkeypatch):
    def fake_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "ffprobe" in command:
            joined = " ".join(str(part) for part in command)
            if "stream=width,height" in joined:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="1080x1920\n", stderr=""
                )
            if "format=duration" in joined:
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="51.600000\n", stderr=""
                )
        return _REAL_SUBPROCESS_RUN(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


class _FakeSpace:
    """最小 scratch space 替身：只提供 root，模拟内存盘已经建好。"""

    def __init__(self, root: Path):
        self._root = root

    @property
    def root(self):
        return self._root


@pytest.fixture
def ram_scratch_env(tmp_path, monkeypatch):
    """
    一个完整的"内存盘 + 真实归档链路"环境。

    任务目录放在模拟的内存盘根下（而不是应用 storage），成片放在作品集 outputs
    下 —— 这正是 ``scratch_mode = "ram"`` 的真实布局。
    """
    scratch_root = tmp_path / "ramdisk"
    tasks_root = scratch_root / "tasks"
    tasks_root.mkdir(parents=True)

    plans_root = tmp_path / "plans"
    outputs_root = tmp_path / "outputs"
    (plans_root / "testch").mkdir(parents=True)
    (plans_root / "testch" / "topics_30day.txt").write_text(
        "Topic One\nTopic Two\n", encoding="utf-8"
    )
    (outputs_root / "testch").mkdir(parents=True)

    monkeypatch.setattr(utils, "task_dir", lambda *_a, **_k: str(tasks_root))
    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(plans_root))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(outputs_root))
    # 成片目录来自布局解析。不改它的话，_find_final_task_video 会去读**开发者真实**
    # 的作品集 outputs，既找不到用例造的成片，又可能碰到真实数据。
    app_root = tmp_path / "app"
    storage = app_root / "storage"

    def _resolve(*_a, **_k):
        return ProjectLayout(
            application_root=app_root,
            storage_root=storage,
            thumbnail_root=storage / "thumbnails",
            descriptor_root=storage / "result_descriptors",
            portfolio_root=tmp_path,
            plans_root=plans_root,
            outputs_root=outputs_root,
        )

    monkeypatch.setattr(project_layout, "resolve_project_layout", _resolve)
    monkeypatch.setattr(archive_video, "probe_dimensions", lambda *_a, **_k: (1080, 1920))
    _stub_ffprobe(monkeypatch)
    st.cache_resource.clear()

    yield {
        "scratch_root": scratch_root,
        "tasks_root": tasks_root,
        "plans_root": plans_root,
        "outputs_root": outputs_root,
        "space": _FakeSpace(scratch_root),
    }


def _seed_task(env, task_id="task-ram", topic="Topic One", channel="testch"):
    """造一个"生成完毕"的任务：中间产物在内存盘，成片在作品集。"""
    task_dir = env["tasks_root"] / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "script.json").write_text(
        json.dumps({"params": {"video_subject": topic}, "script": "x"}),
        encoding="utf-8",
    )
    (task_dir / "subtitle.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nhi\n",
                                            encoding="utf-8")
    (task_dir / "plan_meta.json").write_text(
        json.dumps({"channel": channel, "topic": topic}), encoding="utf-8"
    )
    # 体积 MB 级的中间产物：这些才是清理的目标
    (task_dir / "audio.mp3").write_bytes(b"a" * 4096)
    (task_dir / "combined-1.mp4").write_bytes(b"c" * 4096)
    # 成片在作品集，不在任务目录
    final_dir = env["outputs_root"] / task_id
    final_dir.mkdir(parents=True, exist_ok=True)
    final = final_dir / "final-1.mp4"
    final.write_bytes(b"video-payload")
    old = time.time() - 3600
    os.utime(final, (old, old))
    return task_dir, final


def _archive_via_webui(env):
    """跑一次真实的 AppTest，让 WebUI 自己的归档逻辑去处理这个任务。"""
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception, app.exception
    return app


def test_ram_scratch_purge_keeps_what_archiving_needs(ram_scratch_env):
    """
    清理必须只删媒体，元数据必须留下。

    这条是根因测试：``script.json`` / ``plan_meta.json`` / ``subtitle.srt`` 任意
    一个丢失，下面 test_ram_scratch_still_archives 就会失败。
    """
    env = ram_scratch_env
    task_dir, _final = _seed_task(env)

    task_service._purge_task_scratch("task-ram", space=env["space"])

    for name in ("script.json", "plan_meta.json", "subtitle.srt"):
        assert (task_dir / name).is_file(), f"清理误删了 {name}，归档链路会静默失效"
    for name in ("audio.mp3", "combined-1.mp4"):
        assert not (task_dir / name).exists(), f"媒体中间产物 {name} 没有被清理"


def test_ram_scratch_still_archives_end_to_end(ram_scratch_env):
    """
    完整链路：清理之后，WebUI 仍能把成片归档进频道媒体库并推进月度计划。

    早先这里必然失败——清理把 plan_meta.json 一起删了，``_auto_archive_plan_tasks``
    直接 ``continue``，而任务面板看上去一切正常。
    """
    env = ram_scratch_env
    task_dir, _final = _seed_task(env)

    # 先模拟任务结束时的清理
    task_service._purge_task_scratch("task-ram", space=env["space"])

    # 再让 WebUI 跑一次：它应当自己完成归档
    _archive_via_webui(env)

    month = date.today().strftime("%Y-%m")
    videos = env["outputs_root"] / "testch" / month / "videos"
    archived = list(videos.glob("*.mp4")) if videos.is_dir() else []
    assert archived, (
        "清理之后成片没有进入频道媒体库；"
        "元数据必须在清理中保留，否则归档链路静默失效"
    )
    assert (task_dir / ".plan_archived").is_file(), "归档成功后必须写幂等标记"
    assert repo_plans.first_pending_topic("testch") == (2, "Topic Two"), (
        "月度计划没有推进：下一个待生成主题应当是 #2"
    )


def test_ram_scratch_restore_still_works(ram_scratch_env):
    """
    "重新生成"依赖 script.json 恢复全部参数，清理后必须仍然可用。

    这条对应 UI 上那个被永久禁用的按钮。
    """
    env = ram_scratch_env
    task_dir, _final = _seed_task(env)
    task_service._purge_task_scratch("task-ram", space=env["space"])

    script_path = task_dir / "script.json"
    assert script_path.is_file()
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    assert payload["params"]["video_subject"] == "Topic One", (
        "恢复参数所需的 script.json 内容被破坏"
    )


def test_ram_scratch_does_not_leave_media_on_ssd(ram_scratch_env):
    """SSD 卫生的本意仍然成立：媒体确实被清掉了。"""
    env = ram_scratch_env
    task_dir, final = _seed_task(env)
    task_service._purge_task_scratch("task-ram", space=env["space"])

    assert not (task_dir / "combined-1.mp4").exists()
    assert not (task_dir / "audio.mp3").exists()
    assert final.is_file(), "最终成片绝不能被清理掉"
