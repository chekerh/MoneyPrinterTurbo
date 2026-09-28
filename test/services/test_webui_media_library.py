import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from app.config import config

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

# Main.py 用纯 `import plans`（tools 目录在 sys.path）；必须 patch 同一个模块对象，
# 而不是 tools.plans（两者指向同一文件但可能作为两个模块加载）。
import plans as repo_plans  # noqa: E402

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _seed_channel(tmp_path, channel, months):
    plans_root = tmp_path / "plans"
    outputs_root = tmp_path / "outputs"
    (plans_root / channel).mkdir(parents=True, exist_ok=True)
    (plans_root / channel / "topics_30day.txt").write_text(
        "Topic alpha\nTopic beta\n", encoding="utf-8"
    )
    for month, lines in months.items():
        period = outputs_root / channel / month
        (period / "videos").mkdir(parents=True)
        (period / "monthly-plan.md").write_text("".join(lines), encoding="utf-8")
        for name in _video_names(lines):
            (period / "videos" / name).write_bytes(b"m")


_REAL_SUBPROCESS_RUN = subprocess.run


def _stub_ffprobe(monkeypatch):
    """让媒体库里的成片通过完整性检查。

    媒体库现在会探测归档成片的尺寸，解析不出来就把「显示预览」和「发布」标记
    为不可用。用例里的 payload 是单字节占位，因此这里只打桩 ffprobe 这一层。
    """

    def fake_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "ffprobe" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="1080x1920\n", stderr=""
            )
        return _REAL_SUBPROCESS_RUN(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _video_names(plan_lines):
    names = []
    for line in plan_lines:
        match = re.search(r"videos/([^)\s]+)", line)
        if match:
            names.append(match.group(1))
    return names


@pytest.fixture
def media_library_app(tmp_path, monkeypatch):
    _seed_channel(
        tmp_path,
        "testch",
        {
            "2026-08": [
                "| 1 | Topic alpha | ✓ [1-2026-08-01-alpha.mp4]"
                "(videos/1-2026-08-01-alpha.mp4) - posted: https://youtu.be/AA |\n",
                "| 2 | Topic beta | ✓ [2-2026-08-02-beta.mp4]"
                "(videos/2-2026-08-02-beta.mp4) |\n",
            ]
        },
    )
    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    _stub_ffprobe(monkeypatch)

    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception
        return app


def _metrics(app):
    return {m.label: m.value for m in app.metric}


def test_media_library_renders_per_channel_metrics_and_table(media_library_app):
    app = media_library_app
    values = _metrics(app)
    assert values["Videos built"] == "2"
    assert values["Posted to YouTube"] == "1"
    assert values["Not posted"] == "1"

    selectbox = next(s for s in app.selectbox if s.label == "Preview")
    assert sorted(o[1] for o in selectbox.options) == ["1", "2"]
    assert len(selectbox.options) == 2

    # 预览默认关闭：折叠库不缓冲任何视频
    assert not app.get("video")

    dataframe = app.dataframe[0]
    assert dataframe
    statuses = set(str(value) for value in dataframe.value["Status"])
    assert {"Draft", "Posted"} <= statuses

    draft_badge = next(
        m for m in app.markdown if "mpt-badge--draft" in m.value and "Topic beta" in m.value
    )
    assert "● Draft" in draft_badge.value
    assert not app.exception


def test_media_library_caps_large_tables(tmp_path, monkeypatch):
    # 260 个「过期月份」引用视频模拟海量归档：表格只展示最新 200 条并提示总数。
    channel = "testch"
    bulk = []
    for n in range(260):
        day = n % 28 + 1
        name = f"{n + 1}-2025-02-{day:02d}-bulk.mp4"
        bulk.append(f"| {n + 1} | Bulk topic | ✓ [{name}](videos/{name}) |\n")
    base = [
        "| 1 | Topic alpha | ✓ [1-2026-08-01-alpha.mp4]"
        "(videos/1-2026-08-01-alpha.mp4) - posted: https://youtu.be/AA |\n",
        "| 2 | Topic beta | ✓ [2-2026-08-02-beta.mp4]"
        "(videos/2-2026-08-02-beta.mp4) |\n",
    ]
    _seed_channel(tmp_path, channel, {"2025-02": bulk, "2026-08": base})

    monkeypatch.setattr(repo_plans, "PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setattr(repo_plans, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    _stub_ffprobe(monkeypatch)

    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()

    caption = next(
        c for c in app.caption if "Showing newest" in c.value and "videos" in c.value
    )
    assert "200" in caption.value and "262" in caption.value
    assert _metrics(app)["Videos built"] == "262"
    assert not app.exception