"""把一段已知身份的本地视频渲染成一张确定性的 1280x720 JPEG。

设计约束（都是为了让"重新生成"这件事有意义）：

* **确定性。** 同样的输入必须产出同样的字节。FFmpeg 编码器默认不带时间戳，
  因此这里固定所有会影响输出的参数，并要求结果可与清单里的哈希直接比较。
* **不产生半成品。** 先写临时文件、校验通过后再原子改名。任何失败都不留下
  输出文件，也不会覆盖已存在的产物。
* **文字是数据。** 叠加文字来自用户输入，因此拒绝控制字符与双向覆盖字符——
  后者能让显示顺序与实际内容不一致，对"即将发布的标题"是实打实的欺骗。
* **字体只认内置目录。** 接受任意字体路径等于允许加载任意文件交给字体解析器。
* **不碰任务状态、不写清单、不认识 Streamlit。** 那些是后续步骤的事。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.utils.strict_media_tools import MediaTools

__all__ = [
    "MAX_OVERLAY_CHARS",
    "TARGET_HEIGHT",
    "TARGET_WIDTH",
    "RenderResult",
    "ThumbnailError",
    "ThumbnailSpec",
    "plan_frame_timestamp",
    "render_thumbnail",
]

TARGET_WIDTH = 1280
TARGET_HEIGHT = 720

#: 叠加文字上限。超出的文字在 720p 画面上必然缩到不可读。
MAX_OVERLAY_CHARS = 120

#: 支持的最短时长（毫秒）。低于此值抽帧不稳定。
MIN_DURATION_MS = 600

#: 画面底部安全区高度（像素），文字只画在这个区域内。
_OVERLAY_BAND_PX = 160

#: JPEG 质量。固定值是确定性的前提之一。
_JPEG_QUALITY = 90

#: 输出体积上限。远超此值说明画面异常，应当拒绝而不是写出去。
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024

_EXTRACT_TIMEOUT_SECONDS = 30
_PROBE_TIMEOUT_SECONDS = 20

#: 允许出现在叠加文字里的字符：可打印 ASCII、常见拉丁补充、空白。
#: 不可见 / 可伪装的字符。
#:
#: 这里刻意**不**按语言划白名单。早先的正则只放行拉丁字符，把中文、西里尔、
#: 韩文、阿拉伯文和符号全部挡在门外——而本项目的界面本身就是中文的，等于把
#: 功能对主要用户关闭。真正需要拦的是"看不见、但会改变显示或欺骗读者"的字符：
#: 零宽字符能伪装词边界，变体选择符会改字形，行分隔符会撑破排版。
#: ASCII 控制字符与双向覆盖字符分别由 _CONTROL 和 _BIDI_AND_FORMAT 负责。
#:
#: 全部用 \uXXXX 转义书写：直接把这些字符打进源码里，文件本身就变成了肉眼
#: 不可读的代码，review 时看不出到底禁了什么。
_INVISIBLE_TEXT = re.compile(
    "["
    "\u00ad"                # SOFT HYPHEN：是否显示取决于断行位置
    "\u034f"                # COMBINING GRAPHEME JOINER
    "\u061c"                # ARABIC LETTER MARK
    "\u115f\u1160"          # HANGUL CHOSEONG/JUNGSEONG FILLER
    "\u17b4\u17b5"          # KHMER INVISIBLE SYMBOLS
    "\u180e"                # MONGOLIAN VOWEL SEPARATOR
    "\u200b-\u200d"          # ZERO WIDTH SPACE / NON-JOINER / JOINER
    "\u2028\u2029"          # LINE / PARAGRAPH SEPARATOR
    "\u2060-\u2064"          # WORD JOINER + INVISIBLE OPERATORS
    "\u3164"                # HANGUL FILLER
    "\u034f"                # (combining grapheme joiner)
    "\u180b-\u180e"          # MONGOLIAN FORMAT CHARACTERS
    "\u200b-\u200f"          # ZERO WIDTH + LRM/RLM
    "\u202a-\u202e"          # BIDI EMBEDDING / OVERRIDE
    "\u2066-\u2069"          # BIDI ISOLATES
    "\u2e00-\u2e7f"          # SUPPLEMENTARY PUNCTUATION (invisible blocks)
    "\ufe00-\ufe0f"          # VARIATION SELECTORS
    "\ufeff"                # ZERO WIDTH NO-BREAK SPACE / BOM
    "\uffa0"                # HALFWIDTH HANGUL FILLER
    "\U000e0100-\U000e01ef"  # VARIATION SELECTORS SUPPLEMENT
    "\U000e0020-\U000e007f"  # TAG characters
    "]+"
)

#: 双向覆盖与方向控制字符：显示顺序可以与实际内容不一致。
_BIDI_AND_FORMAT = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

# ASCII 控制字符。
#
# 原先这个范围刻意跳过了 \t \n \r，因为旧的拉丁白名单顺带拦下了它们。移除白名单
# 后这三者就漏了（换行会把文字顶出安全区），所以这里必须覆盖完整的 C0 区。
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ThumbnailError(RuntimeError):
    """源视频、字体或文字不满足要求，或渲染/校验失败。"""


@dataclass(frozen=True)
class ThumbnailSpec:
    """一次渲染所需的全部输入。"""

    source: Path
    output: Path
    overlay_text: str
    font_path: Path
    duration_ms: int | None = None
    frame_at_seconds: float | None = None


@dataclass(frozen=True)
class RenderResult:
    """渲染结果及其可追溯信息。"""

    output_path: Path
    output_sha256: str
    output_bytes: int
    frame_at_seconds: float
    width: int
    height: int
    font_id: str


#: YouTube 会在画面右下角叠时间戳，底部 20% 留给它。
OVERLAY_BAND_TOP = 0.80
#: 默认把文字放在上三分之一：缩略图在大图和小图两种尺寸下都最先被看到。
OVERLAY_BAND_UPPER = "upper"
OVERLAY_BAND_LOWER = "lower"
#: 文字最大字号。720p 画面上 132px 左右在手机上仍然清晰。
_OVERLAY_FONT_START = 132
_OVERLAY_FONT_MIN = 44
_OVERLAY_MARGIN = 64
#: 候选帧数量：给用户挑选的余地。选帧是这个功能最关键的一步。
CANDIDATE_FRAME_COUNT = 3


def candidate_timestamps(duration_ms: int, count: int = CANDIDATE_FRAME_COUNT) -> list[float]:
    """
    给出一条视频的候选抽帧时刻，供用户挑选。

    铺开在正片里：最早的候选也跳过前 8%，那里通常是黑场或 logo，
    当缩略图会像坏片。最晚的不贴片尾。
    """
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise ThumbnailError(f"duration_ms 必须是整数，收到 {duration_ms!r}")
    if duration_ms <= 0:
        raise ThumbnailError(f"duration_ms 必须为正数，收到 {duration_ms}")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ThumbnailError(f"count 必须是正整数，收到 {count!r}")

    total = duration_ms / 1000.0
    # 末帧之前的余量必须给够：只留 0.15 秒时，seek 会落到最后一帧之后，
    # ffmpeg 直接不产出任何帧（"FFmpeg 未产出抽帧结果"）。
    # 10% 再减 0.5 秒对 3 秒和 3 分钟的片子都够。
    latest = max(total * 0.90 - 0.5, total * 0.5)
    first = min(total * 0.08, latest / (count + 1))
    if latest <= first:
        return [round(latest / 2, 3)] * count

    step = (latest - first) / (count - 1) if count > 1 else 0
    return [round(first + step * index, 3) for index in range(count)]


def plan_frame_timestamp(duration_ms: int) -> float:
    """按 25% 规则确定抽帧时刻，并保证不落在片尾。

    长视频取 25% 处以避开片头；短视频按比例会贴近结尾，那里可能还没有
    可解码的帧，因此结果被限制在 ``duration - 0.1`` 之内。
    """
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise ThumbnailError(f"duration_ms 必须是整数，收到 {duration_ms!r}")
    if duration_ms <= 0:
        raise ThumbnailError(f"duration_ms 必须为正数，收到 {duration_ms}")
    if duration_ms < MIN_DURATION_MS:
        raise ThumbnailError(
            f"视频时长 {duration_ms}ms 低于下限 {MIN_DURATION_MS}ms，无法稳定抽帧"
        )

    seconds = duration_ms / 1000.0
    at = min(seconds - 0.1, max(0.5, seconds * 0.25))
    if at <= 0:
        raise ThumbnailError(f"无法为 {duration_ms}ms 的视频确定抽帧时刻")
    return at


def _validate_text(text: object) -> str:
    """校验叠加文字。空白等价于不叠加。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        raise ThumbnailError(f"overlay_text 必须是字符串，收到 {type(text).__name__}")

    stripped = text.strip()
    if not stripped:
        return ""
    if len(stripped) > MAX_OVERLAY_CHARS:
        raise ThumbnailError(
            f"叠加文字长度 {len(stripped)} 超过上限 {MAX_OVERLAY_CHARS}"
        )
    if _CONTROL.search(stripped):
        raise ThumbnailError("叠加文字不能包含控制字符")
    if _BIDI_AND_FORMAT.search(stripped):
        raise ThumbnailError("叠加文字不能包含双向覆盖或方向控制字符")
    if _INVISIBLE_TEXT.search(stripped):
        raise ThumbnailError("叠加文字不能包含不可见或伪装字符")
    return stripped


def _bundled_font_dir() -> Path:
    from app.utils.utils import resource_dir

    return Path(resource_dir("fonts")).resolve()


def _validate_font(font_path: object) -> Path:
    """字体必须真实存在于内置字体目录内。"""
    if font_path is None:
        raise ThumbnailError("必须指定字体")
    candidate = Path(str(font_path))
    if not candidate.is_absolute():
        raise ThumbnailError(f"字体路径必须是绝对路径：{font_path}")

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ThumbnailError(f"字体不可用：{candidate}（{exc}）") from exc

    allowed = _bundled_font_dir()
    if not resolved.is_relative_to(allowed):
        raise ThumbnailError(f"字体必须位于内置字体目录 {allowed} 之内：{resolved}")
    if not resolved.is_file():
        raise ThumbnailError(f"字体不是普通文件：{resolved}")
    return resolved


def _validate_source(source: object) -> Path:
    if source is None:
        raise ThumbnailError("必须指定源视频")
    candidate = Path(str(source))
    if not candidate.is_absolute():
        raise ThumbnailError(f"源视频路径必须是绝对路径：{source}")
    if candidate.is_symlink():
        raise ThumbnailError(f"源视频是符号链接，内容来源不可信：{candidate}")
    if not candidate.is_file():
        raise ThumbnailError(f"源视频不是普通文件：{candidate}")
    return candidate


def _probe_duration_ms(source: Path, ffprobe: Path) -> int:
    try:
        completed = subprocess.run(
            [
                str(ffprobe),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ThumbnailError(f"ffprobe 无法读取源视频：{exc}") from exc

    if completed.returncode != 0:
        raise ThumbnailError(
            f"ffprobe 读取源视频失败（返回码 {completed.returncode}）："
            f"{completed.stderr.strip()[:200]}"
        )

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ThumbnailError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

    raw = None
    for stream in payload.get("streams") or []:
        raw = stream.get("duration")
        if raw:
            break
    if not raw:
        raw = (payload.get("format") or {}).get("duration")
    if raw in (None, "", "N/A"):
        raise ThumbnailError("源视频时长未知，无法安全抽帧")

    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        raise ThumbnailError(f"源视频时长无法解析：{raw!r}") from None
    if seconds <= 0:
        raise ThumbnailError(f"源视频时长非正数：{raw}")
    return int(round(seconds * 1000))


def _run(cmd: list[str], label: str, timeout: int) -> None:
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ThumbnailError(f"{label} 超时（{timeout}s）") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ThumbnailError(f"{label} 无法执行：{exc}") from exc

    if completed.returncode != 0:
        raise ThumbnailError(
            f"{label} 失败（返回码 {completed.returncode}）："
            f"{completed.stderr.strip()[:300]}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wrap_to_width(draw, text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
    """把文字折成不超过 ``max_lines`` 行、每行不超过 ``max_width`` 的若干行。"""
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines]


def _draw_overlay(
    image_path: Path,
    text: str,
    font_path: Path,
    band: str = OVERLAY_BAND_UPPER,
) -> None:
    """
    把文字画在画面上。就地修改 image_path。

    与第一版的差别，都是为了"能上传、能点得动"：

    * **自动缩字 + 折行**，写不下不再报错；
    * **默认上三分之一**，避开 YouTube 叠时间戳的底部 20%；
    * **压暗背景而不是盖黑方块**，画面主体仍然看得见；
    * **加描边**，浅色画面上也不会糊掉。
    """
    if not text:
        return

    from PIL import Image, ImageDraw, ImageFont

    with Image.open(image_path) as source:
        canvas = source.convert("RGB")

    draw = ImageDraw.Draw(canvas)
    max_width = TARGET_WIDTH - _OVERLAY_MARGIN * 2

    font = None
    lines: list[str] = []
    for size in range(_OVERLAY_FONT_START, _OVERLAY_FONT_MIN - 1, -4):
        try:
            candidate = ImageFont.truetype(str(font_path), size)
        except OSError:
            continue
        wrapped = _wrap_to_width(draw, text, candidate, max_width)
        widest = max(
            (draw.textbbox((0, 0), line, font=candidate)[2] for line in wrapped),
            default=0,
        )
        if widest and widest <= max_width:
            font, lines = candidate, wrapped
            break
    if font is None:
        try:
            font = ImageFont.truetype(str(font_path), _OVERLAY_FONT_MIN)
        except OSError as exc:
            raise ThumbnailError(f"字体无法加载：{font_path}") from exc
        lines = _wrap_to_width(draw, text, font, max_width, max_lines=1)
    if not lines:
        return

    heights = []
    widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])

    line_gap = max(4, int(font.size * 0.18))
    block_height = sum(heights) + line_gap * (len(lines) - 1)

    if band == OVERLAY_BAND_LOWER:
        top = int(TARGET_HEIGHT * OVERLAY_BAND_TOP) - block_height - _OVERLAY_MARGIN // 2
    else:
        top = max(_OVERLAY_MARGIN, int(TARGET_HEIGHT * 0.10))
    top = max(0, min(top, TARGET_HEIGHT - block_height - _OVERLAY_MARGIN // 2))

    _dim_band(canvas, top - _OVERLAY_MARGIN // 2,
              top + block_height + _OVERLAY_MARGIN // 2)

    y = top
    for line, width, height in zip(lines, widths, heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        x = _OVERLAY_MARGIN
        # 描边：浅色画面上白字会糊掉
        for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2)):
            draw.text((x + dx, y - bbox[1] + dy), line, fill=(0, 0, 0), font=font)
        draw.text((x, y - bbox[1]), line, fill=(255, 255, 255), font=font)
        y += height + line_gap

    # 不带任何 exif/icc 写出，保证可复现。
    canvas.save(image_path, format="JPEG", quality=_JPEG_QUALITY, optimize=False)


def _dim_band(canvas, top: int, bottom: int) -> None:
    """
    在文字后面铺一层由淡到浓的压暗渐变。

    不用不透明矩形：黑方块会把画面主体挡死，缩略图就只剩一行字。
    渐变既保证文字对比度，又保留底下的画面。
    """
    from PIL import Image

    top = max(0, int(top))
    bottom = min(canvas.height, int(bottom))
    if bottom <= top:
        return
    height = bottom - top
    gradient = Image.new("L", (1, height))
    for row in range(height):
        ratio = row / max(1, height - 1)
        gradient.putpixel((0, row), int(70 + 120 * ratio))
    overlay = Image.new("RGB", (canvas.width, height), (0, 0, 0))
    canvas.paste(overlay, (0, top), gradient.resize((canvas.width, height)))


def _strip_jpeg_metadata(path: Path) -> None:
    """就地移除 JPEG 的全部 APPn 与 COM 段。

    Pillow 写 JPEG 时总会插入一个 APP0/JFIF 段（内含密度信息），而密度可能随
    环境变化，那会直接破坏"同样输入产出同样字节"。与其把 JFIF 列入白名单，
    不如在字节层面把所有应用段与注释段删掉——剩下的只有帧头、量化表、霍夫曼
    表和压缩数据，正好是渲染结果本身。
    """
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ThumbnailError("输出不是有效的 JPEG（缺少 SOI 标记）")

    out = bytearray(b"\xff\xd8")
    index = 2
    while index < len(data) - 1:
        if data[index] != 0xFF:
            raise ThumbnailError("JPEG 段结构异常")
        marker = data[index + 1]
        if marker == 0xD9:  # EOI：必须保留，否则解码器认为文件被截断
            out += data[index:]
            break
        if marker == 0xDA:  # SOS：其后是熵编码数据，原样保留
            out += data[index:]
            break

        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2 or index + 2 + length > len(data):
            raise ThumbnailError("JPEG 段长度异常")
        payload_end = index + 2 + length

        is_application = 0xE0 <= marker <= 0xEF
        is_comment = marker == 0xFE
        if not (is_application or is_comment):
            out += data[index:payload_end]
        index = payload_end

    if bytes(out) == data:
        return
    temp = path.with_name(path.name + ".stripped")
    temp.write_bytes(bytes(out))
    os.replace(temp, path)


def _verify_output(path: Path) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(path) as image:
            if image.format != "JPEG":
                raise ThumbnailError(f"输出不是 JPEG：{image.format}")
            if image.size != (TARGET_WIDTH, TARGET_HEIGHT):
                raise ThumbnailError(
                    f"输出尺寸应为 {TARGET_WIDTH}x{TARGET_HEIGHT}，实际 {image.size}"
                )
            if image.info:
                raise ThumbnailError(f"输出带有不应存在的元数据：{sorted(image.info)}")
            image.verify()
    except ThumbnailError:
        raise
    except OSError as exc:
        raise ThumbnailError(f"输出无法解码：{exc}") from exc
    return TARGET_WIDTH, TARGET_HEIGHT


def render_thumbnail(spec: ThumbnailSpec, tools: MediaTools) -> RenderResult:
    """渲染一张缩略图。失败时不留下任何输出文件。

    成功路径：抽帧 → 叠加文字 → 校验尺寸/格式/元数据 → 原子改名为最终路径。
    """
    source = _validate_source(spec.source)
    font_path = _validate_font(spec.font_path)
    text = _validate_text(spec.overlay_text)

    if spec.duration_ms is None:
        duration_ms = _probe_duration_ms(source, tools.ffprobe)
    else:
        duration_ms = spec.duration_ms
    if duration_ms <= 0:
        raise ThumbnailError("视频时长未知，无法安全抽帧")

    if spec.frame_at_seconds is not None:
        at = float(spec.frame_at_seconds)
        if at <= 0 or at >= duration_ms / 1000.0:
            raise ThumbnailError(f"指定的抽帧时刻 {at} 超出视频时长范围")
    else:
        at = plan_frame_timestamp(duration_ms)

    output = Path(str(spec.output))
    if not output.is_absolute():
        raise ThumbnailError(f"输出路径必须是绝对路径：{spec.output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        dir=output.parent, prefix=".thumb-", suffix=".jpg", delete=False
    )
    temp_path = Path(handle.name)
    handle.close()
    temp_path.unlink(missing_ok=True)

    try:
        # 私有临时文件、不经 shell、单线程；所有影响输出的参数都显式固定。
        _run(
            [
                str(tools.ffmpeg), "-y", "-nostdin", "-loglevel", "error",
                "-threads", "1",
                "-ss", f"{at:.3f}",
                "-i", f"file:{source}",
                "-frames:v", "1",
                "-an", "-sn", "-dn",
                "-vf", f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:flags=bicubic",
                "-pix_fmt", "yuvj420p",
                "-q:v", "3",
                "-map_metadata", "-1",
                "-fflags", "+bitexact",
                "-flags:v", "+bitexact",
                "-f", "image2",
                "-c:v", "mjpeg",
                f"file:{temp_path}",
            ],
            "FFmpeg 抽帧",
            _EXTRACT_TIMEOUT_SECONDS,
        )

        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise ThumbnailError("FFmpeg 未产出抽帧结果")

        _draw_overlay(temp_path, text, font_path)
        _strip_jpeg_metadata(temp_path)
        width, height = _verify_output(temp_path)

        size = temp_path.stat().st_size
        if size > _MAX_OUTPUT_BYTES:
            raise ThumbnailError(f"输出体积 {size} 超过上限 {_MAX_OUTPUT_BYTES}")

        digest = _sha256_file(temp_path)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, output)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    return RenderResult(
        output_path=output,
        output_sha256=digest,
        output_bytes=size,
        frame_at_seconds=at,
        width=width,
        height=height,
        font_id=font_path.name,
    )
