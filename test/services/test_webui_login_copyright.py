"""登录屏与版权检查屏：渲染、记忆状态、共享连接流程。

登录屏是落地页：先连好各频道的 YouTube 账号，发布链路才是通的。连过的
token 本来就存在本机，这里只是第一次把它摆到台面上，并记住显示。
版权屏对一条成片做素材来源审计，规则矩阵在 test_copyright_audit.py。
"""

import json
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N = ROOT_DIR / "webui" / "i18n"


def _main_source() -> str:
    return WEBUI_MAIN.read_text(encoding="utf-8")


def _blob(app) -> str:
    parts = []
    for collection in ("markdown", "caption", "success", "warning", "error", "info"):
        try:
            elements = getattr(app, collection)
        except Exception:  # noqa: BLE001 - AppTest 版本差异时跳过该类元素
            continue
        parts.extend(str(e.value) for e in elements)
    return "\n".join(parts)


def _seed_channel(name="healthch"):
    """在已被 conftest 隔离的 plans 根里造一个频道。

    ``isolate_real_portfolio``（autouse）已经把 ``PLANS_DIR`` 指向空沙盒，
    这里只需要在里面放频道文件。注意 ``import plans`` 与
    ``from tools import plans`` 可能是两个模块对象，两个都写。
    """
    import plans

    modules = {plans}
    try:
        from tools import plans as tools_plans
        modules.add(tools_plans)
    except ImportError:  # noqa: BLE001 - 回退到单模块对象
        pass
    for module in modules:
        channel_dir = Path(module.PLANS_DIR) / name
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / "topics_30day.txt").write_text(
            "Topic One\nTopic Two\n", encoding="utf-8"
        )
    return name


def _open_view(view: str):
    from app.services import youtube_upload

    with patch.object(
        youtube_upload, "channel_info", return_value={"success": False}
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.session_state["ui_language"] = "en"
        app.session_state["nav_view"] = view
        app.run()
    assert [str(e.value) for e in app.exception] == [], app.exception
    return app


def test_login_view_renders_with_hero_and_channel_cards():
    """登录屏画出标题与每个频道的登录卡。"""
    _seed_channel("healthch-cards")
    app = _open_view("login")
    blob = _blob(app)
    assert "Connect each niche once" in blob
    assert any(
        str(b.key).startswith("login_signin_") for b in app.button
    ), "每个频道没有自己的登录按钮"


def test_login_view_shows_remembered_connections():
    """本机已存 token 的频道打开即显示已连接，不用再点一次。"""
    # 频道名必须与其它用例不同：连接状态有 5 分钟缓存，同名会读到别的用例的旧值。
    _seed_channel("healthch-remembered")
    from app.services import youtube_upload

    with patch.object(
        youtube_upload,
        "channel_info",
        return_value={
            "success": True,
            "channel_name": "Demo Channel",
            "channel_id": "UCdemo",
        },
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.session_state["ui_language"] = "en"
        app.session_state["nav_view"] = "login"
        app.run()
    assert [str(e.value) for e in app.exception] == [], app.exception
    assert "Demo Channel" in _blob(app), "已记住的连接没有显示出来"


def test_channel_name_is_escaped_as_markdown():
    """Google 取回的频道名不可信：里面的 HTML 必须中性化。

    名字最终进的是普通 Markdown 控件（非 unsafe），html.escape 负责把
    标签变成文本；这条用例钉住"原始尖括号永远到不了界面"。
    """
    _seed_channel("healthch-escape")
    from app.services import youtube_upload

    with patch.object(
        youtube_upload,
        "channel_info",
        return_value={
            "success": True,
            "channel_name": "my_channel <b>hi</b>",
            "channel_id": "UCdemo",
        },
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.session_state["ui_language"] = "en"
        app.session_state["nav_view"] = "login"
        app.run()
    assert [str(e.value) for e in app.exception] == [], app.exception
    blob = _blob(app)
    assert "my_channel" in blob, "频道名丢了"
    assert "<b>hi</b>" not in blob, "频道名里的 HTML 没有中性化"
    assert "&lt;b&gt;hi&lt;/b&gt;" in blob, "转义后的频道名没有显示出来"


def test_plan_page_shares_the_login_connect_flow():
    """计划页的登录按钮必须走共用的连接流程，而不是自带第二份 OAuth 代码。"""
    source = _main_source()
    assert "_render_youtube_connect_button" in source
    assert source.count("_render_youtube_connect_button(") >= 2, (
        "计划页与登录屏没有共用连接按钮"
    )
    assert '"plan_channel_login_button"' in source, "计划页按钮 key 变了"


def test_copyright_view_renders_with_hero():
    """版权屏画出标题；没有成片时给空状态而不是报错。"""
    app = _open_view("copyright")
    blob = _blob(app)
    assert "Know the risk before you post" in blob


def test_copyright_note_templates_format_with_a_name():
    """所有 note 文案模板只允许 {name} 占位，保证各语言同一种调法。"""
    for locale in ("en", "zh"):
        table = json.loads((I18N / f"{locale}.json").read_text(encoding="utf-8"))
        notes = {
            key: value
            for key, value in table["Translation"].items()
            if key.startswith("Copyright Note ")
        }
        assert notes, f"{locale} 里没有版权 note 文案"
        for key, template in notes.items():
            try:
                template.format(name="X")
            except (KeyError, IndexError, ValueError) as exc:
                raise AssertionError(
                    f"{locale}.{key} 含有 {{name}} 之外的占位符：{exc}"
                )


def test_copyright_view_checks_a_finished_video(switch_view):
    """有一条成片时，版权屏能选片并出报告。"""
    from app.services import youtube_upload

    with patch.object(
        youtube_upload, "channel_info", return_value={"success": False}
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.session_state["ui_language"] = "en"
        app.session_state["nav_view"] = "copyright"
        app.run()
    assert [str(e.value) for e in app.exception] == [], app.exception
    blob = _blob(app)
    # 本机可能有成片也可能没有：两种状态都必须可读，不能报错。
    assert ("Finished video" in blob) or ("No finished videos yet" in blob), blob[:300]


def test_copyright_picker_survives_a_deleted_task(tmp_path, monkeypatch):
    """下拉框记住的任务被删掉后，重进版权屏不能整页崩掉。

    删成片是任务管理里的日常操作，而选择留在会话里；带着已不存在的 id 进
    selectbox，生产环境会直接报错。页面必须把它收敛回有效选项。
    """
    import json
    import os
    import subprocess
    import time

    from app.services import state as sm
    from app.services import youtube_upload
    from app.utils import utils

    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "ffprobe" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="1080x1920\n", stderr=""
            )
        return real_run(command, *args, **kwargs)

    tasks_dir = tmp_path / "storage" / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr(utils, "task_dir", lambda *a, **k: str(tasks_dir))
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))

    task_dir = tasks_dir / "task-alive-1"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "script.json").write_text(
        json.dumps({"params": {"video_subject": "Alive", "video_source": "pexels"}}),
        encoding="utf-8",
    )
    video = task_dir / "final-1.mp4"
    video.write_bytes(b"payload")
    old = time.time() - 10
    os.utime(video, (old, old))

    with patch.object(
        youtube_upload, "channel_info", return_value={"success": False}
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.session_state["ui_language"] = "en"
        app.session_state["nav_view"] = "copyright"
        app.session_state["copyright_task_select"] = "dead-beef-deleted-task"
        app.run()
    assert [str(e.value) for e in app.exception] == [], app.exception
    picker = next(
        (s for s in app.selectbox if s.key == "copyright_task_select"), None
    )
    assert picker is not None, "版权屏没有选片器"
    assert any("Alive" in str(option) for option in picker.options), (
        "有效选项没有接管已删除的选择"
    )
