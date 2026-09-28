"""把字幕画成图片：正在念的词高亮，其余词压暗。

为什么用 Pillow 画而不是 MoviePy 的 TextClip：一次高亮需要**两段颜色**
（亮色 + 暗色）拼在同一行里，TextClip 只接受单一颜色。要么叠两个
TextClip 再对齐位置（很脆），要么退回 PIL 自己排版——后者可控得多，
而且 MoviePy 只需要接收一张现成的图。

画面尺寸由调用方给出，字体也从调用方传入：这里只负责排版与配色，
不猜字体、不猜字号。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.caption_highlight import CaptionCue, build_highlight_timeline

__all__ = [
    "CaptionStyle",
    "DEFAULT_STYLE",
    "render_caption_frame",
    "caption_images_for_cue",
]

#: 高亮色用偏暖的亮黄，暗色用接近白但明显压暗的灰——对比要够，
#: 但暗色也不能黑到看不清，否则整句失去"已经念过"的位置感。
DEFAULT_HIGHLIGHT = (255, 214, 64)
DEFAULT_DIM = (150, 150, 150)


@dataclass(frozen=True)
class CaptionStyle:
    highlight: tuple[int, int, int] = DEFAULT_HIGHLIGHT
    dim: tuple[int, int, int] = DEFAULT_DIM
    stroke: tuple[int, int, int] = (0, 0, 0)
    stroke_width: int = 0
    background: tuple[int, int, int] | None = None
    padding_ratio: float = 0.35
    line_gap_ratio: float = 0.28
    max_lines: int = 2
    margin_x_ratio: float = 0.06
    margin_bottom_ratio: float = 0.16


#: 默认样式：亮黄高亮 + 灰暗已读 + 描边保证任何底色上都看得清。
DEFAULT_STYLE = CaptionStyle(stroke_width=3)


def _layout(draw, words, font, max_width, style: CaptionStyle):
    """
    把词流折行。返回 ``[[(word, x_offset, y_offset), ...], ...]``。

    折行必须按**实际像素宽度**算，不能按字符数猜——中英文混排时
    字符数完全不可靠。
    """
    lines: list[list[tuple[str, int]]] = [[]]
    line_width = 0
    space_w = draw.textlength(" ", font=font)

    for word in words:
        word_w = draw.textlength(word, font=font)
        if lines[-1] and line_width + space_w + word_w > max_width:
            lines.append([])
            line_width = 0
        if lines[-1]:
            line_width += int(space_w)
        lines[-1].append((word, line_width))
        line_width += int(word_w)

    return lines[: style.max_lines]


def render_caption_frame(
    words,
    active_index: int,
    size: tuple[int, int],
    font_path: str | Path,
    font_size: int,
    style: CaptionStyle = DEFAULT_STYLE,
):
    """
    画一帧字幕图并返回 PIL Image。

    只有**正在念的那个词**（下标 ``active_index``）用高亮色，其余全部压暗。

    之前写成"从 active_index 起（含）往后都高亮"，方向是反的：
    于是还没念到的词是亮的、已经念完的反而暗着。看起来像字幕坏了。
    """
    from PIL import Image, ImageDraw, ImageFont

    width, height = size
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not words:
        return canvas

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except OSError:
        font = ImageFont.load_default()

    max_width = int(width * (1 - 2 * style.margin_x_ratio))
    lines = _layout(draw, words, font, max_width, style)
    if not lines or not lines[0]:
        return canvas

    gap = int(font_size * style.line_gap_ratio)
    heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line[0][0], font=font)
        heights.append(bbox[3] - bbox[1])
    line_height = max(heights) if heights else font_size
    block_height = line_height * len(lines) + gap * (len(lines) - 1)

    x0 = int(width * style.margin_x_ratio)
    y = int(height * (1 - style.margin_bottom_ratio)) - block_height

    if style.background:
        pad_x = int(font_size * style.padding_ratio)
        pad_y = int(font_size * 0.25)
        widest = max(
            int(draw.textlength(" ".join(w for w, _ in line), font=font))
            for line in lines
            if line
        )
        draw.rounded_rectangle(
            [
                x0 - pad_x,
                y - pad_y,
                x0 + widest + pad_x,
                y + block_height + pad_y,
            ],
            radius=int(font_size * 0.35),
            fill=style.background,
        )

    word_index = 0
    for row, line in enumerate(lines):
        for text, offset in line:
            colour = style.highlight if word_index == active_index else style.dim
            bbox = draw.textbbox((0, 0), text, font=font)
            position = (x0 + offset, y - bbox[1])
            if style.stroke_width:
                draw.text(
                    position,
                    text,
                    font=font,
                    fill=style.stroke,
                    stroke_width=style.stroke_width,
                )
            draw.text(position, text, font=font, fill=colour)
            word_index += 1
        y += line_height + gap

    return canvas


def caption_images_for_cue(
    cue: CaptionCue,
    size: tuple[int, int],
    font_path: str | Path,
    font_size: int,
    style: CaptionStyle = DEFAULT_STYLE,
) -> list[tuple[float, float, int]]:
    """
    返回 ``(start, end, active_index)`` 序列，供调用方逐段贴图。

    没有词级时间时返回 ``[(cue.start, cue.end, -1)]``，表示"整句原样显示"——
    调用方不需要为"没有高亮"写另一条代码路径。
    """
    if not cue.can_highlight:
        return [(cue.start, cue.end, -1)]
    return build_highlight_timeline(cue)
