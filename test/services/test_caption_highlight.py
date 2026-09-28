"""逐词高亮字幕：时间轴必须和语音对齐，对不齐就不要做。

最省事也最糟的实现是"在整句里按字数均分词"——高亮会和语音明显错位，
观众看到的是"还没念到就亮了"，比不做高亮更劝退。所以这里坚持：
词级时间必须来自真实 ASR 输出，拿不到就整段退回原样。
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.services.caption_highlight import (
    CaptionCue,
    WordTiming,
    build_highlight_timeline,
    read_word_timings,
    word_timing_path,
    write_word_timings,
)


def _cue(words, start=0.0, end=4.0):
    return CaptionCue(
        start=start, end=end, text=" ".join(w.text for w in words), words=tuple(words)
    )


class TestWordTimingRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.srt = Path(self._tmp.name) / "subtitle.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:00:04,000\nhello world\n", encoding="utf-8")

    def test_sidecar_sits_next_to_the_srt(self):
        path = word_timing_path(self.srt)
        self.assertEqual(path.name, "subtitle.words.json")
        self.assertEqual(path.parent, self.srt.parent)

    def test_timings_survive_a_round_trip(self):
        words = [WordTiming(0.0, 0.5, "hello"), WordTiming(0.6, 1.2, "world")]
        write_word_timings(self.srt, words)
        loaded = read_word_timings(self.srt)
        self.assertEqual([w.text for w in loaded], ["hello", "world"])
        self.assertAlmostEqual(loaded[1].end, 1.2, places=3)

    def test_missing_sidecar_is_not_an_error(self):
        """没有词级时间时必须安静退回原样渲染。"""
        self.assertEqual(read_word_timings(self.srt), [])

    def test_corrupt_sidecar_is_not_an_error(self):
        word_timing_path(self.srt).write_text("{not json", encoding="utf-8")
        self.assertEqual(read_word_timings(self.srt), [])

    def test_entries_with_impossible_ranges_are_dropped(self):
        """end < start 的条目会让高亮倒着走，必须丢掉。"""
        word_timing_path(self.srt).write_text(
            json.dumps(
                {
                    "version": 1,
                    "words": [
                        {"start": 2.0, "end": 1.0, "text": "bad"},
                        {"start": 0.0, "end": 0.5, "text": "good"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        loaded = read_word_timings(self.srt)
        self.assertEqual([w.text for w in loaded], ["good"])

    def test_entries_missing_fields_are_skipped(self):
        word_timing_path(self.srt).write_text(
            json.dumps({"version": 1, "words": [{"start": 0.0}, {"end": 1.0, "text": "x"}]}),
            encoding="utf-8",
        )
        self.assertEqual(read_word_timings(self.srt), [])

    def test_words_are_sorted_by_start(self):
        """乱序会让高亮先跳到后面的词。"""
        word_timing_path(self.srt).write_text(
            json.dumps(
                {
                    "version": 1,
                    "words": [
                        {"start": 2.0, "end": 2.5, "text": "later"},
                        {"start": 0.0, "end": 0.5, "text": "earlier"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            [w.text for w in read_word_timings(self.srt)], ["earlier", "later"]
        )


class TestHighlightTimeline(unittest.TestCase):
    def test_each_word_gets_its_own_real_window(self):
        """
        关键性质：高亮窗口必须等于词的真实时长。

        均分整句是最常见的错误实现——高亮会和语音错位。
        """
        words = [
            WordTiming(0.0, 0.30, "one"),
            WordTiming(0.50, 1.40, "two"),
            WordTiming(1.40, 1.60, "three"),
        ]
        timeline = build_highlight_timeline(_cue(words))
        self.assertEqual(
            timeline, [(0.0, 0.30, 0), (0.50, 1.40, 1), (1.40, 1.60, 2)]
        )

    def test_gaps_between_words_are_not_highlighted(self):
        """0.30→0.50 之间的停顿不该有任何词亮着。"""
        words = [WordTiming(0.0, 0.30, "a"), WordTiming(0.50, 0.90, "b")]
        timeline = build_highlight_timeline(_cue(words))
        for start, end, _ in timeline:
            self.assertFalse(start > 0.30 and end < 0.50, "停顿区间被错误地分配了")

    def test_word_count_must_match_the_active_index(self):
        words = [WordTiming(i * 0.3, i * 0.3 + 0.25, f"w{i}") for i in range(5)]
        timeline = build_highlight_timeline(_cue(words))
        self.assertEqual([idx for _, _, idx in timeline], [0, 1, 2, 3, 4])

    def test_single_word_cue_cannot_highlight(self):
        """只有一个词时没有"高亮"的对比意义，整句原样显示。"""
        words = [WordTiming(0.0, 1.0, "solo")]
        self.assertEqual(build_highlight_timeline(_cue(words)), [])

    def test_cue_without_words_falls_back_to_plain(self):
        self.assertEqual(build_highlight_timeline(_cue([])), [])

    def test_words_are_clamped_to_the_cue_bounds(self):
        """词时间超出字幕范围时要夹紧，否则会盖住上一句/下一句。"""
        words = [WordTiming(-1.0, 0.5, "early"), WordTiming(0.5, 99.0, "late")]
        timeline = build_highlight_timeline(_cue(words, start=0.0, end=2.0))
        for start, end, _ in timeline:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(end, 2.0)

    def test_very_short_words_do_not_produce_a_flash(self):
        """
        20ms 的词会闪一下，看着像故障。应当并入前一个窗口。
        """
        words = [WordTiming(0.0, 0.50, "long"), WordTiming(0.50, 0.52, "blip")]
        timeline = build_highlight_timeline(_cue(words))
        self.assertEqual(len(timeline), 1, "过短的词产生了多余的高亮窗口")
        self.assertEqual(timeline[0], (0.0, 0.52, 0))

    def test_timeline_never_goes_backwards(self):
        """
        窗口按时间单调前进。

        注意：窗口之间**允许有空隙**（词与词之间的停顿本就不该点亮），
        所以不能断言"下一个 start ≤ 上一个 end"。真正要保证的是
        start 与 end 都不倒退——否则渲染出来会闪一下再退回。
        """
        words = [WordTiming(i * 0.2, i * 0.2 + 0.18, f"w{i}") for i in range(8)]
        timeline = build_highlight_timeline(_cue(words, start=0.0, end=2.0))
        for (_, prev_end, prev_idx), (next_start, next_end, next_idx) in zip(
            timeline, timeline[1:]
        ):
            with self.subTest(prev=prev_idx, nxt=next_idx):
                self.assertGreaterEqual(next_start, prev_end - 1e-6)
                self.assertGreaterEqual(next_end, prev_end)
                self.assertEqual(next_idx, prev_idx + 1, "高亮下标必须逐一推进")


if __name__ == "__main__":
    unittest.main()


class TestCaptionRendering(unittest.TestCase):
    """真正画出高亮帧，并检查"亮的那部分确实跟着词走"。"""

    def setUp(self):
        self.font = str(
            Path(__file__).resolve().parents[2]
            / "resource" / "fonts" / "Charm-Regular.ttf"
        )
        if not Path(self.font).is_file():
            self.skipTest("bundled font missing")

    def _words(self):
        return ["Why", "a", "day", "on", "Venus", "is", "longer"]

    def _render(self, active):

        from app.services.caption_render import render_caption_frame

        img = render_caption_frame(
            self._words(), active, (1280, 720), self.font, 64
        )
        return img.convert("RGB")

    def _highlight_pixels(self, image):
        """数一下"高亮黄"像素：亮色 RGB 大致是 (255,214,64)。"""
        total = 0
        for r, g, b in image.getdata():
            if r > 200 and 170 < g < 245 and b < 120:
                total += 1
        return total

    def test_frame_actually_draws_text(self):
        assert self._highlight_pixels(self._render(len(self._words()) - 1)) > 500

    def _highlight_centroid_x(self, image):
        """高亮像素的水平重心，用来确认"亮的那块"确实在向右移动。"""
        total = weight = 0
        width = image.size[0]
        px = image.load()
        for y in range(image.size[1]):
            for x in range(width):
                r, g, b = px[x, y][:3]
                if r > 200 and 170 < g < 245 and b < 120:
                    weight += 1
                    total += x
        return (total / weight) if weight else -1.0

    def test_highlight_moves_forward_with_the_word(self):
        """
        功能的核心：高亮必须跟着词往前走。

        早期版本把方向写反了——"下标 >= active" 才高亮，于是还没念到的词
        亮着、念完的暗着。只看"有没有高亮像素"是发现不了的，必须检查
        高亮区域的位置随 active_index 单调右移。
        """
        centroids = [
            self._highlight_centroid_x(self._render(i))
            for i in range(len(self._words()))
        ]
        self.assertTrue(all(c > 0 for c in centroids), f"有帧没有高亮：{centroids}")
        for prev, nxt in zip(centroids, centroids[1:]):
            self.assertGreater(
                nxt, prev, f"高亮没有向右移动：{centroids}"
            )

    def _text_pixels(self, image):
        """所有"有颜色的文字"像素：高亮 + 暗色。"""
        total = 0
        for r, g, b in image.getdata():
            is_highlight = r > 200 and 170 < g < 245 and b < 120
            is_dim = 120 < r < 200 and 120 < g < 200 and 120 < b < 200
            if is_highlight or is_dim:
                total += 1
        return total

    def test_only_the_active_word_is_highlighted(self):
        """
        逐词高亮：同一时刻只有一个词是亮的，其余压暗。

        不能用"高亮像素数差不多"来判定——词长本来就不同
        （"a" 和 "Venus" 差好几倍）。真正要保证的是：
        高亮部分只占整句文字的一小部分。
        """
        for active in range(len(self._words())):
            image = self._render(active)
            highlight = self._highlight_pixels(image)
            text = self._text_pixels(image)
            with self.subTest(active=active):
                self.assertGreater(text, 0, "整句应当都画出文字")
                self.assertLess(
                    highlight,
                    text * 0.4,
                    f"第 {active} 个词时高亮占比过大，说明整句都亮了："
                    f"{highlight}/{text}",
                )

    def test_nothing_is_highlighted_before_the_cue_starts(self):
        from app.services.caption_render import render_caption_frame

        img = render_caption_frame([], 0, (1280, 720), self.font, 64).convert("RGB")
        self.assertEqual(self._highlight_pixels(img), 0)

    def test_long_caption_wraps_into_multiple_lines(self):

        from app.services.caption_render import render_caption_frame

        words = ["a" * 12 for _ in range(20)]
        img = render_caption_frame(words, 20, (1280, 720), self.font, 64)
        bbox = img.convert("RGB").getbbox()
        self.assertIsNotNone(bbox, "长字幕应当被画出来")
        self.assertLessEqual(bbox[2] - bbox[0], 1280, "文字不能超出画面宽度")


class TestBurnPathFallsBackSafely(unittest.TestCase):
    """
    逐词高亮是增强，任何一步不满足都必须退回原样字幕，而不是让出片失败。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.srt = Path(self._tmp.name) / "subtitle.srt"

    def _clips(self, **kwargs):
        from app.services.video import _highlighted_caption_clips

        params = {
            "subtitle_path": str(self.srt),
            "size": (1280, 720),
            "font_path": str(
                Path(__file__).resolve().parents[2]
                / "resource" / "fonts" / "Charm-Regular.ttf"
            ),
            "font_size": 48,
            "background": None,
            "enabled": True,
        }
        params.update(kwargs)
        return _highlighted_caption_clips(**params)

    def test_disabled_produces_nothing(self):
        self.srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello world\n", encoding="utf-8")
        self.assertEqual(self._clips(enabled=False), [])

    def test_missing_subtitle_produces_nothing(self):
        self.assertEqual(self._clips(), [])

    def test_no_word_timings_produces_nothing(self):
        """没有词级时间就必须整句原样显示，绝不按字数均分去猜。"""
        self.srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello world\n", encoding="utf-8")
        self.assertEqual(self._clips(), [])

    def test_one_clip_per_word_when_timings_exist(self):
        self.srt.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nWhy a day on Venus is longer\n",
            encoding="utf-8",
        )
        write_word_timings(
            self.srt,
            [
                WordTiming(0.0, 0.3, "Why"),
                WordTiming(0.3, 0.6, "a"),
                WordTiming(0.6, 0.9, "day"),
                WordTiming(0.9, 1.4, "on"),
                WordTiming(1.4, 1.8, "Venus"),
            ],
        )
        clips = self._clips()
        self.assertGreaterEqual(len(clips), 4, "应当每个词生成一个片段")

    def test_a_clip_never_starts_before_zero(self):
        """负起点会让 MoviePy 抛错，整条视频就没了。"""
        self.srt.write_text(
            "1\n00:00:00,500 --> 00:00:02,500\nhello world\n", encoding="utf-8"
        )
        write_word_timings(
            self.srt,
            [WordTiming(0.0, 0.6, "hello"), WordTiming(0.6, 2.0, "world")],
        )
        for clip in self._clips():
            self.assertGreaterEqual(float(clip.start), 0.0)
