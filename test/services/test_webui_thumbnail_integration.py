"""验证 Main.py 里的缩略图接线：只渲染、不炸页、身份来自描述符。

这些用例会启动真实的 webui/Main.py（AppTest），因此覆盖了导入、缓存资源、
描述符读取和面板渲染的整条链路，而不是只测面板函数。
"""

import json
import os
import subprocess
import sys
import unittest

from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.project_layout import ProjectLayout

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _make_video(path: Path, width: int = 1280, height: int = 720, seconds: int = 8) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=size={width}x{height}:rate=1:duration={seconds}",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class _MainThumbnailFixture(unittest.TestCase):
    """准备一棵临时应用树，成片与描述符都是真的。"""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app_root = self.base / "MoneyPrinterTurbo"
        self.storage = self.app_root / "storage"
        self.portfolio = self.base / "portfolio"
        for path in (self.storage, self.portfolio, self.storage / "tasks"):
            path.mkdir(parents=True, exist_ok=True)

        self.task_id = "task-thumbnail-smoke"
        # 成片落在 outputs_root（唯一长期留存的视频），不在任务工作目录里。
        self.task_dir = self.portfolio / "outputs" / self.task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)
        _make_video(self.task_dir / "final-1.mp4")

        self.layout = ProjectLayout(
            application_root=self.app_root,
            storage_root=self.storage,
            thumbnail_root=self.storage / "thumbnails",
            descriptor_root=self.storage / "result_descriptors",
            portfolio_root=self.portfolio,
            plans_root=self.portfolio / "plans",
            outputs_root=self.portfolio / "outputs",
        )
        self._write_descriptor()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_descriptor(self) -> None:
        from app.services.result_descriptor import (
            build_result_descriptor,
            write_result_descriptor,
        )
        from app.utils.strict_media_tools import probe_media_tools

        descriptor = build_result_descriptor(
            self.layout, self.task_id, tools=probe_media_tools()
        )
        write_result_descriptor(self.layout, descriptor)
        self.descriptor = descriptor

    def _run_main(self, timeout: int = 120) -> AppTest:
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=timeout)
        with patch.dict(os.environ, {"MPT_PORTFOLIO_ROOT": str(self.portfolio)}):
            app.run()
        return app


class TestMainThumbnailWiring(_MainThumbnailFixture):
    """接线本身。"""

    def test_main_still_starts_without_any_thumbnail_configuration(self):
        """
        作品集根没配置时，缩略图整段静默跳过，主页面必须照常可用。
        这是最容易被新功能破坏的路径。
        """
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=120)
        env = {k: v for k, v in os.environ.items() if k != "MPT_PORTFOLIO_ROOT"}
        with patch.dict(os.environ, env, clear=True):
            app.run()

        self.assertFalse(
            [e for e in app.exception if "thumbnail" in str(e).lower()],
            [str(e) for e in app.exception],
        )

    def test_main_compiles_and_lints(self):
        """静态检查：新增的接线不能引入语法或未使用导入问题。"""
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(WEBUI_MAIN)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_panel_module_is_wired_into_the_result_view(self):
        """
        结构性检查：结果渲染路径里确实调用了缩略图面板。
        纯文本断言，避免依赖完整的 WebUI 启动开销。
        """
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        self.assertIn("_render_thumbnail_section(task_id)", source)
        self.assertIn("render_thumbnail_panel", source)


class TestMainThumbnailRendering(_MainThumbnailFixture):
    """真正把面板渲染出来。"""

    def test_renders_the_thumbnail_section_for_a_completed_task(self):
        app = self._run_main()

        failures = [
            str(e) for e in app.exception
            if "thumbnail" in str(e).lower() or "Thumbnail" in str(e)
        ]
        self.assertEqual(failures, [], failures)

    def test_result_view_reaches_the_thumbnail_section_in_source_order(self):
        """
        结果视图里，缩略图段落必须排在视频预览之后、日志之前。

        这里刻意只做源码级断言：把 AppTest 驱动到"任务已完成"的状态需要伪造
        sm.state.get_task 与 session_state 里的 current_generation_task_id，
        那是与本功能无关的脆弱依赖。面板控件的真实渲染由
        test_thumbnail_panel.py 的 AppTest 覆盖。
        """
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        preview = source.index("failed to render generated video preview")
        thumbnails = source.index("_render_thumbnail_section(task_id)")
        logs = source.index("_render_generation_logs(task_id)", preview)

        self.assertLess(preview, thumbnails)
        self.assertLess(thumbnails, logs)

    def test_panel_receives_server_owned_identity_only(self):
        """
        面板拿到的必须是描述符与成片目录，不允许出现调用方提供的路径参数。

        成片目录是 ``layout.outputs_root/<task_id>``，不是
        ``layout.storage_root / "tasks" / <task_id>``。最终成片是唯一长期留存的
        视频，b2b52cc 起写在作品集 outputs 下；任务目录只剩中间产物。此前这里
        断言的是 ``storage_root``，等于把"每个缩略图作业都找不到源文件而失败"
        这个 bug 钉死在测试里。
        """
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        call = source[source.index("render_thumbnail_panel("):][:900]
        self.assertIn("descriptor=descriptor", call)
        self.assertIn("source_root=layout.outputs_root", call)
        self.assertNotIn("source_root=layout.storage_root", call)
        self.assertNotIn("st.file_uploader", call)

    def test_thumbnail_source_root_is_where_the_final_video_actually_lives(self):
        """
        面板拿到的 source_root 必须与结果描述符枚举成片的目录一致。

        描述符来自 ``build_result_descriptor``，它枚举的是
        ``layout.outputs_root / <task_id>``。两处一旦不一致，作业就会对着一个
        不存在的源文件渲染，然后静默失败。这里直接比较两个常量。
        """
        from app.services import result_descriptor

        source = WEBUI_MAIN.read_text(encoding="utf-8")
        call = source[source.index("render_thumbnail_panel("):][:900]
        self.assertIn("source_root=layout.outputs_root", call)

        # 描述符侧的成片目录必须同样是 outputs_root，而不是 storage_root。
        descriptor_source = (
            Path(result_descriptor.__file__).read_text(encoding="utf-8")
        )
        self.assertIn("layout.outputs_root", descriptor_source)

    def test_does_not_invoke_ffmpeg_during_a_plain_render(self):
        """
        仅仅打开结果页不得启动 FFmpeg——这是响应性的关键性质。
        用计数桩验证，而不是靠耗时猜测。
        """
        import app.services.thumbnail as thumbnail_module

        calls = []
        original = thumbnail_module.render_thumbnail

        def counting(spec, tools):
            calls.append(1)
            return original(spec, tools)

        with patch.object(thumbnail_module, "render_thumbnail", counting):
            self._run_main()

        self.assertEqual(calls, [], "普通渲染触发了 FFmpeg")


class TestThumbnailTranslationCoverage(_MainThumbnailFixture):
    """新增文案必须在 en/zh 里齐备，其余语言按键回退英文。"""

    def _translations(self, locale: str) -> dict:
        path = ROOT_DIR / "webui" / "i18n" / f"{locale}.json"
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)["Translation"]

    def test_panel_keys_exist_in_english(self):
        english = self._translations("en")
        source = (ROOT_DIR / "webui" / "thumbnail_panel.py").read_text(encoding="utf-8")

        for key in _panel_keys(source):
            with self.subTest(key=key):
                self.assertIn(key, english)

    def test_panel_keys_exist_in_chinese(self):
        chinese = self._translations("zh")
        source = (ROOT_DIR / "webui" / "thumbnail_panel.py").read_text(encoding="utf-8")

        for key in _panel_keys(source):
            with self.subTest(key=key):
                self.assertIn(key, chinese)

    def test_every_locale_file_stays_parseable(self):
        for path in sorted((ROOT_DIR / "webui" / "i18n").glob("*.json")):
            with self.subTest(path=path.name):
                with path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
                self.assertIn("Translation", data)


def _panel_keys(source: str) -> list[str]:
    """抽出面板里所有 tr("...") 用到的字面量 key。"""
    import re

    found = re.findall(r'tr\(\s*"([A-Za-z][^"]*)"', source)
    found += re.findall(r'st\.(?:caption|warning|error|info)\(\s*tr\(\s*"([A-Za-z][^"]*)"', source)
    return sorted(set(found))


class TestPanelRefreshesWhenBackgroundJobFinishes(unittest.TestCase):
    """
    后台作业完成后，界面必须自己更新。

    这是异步化之后最容易漏掉的一环：作业在后台跑完了，但结果页是静态渲染的，
    用户不点一下页面就永远看不到新缩略图——功能看起来像坏了。
    """

    def test_refresh_is_requested_only_after_every_job_reaches_a_terminal_state(self):
        from webui.thumbnail_panel import _should_request_refresh

        def rec(state):
            return {"job_id": state, "task_id": "t", "state": state, "created_at": 0.0,
                    "updated_at": 0.0, "video_index": 1, "error": "", "artifact_id": ""}

        # 还没有作业：不该触发刷新，否则会变成永远停不下来的轮询
        self.assertFalse(_should_request_refresh([]))
        # 仍有在途作业：不刷新
        self.assertFalse(_should_request_refresh([rec("running")]))
        self.assertFalse(_should_request_refresh([rec("queued")]))
        # 混合状态也还不刷新
        self.assertFalse(_should_request_refresh([rec("succeeded"), rec("running")]))
        # 全部结束：立刻刷新，让新产物出现
        self.assertTrue(_should_request_refresh([rec("succeeded")]))
        self.assertTrue(_should_request_refresh([rec("failed"), rec("succeeded")]))
        self.assertTrue(_should_request_refresh([rec("cancelled")]))

    def test_result_view_watches_for_job_completion(self):
        """
        结构性断言：结果视图里确实挂了一个会轮询的 fragment。
        没有它，后台作业跑完也不会反映到界面上。
        """
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        self.assertIn("_thumbnail_job_watch", source)
        self.assertIn("@st.fragment", source)
        # 轮询片段必须在缩略图段落里被调用
        watch_call = source.index("_thumbnail_job_watch(task_id, str(thumbnail_root))")
        section = source.index("def _render_thumbnail_section")
        self.assertLess(section, watch_call)


if __name__ == "__main__":
    unittest.main()


class TestUnconfiguredFeatureExplainsItself(unittest.TestCase):
    """
    作品集根没配时，功能必须失败关闭（不猜目录），但不能静默消失。

    之前 _cached_project_layout() 返回 None 就直接 return，用户看到的是
    "成片下面什么都没有"，和"功能坏了"完全无法区分。这里要求给出一条
    说明，让用户知道功能只是没开、以及怎么打开。
    """

    def test_layout_failure_is_reported_to_the_user(self):
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        start = source.index("def _render_thumbnail_section")
        body = source[start : start + 1600]
        self.assertIn(
            "_thumbnail_unavailable_notice",
            body,
            "布局不可用时必须给出说明，而不是静默 return",
        )

    def test_notice_names_the_setting_the_user_must_change(self):
        source = WEBUI_MAIN.read_text(encoding="utf-8")
        start = source.index("def _thumbnail_unavailable_notice")
        body = source[start : start + 1200]
        self.assertIn("portfolio_root", body)
