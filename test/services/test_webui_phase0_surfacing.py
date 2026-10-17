"""Phase 0 of plans/_system/UX-AUDIT-AND-ROADMAP-2026-09-27.md — make reality visible.

Four items, all read-only or additive surfacing (no behaviour change):

0.3  ``open_task_folder_on_completion`` had no UI control, yet fires on every
     completion — a daily interruption the user could not see or turn off.
0.4  The task table silently rendered only the first 12 rows, so a 40-task
     filter looked like "there are only 12 tasks".
0.5  The onboarding tour was one-shot and unreplayable.
2.4  "Delete Task" called ``shutil.rmtree`` on the whole task directory on a
     single click, with no confirm, no undo, and no trash.

Each test pins the specific failure, not the implementation, so a rewrite that
keeps the behaviour still passes.
"""

import ast
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

import plans

from app.config import config
from app.models.schema import VideoAspect
from app.services import state as sm
from app.utils import utils

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"

_REAL_SUBPROCESS_RUN = subprocess.run


def _stub_ffprobe(monkeypatch):
    def fake_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "ffprobe" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout="1080x1920\n", stderr=""
            )
        return _REAL_SUBPROCESS_RUN(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _seed_completed_task(tasks_dir: Path, task_id: str, label: str) -> Path:
    """造一个已完成的任务，够任务面板把它列出来。"""
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    import json

    (task_dir / "script.json").write_text(
        json.dumps({"params": {"video_subject": label}, "script": label}),
        encoding="utf-8",
    )
    video = task_dir / "final-1.mp4"
    video.write_bytes(b"payload")
    old = time.time() - 3600
    os.utime(video, (old, old))
    return task_dir


@pytest.fixture
def task_app(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "storage" / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_dir))
    _stub_ffprobe(monkeypatch)
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: ([], 0))
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.run()
        assert not app.exception, app.exception
        yield app, tasks_dir


# --- 0.3 the Finder-popup switch is reachable from the UI --------------------


def test_open_folder_on_completion_has_a_ui_control(task_app):
    """
    这个开关此前只存在于 config.toml，全应用没有任何界面入口。

    于是每个视频完成时弹一次访达窗口，而用户既看不到它在发生，也没法关掉它。
    """
    app, _ = task_app
    with patch.object(config, "try_save_config", return_value=True):
        app.button(key="open_settings_dialog_button").click().run()
        assert not app.exception, app.exception

    checkbox = next(
        (cb for cb in app.checkbox if cb.key == "open_task_folder_on_completion_checkbox"),
        None,
    )
    assert checkbox is not None, (
        "open_task_folder_on_completion 没有任何界面控件，"
        "用户无法关闭每完成一个视频就弹出的资源管理器窗口"
    )
    # 默认值必须与读侧一致：_render_running_generation_task 用 .get(key, True)。
    assert checkbox.value is True


# --- 0.4 the row cap is disclosed -------------------------------------------


def test_task_table_discloses_hidden_rows(task_app, monkeypatch, open_task_manager):
    """
    筛出超过 12 个任务时必须说明被藏了多少。

    不说的话，界面上就是"只有 12 个任务"，用户会以为其余任务丢了或筛选没生效。
    """
    app, tasks_dir = task_app
    for index in range(15):
        _seed_completed_task(tasks_dir, f"task-{index:02d}", f"Topic {index}")

    with patch.object(config, "try_save_config", return_value=True):
        # 任务表只在展开时渲染
        open_task_manager(app)

    captions = [str(c.value) for c in app.caption]
    disclosure = [c for c in captions if "12" in c and "15" in c]
    assert disclosure, (
        f"15 个任务只渲染 12 行时必须告知用户藏了 3 行；当前 caption：{captions!r}"
    )


def test_task_table_is_quiet_when_nothing_is_hidden(task_app, monkeypatch):
    """没藏行时不要显示这句提示——否则每次都是噪音。"""
    app, tasks_dir = task_app
    _seed_completed_task(tasks_dir, "only-one", "Just one")

    with patch.object(config, "try_save_config", return_value=True):
        app.run()
        assert not app.exception, app.exception

    captions = [str(c.value) for c in app.caption]
    assert not [c for c in captions if c.startswith("Showing")]


# --- 2.4 delete needs confirmation ------------------------------------------


def test_delete_task_is_no_longer_a_single_click_rmtree(task_app):
    """
    单击"删除任务"就 rmtree 整个任务目录，不可撤销、没有回收站。

    改法是把删除收进一个确认层：点击行内按钮只展开确认，真正的 rmtree 只挂在
    确认按钮上。判定方式不是"确认按钮此刻不存在"——``st.popover`` 即使关闭也会
    把子树渲染进元素树（只是不可见），所以按钮一直在；要看的是**没有那个一键
    直删的按钮**，且未经确认时目录必须还在。
    """
    app, tasks_dir = task_app
    task_dir = _seed_completed_task(tasks_dir, "doomed", "Delete me")

    with patch.object(config, "try_save_config", return_value=True):
        app.run()
        assert not app.exception, app.exception

    one_click = [
        b
        for b in app.button
        if b.key and str(b.key).startswith("delete_task_")
        and not str(b.key).startswith(("delete_task_confirm", "delete_task_cancel"))
        and "popover" not in str(b.key)
    ]
    assert not one_click, (
        f"仍存在一键直删的按钮：{[str(b.key) for b in one_click]}；"
        "删除必须先经过确认层"
    )

    assert task_dir.is_dir(), "仅仅渲染页面就删掉了任务目录"


def test_delete_task_removes_the_directory_only_after_confirming(task_app, open_task_manager):
    """确认之后必须真的删掉，否则这道门就成了摆设。"""
    app, tasks_dir = task_app
    task_dir = _seed_completed_task(tasks_dir, "doomed", "Delete me")

    with patch.object(config, "try_save_config", return_value=True):
        open_task_manager(app)
        assert not app.exception, app.exception

        confirm = next(
            b
            for b in app.button
            if b.key and str(b.key).startswith("delete_task_confirm")
        )
        confirm.click().run()
        assert not app.exception, app.exception

    assert not task_dir.exists(), "点了确认仍然没有删除任务"


# --- 0.5 the tour can be replayed --------------------------------------------


def test_onboarding_tour_is_replayable(task_app):
    """
    引导默认只播一次，完成状态写在浏览器 localStorage 里。

    streamlit-tour 用 ``key`` 索引那段存储，所以重播的正确做法是换 key。
    钉住"key 必须带上重播 nonce"这一点：写死 key 就等于永远无法重播。
    """
    source = WEBUI_MAIN.read_text(encoding="utf-8")
    assert 'ONBOARDING_TOUR_KEY = "mpt-onboarding-v1"' in source

    start = source.index("def render_onboarding_tour")
    body = source[start : start + 4000]
    assert "replay_nonce" in body, (
        "引导的 tour key 没有参与重播计数，once_tour=True 之后用户无法再看一遍"
    )
    assert 'f"{ONBOARDING_TOUR_KEY}-{replay_nonce}"' in body


def test_replay_tour_button_exists(task_app):
    """顶部栏必须有一个重播入口，否则 nonce 机制没有触发点。"""
    app, _ = task_app
    assert any(
        b.key == "replay_onboarding_tour_button" for b in app.button
    ), "顶部栏缺少重播引导的按钮"


def _seed_channel(name: str = "testch") -> None:
    """
    在 conftest 隔离出来的 plans 根里造一个频道，让计划面板真的渲染出来。

    没有频道时 ``_render_plan_panel`` 在第一行就 return，于是关于计划面板的断言
    全部变成空跑——包括"某个按钮必须不存在"这种，因为面板压根没画。
    """
    channel_dir = Path(plans.PLANS_DIR) / name
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "topics_30day.txt").write_text(
        "Topic One\nTopic Two\n", encoding="utf-8"
    )


def _seed_env(channel_dir: Path, body: str) -> None:
    """写一份 automation.env。"""
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "automation.env").write_text(body, encoding="utf-8")


# --- 2.3 the unsafe "publish latest" button is gone --------------------------


def test_publish_latest_button_is_gone(task_app, switch_view):
    """
    「发布最新视频」必须不存在。

    它取的是**任意**任务目录里最新的成片（不区分频道），发布目标却是计划面板上
    选中的那个频道账号；``youtube_uploader`` 又把可见性硬编码为 public，发出去即刻
    公开、无法撤回。多频道工作流里点一下就可能把 A 频道的片子发到 B 频道上。
    它还固定传 index=None，月度计划那一行永远不会被标记为已发布。

    正确入口是媒体库里每条视频自己的「发布」按钮：那个按钮从所属频道的归档记录
    取来源，并且总是带着 plan_dir。
    """
    app, tasks_dir = task_app
    _seed_channel()
    _seed_completed_task(tasks_dir, "some-channel-video", "A video from elsewhere")

    with patch.object(config, "try_save_config", return_value=True):
        # 那个按钮过去住在计划面板上
        switch_view(app, "plan")
        app.run()
        assert not app.exception, app.exception

    # 先确认计划面板确实画出来了，否则下面的"不存在"是空跑。
    assert any(
        s.label == "Channel" for s in app.selectbox
    ), "计划面板没有渲染，用例会变成空跑"

    labels = [str(b.label) for b in app.button]
    assert "Publish latest video" not in labels, (
        "「发布最新视频」按钮仍在：它会把任意频道的成片发到当前选中的频道账号，"
        "且可见性硬编码为 public"
    )
    assert not any(
        b.key and str(b.key).startswith("plan_publish_latest") for b in app.button
    ), "plan_publish_latest_button 仍存在"


def test_publish_points_the_user_at_the_library(task_app, switch_view):
    """删除按钮的同时必须给出替代路径，否则用户会以为发布功能没了。"""
    app, _ = task_app
    _seed_channel()

    with patch.object(config, "try_save_config", return_value=True):
        # 指向成片库的说明画在计划面板上
        switch_view(app, "plan")
        assert not app.exception, app.exception

    captions = [str(c.value) for c in app.caption]
    assert any(
        "library" in c.lower() or "库" in c for c in captions
    ), f"没有指向媒体库发布入口的说明；当前 caption：{captions!r}"


# --- 0.1 / P0 the plan path must actually use the form's settings ----------


def _load_plan_param_namespace():
    """
    把计划参数相关的纯函数从 Main.py 里抽出来，编译到独立命名空间执行。

    直接 import Main.py 会跑完整页渲染。这里只需要 ``_build_plan_params``、
    ``_apply_plan_env``、``_coerce_plan_env_value`` 和那张映射表，把它们的协作者
    显式摆好，测的就是生产代码本身。
    """
    import ast
    import logging

    from app.models.schema import VideoParams

    tree = ast.parse(Path(WEBUI_MAIN).read_text(encoding="utf-8"))
    wanted_funcs = {
        "_build_plan_params",
        "_apply_plan_env",
        "_coerce_plan_env_value",
    }
    wanted_consts = {
        "_PLAN_ENV_FIELD_MAP",
        "_PLAN_ENV_INT_FIELDS",
        "_PLAN_ENV_FLOAT_FIELDS",
        "_PLAN_ENV_BOOL_FIELDS",
    }
    picked = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted_funcs:
            picked.append(node)
        elif isinstance(node, ast.AnnAssign) and (
            isinstance(node.target, ast.Name) and node.target.id in wanted_consts
        ):
            picked.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted_consts for t in node.targets
        ):
            picked.append(node)

    namespace = {
        "VideoParams": VideoParams,
        "VideoAspect": VideoAspect,
        "plans": plans,
        "logger": logging.getLogger("test"),
        "os": os,
    }
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(WEBUI_MAIN), "exec"), namespace)
    return namespace


def test_plan_path_keeps_the_form_settings():
    """
    这是 0.1 的根因修复：按下"生成今日视频"时，表单里设的设置必须真的传到生成器。

    旧实现是 ``VideoParams(video_subject=topic)`` —— 一个全新对象，只套用
    automation.env 的七个键，表单上的其余 31 项连同它们在 config.toml 里的取值
    一起被丢掉。用户于是花两个多小时，得到一份与眼前设置无关的成片。
    """
    from app.models.schema import VideoParams

    ns = _load_plan_param_namespace()
    form = VideoParams(
        video_subject="",
        video_clip_duration=7,
        font_size=48,
        video_count=3,
        voice_volume=0.8,
        subtitle_position="top",
    )
    with patch.dict(plans.__dict__, {"read_automation_env": lambda ch: {},
                                     "read_file_text": lambda ch, *a: ""}):
        result = ns["_build_plan_params"]("effch", "A topic", form)

    assert result.video_clip_duration == 7, "表单里的片段时长被丢掉了"
    assert result.font_size == 48, "表单里的字号被丢掉了"
    assert result.video_count == 3, "表单里的每次生成条数被丢掉了"
    assert result.voice_volume == 0.8, "表单里的配音音量被丢掉了"
    assert result.subtitle_position == "top", "表单里的字幕位置被丢掉了"
    assert result.video_subject == "A topic"


def test_channel_env_still_overrides_the_form():
    """
    频道文件优先于表单，这一点不能因为修复而丢掉。

    四个频道正是靠 automation.env 里的 VOICE_NAME / BGM_TYPE / BGM_VOLUME 区分
    品牌的。若改成表单优先，这些配置就全部失效——只是把"设置被丢弃"换了个方向。
    """
    from app.models.schema import VideoParams

    ns = _load_plan_param_namespace()
    form = VideoParams(
        video_subject="", voice_name="form-voice", bgm_volume=0.9, video_clip_duration=5
    )
    env = {
        "VOICE_NAME": "channel-voice",
        "BGM_VOLUME": "0.2",
        "VIDEO_CLIP_DURATION": "4",
    }
    with patch.dict(plans.__dict__, {"read_automation_env": lambda ch: env,
                                     "read_file_text": lambda ch, *a: ""}):
        result = ns["_build_plan_params"]("effch", "A topic", form)

    assert result.voice_name == "channel-voice"
    assert result.bgm_volume == 0.2
    assert result.video_clip_duration == 4


def test_channel_env_ignores_keys_the_app_does_not_recognise():
    """
    不认识的键必须被忽略，且不被静默地当成有效配置。

    automation.env 常常留着注释性的键；忽略掉没问题，但不能让它看起来生效了。
    """
    from app.models.schema import VideoParams

    ns = _load_plan_param_namespace()
    form = VideoParams(video_subject="", font_size=44)
    env = {"NOT_A_REAL_SETTING": "whatever", "FONT_SIZE": "52"}
    with patch.dict(plans.__dict__, {"read_automation_env": lambda ch: env,
                                     "read_file_text": lambda ch, *a: ""}):
        result = ns["_build_plan_params"]("effch", "A topic", form)

    assert result.font_size == 52
    assert not hasattr(result, "not_a_real_setting")


def test_bad_env_value_is_skipped_rather_than_killing_the_run():
    """
    一个写坏的键不该毁掉整次生成。

    以前这类值会在提交任务时才被 pydantic 拒绝，那时用户已经按下"生成"并等了一段
    时间。现在在读配置时就跳过并记录。
    """
    from app.models.schema import VideoParams

    ns = _load_plan_param_namespace()
    form = VideoParams(video_subject="", font_size=44)
    env = {"FONT_SIZE": "not-a-number"}
    with patch.dict(plans.__dict__, {"read_automation_env": lambda ch: env,
                                     "read_file_text": lambda ch, *a: ""}):
        result = ns["_build_plan_params"]("effch", "A topic", form)

    assert result.font_size == 44, "写坏的键不该覆盖表单里的有效值"


def test_plan_params_do_not_mutate_the_form_snapshot():
    """
    计划渲染不能改到用户正在编辑的那份参数。

    两者若共享同一个对象，一次计划渲染就会把表单改掉，症状是"点一下生成，界面上的
    设置自己变了"。
    """
    from app.models.schema import VideoParams

    ns = _load_plan_param_namespace()
    form = VideoParams(video_subject="", voice_name="original")
    with patch.dict(plans.__dict__, {"read_automation_env": lambda ch: {"VOICE_NAME": "chan"},
                                     "read_file_text": lambda ch, *a: ""}):
        result = ns["_build_plan_params"]("effch", "A topic", form)
    assert form.voice_name == "original", "表单快照被计划渲染改掉了"
    assert result is not form


def test_readout_names_the_channel_overrides(task_app, switch_view):
    """
    界面必须回答"我改的设置会被谁盖掉"。

    这段以前是七个小节解释"设置会被丢弃"。现在设置不再被丢弃，只需要说明频道文件
    覆盖了哪几项。
    """
    app, _ = task_app
    _seed_channel("effch")
    _seed_env(
        Path(plans.PLANS_DIR) / "effch",
        "VOICE_NAME=en-US-AvaMultilingualNeural\nBGM_VOLUME=0.2\n",
    )

    with patch.object(config, "try_save_config", return_value=True):
        # 覆盖摘要画在计划面板上，而计划面板现在是左侧导航里的一屏
        switch_view(app, "plan")
        assert not app.exception, app.exception

    text = " ".join(
        [str(t.value) for t in app.text]
        + [str(m.value) for m in app.markdown]
        + [str(c.value) for c in app.caption]
    )
    assert "VOICE_NAME" in text, "没有列出频道会覆盖的键"
    assert "BGM_VOLUME" in text


# --- UX: disabled buttons must explain themselves ---------------------------


def test_silent_disabled_buttons_carry_help():
    """
    会变灰的按钮必须用 help= 说清原因，否则就是死胡同。

    这些按钮的禁用条件（三选一的凭证、报价是否过期、确认框是否勾选）从按钮
    文字上看不出来；旁边的警告只覆盖了其中一种情况。AST 钉住 help= 的存在，
    文案内容由 i18n 覆盖测试保证。
    """
    source = WEBUI_MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    expected = {
        "clean_video_cache_now": "Clean Cache Now Help",
        "loomloom_quote_videos": "LoomLoom Video Quote Help",
        "loomloom_quote_scripts": "LoomLoom Script Quote Help",
        "loomloom_execute_scripts": "LoomLoom Run Help",
    }
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name != "button":
            continue
        key = None
        has_help = False
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                key = kw.value.value
            if kw.arg == "help":
                has_help = True
        if key in expected:
            found[key] = has_help
    missing = sorted(set(expected) - set(found))
    assert not missing, f"找不到这些按钮，help 断言无从谈起：{missing}"
    silent = sorted(k for k, v in found.items() if not v)
    assert not silent, f"这些按钮会变灰却没有 help= 说明原因：{silent}"
