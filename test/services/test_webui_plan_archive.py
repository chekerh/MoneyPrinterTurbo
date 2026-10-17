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
from app.services.project_layout import ProjectLayout
from app.utils import utils

# tools/ 现在随仓库一起提供。仓库根用 pyproject.toml 定位，而不是数 dirname()：
# 原��写法多套了一层，在本机恰好指到"仓库的上一级"——那里也有个 tools/，
# 于是本机全绿，干净克隆却 import 不到。按标记文件找根不会数错层数。
REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
while REPO_ROOT != os.path.dirname(REPO_ROOT) and not os.path.isfile(
    os.path.join(REPO_ROOT, "pyproject.toml")
):
    REPO_ROOT = os.path.dirname(REPO_ROOT)
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# Main.py 用纯 `import plans`（tools 目录在 sys.path）；必须 patch 同一个模块对象。
import archive_video  # noqa: E402
import plans as repo_plans  # noqa: E402

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"

_REAL_SUBPROCESS_RUN = subprocess.run


def _layout_for(outputs_root: Path):
    """
    造一个 ``outputs_root`` 指向测试目录的布局解析结果。

    只关心成片目录，其余根保持在一个不会与 outputs 冲突的占位目录上。用例关心
    的是"归档去成片的真实位置取文件"，不需要真实布局的全部校验。
    """
    app_root = outputs_root.parent / "app"
    storage = app_root / "storage"

    def _resolve(*_args, **_kwargs):
        return ProjectLayout(
            application_root=app_root,
            storage_root=storage,
            thumbnail_root=storage / "thumbnails",
            descriptor_root=storage / "result_descriptors",
            portfolio_root=outputs_root.parent,
            plans_root=outputs_root.parent / "plans",
            outputs_root=outputs_root,
        )

    return _resolve


def _stub_ffprobe(monkeypatch):
    """只拦截 ffprobe 调用，其余 subprocess 调用原样放行。

    任务面板判定「成片已完成」和归档复制前的校验都依赖 ffprobe 解析出的尺寸。
    用例里的 payload 是占位字节，因此这里按命令内容返回固定的探测结果：真正的
    尺寸/时长解析代码仍然执行，只有外部二进制被打桩。
    """

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


def _seed_plan_task(tasks_root: Path, finals_root: Path, task_id: str, topic: str):
    """
    造一个已完成的计划任务。

    ``finals_root`` 是成片的**权威目录**（``<outputs_root>/<task_id>/``）。最终成片
    是唯一长期留存的视频，从 b2b52cc 起就不再写进任务工作目录了；任务目录只留
    script.json 与字幕这类仍随任务走的文件。归档链路必须去成片目录取成片 ——
    只认任务目录的话，WebUI 会把每个已完成任务都当成"没有成片"而静默跳过归档。
    """
    task_path = tasks_root / task_id
    task_path.mkdir(parents=True, exist_ok=True)
    (task_path / "script.json").write_text(
        json.dumps(
            {
                "params": {
                    "video_subject": topic,
                    "video_aspect": "9:16",
                    "voice_name": "mia",
                },
                "script": "A short clip.",
                "search_terms": ["term"],
                "material_sources": [],
            }
        ),
        encoding="utf-8",
    )
    (task_path / "subtitle.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nhi\n", encoding="utf-8",
    )
    _write_final_video(finals_root / task_id / "final-1.mp4")


def _write_final_video(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video-payload")
    # 归档现在要求成片「能解析出尺寸且已静默超过一个轮询窗口」，否则复制到
    # 媒体库的是半截文件。这里回退 mtime 并打桩 ffprobe，让用例专注于归档的
    # 幂等性与计划推进；成片校验本身由 test/services/test_archive_video.py 覆盖。
    old = time.time() - 3600
    os.utime(path, (old, old))


@pytest.fixture
def plan_archive_env(tmp_path, monkeypatch):
    plans_root = tmp_path / "plans"
    outputs_root = tmp_path / "outputs"
    tasks_root = tmp_path / "tasks"
    (plans_root / "testch").mkdir(parents=True, exist_ok=True)
    (plans_root / "testch" / "topics_30day.txt").write_text(
        "Topic One\nTopic Two\n", encoding="utf-8"
    )
    (outputs_root / "testch").mkdir(parents=True, exist_ok=True)

    _seed_plan_task(tasks_root, outputs_root, "task-abc", "Topic One")
    (tasks_root / "task-abc" / "plan_meta.json").write_text(
        json.dumps({"channel": "testch", "topic": "Topic One"}), encoding="utf-8"
    )

    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(plans_root))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(outputs_root))
    # Main.py 经 `from app.utils import utils` 使用同一模块对象，patch 直属函数
    # 即可让任务面板与自动归档读取测试目录而非开发者真实任务目录。
    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_root))
    # 成片目录来自布局解析。必须把它指到测试目录，否则归档会去读**开发者真实的**
    # 作品集 outputs 目录，用例既跑不通又会往真实数据里写。
    # _cached_project_layout 是 @st.cache_resource，进程内会跨 AppTest 复用，
    # 不清掉的话会拿到上一个用例的 tmp 路径 —— 症状是"归档去别的目录找成片"。
    st.cache_resource.clear()
    monkeypatch.setattr(
        project_layout, "resolve_project_layout", _layout_for(outputs_root)
    )
    monkeypatch.setattr(archive_video, "probe_dimensions", lambda *_a, **_k: (1080, 1920))
    _stub_ffprobe(monkeypatch)

    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception

    return app, tmp_path


def test_plan_task_auto_archives_and_advances_pending_topic(plan_archive_env):
    app, tmp_path = plan_archive_env
    month = date.today().strftime("%Y-%m")
    videos = tmp_path / "outputs" / "testch" / month / "videos"
    plan_file = tmp_path / "outputs" / "testch" / month / "monthly-plan.md"

    mp4s = list(videos.glob("*.mp4"))
    assert len(mp4s) == 1
    base = mp4s[0].stem
    assert base.startswith("01-")
    assert (videos / f"{base}.txt").exists()
    assert (videos / f"{base}.srt").exists()

    plan_text = plan_file.read_text(encoding="utf-8")
    assert "1/2 topics recorded" in plan_text
    assert "Topic One" in plan_text

    assert repo_plans.first_pending_topic("testch") == (2, "Topic Two")
    assert not app.exception


def test_plan_auto_archive_is_idempotent(plan_archive_env):
    app, tmp_path = plan_archive_env
    with patch.object(config, "try_save_config", return_value=True):
        second = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        second.run()
        assert not second.exception

    month = date.today().strftime("%Y-%m")
    videos = tmp_path / "outputs" / "testch" / month / "videos"
    assert len(list(videos.glob("*.mp4"))) == 1
    plan_text = (tmp_path / "outputs" / "testch" / month / "monthly-plan.md").read_text(
        encoding="utf-8"
    )
    assert "1/2 topics recorded" in plan_text


def test_non_plan_task_with_final_video_is_not_archived(plan_archive_env):
    app, tmp_path = plan_archive_env
    tasks_root = tmp_path / "tasks"
    # 有最终成片但没有 plan_meta.json 的历史任务（非计划入口生成）不应归档。
    _seed_plan_task(tasks_root, tmp_path / "outputs", "task-no-meta", "Topic Two")

    with patch.object(config, "try_save_config", return_value=True):
        second = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        second.run()
        assert not second.exception

    month = date.today().strftime("%Y-%m")
    videos = tmp_path / "outputs" / "testch" / month / "videos"
    assert len(list(videos.glob("*.mp4"))) == 1
    plan_text = (tmp_path / "outputs" / "testch" / month / "monthly-plan.md").read_text(
        encoding="utf-8"
    )
    assert "1/2 topics recorded" in plan_text
    assert repo_plans.first_pending_topic("testch") == (2, "Topic Two")


def test_plan_panel_promotes_next_topic_after_auto_archive(plan_archive_env, switch_view):
    app, tmp_path = plan_archive_env
    # 计划面板现在是左侧导航里的一屏
    switch_view(app, "plan")
    assert not app.exception
    markdown_text = " ".join(str(m.value) for m in app.markdown)
    assert "#2: Topic Two" in markdown_text
    assert "Topic Two" in markdown_text


def test_final_video_is_read_from_the_output_directory_not_the_task_dir(plan_archive_env):
    """
    成片在任务目录之外时，归档仍然必须成功。

    钉住 ``b2b52cc`` 那次搬迁：最终成片改写到作品集的 ``outputs/<task_id>/``，
    任务目录只留中间产物。fixture 只在成片目录里放 ``final-1.mp4``，任务目录里
    一个成片都没有 —— 如果有人把归档改回读 ``task_dir/final-1.mp4``，
    ``copy_media`` 的完整性校验就会失败，用例立刻变红。
    """
    _app, tmp_path = plan_archive_env

    task_dir = tmp_path / "tasks" / "task-abc"
    assert not list(task_dir.glob("final-*.mp4")), "任务目录里不该有成片"

    month = date.today().strftime("%Y-%m")
    videos = tmp_path / "outputs" / "testch" / month / "videos"
    assert len(list(videos.glob("*.mp4"))) == 1, "成片应已归档进频道媒体库"

    # 归档成功后工作副本被消费掉。这里曾断言它还在——那条断言是在钉住
    # "成片留在 outputs/<task_id>/" 的旧行为，也正是 outputs/ 顶层攒出 9 个
    # 无名 UUID 目录（1.1 GB 逐字节副本）的原因。归档已复制+校验过，副本没有
    # 保留价值，而 outputs/<task_id>/ 不属于任何自动清理范围。
    assert not (tmp_path / "outputs" / "task-abc" / "final-1.mp4").exists(), (
        "归档成功后 outputs/<task_id>/ 里的工作副本应当被删除"
    )


def test_legacy_task_dir_final_video_is_still_archived(tmp_path, monkeypatch):
    """
    搬迁之前的历史任务把成片留在任务目录里，那批任务仍要能被归档。

    没有这条兜底，升级当天之前的已完成任务会永远卡在"未归档"，而它们恰恰是
    用户最可能回头去补发的那批。走真实的 AppTest 链路，而不是直接调用内部函数：
    Main.py 在导入时就会执行整个页面，单元测试没法安全地 import 它。
    """
    plans_root = tmp_path / "plans"
    outputs_root = tmp_path / "outputs"
    tasks_root = tmp_path / "tasks"
    (plans_root / "testch").mkdir(parents=True)
    (plans_root / "testch" / "topics_30day.txt").write_text(
        "Topic One\nTopic Two\n", encoding="utf-8"
    )
    (outputs_root / "testch").mkdir(parents=True)

    legacy = tasks_root / "task-old"
    legacy.mkdir(parents=True)
    (legacy / "script.json").write_text(
        json.dumps({"params": {"video_subject": "Topic One"}, "script": "x"}),
        encoding="utf-8",
    )
    (legacy / "plan_meta.json").write_text(
        json.dumps({"channel": "testch", "topic": "Topic One"}), encoding="utf-8"
    )
    # 成片留在**任务目录**里 —— 这正是 b2b52cc 之前的位置。
    _write_final_video(legacy / "final-1.mp4")

    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(plans_root))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(outputs_root))
    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_root))
    st.cache_resource.clear()
    monkeypatch.setattr(
        project_layout, "resolve_project_layout", _layout_for(outputs_root)
    )
    monkeypatch.setattr(archive_video, "probe_dimensions", lambda *_a, **_k: (1080, 1920))
    _stub_ffprobe(monkeypatch)

    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception

    month = date.today().strftime("%Y-%m")
    videos = tmp_path / "outputs" / "testch" / month / "videos"
    assert len(list(videos.glob("*.mp4"))) == 1, "老位置有成片也必须能归档"
    assert (legacy / ".plan_archived").is_file()
