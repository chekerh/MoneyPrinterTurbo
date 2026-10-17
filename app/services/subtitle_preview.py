"""草稿预览：用与出片完全相同的排版，画出 9:16 画面里字幕的真实样子。

这个模块刻意不自己排版。字幕片段一律交给
:func:`app.services.video._build_subtitle_clip`，位置交给
:func:`app.services.video._position_subtitle_clip`——就是出片时用的那两段代码。

原因很实际：预览最常见的失败方式不是报错，而是**看起来能用但其实在骗你**。
如果这里另写一份排版，字号、颜色、描边、位置迟早会和出片路径分叉；用户照着预览
调好参数，等到真正出片才发现样子不一样，那这个预览反而比没有更糟。
曾经就分叉过一次——颜色和描边只在逐词高亮打开时生效，预览做出来才发现。

因此这里只做三件事：

1. 按所选画幅算出分辨率；
2. 铺一个中性底板（真实素材此时还没有，编不如编）；
3. 把预览文字交给真实渲染器，输出 PNG 字节。

底板是刻意中性的深灰，不做拟真包装。真实素材还没生成，任何"预览画面"都是虚构
的；一个诚实的空白底板明确告诉你"这里将来是素材，字幕长这样"，比一张编出来的
风景照更不容易误导。
"""

from __future__ import annotations

import io
import os

import numpy as np
from PIL import Image
from moviepy import CompositeVideoClip, ImageClip

from app.models.schema import VideoAspect, VideoParams
from app.services.video import (
    _build_subtitle_clip,
    _position_subtitle_clip,
)

# 预览底板颜色：中性深灰，明确是"占位"而不是"素材"。
PLACEHOLDER_COLOR = (24, 24, 27)


def preview_aspect(params: VideoParams) -> VideoAspect:
    """预览画幅，未配置时回落到竖屏（短视频的默认形态）。"""
    try:
        return VideoAspect(params.video_aspect)
    except (ValueError, TypeError):
        return VideoAspect("9:16")


def resolve_subtitle_font_path(params: VideoParams) -> str:
    """按出片时相同的方式解析字体文件。

    字体不同，度量结果就不同；预览必须用同一个字体，否则换行位置会骗人。
    """
    if not params.font_name:
        params.font_name = "STHeitiMedium.ttc"
    from app.utils import utils

    font_path = os.path.join(utils.font_dir(), params.font_name)
    if os.name == "nt":
        font_path = font_path.replace("\\", "/")
    return font_path


def _resolve_background_color(params: VideoParams):
    """归一化字幕底色，兼容历史布尔值。"""
    if isinstance(params.text_background_color, bool):
        return "#000000" if params.text_background_color else None
    return params.text_background_color


def render_subtitle_draft_png(
    params: VideoParams,
    text: str,
    max_lines: int = 3,
) -> bytes:
    """渲染一张 9:16 草稿预览，返回 PNG 字节。

    ``text`` 会被裁到 ``max_lines`` 行。字幕在成片里是逐条出现的，预览把最前面几行
    叠在一张图上，方便一眼看出字号、颜色、描边与位置是否合适。

    字幕关闭时返回的仍是纯净底板——预览要如实反映"这条片子不会有字幕"，而不是
    默默显示一段并不存在的文字。
    """
    aspect = preview_aspect(params)
    width, height = aspect.to_resolution()

    # 直接铺底色即可：预览只需要一张静帧，没必要真的走一遍 ColorClip。
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = PLACEHOLDER_COLOR

    if params.subtitle_enabled and str(text or "").strip():
        lines = [line for line in str(text).splitlines() if line.strip()][:max_lines]
        phrase = "\n".join(lines)
        if phrase.strip():
            frame = _draw_subtitle(frame, params, phrase)

    image = Image.fromarray(frame)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_subtitle(frame: np.ndarray, params: VideoParams, phrase: str) -> np.ndarray:
    """把一段字幕按出片时的方式画到画面上，返回新帧。"""
    width, height = frame.shape[1], frame.shape[0]
    font_path = resolve_subtitle_font_path(params)
    subtitle = _build_subtitle_clip(
        phrase=phrase,
        params=params,
        font_path=font_path,
        video_width=width,
        bg_color=_resolve_background_color(params),
    )
    # 位置规则与出片共用同一个函数；这里给一个固定时长即可，画面是静帧。
    subtitle = subtitle.with_duration(1.0)
    subtitle = _position_subtitle_clip(subtitle, params=params, video_height=height)

    composed = CompositeVideoClip(
        [
            ImageClip(frame).with_duration(1.0),
            subtitle,
        ],
        size=(width, height),
    )
    try:
        return np.array(composed.get_frame(0))
    finally:
        composed.close()
