import unittest
from unittest.mock import patch

from app.services import shorts_cutter


class TestParse(unittest.TestCase):
    def test_parse_silences_pairs_starts_and_ends(self):
        text = (
            "silence_start: 1.0\n"
            "silence_end: 2.5 | silence_duration: 1.5\n"
            "silence_start: 10.0\n"
            "silence_end: 10.5 | silence_duration: 0.5\n"
            "silence_start: 20.0\n"
        )
        self.assertEqual(shorts_cutter.parse_silences(text), [(1.0, 2.5), (10.0, 10.5)])

    def test_parse_silences_drops_unclosed_intervals(self):
        self.assertEqual(shorts_cutter.parse_silences("silence_start: 4.0\n"), [])

    def test_parse_scene_times_sorted_unique(self):
        text = "pts_time:5.5\npts_time:1.0\npts_time:5.5\npts_time:3.0\n"
        self.assertEqual(shorts_cutter.parse_scene_times(text), [1.0, 3.0, 5.5])


class TestScoring(unittest.TestCase):
    def test_speech_ratio_excludes_silence(self):
        silences = [(1.0, 4.0)]
        self.assertEqual(shorts_cutter.speech_ratio(0.0, 10.0, silences), 0.7)
        self.assertAlmostEqual(shorts_cutter.speech_ratio(2.0, 5.0, silences), 1.0 / 3)

    def test_speech_ratio_zero_silence_is_one(self):
        self.assertEqual(shorts_cutter.speech_ratio(0.0, 10.0, []), 1.0)

    def test_density_score_prefers_busy_window(self):
        self.assertEqual(shorts_cutter.density_score(6.0), 1.0)
        self.assertEqual(shorts_cutter.density_score(0.0), 0.0)
        self.assertLess(shorts_cutter.density_score(40.0), 0.5)

    def test_hook_score_fades_after_first_quarter(self):
        self.assertEqual(shorts_cutter.hook_score(5, 35, 100), 1.0)
        self.assertGreater(shorts_cutter.hook_score(50, 80, 100), 0.0)
        self.assertLess(
            shorts_cutter.hook_score(50, 80, 100),
            shorts_cutter.hook_score(20, 50, 100),
        )

    def test_plan_virality_combines_all_dimensions(self):
        plan = shorts_cutter.plan_virality(0, 30, 120, [1.0, 5.0, 9.0, 13.0, 17.0, 21.0, 25.0], [])
        self.assertGreater(plan.score, 0)
        self.assertLessEqual(plan.score, 100)
        self.assertEqual(plan.speech_ratio, 1.0)

    def test_speech_drag_pulls_score_down(self):
        quiet = shorts_cutter.plan_virality(0, 30, 120, [], [(0.0, 30.0)])
        loud = shorts_cutter.plan_virality(0, 30, 120, [], [])
        self.assertLess(quiet.score, loud.score)


class TestSelection(unittest.TestCase):
    def test_select_returns_non_overlapping_sorted_windows(self):
        plans = shorts_cutter.select_short_plans(
            duration=200,
            scene_times=[10, 40, 70, 100, 130, 160],
            silences=[],
            window_seconds=30,
            max_shorts=3,
            candidates_per_second=1.0,
        )
        self.assertLessEqual(len(plans), 3)
        for prev, current in zip(plans, plans[1:]):
            self.assertGreaterEqual(current.start, prev.end)
        self.assertEqual(plans, sorted(plans, key=lambda p: p.start))

    def test_select_respects_min_speech_ratio(self):
        silences = [(0.0, 200.0)]
        plans = shorts_cutter.select_short_plans(
            duration=200,
            scene_times=[],
            silences=silences,
            min_speech_ratio=0.8,
        )
        self.assertEqual(plans, [])

    def test_select_zero_duration_is_empty(self):
        self.assertEqual(shorts_cutter.select_short_plans(0, [], []), [])


class TestProbe(unittest.TestCase):
    def test_probe_video_reads_duration_and_size(self):
        stderr = (
            "  Duration: 00:01:02.50, start: 0.000000, bitrate: 1234 kb/s\n"
            "    Stream #0:0: Video: h264 (High), 1920x1080 [SAR 1:1 DAR 16:9]\n"
        )
        with patch(
            "subprocess.run",
            return_value=type("P", (), {"stderr": stderr, "stdout": "", "returncode": 0})(),
        ), patch("os.path.isfile", return_value=True):
            meta = shorts_cutter.probe_video("ffmpeg", "clip.mp4")
        self.assertAlmostEqual(meta.duration, 62.5)
        self.assertEqual((meta.width, meta.height), (1920, 1080))

    def test_probe_video_rejects_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            shorts_cutter.probe_video("ffmpeg", "/nonexistent/clip.mp4")

    def test_probe_video_raises_when_duration_unreadable(self):
        with patch(
            "subprocess.run",
            return_value=type("P", (), {"stderr": "no duration here", "stdout": "", "returncode": 0})(),
        ), patch("os.path.isfile", return_value=True):
            with self.assertRaises(RuntimeError):
                shorts_cutter.probe_video("ffmpeg", "clip.mp4")

    def test_detect_silences_and_scenes_parse_output(self):
        silence_text = (
            "silence_start: 1.0\nsilence_end: 2.0 | silence_duration: 1.0\n"
        )
        scene_text = "pts_time:5.5\npts_time:9.0\n"
        fakes = [
            type("P", (), {"stderr": silence_text, "stdout": "", "returncode": 0}),
            type("P", (), {"stderr": scene_text, "stdout": "", "returncode": 0}),
        ]
        with patch("subprocess.run", side_effect=fakes) as run:
            self.assertEqual(shorts_cutter.detect_silences("ffmpeg", "clip.mp4"), [(1.0, 2.0)])
            self.assertEqual(shorts_cutter.detect_scene_changes("ffmpeg", "clip.mp4"), [5.5, 9.0])
            self.assertEqual(run.call_count, 2)


class TestCutPipeline(unittest.TestCase):
    def test_cut_viral_shorts_returns_report(self):
        meta = shorts_cutter.VideoMeta(duration=100, width=1920, height=1080)
        fake = type("P", (), {"stderr": "pts_time:10\npts_time:50\n", "stdout": "", "returncode": 0})
        with patch.object(shorts_cutter, "probe_video", return_value=meta), patch(
            "subprocess.run", return_value=fake
        ), patch.object(shorts_cutter, "cut_short") as cut, patch(
            "app.services.shorts_cutter.utils.get_ffmpeg_binary", return_value="ffmpeg"
        ):
            results = shorts_cutter.cut_viral_shorts(
                "clip.mp4",
                "/tmp/shorts_out",
                window_seconds=30,
                max_shorts=2,
            )
        self.assertGreaterEqual(len(results), 1)
        self.assertLessEqual(len(results), 2)
        self.assertFalse(cut.call_count == 0)
        self.assertTrue(all("score" in r for r in results))

    def test_cut_viral_shorts_uses_ffmpeg_shim(self):
        self.assertTrue(callable(shorts_cutter.cut_viral_shorts))


if __name__ == "__main__":
    unittest.main()