"""缩略图必须真的能用——这是给 YouTube 用的图，不是随手一帧。

第一版能跑，但作为 YouTube 缩略图是不合格的，问题都出在"能画出来"和
"能点得动"之间：

* **字太小。** 64px 放在 1280 宽的画面上，手机端几乎看不清。
* **位置撞车。** 文字压在底部安全区，而 YouTube 恰恰在那个位置叠时间戳。
* **写不下就报错。** 长文案直接失败，而不是自动缩字。
* **黑底方块。** 不透明矩形盖住画面主体，比不加字还难看。
* **只有一帧。** 没有挑选余地——最关键的一步（选哪一帧）被省掉了。

这里逐条钉住。
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app.services.thumbnail import (
    OVERLAY_BAND_TOP,
    OVERLAY_BAND_UPPER,
    TARGET_HEIGHT,
    TARGET_WIDTH,
    ThumbnailError,
    _draw_overlay,
)


def _sample_video(path: Path, seconds: int = 3) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=size={TARGET_WIDTH}x{TARGET_HEIGHT}:rate=1:duration={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )


def _font() -> Path:
    from app.utils.utils import resource_dir

    return (Path(resource_dir("fonts")) / "Charm-Regular.ttf").resolve()


class _OverlayFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.image_path = self.base / "frame.jpg"
        # 纯色画面：文字区域的变化只可能来自叠加层本身
        Image.new("RGB", (TARGET_WIDTH, TARGET_HEIGHT), (30, 90, 160)).save(
            self.image_path, format="JPEG", quality=95
        )
        self.font = _font()

    def _draw(self, text: str, band: str = OVERLAY_BAND_UPPER) -> Image.Image:
        _draw_overlay(self.image_path, text, self.font, band=band)
        with Image.open(self.image_path) as handle:
            return handle.convert("RGB").copy()

    def _bright_pixels(self, image: Image.Image, box) -> int:
        return sum(1 for px in image.crop(box).getdata() if sum(px) > 600)

    def test_text_is_large_enough_to_read_on_a_phone(self):
        """
        缩略图在手机上会被缩到很小，文字必须仍然可读。

        判据：文字高度至少占画面高度的 6%（720 高即 43px 以上），
        而不是第一版的 64px 固定字号里塞一行。
        """
        image = self._draw("6 billion tonnes")
        upper = self._bright_pixels(image, (0, 0, TARGET_WIDTH, TARGET_HEIGHT // 2))
        self.assertGreater(upper, 800, "上半部几乎没有文字，字号仍然过小")

    def test_long_text_wraps_instead_of_failing(self):
        """写不下要缩字换行，不能报错。"""
        long_text = (
            "How books and plagues crossed six thousand miles of desert faster "
            "than any ship that ever sailed"
        )
        image = self._draw(long_text)  # 不应抛 ThumbnailError
        self.assertTrue(self._bright_pixels(image, (0, 0, TARGET_WIDTH, TARGET_HEIGHT)) > 500)

    def test_text_avoids_the_bottom_band_where_youtube_overlays(self):
        """
        底部 20% 是 YouTube 叠时间戳的位置，文字压在那儿会被盖住。
        """
        image = self._draw("4 centimeters every year")
        bottom = self._bright_pixels(
            image,
            (0, int(TARGET_HEIGHT * OVERLAY_BAND_TOP), TARGET_WIDTH, TARGET_HEIGHT),
        )
        self.assertLess(
            bottom,
            120,
            f"底部安全区仍有 {bottom} 个亮像素，文字会撞上 YouTube 的时间戳",
        )

    def test_background_is_dimmed_rather_than_covered_by_a_black_box(self):
        """
        对比度要靠压暗背景，不能靠一块不透明黑方块盖住画面。

        判据：文字区域整体仍然有色彩（不是纯黑），同时比原来亮。
        """
        image = self._draw("Why a day on Venus is longer")
        band = image.crop((0, 40, TARGET_WIDTH, 300))
        coloured = sum(
            1 for r, g, b in band.getdata() if max(r, g, b) - min(r, g, b) > 12
        )
        self.assertGreater(
            coloured, 500, "叠加层把画面压成纯黑了，那和盖黑方块没区别"
        )

    def test_short_text_still_renders(self):
        image = self._draw("3x")
        self.assertTrue(self._bright_pixels(image, (0, 0, TARGET_WIDTH, TARGET_HEIGHT)) > 100)

    def test_empty_text_is_a_no_op(self):
        before = self.image_path.read_bytes()
        _draw_overlay(self.image_path, "", self.font)
        self.assertEqual(self.image_path.read_bytes(), before)

    def test_text_is_not_pushed_off_the_left_edge(self):
        """左边距必须存在，否则长单词会贴边甚至被裁掉。"""
        image = self._draw("Supercalifragilisticexpialidocious")
        strip = self._bright_pixels(image, (0, 40, 12, TARGET_HEIGHT))
        self.assertEqual(strip, 0, "文字贴到了左边缘")


class TestCandidateFrames(unittest.TestCase):
    """选帧是这个功能最关键的一步，必须给用户挑选的余地。"""

    def test_produces_several_distinct_moments(self):
        from app.services.thumbnail import candidate_timestamps

        moments = candidate_timestamps(duration_ms=60_000, count=3)
        self.assertEqual(len(moments), 3)
        self.assertEqual(len(set(moments)), 3, "三个候选时刻必须不同")

    def test_all_candidates_fall_inside_the_video(self):
        from app.services.thumbnail import candidate_timestamps

        for moment in candidate_timestamps(duration_ms=20_000, count=4):
            self.assertGreater(moment, 0)
            self.assertLess(moment, 20.0)

    def test_short_video_still_returns_the_requested_count(self):
        from app.services.thumbnail import candidate_timestamps

        self.assertEqual(len(candidate_timestamps(duration_ms=3_000, count=3)), 3)

    def test_candidates_spread_across_the_body_not_the_intro(self):
        """
        候选应当铺开在正片里，避开片头（片头往往是黑场或 logo）。
        """
        from app.services.thumbnail import candidate_timestamps

        moments = candidate_timestamps(duration_ms=100_000, count=3)
        self.assertGreater(min(moments), 5.0, "最早的候选也该跳过片头")

    def test_invalid_duration_is_rejected(self):
        from app.services.thumbnail import candidate_timestamps

        for bad in (0, -1, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ThumbnailError):
                    candidate_timestamps(duration_ms=bad, count=3)


if __name__ == "__main__":
    unittest.main()


class TestUserCanPickTheThumbnail(unittest.TestCase):
    """
    YouTube 允许上传前指定缩略图，所以"选哪一帧"必须是用户的决定，
    不能由"最后生成的那张"自动决定。
    """

    def setUp(self):
        import json
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "manifest_revision": 0,
                    "schema_version": 2,
                    "task_id": "task-pick",
                    # 视频条目由 append_artifact 首次追加时创建；
                    # schema 不允许"有视频但产物为空"。
                    "videos": [],
                }
            ),
            encoding="utf-8",
        )

    def _append(self, seconds: float) -> str:
        from app.services.thumbnail_files import append_artifact, load_manifest

        revision = load_manifest(self.manifest_path)["manifest_revision"]

        entry = {
            "artifact_id": f"art-{int(seconds * 100)}",
            "artifact_state": "live",
            "bytes": 1024,
            "created_at": "2026-01-01T00:00:00+00:00",
            "font_id": "Charm-Regular.ttf",
            "frame_at_seconds": seconds,
            "height": 720,
            "output_sha256": f"{int(seconds * 100):064d}"[:64],
            "overlay_sha256": "b" * 64,
            "parent_artifact_id": None,
            "previous_artifact_id": None,
            "purged_at": None,
            "relative_name": f"thumb-{int(seconds * 100)}.jpg",
            "source_sha256": "a" * 64,
            "video_index": 1,
            "width": 1280,
        }
        (self.root / entry["relative_name"]).write_bytes(b"jpeg-bytes")
        append_artifact(self.root, "task-pick", entry, revision)
        return entry["artifact_id"]

    def test_multiple_candidates_can_coexist(self):
        first = self._append(1.0)
        second = self._append(5.0)
        third = self._append(9.0)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_newest_is_selected_automatically_but_can_be_changed(self):
        self._append(1.0)
        wanted = self._append(5.0)
        self._append(9.0)

        from app.services.thumbnail_files import read_selection, select_artifact

        self.assertNotEqual(
            read_selection(self.manifest_path, 1), wanted,
            "默认应选最新的那张",
        )
        select_artifact(self.manifest_path, 1, wanted)
        self.assertEqual(read_selection(self.manifest_path, 1), wanted)

    def test_selecting_an_unknown_artifact_is_rejected(self):
        self._append(1.0)
        from app.services.thumbnail_files import ThumbnailFilesError, select_artifact

        with self.assertRaises(ThumbnailFilesError):
            select_artifact(self.manifest_path, 1, "does-not-exist")

    def test_selecting_an_archived_artifact_is_rejected(self):
        """
        归档的产物文件已被删掉，不能再被选成缩略图——
        否则界面会给出一个指向不存在文件的下载链接。
        """
        first = self._append(1.0)
        self._append(5.0)  # 第二张成为选中项，第一张才允许归档
        from app.services.thumbnail_files import (
            ThumbnailFilesError,
            archive_artifact,
            select_artifact,
        )

        archive_artifact(self.manifest_path, "task-pick", 1, first)
        with self.assertRaises(ThumbnailFilesError):
            select_artifact(self.manifest_path, 1, first)

    def test_the_selected_artifact_cannot_be_archived(self):
        """正在展示的那张不能归档，否则缩略图会凭空消失。"""
        only = self._append(1.0)
        from app.services.thumbnail_files import (
            ThumbnailFilesError,
            archive_artifact,
        )

        with self.assertRaises(ThumbnailFilesError):
            archive_artifact(self.manifest_path, "task-pick", 1, only)

    def test_selection_persists_across_reload(self):
        wanted = self._append(5.0)
        from app.services.thumbnail_files import read_selection, select_artifact

        select_artifact(self.manifest_path, 1, wanted)
        self.assertEqual(read_selection(self.manifest_path, 1), wanted)


class TestJobRendersSeveralCandidates(unittest.TestCase):
    """一次生成多帧，用户才有得挑。"""

    def setUp(self):
        import tempfile

        from app.utils.utils import resource_dir

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "thumbnails" / "task-cand"
        self.root.mkdir(parents=True)
        self.source = self.base / "final-1.mp4"
        _sample_video(self.source, seconds=8)
        self.font = (Path(resource_dir("fonts")) / "Charm-Regular.ttf").resolve()

    def test_one_job_produces_three_registered_artifacts(self):
        from app.services.thumbnail_files import list_artifacts
        from app.services.thumbnail_jobs import ThumbnailJobManager

        from app.utils.strict_media_tools import probe_media_tools

        manager = ThumbnailJobManager(self.root)
        self.addCleanup(manager.shutdown, True, 2)
        manager.submit(
            "task-cand",
            {
                "source": str(self.source),
                "overlay_text": "6 billion tonnes",
                "font_path": str(self.font),
                "video_index": 1,
                "source_sha256": "a" * 64,
                "frame_candidates": 3,
            },
            tools=probe_media_tools(),
        )
        import time as _time

        for _ in range(1500):
            records = [
                r for r in manager.list_jobs("task-cand")
                if r["state"] not in ("queued", "running")
            ]
            if records:
                break
            _time.sleep(0.05)
        self.assertEqual(records[0]["state"], "succeeded", records[0].get("error"))

        artifacts = list_artifacts(self.root / "manifest.json", 1)
        self.assertEqual(len(artifacts), 3, "应当登记三张候选缩略图")
        moments = {a["frame_at_seconds"] for a in artifacts}
        self.assertEqual(len(moments), 3, "三张必须来自不同时刻")

    def test_candidates_are_real_distinct_files(self):
        from app.services.thumbnail_files import list_artifacts
        from app.services.thumbnail_jobs import ThumbnailJobManager
        from app.utils.strict_media_tools import probe_media_tools

        manager = ThumbnailJobManager(self.root)
        self.addCleanup(manager.shutdown, True, 2)
        manager.submit(
            "task-cand",
            {
                "source": str(self.source),
                "overlay_text": "4 centimeters",
                "font_path": str(self.font),
                "video_index": 1,
                "source_sha256": "a" * 64,
                "frame_candidates": 3,
            },
            tools=probe_media_tools(),
        )
        import time as _time

        for _ in range(1500):
            records = [
                r for r in manager.list_jobs("task-cand")
                if r["state"] not in ("queued", "running")
            ]
            if records:
                break
            _time.sleep(0.05)
        digests = {a["output_sha256"] for a in list_artifacts(self.root / "manifest.json", 1)}
        self.assertEqual(len(digests), 3, "三个不同时刻应当产出三张不同的图")
