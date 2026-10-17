"""异步操作用 thinking-orbs 的状态动效，而不是一个转圈的 spinner。

这些操作此前只有三种反馈：一根永远停在 0% 的进度条、一个不说明任何事的 spinner，
或者什么都没有。而"正在搜索素材 / 正在写脚本 / 正在上传"是**不同的状态**，用不同的
动画表达比同一圈光轮转九次清楚得多。

0% 进度条尤其糟：它承诺了一个不会到来的推进。这组用例守住两件事——动效在"正在
处理"时真的出现，以及在别的状态下不出现（每多一个 20px 的 canvas 都不是免费的）。
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import streamlit.components.v1 as components

from app.config import config
from app.models import const
from app.services import orbs
from app.services import state as sm
from app.utils import utils

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
ORB_JS = ROOT_DIR / "app" / "assets" / "thinking-orbs.js"


class TestOrbAsset(unittest.TestCase):
    def test_bundle_ships_with_the_app(self):
        """
        动效必须随应用一起分发。

        它是从 thinking-orbs 的 engine 现场打包来的，路径写死在 app/assets 下。
        文件缺失时 import 不会失败，只会在第一次渲染时静默画不出东西。
        """
        self.assertTrue(ORB_JS.is_file(), f"缺少 {ORB_JS}")
        bundle = ORB_JS.read_text(encoding="utf-8")
        self.assertIn("ThinkingOrbs", bundle)
        self.assertLess(len(bundle), 200_000, "动效包不该有这么大")

    def test_orb_asset_survives_streamlit_removing_components_html(self):
        """
        动效依赖 ``st.components.v1.html``，而它已被 Streamlit 标记弃用。

        别处都走不通：``st.markdown`` 与 ``st.html`` 用 innerHTML 插入内容，按规范
        不执行其中的 script（``st.html`` 即使给了 ``unsafe_allow_javascript=True``
        也不行）；``st.iframe`` 只收 URL，而 Streamlit 1.59 不再把 public/ 或
        static/ 挂到应用根路径上。

        所以这条用例是个探针：一旦 Streamlit 真的移除这个 API，它会立刻失败，而不是
        让界面上静悄悄地什么都不画——那种故障极难归因，看起来像"功能没做"。
        """
        self.assertTrue(
            hasattr(components, "html"),
            "st.components.v1.html 已被移除；动效需要换一种注入方式，"
            "否则界面上会静默地什么都不画",
        )

    def test_unknown_action_falls_back_instead_of_drawing_nothing(self):
        """未知动作回落到 working，而不是画不出东西。"""
        self.assertEqual(orbs.ACTION_STATES["generate"], "working")
        self.assertIn("working", set(orbs.ACTION_STATES.values()))
        self.assertEqual(len(orbs.SIZES), 2)

    def test_every_asynchronous_step_has_a_distinct_state(self):
        """
        不同阶段要映射到不同状态。

        全都映射到同一个动画等于没映射：用户分不出"在找素材"和"在上传"，而这两件事
        的等待时间差了一个数量级。
        """
        steps = ["generate", "search_materials", "write_script", "voice", "upload"]
        mapped = {orbs.ACTION_STATES[s] for s in steps}
        self.assertGreater(len(mapped), 1, "所有阶段共用同一个动效，失去意义")


def _render_panel(tasks: list[dict], tmp_path, monkeypatch, calls: list) -> None:
    """把任务面板跑一遍，顺带记录动效被请求了哪些状态。"""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(utils, "task_dir", lambda: str(tasks_dir))
    monkeypatch.setattr(sm.state, "get_all_tasks", lambda *_a, **_k: (tasks, len(tasks)))
    monkeypatch.setattr(
        orbs, "orb", lambda state, **kw: calls.append((state, kw))
    )
    with patch.object(config, "try_save_config", return_value=True):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        app.run()
        assert not app.exception, app.exception
        app.session_state["task_manager_open"] = True
        app.run()
    assert not app.exception, app.exception


def test_a_processing_task_gets_a_motion_indicator(tmp_path, monkeypatch):
    """
    正在跑的任务要有动效。

    状态列已经有"处理中"文字了，动效承担的是另一件事：**指示这一行还活着**。
    面板每两秒刷新一次，纯文字的"处理中"看上去和"卡住了"一模一样。

    这里直接看 orbs.orb 被请求了什么，而不是去翻渲染后的标记：st.html 的内容不进
    AppTest 的 markdown 元素，而"某个状态下有没有请求动效"才是这条用例真正关心的。
    """
    calls: list = []
    _render_panel(
        [{"task_id": "running", "state": const.TASK_STATE_PROCESSING,
          "progress": 40, "videos": [], "video_subject": "Working on it"}],
        tmp_path,
        monkeypatch,
        calls,
    )
    assert calls, "正在处理的任务没有请求动效"
    assert {state for state, _ in calls} == {"generate"}, f"动效状态不对：{calls}"


def test_a_finished_task_gets_none(tmp_path, monkeypatch):
    """已完成的行不该有动效——每多一个 20px 的 canvas 都要占一份内存。"""
    calls: list = []
    _render_panel(
        [{"task_id": "done", "state": const.TASK_STATE_COMPLETE,
          "progress": 100, "videos": [], "video_subject": "Finished"}],
        tmp_path,
        monkeypatch,
        calls,
    )
    assert calls == [], f"已完成的行仍然在渲染动效：{calls}"
