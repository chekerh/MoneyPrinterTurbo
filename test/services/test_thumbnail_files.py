"""验证缩略图清单是唯一的提交记录，并且能在崩溃后判断出真实状态。

清单要回答两个问题：现在选中的产物是哪个，以及"多出来的那个文件"到底是
一次成功但没记账的发布，还是一次失败留下的垃圾。区分不了这两者，清理逻辑
就只能两种极端：全删（丢掉真结果），或全留（垃圾无限堆积）。
"""

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.thumbnail_files import (
    MANIFEST_FILENAME,
    ThumbnailFilesError,
    archive_artifact,
    append_artifact,
    list_artifacts,
    load_manifest,
    manifest_digest,
    next_artifact_id,
    read_selection,
    recover_orphans,
    write_manifest,
)


class _ManifestFixture(unittest.TestCase):
    """准备一棵临时的缩略图根。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "thumbnails" / "task-a"
        self.root.mkdir(parents=True)
        self.task_id = "task-a"

    def tearDown(self):
        self._tmp.cleanup()

    def _video(self, index: int, content: bytes = b"v") -> Path:
        name = f"final-{index}.mp4"
        path = self.root / name
        path.write_bytes(content)
        return path

    def _id(self, index: int) -> str:
        """真实产物 ID 是 uuid4().hex（32 位十六进制）。"""
        return f"{index:032x}"

    def _orphan_name(self) -> str:
        """一个格式合法但不在清单里的产物文件名。"""
        return "thumbnail-" + "e" * 32 + ".jpg"

    def _artifact_file(self, artifact_id: str) -> Path:
        """创建产物文件。清单不允许引用不存在的文件。"""
        path = self.root / f"thumbnail-{artifact_id}.jpg"
        path.write_bytes(b"jpeg")
        return path

    def _artifact(self, ordinal: int, video_index: int = 1, **overrides) -> dict:
        """
        构造一个产物条目。

        ``ordinal`` 只用于生成互不相同的 ID 和文件名；``video_index`` 决定它属于
        哪个视频。两者必须分开——把"第二个产物"写成 video_index=2 会新建一个视频，
        而不是给视频 1 追加产物。
        """
        fields = {
            "artifact_id": self._id(ordinal),
            "video_index": video_index,
            "parent_artifact_id": None,
            "previous_artifact_id": None,
            "relative_name": f"thumbnail-{self._id(ordinal)}.jpg",
            "source_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "overlay_sha256": "c" * 64,
            "font_id": "Charm-Regular.ttf",
            "frame_at_seconds": 2.0,
            "width": 1280,
            "height": 720,
            "bytes": 1234,
            "created_at": "2026-09-26T00:00:00+00:00",
            "artifact_state": "live",
            "purged_at": None,
        }
        fields.update(overrides)
        return fields

    def _seed(self, count: int = 1) -> dict:
        """写入一份含 count 个产物、已选中的清单。"""
        manifest = {
            "schema_version": 2,
            "task_id": self.task_id,
            "manifest_revision": count,
            "videos": [],
        }
        for index in range(1, count + 1):
            self._video(index)
            self._artifact_file(self._id(index))
            manifest["videos"].append({
                "video_index": index,
                "source_sha256": "a" * 64,
                "artifacts": [self._artifact(index, video_index=index)],
                "selected_artifact_id": self._id(index),
            })
        write_manifest(self.root, manifest)
        return manifest

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILENAME

    def read_all(self) -> dict:
        """读回清单原始结构，供断言直接取字段。"""
        return load_manifest(self.manifest_path)


class TestAppendArtifact(_ManifestFixture):
    """追加产物：只追加，不覆盖，并且只在提交成功后移动选择。"""

    def test_creates_a_manifest_when_none_exists(self):
        self._artifact_file(self._id(1))
        entry = self._artifact(1)

        manifest = append_artifact(self.root, self.task_id, entry, expected_revision=0)

        self.assertEqual(manifest["manifest_revision"], 1)
        self.assertEqual(manifest["videos"][0]["selected_artifact_id"], self._id(1))
        self.assertTrue(self.manifest_path.is_file())

    def test_appends_without_replacing_the_previous_entry(self):
        self._seed(count=1)
        self._artifact_file(self._id(2))
        previous = self.read_all()["videos"][0]["artifacts"][0]["artifact_id"]

        append_artifact(
            self.root,
            self.task_id,
            self._artifact(2, video_index=1, previous_artifact_id=previous),
            expected_revision=1,
        )

        artifacts = self.read_all()["videos"][0]["artifacts"]
        self.assertEqual([a["artifact_id"] for a in artifacts], [self._id(1), self._id(2)])

    def test_moves_the_selection_to_the_new_artifact(self):
        self._seed(count=1)
        self._artifact_file(self._id(2))

        append_artifact(
            self.root, self.task_id, self._artifact(2, video_index=1), expected_revision=1
        )

        self.assertEqual(self.read_all()["videos"][0]["selected_artifact_id"], self._id(2))

    def test_rejects_a_stale_revision(self):
        """
        清单文件是被替换的那一个，所以并发写者必须在替换前确认
        磁盘上的版本仍是自己读到的那一版。
        """
        self._seed(count=1)

        with self.assertRaises(ThumbnailFilesError) as ctx:
            append_artifact(
                self.root, self.task_id, self._artifact(2, video_index=1), expected_revision=99
            )

        self.assertIn("revision", str(ctx.exception))

    def test_rejects_a_rejected_duplicate_artifact_id(self):
        self._seed(count=1)
        self._artifact_file(self._id(1))

        with self.assertRaises(ThumbnailFilesError):
            append_artifact(
                self.root,
                self.task_id,
                self._artifact(1, video_index=1),
                expected_revision=1,
            )

    def test_rejects_an_artifact_whose_file_is_absent(self):
        """
        清单不能引用不存在的文件，否则 UI 会展示一个打不开的缩略图。
        """
        with self.assertRaises(ThumbnailFilesError) as ctx:
            append_artifact(self.root, self.task_id, self._artifact(1, video_index=1), expected_revision=0)

        self.assertIn("不存在", str(ctx.exception))

    def test_rejects_a_path_escape_in_relative_name(self):
        entry = self._artifact(1, video_index=1, relative_name="../../etc/passwd")

        with self.assertRaises(ThumbnailFilesError):
            append_artifact(self.root, self.task_id, entry, expected_revision=0)

    def test_rejects_an_absolute_relative_name(self):
        with self.assertRaises(ThumbnailFilesError):
            append_artifact(
                self.root, self.task_id, self._artifact(1, video_index=1, relative_name="/etc/passwd"),
                expected_revision=0,
            )

    def test_rejects_a_symlinked_artifact_file(self):
        """清单里的每个文件都必须是任务目录内的真实文件。"""
        real = self.base / "outside.jpg"
        real.write_bytes(b"jpeg")
        (self.root / f"thumbnail-{self._id(1)}.jpg").symlink_to(real)

        with self.assertRaises(ThumbnailFilesError) as ctx:
            append_artifact(self.root, self.task_id, self._artifact(1, video_index=1), expected_revision=0)

        self.assertIn("符号链接", str(ctx.exception))

    def test_rejects_a_symlink_that_points_inside_the_root(self):
        """
        指向根内的链接同样要拒绝。resolve() 会跟随它，只查"是否逃出根目录"
        会把这种链接判为合法。
        """
        real = self._artifact_file(self._id(9))
        (self.root / f"thumbnail-{self._id(1)}.jpg").symlink_to(real)

        with self.assertRaises(ThumbnailFilesError) as ctx:
            append_artifact(self.root, self.task_id, self._artifact(1, video_index=1), expected_revision=0)

        self.assertIn("符号链接", str(ctx.exception))

    def test_rejects_a_manifest_whose_selected_artifact_is_missing(self):
        manifest = self._seed(count=1)
        manifest["videos"][0]["selected_artifact_id"] = "f" * 32
        write_manifest(self.root, manifest)

        with self.assertRaises(ThumbnailFilesError):
            append_artifact(
                self.root, self.task_id, self._artifact(2, video_index=1), expected_revision=1
            )

    def test_does_not_touch_the_manifest_when_validation_fails(self):
        self._seed(count=1)
        before = self.manifest_path.read_bytes()

        with self.assertRaises(ThumbnailFilesError):
            append_artifact(
                self.root, self.task_id, self._artifact(2, video_index=1, relative_name="../x"),
                expected_revision=1,
            )

        self.assertEqual(self.manifest_path.read_bytes(), before)


class TestLoadManifest(_ManifestFixture):
    """读取与校验：未知字段、版本漂移、坏 JSON 一律失败。"""

    def test_round_trips_a_seeded_manifest(self):
        self._seed(count=2)
        manifest = load_manifest(self.manifest_path)
        self.assertEqual(manifest["task_id"], self.task_id)
        self.assertEqual(manifest["manifest_revision"], 2)

    def test_rejects_a_missing_manifest(self):
        with self.assertRaises(ThumbnailFilesError):
            load_manifest(self.manifest_path)

    def test_rejects_malformed_json(self):
        self.manifest_path.write_text("{not json")
        with self.assertRaises(ThumbnailFilesError):
            load_manifest(self.manifest_path)

    def test_rejects_an_unknown_field(self):
        self._seed(count=1)
        raw = json.loads(self.manifest_path.read_text())
        raw["surprise"] = True
        self.manifest_path.write_text(json.dumps(raw))

        with self.assertRaises(ThumbnailFilesError) as ctx:
            load_manifest(self.manifest_path)

        self.assertIn("surprise", str(ctx.exception))

    def test_rejects_an_unsupported_schema_version(self):
        self._seed(count=1)
        raw = json.loads(self.manifest_path.read_text())
        raw["schema_version"] = 1
        self.manifest_path.write_text(json.dumps(raw))

        with self.assertRaises(ThumbnailFilesError) as ctx:
            load_manifest(self.manifest_path)

        self.assertIn("schema_version", str(ctx.exception))

    def test_rejects_a_task_id_mismatch(self):
        """清单必须属于它所在的任务目录，否则清理会算错范围。"""
        self._seed(count=1)
        raw = json.loads(self.manifest_path.read_text())
        raw["task_id"] = "other-task"
        self.manifest_path.write_text(json.dumps(raw))

        with self.assertRaises(ThumbnailFilesError) as ctx:
            load_manifest(self.manifest_path, expected_task_id=self.task_id)

        self.assertIn("other-task", str(ctx.exception))

    def test_digest_is_stable_and_content_sensitive(self):
        self._seed(count=1)
        first = manifest_digest(load_manifest(self.manifest_path))

        again = manifest_digest(load_manifest(self.manifest_path))
        self.assertEqual(first, again)

        raw = json.loads(self.manifest_path.read_text())
        raw["manifest_revision"] = 99
        self.manifest_path.write_text(json.dumps(raw))
        self.assertNotEqual(first, manifest_digest(load_manifest(self.manifest_path)))


class TestWriteManifest(_ManifestFixture):
    """写入必须是原子的，否则崩溃会留下半个 JSON。"""

    def test_leaves_no_temporary_file_behind(self):
        self._seed(count=1)
        leftovers = list(self.root.glob(".manifest-*.tmp"))
        self.assertEqual(leftovers, [])

    def test_replaces_atomically(self):
        self._seed(count=1)
        write_manifest(self.root, load_manifest(self.manifest_path))
        self.assertEqual(len(list(self.root.glob(".manifest-*.tmp"))), 0)

    def test_sets_owner_only_permissions_on_non_windows(self):
        if os.name == "nt":
            self.skipTest("POSIX 权限位不适用")
        self._seed(count=1)
        self.assertEqual(self.manifest_path.stat().st_mode & 0o777, 0o600)

    def test_translates_exdev_into_a_typed_error(self):
        """
        macOS 上所有 APFS 卷共用同一个 st_dev，预检判断不出跨卷，
        所以真正的兜底是捕获 EXDEV 并转成可识别的类型化错误。
        """
        manifest = load_manifest(self.manifest_path) if self.manifest_path.is_file() \
            else self._seed(count=1)

        with patch("os.replace", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            with self.assertRaises(ThumbnailFilesError) as ctx:
                write_manifest(self.root, manifest)

        self.assertIn("同一文件系统", str(ctx.exception))

    def test_exdev_leaves_no_temporary_file_behind(self):
        manifest = load_manifest(self.manifest_path) if self.manifest_path.is_file() \
            else self._seed(count=1)

        with patch("os.replace", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            with self.assertRaises(ThumbnailFilesError):
                write_manifest(self.root, manifest)

        self.assertEqual(list(self.root.glob(".manifest-*.tmp")), [])

    def test_other_os_errors_keep_their_identity(self):
        """
        只有 EXDEV 需要翻译。其他 OSError 原样抛出，避免掩盖真实原因
        （权限、只读文件系统等）。
        """
        manifest = load_manifest(self.manifest_path) if self.manifest_path.is_file() \
            else self._seed(count=1)

        with patch("os.replace", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PermissionError):
                write_manifest(self.root, manifest)


class TestSelectionAndListing(_ManifestFixture):
    """选择与列举只暴露描述符/清单里有的东西。"""

    def test_reads_the_selected_artifact(self):
        self._seed(count=1)
        self.assertEqual(read_selection(self.manifest_path, 1), self._id(1))

    def test_returns_none_for_an_unknown_video(self):
        self._seed(count=1)
        self.assertIsNone(read_selection(self.manifest_path, 7))

    def test_returns_none_when_nothing_is_selected(self):
        self._seed(count=1)
        raw = json.loads(self.manifest_path.read_text())
        raw["videos"][0]["selected_artifact_id"] = None
        self.manifest_path.write_text(json.dumps(raw))

        self.assertIsNone(read_selection(self.manifest_path, 1))

    def test_lists_only_live_artifacts(self):
        self._seed(count=1)
        manifest = load_manifest(self.manifest_path)
        manifest["videos"][0]["artifacts"][0]["artifact_state"] = "purged"
        write_manifest(self.root, manifest)

        self.assertEqual(list_artifacts(self.manifest_path, 1, live_only=True), [])

    def test_lists_purged_artifacts_when_asked(self):
        self._seed(count=1)
        manifest = load_manifest(self.manifest_path)
        manifest["videos"][0]["artifacts"][0]["artifact_state"] = "purged"
        write_manifest(self.root, manifest)

        self.assertEqual(len(list_artifacts(self.manifest_path, 1, live_only=False)), 1)

    def test_artifact_ids_are_unique_and_opaque(self):
        first = next_artifact_id()
        second = next_artifact_id()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 16)


class TestRecoverOrphans(_ManifestFixture):
    """崩溃恢复：区分"已提交"与"没记账"。"""

    def test_quarantines_a_file_not_referenced_by_the_manifest(self):
        """
        抽帧成功但清单提交前崩溃：文件在磁盘上，清单里没有。
        它必须被隔离而不是当成有效产物。
        """
        self._seed(count=1)
        orphan = self.root / self._orphan_name()
        orphan.write_bytes(b"orphan jpeg")

        removed = recover_orphans(self.root, self.manifest_path)

        self.assertEqual(removed, [orphan.name])
        self.assertFalse(orphan.exists())
        self.assertEqual(
            [p.name for p in (self.root / ".quarantine").iterdir()],
            [self._orphan_name()],
        )

    def test_keeps_files_the_manifest_references(self):
        self._seed(count=1)

        removed = recover_orphans(self.root, self.manifest_path)

        self.assertEqual(removed, [])
        self.assertTrue((self.root / f"thumbnail-{self._id(1)}.jpg").exists())

    def test_is_a_no_op_when_there_is_no_manifest(self):
        """
        没有清单意味着还没有任何已提交状态，此时不应当把整个目录
        当成垃圾清空——它可能正处在首次发布的中间。
        """
        stray = self.root / f"thumbnail-{self._id(1)}.jpg"
        stray.write_bytes(b"x")

        removed = recover_orphans(self.root, self.manifest_path)

        self.assertEqual(removed, [])
        self.assertTrue(stray.exists())

    def test_does_not_touch_source_videos(self):
        self._seed(count=1)
        recover_orphans(self.root, self.manifest_path)
        self.assertTrue((self.root / "final-1.mp4").exists())

    def test_is_idempotent(self):
        self._seed(count=1)
        orphan = self.root / self._orphan_name()
        orphan.write_bytes(b"orphan")

        first = recover_orphans(self.root, self.manifest_path)
        second = recover_orphans(self.root, self.manifest_path)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_ignores_the_quarantine_directory_itself(self):
        self._seed(count=1)
        recover_orphans(self.root, self.manifest_path)

        self.assertEqual(recover_orphans(self.root, self.manifest_path), [])


class TestArchiveArtifact(_ManifestFixture):
    """归档：只删文件，保留血缘。"""

    def test_marks_an_unselected_artifact_purged_and_removes_its_file(self):
        self._seed(count=1)
        self._artifact_file(self._id(2))
        append_artifact(
            self.root, self.task_id, self._artifact(2, video_index=1), expected_revision=1
        )

        manifest = archive_artifact(self.manifest_path, self.task_id, 1, self._id(1))

        states = {
            a["artifact_id"]: a["artifact_state"] for a in manifest["videos"][0]["artifacts"]
        }
        self.assertEqual(states[self._id(1)], "purged")
        self.assertEqual(states[self._id(2)], "live")
        self.assertFalse((self.root / f"thumbnail-{self._id(1)}.jpg").exists())
        self.assertTrue((self.root / f"thumbnail-{self._id(2)}.jpg").exists())

    def test_preserves_lineage_fields_after_archiving(self):
        self._seed(count=1)
        self._artifact_file(self._id(2))
        append_artifact(
            self.root, self.task_id, self._artifact(2, video_index=1), expected_revision=1
        )

        manifest = archive_artifact(self.manifest_path, self.task_id, 1, self._id(1))

        archived = next(
            a for a in manifest["videos"][0]["artifacts"] if a["artifact_id"] == self._id(1)
        )
        self.assertEqual(archived["output_sha256"], "b" * 64)
        self.assertEqual(archived["source_sha256"], "a" * 64)
        self.assertIn("purged_at", archived)

    def test_refuses_to_archive_the_selected_artifact(self):
        self._seed(count=1)

        with self.assertRaises(ThumbnailFilesError) as ctx:
            archive_artifact(self.manifest_path, self.task_id, 1, self._id(1))

        self.assertIn("选中", str(ctx.exception))
        self.assertTrue((self.root / f"thumbnail-{self._id(1)}.jpg").exists())

    def test_refuses_to_archive_an_unknown_artifact(self):
        self._seed(count=1)
        with self.assertRaises(ThumbnailFilesError):
            archive_artifact(self.manifest_path, self.task_id, 1, "n" * 32)

    def test_refuses_to_archive_for_an_unknown_video(self):
        self._seed(count=1)
        with self.assertRaises(ThumbnailFilesError):
            archive_artifact(self.manifest_path, self.task_id, 7, self._id(1))

    def test_is_idempotent_for_an_already_purged_artifact(self):
        self._seed(count=1)
        self._artifact_file(self._id(2))
        append_artifact(
            self.root, self.task_id, self._artifact(2, video_index=1), expected_revision=1
        )
        archive_artifact(self.manifest_path, self.task_id, 1, self._id(1))

        manifest = archive_artifact(self.manifest_path, self.task_id, 1, self._id(1))

        states = [a["artifact_state"] for a in manifest["videos"][0]["artifacts"]]
        self.assertEqual(states.count("purged"), 1)


if __name__ == "__main__":
    unittest.main()
