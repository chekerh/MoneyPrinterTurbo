"""验证任务完成时写出的成片身份记录，以及它失败时不会拖垮视频任务。

这个接线点必须满足一条硬性要求：**缩略图是附加能力，成片任务的成败不能由它
决定。** 因此这里的测试重点不是"记录写对了没有"，而是"记录写不出来时，视频
任务依然成功，只是缩略图被标记为不可用"。
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.project_layout import ProjectLayout
from app.services.task import _record_result_descriptor
from app.utils.strict_media_tools import probe_media_tools


def _make_video(path: Path, width: int = 640, height: int = 360, seconds: int = 4) -> None:
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


class TestRecordResultDescriptor(unittest.TestCase):
    """接线函数本身。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app_root = self.base / "MoneyPrinterTurbo"
        self.storage = self.app_root / "storage"
        self.task_dir = self.storage / "tasks" / "task-x"
        self.task_dir.mkdir(parents=True)
        self.portfolio = self.base / "portfolio"
        self.portfolio.mkdir()
        self.task_id = "task-x"
        self.tools = probe_media_tools()
        self.layout = ProjectLayout(
            application_root=self.app_root,
            storage_root=self.storage,
            thumbnail_root=self.storage / "thumbnails",
            descriptor_root=self.storage / "result_descriptors",
            portfolio_root=self.portfolio,
            plans_root=self.portfolio / "plans",
            outputs_root=self.portfolio / "outputs",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _video(self, index: int = 1) -> Path:
        path = self.task_dir / f"final-{index}.mp4"
        if not path.exists():
            _make_video(path)
        return path

    # --- 成功路径 ---------------------------------------------------------

    def test_writes_a_readable_descriptor(self):
        self._video()

        result = _record_result_descriptor(
            self.task_id, layout=self.layout, tools=self.tools
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["reason"], "")
        descriptor_path = self.layout.descriptor_root / self.task_id / "result_descriptor.json"
        self.assertTrue(descriptor_path.is_file())

        from app.services.result_descriptor import read_result_descriptor

        descriptor = read_result_descriptor(descriptor_path)
        self.assertEqual(descriptor.task_id, self.task_id)
        self.assertEqual(len(descriptor.videos), 1)

    def test_returns_the_descriptor_id_and_digest(self):
        """任务状态里要能回答"这是哪一份记录"，以便日志追溯。"""
        self._video()

        result = _record_result_descriptor(
            self.task_id, layout=self.layout, tools=self.tools
        )

        self.assertTrue(result["descriptor_id"])
        self.assertEqual(len(result["descriptor_sha256"]), 64)

    def test_describes_every_final_video(self):
        self._video(1)
        self._video(2)

        result = _record_result_descriptor(
            self.task_id, layout=self.layout, tools=self.tools
        )

        self.assertEqual(result["video_count"], 2)

    # --- 失败路径：绝不能让视频任务失败 -------------------------------------

    def test_unavailable_tools_degrade_instead_of_raising(self):
        """
        工具解析不出来时必须降级。tools=None 会触发内部重新解析；
        把解析也打掉，验证最外层仍然不抛。
        """
        self._video()

        with patch(
            "app.utils.strict_media_tools.probe_media_tools",
            side_effect=RuntimeError("ffprobe not found"),
        ):
            result = _record_result_descriptor(
                self.task_id, layout=self.layout, tools=None
            )

        self.assertFalse(result["eligible"])
        self.assertIn("ffprobe", result["reason"])

    def test_missing_task_directory_degrades(self):
        result = _record_result_descriptor(
            "no-such-task", layout=self.layout, tools=self.tools
        )

        self.assertFalse(result["eligible"])
        self.assertIn("no-such-task", result["reason"])

    def test_task_without_videos_degrades(self):
        result = _record_result_descriptor(
            self.task_id, layout=self.layout, tools=self.tools
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(result["reason"])

    def test_unreadable_video_degrades(self):
        (self.task_dir / "final-1.mp4").write_bytes(b"not an mp4")

        result = _record_result_descriptor(
            self.task_id, layout=self.layout, tools=self.tools
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(result["reason"])

    def test_layout_unavailable_degrades(self):
        """
        作品集根没配置时布局解析会失败。此时缩略图整体不可用，
        但视频任务必须照常完成。
        """
        self._video()

        with patch(
            "app.services.project_layout.resolve_project_layout",
            side_effect=RuntimeError("portfolio_root 未配置"),
        ):
            result = _record_result_descriptor(
                self.task_id, layout=None, tools=self.tools
            )

        self.assertFalse(result["eligible"])
        self.assertIn("portfolio_root", result["reason"])

    def test_result_shape_is_stable_across_success_and_failure(self):
        """
        调用方要无条件解包返回值，因此两个分支的键必须一致。
        """
        success = _record_result_descriptor(
            self.task_id, layout=self.layout, tools=self.tools
        )
        self.assertFalse(
            _record_result_descriptor(
                "absent", layout=self.layout, tools=self.tools
            )["eligible"]
        )

        self.assertEqual(
            set(success),
            {"eligible", "reason", "descriptor_id", "descriptor_sha256", "video_count"},
        )

    def test_never_leaves_a_partial_descriptor_behind(self):
        """写入失败时不能留下半截 JSON，那会让后续读取一直失败。"""
        self._video()

        with patch(
            "app.services.result_descriptor.write_result_descriptor",
            side_effect=OSError("ENOSPC"),
        ):
            result = _record_result_descriptor(
                self.task_id, layout=self.layout, tools=self.tools
            )

        self.assertFalse(result["eligible"])
        descriptor_path = self.layout.descriptor_root / self.task_id / "result_descriptor.json"
        self.assertFalse(descriptor_path.is_file())


class TestCompletionPathIntegration(unittest.TestCase):
    """确认完成路径确实调用了它，且返回值进入任务状态。"""

    def test_task_module_exposes_the_helper(self):
        from app.services import task

        self.assertTrue(callable(task._record_result_descriptor))

    def test_source_calls_the_helper_before_marking_complete(self):
        """
        结构性断言：写记录必须发生在"把成片列表写进任务状态"之前。

        完成那次状态更新是用 ``**kwargs`` 展开的，``videos`` 不在关键字参数里，
        所以这里定位的是"构造出含 videos 的 kwargs 字典"那一行。
        文件里还有几处提前退出式的 ``TASK_STATE_COMPLETE``（例如 stop_at="materials"），
        那些路径根本没有成片，不该要求写记录。
        """
        import ast
        from pathlib import Path as P

        source = P(__file__).parent.parent.parent / "app" / "services" / "task.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        call_line = None
        kwargs_line = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_record_result_descriptor"
                and call_line is None
            ):
                call_line = node.lineno
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "kwargs" not in targets:
                continue
            has_videos = any(
                isinstance(key, ast.Constant) and key.value == "videos"
                for key in node.value.keys
            )
            if has_videos and kwargs_line is None:
                kwargs_line = node.lineno

        self.assertIsNotNone(call_line, "完成路径没有调用 _record_result_descriptor")
        self.assertIsNotNone(kwargs_line, "没有找到带 videos 的任务状态字典")
        self.assertLess(
            call_line,
            kwargs_line,
            "写记录必须早于把成片列表写进任务状态",
        )

    def test_completion_state_records_thumbnail_eligibility(self):
        """任务状态里要带上可用性，WebUI 才能决定是否渲染缩略图。"""
        from pathlib import Path as P

        source = P(__file__).parent.parent.parent / "app" / "services" / "task.py"
        text = source.read_text(encoding="utf-8")

        self.assertIn('"thumbnail_eligibility"', text)
        self.assertIn('"thumbnail_unsupported_reason"', text)
        self.assertIn('"thumbnail_descriptor_id"', text)


if __name__ == "__main__":
    unittest.main()
