"""字幕样式必须在两条渲染路径上表现一致（回归测试）。

视频渲染有两条字幕路径：

* 逐词高亮（``_highlighted_caption_clips``），词级时间齐备且 ``caption_word_highlight``
  打开时走这条；
* 整句字幕（``SubtitlesClip`` + ``make_textclip``），其余情况走这条。

真正决定整句字幕长相的只有 ``_build_subtitle_clip`` 一处：``SubtitlesClip`` 在本
项目里只被当作 SRT 解析器（``sub.subtitles``），它的 ``make_frame`` 从不参与合成，
``make_textclip`` 闭包同理——唯一一次调用是 moviepy 构造期算 ``hasmask``，只取
``.mask``。因此样式与位置必须钉在"真正上画面的那条路径"上。

位置规则曾经只写了一份，关掉逐词高亮后字幕就跳回 libass 的底部默认位置：字幕照常
出现，只是样子不对，而界面上那个控件明明是打开的。这种症状比"设置彻底不生效"更
难查，所以先把它钉住。
"""

import ast
import unittest

import numpy as np
from pathlib import Path

VIDEO_PY = Path(__file__).parent.parent.parent / "app" / "services" / "video.py"
SOURCE = VIDEO_PY.read_text(encoding="utf-8")


def _function_body(name: str, source: str = SOURCE) -> str:
    """取某个函数的源码文本（含装饰器），供结构性断言使用。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"没有找到函数 {name}")


class TestSubtitleStylingIsConsistentAcrossPaths(unittest.TestCase):
    def test_built_clip_actually_paints_the_chosen_colour_and_stroke(self):
        """
        整句字幕真正上画面的那条路径必须把颜色与描边画进像素里。

        早先这条路径只传字体与字号，颜色与描边被整个丢弃，界面上的控件却亮着。
        这条断言刻意查**像素**而不是查源码：源码断言只能证明"代码里写了某个
        关键字"，而这里的 bug 形态恰恰是"样式写在了从不被渲染的那条分支上"——
        关键字都在，画面还是错的。``make_textclip`` 就是这样一个从不参与合成的闭包，
        给它加颜色不会让任何一个像素改变。
        """
        from app.models.schema import VideoParams
        from app.services.subtitle_preview import resolve_subtitle_font_path
        from app.services.video import _build_subtitle_clip

        params = VideoParams(
            video_subject="space",
            subtitle_enabled=True,
            # 用饱和的纯色，容不了一点灰度混合造成的歧义
            text_fore_color="#FF0000",
            stroke_color="#0000FF",
            stroke_width=8,
            font_size=64,
        )
        clip = _build_subtitle_clip(
            phrase="Colour must reach the pixels",
            params=params,
            font_path=resolve_subtitle_font_path(params),
            video_width=1080,
            bg_color=None,
        )
        frame = np.asarray(clip.get_frame(0))[:, :, :3].astype(int)

        red = (frame[:, :, 0] > 180) & (frame[:, :, 1] < 80) & (frame[:, :, 2] < 80)
        blue = (frame[:, :, 2] > 180) & (frame[:, :, 0] < 80) & (frame[:, :, 1] < 80)
        self.assertGreater(
            red.sum(), 50, "整句字幕没有画出所选的前景色；颜色被丢弃了"
        )
        self.assertGreater(
            blue.sum(), 50, "整句字幕没有画出所选的描边色；描边被丢弃了"
        )

    def test_both_paths_use_the_shared_position_rule(self):
        """
        两条路径必须共用同一个摆放函数。

        位置规则各写一份时，``subtitle_position`` 会在关掉高亮之后失效，而字幕看起来
        完全正常，因此极难与"设置没生效"区分开。
        """
        self.assertIn(
            "def _position_subtitle_clip",
            SOURCE,
            "缺少共用的字幕摆放函数；两条路径各写一份位置逻辑必然再次分叉",
        )
        body = _function_body("generate_video")
        self.assertIn(
            "_position_subtitle_clip",
            body,
            "generate_video 没有调用共用的摆放函数",
        )
        # 分叉的迹象：generate_video 里不应再出现裸的 with_position 位置分支
        self.assertNotIn(
            'params.subtitle_position == "bottom"',
            body,
            "generate_video 里仍有内联的位置判断，说明两条路径没有真正共用规则",
        )

    def test_position_helper_covers_every_documented_option(self):
        """bottom / top / custom / 其余（居中）四种都必须有明确行为。"""
        helper = _function_body("_position_subtitle_clip")
        for option in ("bottom", "top", "custom"):
            self.assertIn(
                f'params.subtitle_position == "{option}"',
                helper,
                f"摆放函数没有处理 subtitle_position={option}",
            )
        self.assertIn(
            '("center", "center")',
            helper,
            "摆放函数没有处理其余取值（居中）",
        )

    def test_plain_path_is_positioned_not_left_to_libass(self):
        """整句字幕的兜底分支必须显式摆放，不能交给 libass 决定。"""
        body = _function_body("generate_video")
        marker = "if highlighted:"
        self.assertIn(marker, body)
        fallback = body[body.index(marker) :]
        # 取到 bgm 段落为止，那一段就是两条路径的分叉
        fallback = fallback[: fallback.index("bgm_enabled")]
        self.assertIn(
            "_position_subtitle_clip",
            fallback,
            "整句字幕的兜底分支没有摆放字幕，位置会退回 libass 默认值",
        )


if __name__ == "__main__":
    unittest.main()
