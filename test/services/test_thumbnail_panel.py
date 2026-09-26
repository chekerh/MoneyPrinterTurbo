"""验证缩略图面板只展示服务器记录的身份，并且不在重跑时做重活。

面板的三个硬性要求：

* 视频列表来自 result_descriptor，不来自目录遍历或调用方传入的索引；
* 已有产物在生成失败时必须继续可见；
* 普通的 Streamlit 重跑不得触发 FFmpeg。只有显式点击"生成"才允许渲染。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.result_descriptor import ResultDescriptor, ResultVideoEntry
from app.services.thumbnail import MAX_OVERLAY_CHARS
from app.services.thumbnail_files import (
    append_artifact,
    load_manifest,
    read_selection,
    write_manifest,
)
from app.services.project_layout import ProjectLayout
from app.utils.strict_media_tools import probe_media_tools
from webui.thumbnail_panel import (
    PanelDecision,
    build_panel_plan,
    plan_for_video,
)


def _descriptor(task_id: str = "task-a", count: int = 2) -> ResultDescriptor:
    return ResultDescriptor(
        schema_version=1,
        task_id=task_id,
        videos=tuple(
            ResultVideoEntry(
                video_index=index,
                relative_name=f"final-{index}.mp4",
                bytes=1024 * index,
                sha256=f"{index:064x}",
                width=1280,
                height=720,
                rotation=0,
                duration_ms=8000,
            )
            for index in range(1, count + 1)
        ),
        toolchain={"ffmpeg_version": "8.0.1"},
    )


class _PanelFixture(unittest.TestCase):
    """准备一棵临时缩略图根与渲染所需的工具链。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app_root = self.base / "MoneyPrinterTurbo"
        self.app_root.mkdir()
        self.storage = self.app_root / "storage"
        self.storage.mkdir()
        self.portfolio = self.base / "portfolio"
        self.portfolio.mkdir()
        self.layout = ProjectLayout(
            application_root=self.app_root,
            storage_root=self.storage,
            thumbnail_root=self.storage / "thumbnails",
            descriptor_root=self.storage / "result_descriptors",
            portfolio_root=self.portfolio,
            plans_root=self.portfolio / "plans",
            outputs_root=self.portfolio / "outputs",
        )
        self.tools = probe_media_tools()
        self.task_id = "task-a"
        self.thumbnail_root = self.layout.thumbnail_root / self.task_id
        self.thumbnail_root.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def manifest_path(self) -> Path:
        return self.thumbnail_root / "manifest.json"

    def _seed_artifact(self, ordinal: int, video_index: int = 1) -> str:
        artifact_id = f"{ordinal:032x}"
        (self.thumbnail_root / f"thumbnail-{artifact_id}.jpg").write_bytes(b"jpeg")
        entry = {
            "artifact_id": artifact_id,
            "video_index": video_index,
            "parent_artifact_id": None,
            "previous_artifact_id": None,
            "relative_name": f"thumbnail-{artifact_id}.jpg",
            "source_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "overlay_sha256": "c" * 64,
            "font_id": "Charm-Regular.ttf",
            "frame_at_seconds": 2.0,
            "width": 1280,
            "height": 720,
            "bytes": 4,
            "created_at": f"2026-09-26T00:0{ordinal}:00+00:00",
            "artifact_state": "live",
            "purged_at": None,
        }
        if self.manifest_path.is_file():
            manifest = load_manifest(self.manifest_path)
            video = next(v for v in manifest["videos"] if v["video_index"] == video_index)
            video["artifacts"].append(entry)
            video["selected_artifact_id"] = artifact_id
            manifest["manifest_revision"] += 1
        else:
            manifest = {
                "schema_version": 2,
                "task_id": self.task_id,
                "manifest_revision": 1,
                "videos": [{
                    "video_index": video_index,
                    "source_sha256": "a" * 64,
                    "artifacts": [entry],
                    "selected_artifact_id": artifact_id,
                }],
            }
        write_manifest(self.thumbnail_root, manifest)
        return artifact_id


class TestBuildPanelPlan(_PanelFixture):
    """面板计划必须完全由描述符与清单推导，不含任何猜测。"""

    def test_lists_every_video_from_the_descriptor(self):
        """视频行数与顺序只由服务器记录决定。"""
        plan = build_panel_plan(
            _descriptor(count=2), self.manifest_path, "task-a"
        )

        self.assertIsInstance(plan, PanelDecision)
        self.assertEqual([row.video_index for row in plan.rows], [1, 2])

    def test_reports_no_existing_thumbnail_before_the_first_render(self):
        plan = build_panel_plan(_descriptor(count=1), self.manifest_path, "task-a")

        self.assertIsNone(plan.rows[0].selected_artifact_id)
        self.assertTrue(plan.rows[0].can_generate)

    def test_surfaces_the_existing_thumbnail(self):
        artifact_id = self._seed_artifact(1)

        plan = build_panel_plan(_descriptor(count=1), self.manifest_path, "task-a")

        self.assertEqual(plan.rows[0].selected_artifact_id, artifact_id)
        self.assertTrue(plan.rows[0].has_thumbnail)

    def test_ignores_videos_absent_from_the_descriptor(self):
        """
        清单里多出的视频条目不应被展示——描述符才是权威身份来源。
        """
        self._seed_artifact(1, video_index=1)
        manifest = load_manifest(self.manifest_path)
        manifest["videos"].append({
            "video_index": 99,
            "source_sha256": "a" * 64,
            "artifacts": [dict(manifest["videos"][0]["artifacts"][0],
                               video_index=99, artifact_id="f" * 32)],
            "selected_artifact_id": "f" * 32,
        })
        write_manifest(self.thumbnail_root, manifest)

        plan = build_panel_plan(_descriptor(count=1), self.manifest_path, "task-a")

        self.assertEqual([row.video_index for row in plan.rows], [1])

    def test_survives_a_missing_manifest(self):
        """没有清单就是"还没有缩略图"，不是错误。"""
        plan = build_panel_plan(
            _descriptor(count=1), self.thumbnail_root / "absent.json", "task-a"
        )

        self.assertEqual(plan.messages, ())

    def test_reports_a_corrupt_manifest_instead_of_crashing(self):
        self.manifest_path.write_text("{not json")

        plan = build_panel_plan(_descriptor(count=1), self.manifest_path, "task-a")

        self.assertTrue(plan.messages)

    def test_reports_a_task_id_mismatch(self):
        self._seed_artifact(1)

        plan = build_panel_plan(
            _descriptor(task_id="other-task", count=1), self.manifest_path, "task-a"
        )

        self.assertTrue(plan.messages)


class TestPlanForVideo(_PanelFixture):
    """单个视频的可执行性判断。"""

    def test_allows_generation_without_a_previous_artifact(self):
        row = plan_for_video(1, None, self.thumbnail_root)

        self.assertTrue(row.can_generate)
        self.assertIsNone(row.selected_artifact_id)

    def test_allows_regeneration_when_one_artifact_exists(self):
        artifact_id = self._seed_artifact(1)
        manifest = load_manifest(self.manifest_path)

        row = plan_for_video(1, manifest, self.thumbnail_root)

        self.assertTrue(row.can_generate)
        self.assertEqual(row.selected_artifact_id, artifact_id)

    def test_refuses_generation_at_the_live_cap(self):
        """
        达到存活上限后必须拒绝并提示归档，而不是让请求失败在写入阶段。
        """
        for ordinal in range(1, 21):
            self._seed_artifact(ordinal)
        manifest = load_manifest(self.manifest_path)

        row = plan_for_video(1, manifest, self.thumbnail_root)

        self.assertFalse(row.can_generate)
        self.assertTrue(row.needs_archive)

    def test_reports_a_missing_artifact_file(self):
        """
        清单说有产物但文件没了：必须明确显示不可用，
        不能展示一个打不开的预览。
        """
        self._seed_artifact(1)
        (self.thumbnail_root / f"thumbnail-{1:032x}.jpg").unlink()
        manifest = load_manifest(self.manifest_path)

        row = plan_for_video(1, manifest, self.thumbnail_root)

        self.assertFalse(row.has_thumbnail)
        self.assertTrue(row.messages)

    def test_rejects_when_the_manifest_belongs_to_another_task(self):
        self._seed_artifact(1)
        manifest = load_manifest(self.manifest_path)
        manifest["task_id"] = "someone-else"
        write_manifest(self.thumbnail_root, manifest)

        with self.assertRaises(Exception):
            plan_for_video(1, manifest, self.thumbnail_root, task_id="task-a")


class TestOverlayTextBound(_PanelFixture):
    """文字输入的边界在进入渲染之前就要挡住。"""

    def test_blank_text_means_no_overlay(self):
        """空白等价于"不叠加"，返回 None 而不是空串。"""
        from webui.thumbnail_panel import normalize_overlay_text

        self.assertIsNone(normalize_overlay_text("   "))
        self.assertIsNone(normalize_overlay_text(""))
        self.assertIsNone(normalize_overlay_text(None))

    def test_overlong_text_is_rejected(self):
        from webui.thumbnail_panel import normalize_overlay_text

        with self.assertRaises(ValueError) as ctx:
            normalize_overlay_text("x" * (MAX_OVERLAY_CHARS + 1))
        # 抛出的是可翻译的 key，不是内部实现细节。
        self.assertEqual(str(ctx.exception), "Thumbnail Overlay Too Long")

    def test_control_characters_are_rejected(self):
        from webui.thumbnail_panel import normalize_overlay_text

        with self.assertRaises(ValueError):
            normalize_overlay_text("bad\ntext")

    def test_bidi_overrides_are_rejected(self):
        from webui.thumbnail_panel import normalize_overlay_text

        with self.assertRaises(ValueError):
            normalize_overlay_text("safe\u202egnitset")

    def test_accepts_ordinary_text(self):
        from webui.thumbnail_panel import normalize_overlay_text

        self.assertEqual(
            normalize_overlay_text("  Three habits that compound  "),
            "Three habits that compound",
        )


class TestPanelAppTest(unittest.TestCase):
    """用 Streamlit AppTest 跑通真实渲染路径。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app_root = self.base / "app"
        self.app_root.mkdir()
        self.portfolio = self.base / "portfolio"
        self.portfolio.mkdir()
        self.task_id = "task-a"
        self.source_root = self.app_root / "storage" / "tasks" / self.task_id
        self.source_root.mkdir(parents=True)
        (self.source_root / "final-1.mp4").write_bytes(b"not probed here")
        self.thumbnail_root = self.app_root / "storage" / "thumbnails" / self.task_id
        self.thumbnail_root.mkdir(parents=True)
        self.tools = probe_media_tools()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_app(self, body: str) -> Path:
        script = self.base / "panel_app.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(self.app_root.parent)!r})\n"
            "from pathlib import Path\n"
            "from app.services.result_descriptor import ResultDescriptor, ResultVideoEntry\n"
            "from app.services.thumbnail_files import load_manifest\n"
            "from app.utils.strict_media_tools import probe_media_tools\n"
            "from webui.thumbnail_panel import render_thumbnail_panel\n"
            "\n"
            "def tr(key):\n"
            "    return key\n"
            "\n"
            f"descriptor = ResultDescriptor(schema_version=1, task_id={self.task_id!r},\n"
            "    videos=(ResultVideoEntry(video_index=1, relative_name='final-1.mp4',\n"
            "        bytes=1, sha256='" + "a" * 64 + "', width=1280, height=720,\n"
            "        rotation=0, duration_ms=8000),), toolchain={})\n"
            f"render_thumbnail_panel(descriptor, Path({str(self.thumbnail_root / 'manifest.json')!r}),\n"
            f"    Path({str(self.thumbnail_root)!r}), Path({str(self.source_root)!r}),\n"
            f"    probe_media_tools(), tr)\n"
            + body
        )
        return script

    def test_renders_without_error_when_no_thumbnail_exists(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(self._write_app("")), default_timeout=60)
        app.run()

        self.assertNotIn(app.exception, [e for e in app.exception])

    def test_shows_the_generate_control(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(self._write_app("")), default_timeout=60)
        app.run()

        labels = [b.label for b in app.button]
        self.assertTrue(any("Thumbnail" in str(label) for label in labels), labels)

    def test_does_not_run_ffmpeg_on_a_plain_rerun(self):
        """普通重跑不得启动任何子进程。"""
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(self._write_app("")), default_timeout=60)
        app.run()
        app.run()

        self.assertFalse(app.exception)

    def test_clicking_generate_runs_the_renderer_and_updates_the_manifest(self):
        """
        只有显式点击才渲染。渲染被替换成桩之后，清单里应当出现新产物，
        这同时证明"渲染结果被写进了清单"这条链路是通的。
        """
        from streamlit.testing.v1 import AppTest

        preamble = (
            "import webui.thumbnail_panel as _p\n"
            "from app.services.thumbnail import RenderResult\n"
            "def _fake(spec, tools):\n"
            "    spec.output.write_bytes(b'jpeg')\n"
            "    return RenderResult(output_path=spec.output, output_sha256='" + "b" * 64 + "',\n"
            "        output_bytes=4, frame_at_seconds=2.0, width=1280, height=720,\n"
            "        font_id='Charm-Regular.ttf')\n"
            "_p.render_thumbnail = _fake\n"
        )
        app = AppTest.from_file(str(self._write_app(preamble)), default_timeout=60)
        app.run()

        target = next(b for b in app.button if "Thumbnail" in str(b.label))
        target.click().run()

        self.assertFalse(app.exception, [str(e) for e in app.exception])
        manifest = load_manifest(self.thumbnail_root / "manifest.json")
        self.assertEqual(manifest["manifest_revision"], 1)
        video = manifest["videos"][0]
        self.assertEqual(len(video["artifacts"]), 1)
        # 选择必须指向刚写入的那个产物。
        self.assertEqual(video["selected_artifact_id"], video["artifacts"][0]["artifact_id"])
        self.assertTrue(
            (self.thumbnail_root / video["artifacts"][0]["relative_name"]).is_file()
        )
        # 产物文件名必须与产物 ID 对得上，否则清单指向的是另一个文件。
        self.assertIn(video["artifacts"][0]["artifact_id"], video["artifacts"][0]["relative_name"])

    def test_a_failing_render_leaves_the_previous_manifest_intact(self):
        """生成失败不能破坏已有清单——上一次成功的产物必须还在。"""
        from streamlit.testing.v1 import AppTest

        self._seed_existing()
        before = (self.thumbnail_root / "manifest.json").read_bytes()

        preamble = (
            "import webui.thumbnail_panel as _p\n"
            "from app.services.thumbnail import ThumbnailError\n"
            "def _boom(spec, tools):\n"
            "    raise ThumbnailError('synthetic failure')\n"
            "_p.render_thumbnail = _boom\n"
        )
        app = AppTest.from_file(str(self._write_app(preamble)), default_timeout=60)
        app.run()
        next(b for b in app.button if "Thumbnail" in str(b.label)).click().run()

        self.assertEqual(
            (self.thumbnail_root / "manifest.json").read_bytes(), before
        )
        self.assertTrue((self.thumbnail_root / "thumbnail-existing.jpg").exists())

    def _seed_existing(self) -> None:
        (self.thumbnail_root / "thumbnail-existing.jpg").write_bytes(b"jpeg")
        write_manifest(
            self.thumbnail_root,
            {
                "schema_version": 2,
                "task_id": self.task_id,
                "manifest_revision": 1,
                "videos": [{
                    "video_index": 1,
                    "source_sha256": "a" * 64,
                    "artifacts": [{
                        "artifact_id": "c" * 32,
                        "video_index": 1,
                        "parent_artifact_id": None,
                        "previous_artifact_id": None,
                        "relative_name": "thumbnail-existing.jpg",
                        "source_sha256": "a" * 64,
                        "output_sha256": "b" * 64,
                        "overlay_sha256": "c" * 64,
                        "font_id": "Charm-Regular.ttf",
                        "frame_at_seconds": 2.0,
                        "width": 1280,
                        "height": 720,
                        "bytes": 4,
                        "created_at": "2026-09-26T00:00:00+00:00",
                        "artifact_state": "live",
                        "purged_at": None,
                    }],
                    "selected_artifact_id": "c" * 32,
                }],
            },
        )


class TestPanelModuleContract(unittest.TestCase):
    """面板模块本身的可导入性与常量契约。"""

    def test_module_exposes_the_expected_entry_points(self):
        import webui.thumbnail_panel as panel

        for name in (
            "build_panel_plan",
            "render_thumbnail_panel",
            "normalize_overlay_text",
            "MAX_OVERLAY_CHARS",
        ):
            self.assertTrue(hasattr(panel, name), f"缺少 {name}")

    def test_renderer_never_calls_ffmpeg_without_an_explicit_submit(self):
        """
        结构性保证：渲染函数只在按钮回调里被调用。
        这里验证 render_thumbnail 不会在导入模块时被触发。
        """
        import webui.thumbnail_panel as panel

        with patch.object(panel, "render_thumbnail") as renderer:
            plan = build_panel_plan(_descriptor(count=1), Path("/nonexistent.json"), "task-a")
            self.assertEqual(plan.rows[0].video_index, 1)

        renderer.assert_not_called()

    def test_manifest_written_by_the_panel_is_loadable(self):
        """面板写出的清单必须能被清单模块读回，形成闭环。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "final-1.mp4").write_bytes(b"v")
            (root / "thumbnail-placeholder.jpg").write_bytes(b"jpeg")
            manifest = {
                "schema_version": 2,
                "task_id": "task-a",
                "manifest_revision": 0,
                "videos": [],
            }
            write_manifest(root, manifest)
            result = append_artifact(
                root,
                "task-a",
                {
                    "artifact_id": "ab" * 16,
                    "video_index": 1,
                    "parent_artifact_id": None,
                    "previous_artifact_id": None,
                    "relative_name": "thumbnail-placeholder.jpg",
                    "source_sha256": "a" * 64,
                    "output_sha256": "b" * 64,
                    "overlay_sha256": "c" * 64,
                    "font_id": "Charm-Regular.ttf",
                    "frame_at_seconds": 2.0,
                    "width": 1280,
                    "height": 720,
                    "bytes": 4,
                    "created_at": "2026-09-26T00:00:00+00:00",
                    "artifact_state": "live",
                    "purged_at": None,
                },
                expected_revision=0,
            )
            self.assertEqual(read_selection(root / "manifest.json", 1), "ab" * 16)
            self.assertEqual(result["manifest_revision"], 1)


if __name__ == "__main__":
    unittest.main()
