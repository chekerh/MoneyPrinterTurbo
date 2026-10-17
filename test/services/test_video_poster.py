"""归档成片的封面帧：缓存、失效与降级。

计划表现在每行都有一张封面，但这些成片是归档后的副本，缩略图功能按任务存的那份
manifest 认不出它们，因此只能现抽。抽帧不便宜（每条约 0.14 秒，几乎全是进程启动
开销），所以缓存的正确性直接决定这个列表还能不能用：

* 缓存必须生效，否则 11 行就是 1.5 秒起步，每次打开都重来一遍；
* 缓存必须会失效，否则被重写的成片会一直显示旧画面，而且没有任何症状；
* 抽不出来必须返回 None 而不是抛异常——素材库里混着损坏文件、正在写入的成片、
  以及压根没装 ffmpeg 的机器。
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import video_poster


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestVideoPoster(unittest.TestCase):
    def setUp(self):
        video_poster._MEMO.clear()
        self._tmp = None

    def _video(self, tmp, name="v.mp4", payload=b"x" * 2048):
        path = Path(tmp) / name
        path.write_bytes(payload)
        return str(path)

    def _redirect_storage(self, tmp):
        # 必须尊重 sub_dir：stub 一律返回 tmp 的话，服务会去找 tmp/<key>.jpg，
        # 而用例写在 tmp/posters/ 下，于是永远测的是"缓存没命中"。
        base = Path(tmp)

        def storage_dir(sub_dir="", create=False):
            target = base / sub_dir if sub_dir else base
            if create:
                target.mkdir(parents=True, exist_ok=True)
            return str(target)

        return patch.object(video_poster.utils, "storage_dir", storage_dir)

    def test_missing_file_yields_none(self):
        self.assertIsNone(video_poster.poster_frame_path("/definitely/not/here.mp4"))

    def test_empty_path_yields_none(self):
        self.assertIsNone(video_poster.poster_frame_path(""))

    def test_duration_is_probed_with_ffprobe_not_a_full_decode(self):
        """
        读时长只能用 ffprobe。

        ``ffmpeg -f null -`` 会把整段视频解一遍：一条 50 秒 1080p 要一秒，而我们要的
        只是头部那行 Duration。列表里每行都付这一秒，是这个功能能不能用的分界线。
        """
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return _FakeCompleted(stdout="50.93\n")

        with self._redirect_storage("/tmp"), patch.object(
            video_poster.subprocess, "run", side_effect=fake_run
        ), patch.object(video_poster.utils, "get_ffmpeg_binary", return_value="ffmpeg"):
            self.assertEqual(video_poster._probe_duration_seconds("/x.mp4"), 50.93)

        self.assertTrue(calls, "没有调用 ffprobe")
        self.assertIn("ffprobe", calls[0])
        self.assertNotIn("-f", calls[0], "时长探测不该走 ffmpeg 的解码路径")
        self.assertNotIn("null", calls[0], "时长探测不该把整段视频解一遍")

    def test_unparseable_duration_yields_none(self):
        with patch.object(
            video_poster.subprocess,
            "run",
            return_value=_FakeCompleted(stdout="not-a-number"),
        ):
            self.assertIsNone(video_poster._probe_duration_seconds("/x.mp4"))

    def test_poster_is_cached_on_disk(self):
        """
        第二次调用不得再起 ffmpeg。

        这是"抽帧很贵"这件事唯一的保护：没有它，计划表每打开一次就要付 11 × 0.14 秒。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(tmp)
            cache = Path(tmp) / "posters"
            cache.mkdir()
            # 必须落在真实的缓存文件名上，否则测的是"缓存没命中"而不是"命中后不再抽帧"
            produced = cache / f"{video_poster._cache_key(video)}.jpg"
            produced.write_bytes(b"jpeg-bytes")

            def fake_run(cmd, **_kwargs):
                raise AssertionError("命中缓存时不该调用 ffmpeg")

            with self._redirect_storage(tmp), patch.object(
                video_poster.subprocess, "run", side_effect=fake_run
            ):
                result = video_poster.poster_frame_path(video)

            self.assertIsNotNone(result)
            self.assertEqual(result.read_bytes(), b"jpeg-bytes")

    def test_a_rewritten_video_does_not_keep_the_stale_poster(self):
        """成片被重写后必须重新抽帧，否则会一直显示旧画面。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(tmp, payload=b"x" * 2048)
            cache = Path(tmp) / "posters"
            cache.mkdir()
            # 旧缓存按"重写之前"那把键存下
            before = video_poster._cache_key(video)
            (cache / f"{before}.jpg").write_bytes(b"old")

            # 真正改写视频：大小与 mtime 都变，键必须跟着变
            Path(video).write_bytes(b"y" * 4096)
            after = video_poster._cache_key(video)
            self.assertNotEqual(
                after,
                before,
                "成片重写之后缓存键没有变化，旧封面会被一直沿用",
            )

            extracted = []

            def fake_run(cmd, **_kwargs):
                if "ffprobe" in cmd[0]:
                    return _FakeCompleted(stdout="10.0")
                extracted.append(cmd)
                Path(cmd[-1]).write_bytes(b"new-jpeg")
                return _FakeCompleted()

            with self._redirect_storage(tmp), patch.object(
                video_poster.subprocess, "run", side_effect=fake_run
            ), patch.object(video_poster.utils, "get_ffmpeg_binary", return_value="ffmpeg"):
                result = video_poster.poster_frame_path(video)

            self.assertTrue(extracted, "内容变了却仍在用旧缓存")
            self.assertEqual(result.read_bytes(), b"new-jpeg")

    def test_failed_extraction_leaves_no_half_written_cache(self):
        """
        抽帧失败不能留下半张图。

        留着半张图的话，后续调用会把它当成有效缓存直接读走，用户看到的是一张
        永远修不好的坏图——比明确地"没有封面"糟糕得多。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(tmp)

            def fake_run(cmd, **_kwargs):
                if "ffprobe" in cmd[0]:
                    return _FakeCompleted(stdout="10.0")
                Path(cmd[-1]).write_bytes(b"half")
                return _FakeCompleted(returncode=1)

            with self._redirect_storage(tmp), patch.object(
                video_poster.subprocess, "run", side_effect=fake_run
            ), patch.object(video_poster.utils, "get_ffmpeg_binary", return_value="ffmpeg"):
                self.assertIsNone(video_poster.poster_frame_path(video))

            leftovers = list((Path(tmp) / "posters").glob("*.jpg"))
            self.assertEqual(leftovers, [], f"失败后仍留下了缓存文件：{leftovers}")

    def test_missing_ffmpeg_is_not_an_error(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(tmp)
            with self._redirect_storage(tmp), patch.object(
                video_poster.utils,
                "get_ffmpeg_binary",
                side_effect=RuntimeError("ffmpeg not installed"),
            ):
                self.assertIsNone(video_poster.poster_frame_path(video))

    def test_repeated_calls_do_not_respawn_subprocesses(self):
        """2 秒轮询的面板不能每次都起子进程。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            video = self._video(tmp)
            cache = Path(tmp) / "posters"
            cache.mkdir()
            (cache / f"{video_poster._cache_key(video)}.jpg").write_bytes(b"jpeg")

            calls = []

            def counting_run(cmd, **_kwargs):
                calls.append(cmd)
                return _FakeCompleted(stdout="10.0")

            with self._redirect_storage(tmp), patch.object(
                video_poster.subprocess, "run", side_effect=counting_run
            ), patch.object(video_poster.utils, "get_ffmpeg_binary", return_value="ffmpeg"):
                for _ in range(5):
                    video_poster.poster_frame_path(video)

            self.assertEqual(calls, [], "命中进程内备忘时不该再起子进程")

    def test_frame_timestamp_matches_the_thumbnail_service(self):
        """
        封面与缩略图必须取同一帧。

        两处各算各的，用户就会在两个界面看到不同的画面，然后怀疑其中一个坏了。
        """
        from app.services.thumbnail import plan_frame_timestamp

        for duration in (1.2, 4.0, 12.0, 180.0):
            with self.subTest(duration=duration):
                expected = plan_frame_timestamp(int(duration * 1000))
                got = min(duration - 0.1, max(0.5, duration * 0.25))
                self.assertAlmostEqual(got, expected, places=3)


if __name__ == "__main__":
    unittest.main()
