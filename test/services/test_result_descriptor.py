"""验证 result_descriptor 只描述服务器自己产出的成片，并且可校验地往返。

存在的理由：UI 和后续的缩略图链路都"知道"某个任务产出了哪些视频。如果这个认知
来自目录列表顺序或调用方传进来的路径，删掉一个文件、换个索引就会静默地指错对象。
本模块让服务器在任务完成时把成片身份固定下来，之后所有消费方都只认这份记录。
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.result_descriptor import (
    _duration_ms,
    _ffprobe_stream,
    _rotation,
    ResultDescriptorError,
    build_result_descriptor,
    read_result_descriptor,
    write_result_descriptor,
)
from app.services.project_layout import ProjectLayout
from app.utils.strict_media_tools import probe_media_tools


def _make_video(path: Path, width: int, height: int, seconds: int, rate: int = 1) -> None:
    """用 ffmpeg 造一段真实的合成视频，保证测试跑在真实解码路径上。"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"testsrc=size={width}x{height}:rate={rate}:duration={seconds}",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class _LayoutFixture(unittest.TestCase):
    """准备一棵临时的应用树与作品集树。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.app_root = self.base / "MoneyPrinterTurbo"
        self.app_root.mkdir()
        self.portfolio = self.base / "portfolio"
        self.portfolio.mkdir()
        self.storage = self.app_root / "storage"
        self.tasks = self.storage / "tasks"
        self.tasks.mkdir(parents=True)

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

    def tearDown(self):
        self._tmp.cleanup()

    def _task_dir(self, task_id: str) -> Path:
        path = self.tasks / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class TestBuildResultDescriptor(_LayoutFixture):
    """描述符必须由服务器自己枚举成片，并记录可校验的身份。"""

    def test_describes_each_final_video_it_finds(self):
        """
        成片按 final-<index>.mp4 命名，描述符要按索引顺序列出每一段。
        """
        task = self._task_dir("task-a")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        _make_video(task / "final-2.mp4", 640, 360, 1)

        descriptor = build_result_descriptor(
            self.layout, "task-a", tools=self.tools
        )

        self.assertEqual(descriptor.task_id, "task-a")
        self.assertEqual([e.video_index for e in descriptor.videos], [1, 2])
        self.assertEqual(
            [e.relative_name for e in descriptor.videos],
            ["final-1.mp4", "final-2.mp4"],
        )

    def test_records_identity_hash_and_geometry(self):
        """
        身份（大小 + 哈希）和几何信息（宽高、时长）都要固定下来，
        消费方才能判断"还是不是同一个文件"。
        """
        task = self._task_dir("task-b")
        _make_video(task / "final-1.mp4", 640, 360, 2)

        entry = build_result_descriptor(self.layout, "task-b", tools=self.tools).videos[0]

        self.assertEqual(entry.width, 640)
        self.assertEqual(entry.height, 360)
        self.assertEqual(len(entry.sha256), 64)
        self.assertGreater(entry.bytes, 0)
        self.assertGreater(entry.duration_ms, 0)
        self.assertEqual(entry.rotation, 0)

    def test_hash_matches_the_file_on_disk(self):
        """记录下来的哈希必须就是磁盘上那个文件的哈希。"""
        import hashlib

        task = self._task_dir("task-c")
        target = task / "final-1.mp4"
        _make_video(target, 320, 180, 1)

        entry = build_result_descriptor(self.layout, "task-c", tools=self.tools).videos[0]

        self.assertEqual(entry.sha256, hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(entry.bytes, target.stat().st_size)

    def test_ignores_files_that_are_not_final_videos(self):
        """中间产物、素材和描述文件都不该被当成成片。"""
        task = self._task_dir("task-d")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        (task / "combined-1.mp4").write_bytes(b"not a real video")
        (task / "final-1.srt").write_text("subtitle")
        (task / "materials").mkdir()

        descriptor = build_result_descriptor(self.layout, "task-d", tools=self.tools)

        self.assertEqual([e.video_index for e in descriptor.videos], [1])

    def test_missing_task_directory_raises_typed_error(self):
        with self.assertRaises(ResultDescriptorError) as ctx:
            build_result_descriptor(self.layout, "no-such-task", tools=self.tools)

        self.assertIn("no-such-task", str(ctx.exception))

    def test_task_with_no_final_videos_raises_typed_error(self):
        """
        没有成片是正常状态（例如任务失败），但它不是"空的成功"，
        消费方需要能区分两者。
        """
        self._task_dir("task-e")

        with self.assertRaises(ResultDescriptorError):
            build_result_descriptor(self.layout, "task-e", tools=self.tools)

    def test_rejects_a_symlinked_final_video(self):
        """
        软链成片意味着服务器描述的可能是任务目录之外的文件。
        """
        task = self._task_dir("task-f")
        real = self.base / "elsewhere.mp4"
        _make_video(real, 320, 180, 1)
        (task / "final-1.mp4").symlink_to(real)

        with self.assertRaises(ResultDescriptorError) as ctx:
            build_result_descriptor(self.layout, "task-f", tools=self.tools)

        self.assertIn("符号链接", str(ctx.exception))

    def test_rejects_a_non_regular_final_video(self):
        task = self._task_dir("task-g")
        (task / "final-1.mp4").mkdir()

        with self.assertRaises(ResultDescriptorError) as ctx:
            build_result_descriptor(self.layout, "task-g", tools=self.tools)

        self.assertIn("普通文件", str(ctx.exception))

    def test_rejects_duplicate_indices(self):
        """
        final-01.mp4 与 final-1.mp4 在某些文件系统上是同一个名字，
        在另一些上是两个。规范化后冲突必须失败，不能随便挑一个。
        """
        task = self._task_dir("task-h")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        (task / "final-01.mp4").write_bytes(b"x")

        with self.assertRaises(ResultDescriptorError):
            build_result_descriptor(self.layout, "task-h", tools=self.tools)

    def test_rejects_a_task_id_that_is_not_an_opaque_identifier(self):
        """
        task_id 会进入文件路径，必须先校验再拼路径。
        """
        with self.assertRaises(ResultDescriptorError):
            build_result_descriptor(self.layout, "../../etc", tools=self.tools)

    def test_rejects_a_corrupt_video(self):
        """损坏的成片不能被写进描述符，否则下游会一直失败。"""
        task = self._task_dir("task-i")
        (task / "final-1.mp4").write_bytes(b"definitely not an mp4")

        with self.assertRaises(ResultDescriptorError) as ctx:
            build_result_descriptor(self.layout, "task-i", tools=self.tools)

        self.assertIn("task-i", str(ctx.exception))


class TestWriteAndReadResultDescriptor(_LayoutFixture):
    """描述符要落在公开任务挂载之外，并能原样读回。"""

    def test_writes_outside_the_public_task_mount(self):
        """
        storage/tasks 会被静态挂载到 /tasks。描述符必须落在
        descriptor_root，否则任务元数据会跟着公开暴露。
        """
        task = self._task_dir("task-w")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-w", tools=self.tools)

        path, descriptor_id, digest = write_result_descriptor(self.layout, descriptor)

        self.assertTrue(path.is_file())
        self.assertFalse(str(path).startswith(str(self.tasks)))
        self.assertEqual(path.parent.parent, self.layout.descriptor_root)
        self.assertTrue(descriptor_id)
        self.assertEqual(len(digest), 64)

    def test_round_trips_through_disk(self):
        task = self._task_dir("task-r")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        original = build_result_descriptor(self.layout, "task-r", tools=self.tools)

        path, _, _ = write_result_descriptor(self.layout, original)
        loaded = read_result_descriptor(path)

        self.assertEqual(loaded.task_id, original.task_id)
        self.assertEqual(len(loaded.videos), 1)
        self.assertEqual(loaded.videos[0].sha256, original.videos[0].sha256)
        self.assertEqual(loaded.videos[0].width, original.videos[0].width)

    def test_descriptor_id_is_stable_across_rewrites(self):
        """
        同一份描述符反复写入必须得到同一个 descriptor_id 和摘要，
        否则"这个结果变了吗"没法回答。注意 task_id 本身属于内容，
        所以不同任务本来就应该有不同的 ID。
        """
        task = self._task_dir("task-s1")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-s1", tools=self.tools)

        first_path, id_one, digest_one = write_result_descriptor(self.layout, descriptor)
        first_bytes = first_path.read_bytes()
        second_path, id_two, digest_two = write_result_descriptor(self.layout, descriptor)

        self.assertEqual(id_one, id_two)
        self.assertEqual(digest_one, digest_two)
        self.assertEqual(first_bytes, second_path.read_bytes())

    def test_different_tasks_get_different_descriptor_ids(self):
        """成片完全相同但任务不同，ID 必须不同，否则会互相覆盖。"""
        task = self._task_dir("task-s1")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        first = build_result_descriptor(self.layout, "task-s1", tools=self.tools)

        task2 = self._task_dir("task-s2")
        _make_video(task2 / "final-1.mp4", 320, 180, 1)
        second = build_result_descriptor(self.layout, "task-s2", tools=self.tools)

        _, id_one, _ = write_result_descriptor(self.layout, first)
        _, id_two, _ = write_result_descriptor(self.layout, second)

        self.assertNotEqual(id_one, id_two)

    def test_read_rejects_unknown_fields(self):
        """
        向前兼容不等于静默接受：未知字段说明写方比读方新，
        必须显式失败而不是丢掉。
        """
        task = self._task_dir("task-u")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-u", tools=self.tools)
        path, _, _ = write_result_descriptor(self.layout, descriptor)

        raw = json.loads(path.read_text())
        raw["surprise"] = 1
        path.write_text(json.dumps(raw))

        with self.assertRaises(ResultDescriptorError):
            read_result_descriptor(path)

    def test_read_rejects_an_unsupported_schema_version(self):
        task = self._task_dir("task-v")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-v", tools=self.tools)
        path, _, _ = write_result_descriptor(self.layout, descriptor)

        raw = json.loads(path.read_text())
        raw["schema_version"] = 99
        path.write_text(json.dumps(raw))

        with self.assertRaises(ResultDescriptorError) as ctx:
            read_result_descriptor(path)

        self.assertIn("schema_version", str(ctx.exception))

    def test_read_rejects_path_traversal_in_relative_name(self):
        task = self._task_dir("task-t")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-t", tools=self.tools)
        path, _, _ = write_result_descriptor(self.layout, descriptor)

        raw = json.loads(path.read_text())
        raw["videos"][0]["relative_name"] = "../../../etc/passwd"
        path.write_text(json.dumps(raw))

        with self.assertRaises(ResultDescriptorError):
            read_result_descriptor(path)

    def test_read_rejects_a_missing_file(self):
        with self.assertRaises(ResultDescriptorError):
            read_result_descriptor(self.base / "nope.json")

    def test_read_rejects_malformed_json(self):
        broken = self.base / "broken.json"
        broken.write_text("{not json")

        with self.assertRaises(ResultDescriptorError):
            read_result_descriptor(broken)

    def test_descriptor_records_no_absolute_paths_or_secrets(self):
        """
        描述符会随任务状态一起流转，不能携带本机路径。
        """
        task = self._task_dir("task-p")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-p", tools=self.tools)
        path, _, _ = write_result_descriptor(self.layout, descriptor)

        text = path.read_text()
        self.assertNotIn(str(self.base), text)
        self.assertNotIn("/Users/", text)


class TestValidationHelpers(_LayoutFixture):
    """读回路径上的每一条校验都要有独立的失败用例。"""

    def _written(self, task_id: str) -> tuple[Path, dict]:
        task = self._task_dir(task_id)
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, task_id, tools=self.tools)
        path, _, _ = write_result_descriptor(self.layout, descriptor)
        return path, json.loads(path.read_text())

    def _assert_rejected(self, task_id: str, mutate) -> None:
        path, raw = self._written(task_id)
        mutate(raw)
        path.write_text(json.dumps(raw))
        with self.assertRaises(ResultDescriptorError):
            read_result_descriptor(path)

    def test_rejects_wrong_types_for_scalars(self):
        """布尔值在 Python 里是 int 的子类，必须显式拒绝。"""
        for field, bad in (
            ("video_index", True),
            ("bytes", "12"),
            ("width", None),
            ("height", 1.5),
            ("rotation", -1),
            ("duration_ms", -5),
        ):
            with self.subTest(field=field):
                self._assert_rejected(
                    "task-ty",
                    lambda raw, f=field, v=bad: raw["videos"][0].__setitem__(f, v),
                )

    def test_rejects_a_short_or_non_hex_digest(self):
        for bad in ("abc", "z" * 64, 123):
            with self.subTest(bad=bad):
                self._assert_rejected(
                    "task-dg",
                    lambda raw, v=bad: raw["videos"][0].__setitem__("sha256", v),
                )

    def test_rejects_a_bad_relative_name(self):
        for bad in ("final-1.mp4.bak", "sub/final-1.mp4", "..", "final-x.mp4", 5):
            with self.subTest(bad=bad):
                self._assert_rejected(
                    "task-rn",
                    lambda raw, v=bad: raw["videos"][0].__setitem__("relative_name", v),
                )

    def test_rejects_a_bad_task_id(self):
        for bad in ("../etc", "with space", "", "a" * 80, 5):
            with self.subTest(bad=bad):
                self._assert_rejected(
                    "task-ti",
                    lambda raw, v=bad: raw.__setitem__("task_id", v),
                )

    def test_rejects_missing_fields_in_an_entry(self):
        self._assert_rejected(
            "task-mf", lambda raw: raw["videos"][0].pop("width")
        )

    def test_rejects_a_non_object_entry(self):
        self._assert_rejected("task-no", lambda raw: raw["videos"].__setitem__(0, "nope"))

    def test_rejects_empty_or_non_list_videos(self):
        self._assert_rejected("task-ee", lambda raw: raw.__setitem__("videos", []))
        self._assert_rejected("task-el", lambda raw: raw.__setitem__("videos", {}))

    def test_rejects_a_non_object_toolchain(self):
        self._assert_rejected("task-tc", lambda raw: raw.__setitem__("toolchain", []))

    def test_rejects_duplicate_indices_on_read(self):
        def mutate(raw):
            raw["videos"].append(dict(raw["videos"][0]))

        self._assert_rejected("task-di", mutate)


class TestRotationAndDurationParsing(unittest.TestCase):
    """旋转角与时长的来源在不同 ffprobe 版本里位置不同，都要认。"""

    def test_reads_rotation_from_tags(self):
        self.assertEqual(_rotation({"tags": {"rotate": "90"}}), 90)

    def test_reads_rotation_from_side_data(self):
        stream = {"side_data_list": [{"rotation": "-90"}]}
        self.assertEqual(_rotation(stream), 270)

    def test_normalises_equivalent_angles(self):
        self.assertEqual(_rotation({"tags": {"rotate": "360"}}), 0)
        self.assertEqual(_rotation({"tags": {"rotate": "180"}}), 180)
        self.assertEqual(_rotation({"tags": {"rotate": "-180"}}), 180)

    def test_absent_or_unparsable_rotation_defaults_to_zero(self):
        for stream in (
            {},
            {"tags": {}},
            {"tags": {"rotate": "N/A"}},
            {"tags": {"rotate": "sideways"}},
        ):
            with self.subTest(stream=stream):
                self.assertEqual(_rotation(stream), 0)

    def test_reads_duration_from_the_stream(self):
        self.assertEqual(_duration_ms({"duration": "12.5"}, "v.mp4"), 12500)

    def test_falls_back_to_duration_in_tags(self):
        stream = {"duration": None, "tags": {"duration": "2"}}
        self.assertEqual(_duration_ms(stream, "v.mp4"), 2000)

    def test_rejects_unknown_duration(self):
        """
        未知时长无法安全抽帧，必须显式失败而不是当成 0。
        """
        for stream in ({}, {"duration": "N/A"}, {"duration": ""}, {"duration": "abc"}):
            with self.subTest(stream=stream):
                with self.assertRaises(ResultDescriptorError):
                    _duration_ms(stream, "v.mp4")

    def test_rejects_non_positive_duration(self):
        with self.assertRaises(ResultDescriptorError):
            _duration_ms({"duration": "0"}, "v.mp4")


class TestRotationInRealFiles(_LayoutFixture):
    """用真实文件确认旋转信息确实来自 ffprobe 而不是测试桩。"""

    def test_records_rotation_for_a_rotated_video(self):
        task = self._task_dir("task-rot")
        target = task / "final-1.mp4"
        _make_video(target, 320, 180, 1)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(target),
                "-c", "copy", "-metadata:s:v:0", "rotate=90",
                str(self.base / "rotated.mp4"),
            ],
            check=True,
            capture_output=True,
        )
        (self.base / "rotated.mp4").replace(target)

        entry = build_result_descriptor(self.layout, "task-rot", tools=self.tools).videos[0]

        self.assertIn(entry.rotation, (0, 90, 180, 270))


class TestFfprobeFailureHandling(unittest.TestCase):
    """ffprobe 本身出问题时的降级行为：全部转成类型化错误，不泄漏原始异常。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.video = self.base / "clip.mp4"
        self.video.write_bytes(b"payload")

    def tearDown(self):
        self._tmp.cleanup()

    def test_wraps_an_oserror_from_spawning_ffprobe(self):
        """可执行文件在解析后被删除，subprocess 会抛 FileNotFoundError。"""
        with patch("subprocess.run", side_effect=FileNotFoundError("gone")):
            with self.assertRaises(ResultDescriptorError) as ctx:
                _ffprobe_stream(self.video, Path("/nonexistent/ffprobe"))

        self.assertIn("ffprobe", str(ctx.exception))

    def test_wraps_a_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 20)):
            with self.assertRaises(ResultDescriptorError):
                _ffprobe_stream(self.video, Path("/bin/true"))

    def test_rejects_non_json_output(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json at all", stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ResultDescriptorError) as ctx:
                _ffprobe_stream(self.video, Path("/bin/true"))

        self.assertIn("JSON", str(ctx.exception))

    def test_rejects_output_without_a_video_stream(self):
        """音频文件之类的输入必须被明确拒绝。"""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"streams": []}', stderr=""
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ResultDescriptorError) as ctx:
                _ffprobe_stream(self.video, Path("/bin/true"))

        self.assertIn("没有视频流", str(ctx.exception))

    def test_treats_empty_output_as_no_streams(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ResultDescriptorError):
                _ffprobe_stream(self.video, Path("/bin/true"))

    def test_surfaces_a_nonzero_return_code(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="moov atom not found"
        )
        with patch("subprocess.run", return_value=completed):
            with self.assertRaises(ResultDescriptorError) as ctx:
                _ffprobe_stream(self.video, Path("/bin/true"))

        self.assertIn("moov atom not found", str(ctx.exception))


class TestWriteFailureHandling(_LayoutFixture):
    """写描述符失败时不能留下半成品临时文件。"""

    def test_removes_the_temporary_file_when_replace_fails(self):
        """
        原子改名失败会留下一个 .tmp。残留的临时文件会被下一次
        枚举当成有效描述符，或持续占用磁盘。
        """
        task = self._task_dir("task-x")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-x", tools=self.tools)

        with patch("os.replace", side_effect=OSError("EXDEV")):
            with self.assertRaises(OSError):
                write_result_descriptor(self.layout, descriptor)

        leftovers = list(
            self.layout.descriptor_root.rglob(".result_descriptor-*.tmp")
        )
        self.assertEqual(leftovers, [])

    def test_creates_the_descriptor_directory_with_restrictive_permissions(self):
        task = self._task_dir("task-y")
        _make_video(task / "final-1.mp4", 320, 180, 1)
        descriptor = build_result_descriptor(self.layout, "task-y", tools=self.tools)

        path, _, _ = write_result_descriptor(self.layout, descriptor)

        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
