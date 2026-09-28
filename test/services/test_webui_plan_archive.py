import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config
from app.utils import utils

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# Main.py 用纯 `import plans`（tools 目录在 sys.path）；必须 patch 同一个模块对象。
import archive_video  # noqa: E402
import plans as repo_plans  # noqa: E402

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"

_REAL_SUBPROCESS_RUN = subprocess.run


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


def _seed_plan_task(tasks_root: Path, task_id: str, topic: str):
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
    (task_path / "final-1.mp4").write_bytes(b"video-payload")
    (task_path / "subtitle.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nhi\n", encoding="utf-8",
    )
    # 归档现在要求成片「能解析出尺寸且已静默超过一个轮询窗口」，否则复制到
    # 媒体库的是半截文件。这里回退 mtime 并打桩 ffprobe，让用例专注于归档的
    # 幂等性与计划推进；成片校验本身由 test/services/test_archive_video.py 覆盖。
    old = time.time() - 3600
    os.utime(task_path / "final-1.mp4", (old, old))


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

    _seed_plan_task(tasks_root, "task-abc", "Topic One")
    (tasks_root / "task-abc" / "plan_meta.json").write_text(
        json.dumps({"channel": "testch", "topic": "Topic One"}), encoding="utf-8"
    )

    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(plans_root))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(outputs_root))
    # Main.py 经 `from app.utils import utils` 使用同一模块对象，patch 直属函数
    # 即可让任务面板与自动归档读取测试目录而非开发者真实任务目录。
    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_root))
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
    _seed_plan_task(tasks_root, "task-no-meta", "Topic Two")

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


def test_plan_panel_promotes_next_topic_after_auto_archive(plan_archive_env):
    app, tmp_path = plan_archive_env
    assert not app.exception
    markdown_text = " ".join(str(m.value) for m in app.markdown)
    assert "#2: Topic Two" in markdown_text
    assert "Topic Two" in markdown_text