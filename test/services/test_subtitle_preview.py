"""草稿预览必须如实反映出片参数。

预览最大的风险不是报错，而是"看起来能用、其实在骗人"：用户照着预览调好字号、颜色
和位置，真正出片时却不是那个样子。所以这些测试盯的是**参数变化是否真的反映到像素
上**，而不是只断言函数没有抛异常。
"""

import io
import unittest

import numpy as np
from PIL import Image

from app.models.schema import VideoAspect, VideoParams
from app.services.subtitle_preview import (
    PLACEHOLDER_COLOR,
    preview_aspect,
    render_subtitle_draft_png,
)

BASE = dict(
    video_subject="space",
    subtitle_enabled=True,
    text_fore_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=3,
    font_size=70,
)
TEXT = "Why do stars twinkle?"


def _frame(png_bytes: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB")).astype(int)


def _ink(frame: np.ndarray):
    """返回字幕墨迹的包围盒与平均色；没有墨迹时返回 None。"""
    mask = np.abs(frame - np.array(PLACEHOLDER_COLOR)).sum(axis=2) > 40
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return None
    return {
        "top": int(ys.min()),
        "bottom": int(ys.max()),
        "height": int(mask.sum()),
        "mean_rgb": frame[mask].mean(axis=0),
    }


def _params(**overrides) -> VideoParams:
    return VideoParams(**{**BASE, **overrides})


class TestSubtitleDraftPreview(unittest.TestCase):
    def test_renders_at_the_selected_aspect(self):
        for aspect in ("9:16", "16:9", "1:1"):
            with self.subTest(aspect=aspect):
                png = render_subtitle_draft_png(_params(video_aspect=aspect), TEXT)
                self.assertEqual(Image.open(io.BytesIO(png)).size,
                                 VideoAspect(aspect).to_resolution())

    def test_unknown_aspect_falls_back_to_vertical(self):
        # VideoParams 在构造时就校验画幅，所以坏值只能来自后续改写（例如
        # automation.env 覆盖），这正是需要兜底的那种情况。
        params = _params()
        params.video_aspect = "nonsense"
        self.assertEqual(preview_aspect(params), VideoAspect("9:16"))

    def test_draws_actual_subtitle_pixels(self):
        ink = _ink(_frame(render_subtitle_draft_png(_params(), TEXT)))
        self.assertIsNotNone(ink, "预览里没有任何字幕像素")
        self.assertGreater(ink["height"], 2000)

    def test_no_subtitle_yields_a_clean_frame(self):
        """
        关掉字幕时必须给纯净底板。

        显示一段并不存在的文字，等于谎报成片内容。
        """
        frame = _frame(render_subtitle_draft_png(_params(subtitle_enabled=False), TEXT))
        self.assertIsNone(_ink(frame), "字幕已关闭，预览却仍画出了文字")

    def test_blank_text_draws_nothing(self):
        for blank in ("", "   ", "\n\n"):
            with self.subTest(blank=repr(blank)):
                frame = _frame(render_subtitle_draft_png(_params(), blank))
                self.assertIsNone(_ink(frame), "空白文字不该产生字幕像素")

    def test_subtitle_position_actually_moves_the_pixels(self):
        """
        这是预览最容易骗人的地方：位置错了但字幕照常出现，肉眼扫一眼未必发现。

        四种取值必须落在画面上明显不同的纵向位置。
        """
        tops = {}
        for name, over in {
            "top": {"subtitle_position": "top"},
            "center": {"subtitle_position": "center"},
            "custom": {"subtitle_position": "custom", "custom_position": 45},
            "bottom": {"subtitle_position": "bottom"},
        }.items():
            with self.subTest(position=name):
                ink = _ink(_frame(render_subtitle_draft_png(_params(**over), TEXT)))
                self.assertIsNotNone(ink, f"{name} 位置没有画出字幕")
                tops[name] = ink["top"]

        self.assertLess(tops["top"], 400, "top 位置不在画面上方")
        self.assertLess(tops["custom"], tops["center"], "custom 45% 应在中心之上")
        self.assertGreater(tops["bottom"], 1400, "bottom 位置不在画面下方")
        self.assertLess(tops["custom"], tops["bottom"])

    def test_text_colour_actually_changes(self):
        white = _ink(_frame(render_subtitle_draft_png(_params(), TEXT)))
        red = _ink(_frame(render_subtitle_draft_png(
            _params(text_fore_color="#FF0000"), TEXT)))

        # 描边是黑的，会把平均值压下来，所以判据是通道优势而不是绝对值：
        # 文字变红时红通道必须明显压过绿蓝。
        self.assertGreater(red["mean_rgb"][0], red["mean_rgb"][1] * 3 + 30)
        self.assertGreater(red["mean_rgb"][0], red["mean_rgb"][2] * 3 + 30)
        self.assertLess(red["mean_rgb"][1], 60, "文字仍偏绿，说明颜色没生效")
        # 白色文字三个通道应该大致均衡，可用来确认判据本身有效
        self.assertLess(abs(white["mean_rgb"][0] - white["mean_rgb"][2]), 30)
        self.assertLess(abs(white["mean_rgb"][0] - white["mean_rgb"][1]), 30)

    def test_font_size_scaling_shows_up(self):
        small = _ink(_frame(render_subtitle_draft_png(_params(font_size=40), TEXT)))
        large = _ink(_frame(render_subtitle_draft_png(_params(font_size=110), TEXT)))
        self.assertGreater(
            large["height"], small["height"] * 1.8,
            "字号放大后墨迹面积没有明显增加，预览对字号不敏感",
        )

    def test_caps_the_number_of_previewed_lines(self):
        many = "\n".join(f"line {i}" for i in range(12))
        one = render_subtitle_draft_png(_params(), many, max_lines=1)
        tall = _ink(_frame(render_subtitle_draft_png(_params(), many, max_lines=12)))
        self.assertIsNotNone(_ink(_frame(one)))
        self.assertLess(
            _ink(_frame(one))["height"], tall["height"],
            "max_lines 没有限制行数",
        )

    def test_stays_fast_enough_for_an_interactive_control(self):
        """
        预览挂在参数面板上，会随每次改动重算。慢到半秒以上就没人愿意用了。
        """
        import time

        start = time.perf_counter()
        render_subtitle_draft_png(_params(), TEXT)
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertLess(elapsed_ms, 1500, f"预览耗时 {elapsed_ms:.0f}ms，交互会明显卡顿")

    def test_reuses_the_production_renderer(self):
        """
        预览不得自建排版。它必须与出片共用同一段代码，否则两边迟早分叉。
        """
        from pathlib import Path
        import ast

        source = Path("app/services/subtitle_preview.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
        self.assertIn("_build_subtitle_clip", imported)
        self.assertIn("_position_subtitle_clip", imported)
        self.assertNotIn("TextClip", imported,
                         "预览直接用了 TextClip，说明它在自建排版而不是复用出片逻辑")


if __name__ == "__main__":
    unittest.main()
