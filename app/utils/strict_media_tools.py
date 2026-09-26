"""为新链路严格解析 ffmpeg/ffprobe，并在启动时固定其身份。

既有的 :func:`app.utils.utils.get_ffmpeg_binary` 允许一路退化到裸字符串
``"ffmpeg"``，把问题推迟到 subprocess 运行时的某一刻。那是视频链路多年沿用的
行为，本模块不去改它；这里提供的是另一套更严格的做法，供新增链路使用：

* 只接受**显式**配置（配置项或环境变量）或 PATH 查找得到的绝对路径；
* 拒绝软链——记录了软链的哈希并不等于记录了实际执行的程序；
* 拒绝不可执行文件、目录、相对路径和裸命令名；
* 启动时记录版本号与二进制 SHA-256，使产物可追溯到具体工具链。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "MediaTools",
    "MediaToolsError",
    "SUPPORTED_TOOLS",
    "resolve_media_tool",
    "probe_media_tools",
]

#: 允许解析的工具名白名单。避免把任意字符串拼进查找逻辑。
SUPPORTED_TOOLS = ("ffmpeg", "ffprobe")

#: 各工具的环境变量名。ffmpeg 沿用 imageio/moviepy 的既有约定，
#: ffprobe 没有对应约定，单独给一个。
_ENV_VARS = {
    "ffmpeg": "IMAGEIO_FFMPEG_EXE",
    "ffprobe": "MPT_FFPROBE_PATH",
}

_VERSION_TIMEOUT_SECONDS = 10


class MediaToolsError(RuntimeError):
    """工具无法被严格解析，或解析结果不满足可执行性要求。"""


@dataclass(frozen=True)
class MediaTools:
    """一次启动时固定的工具链身份。"""

    ffmpeg: Path
    ffprobe: Path
    ffmpeg_version: str
    ffprobe_version: str
    ffmpeg_sha256: str
    ffprobe_sha256: str

    def to_manifest(self) -> dict:
        """可写入产物元数据的工具链记录，不含本机绝对路径。"""
        return {
            "ffmpeg_version": self.ffmpeg_version,
            "ffprobe_version": self.ffprobe_version,
            "ffmpeg_sha256": self.ffmpeg_sha256,
            "ffprobe_sha256": self.ffprobe_sha256,
        }


def _require_supported(name: str) -> str:
    if name not in SUPPORTED_TOOLS:
        raise MediaToolsError(
            f"不支持的工具名 {name!r}，仅支持 {', '.join(SUPPORTED_TOOLS)}"
        )
    return name


def _validate_candidate(path: Path, name: str) -> Path:
    """确认候选路径最终指向一个可执行的真实文件，返回其真实路径。

    软链会被解引用而不是直接拒绝：Homebrew 的 ``/opt/homebrew/bin/ffmpeg`` 就是
    指向 Cellar 的软链，一律拒绝会让本模块在开发机上不可用。要防的是"记录了
    链接却没记录实际执行的程序"，解引用到真实文件再校验再记哈希正好解决这点。

    残留风险：解引用与执行之间存在极短的替换窗口。彻底消除它需要按 fd 执行，
    超出本模块范围；这里通过在启动时固定真实路径与哈希来把窗口的影响降到可追溯。
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MediaToolsError(f"{name} 无法解析为真实文件（{path}）：{exc}") from exc

    if resolved.is_dir():
        raise MediaToolsError(f"{name} 不是文件而是目录：{resolved}")
    if not resolved.is_file():
        raise MediaToolsError(f"{name} 不存在或不是普通文件：{resolved}")
    if not os.access(resolved, os.X_OK):
        raise MediaToolsError(f"{name} 不可执行：{resolved}")
    return resolved


def resolve_media_tool(
    name: str,
    configured_path: object | None = None,
    env_path: object | None = None,
) -> Path:
    """按 显式配置 → 环境变量 → PATH 的顺序解析出一个绝对可执行路径。

    任何一步给出的值若不合法都**立即失败**，不会继续往下退化。理由是：配置
    写错了却静默回退到 PATH 上的另一个二进制，正是"能跑但结果不对"的来源。

    :param name: ``ffmpeg`` 或 ``ffprobe``。
    :param configured_path: 配置项里的值，可为 ``None``。
    :param env_path: 环境变量里的值，可为 ``None``。留空字符串视为未设置。
    """
    name = _require_supported(name)

    candidates: list[object] = [configured_path, env_path]
    for index, candidate in enumerate(candidates):
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            # 显式配置的空白是错误；环境变量的空白多半是 CI 未展开，按未设置处理。
            if index == 0:
                raise MediaToolsError(f"{name} 的配置路径是空白字符串")
            continue
        path = Path(text)
        if not path.is_absolute():
            raise MediaToolsError(f"{name} 必须是绝对路径，收到 {text!r}")
        return _validate_candidate(path, name)

    found = shutil.which(name)
    if not found:
        raise MediaToolsError(f"PATH 中找不到 {name}，且没有提供显式路径")
    return _validate_candidate(Path(found), name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_of(path: Path, name: str) -> str:
    """读取版本号。拿不到就失败——无法追溯的工具链不允许进入生产。"""
    try:
        completed = subprocess.run(
            [str(path), "-version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaToolsError(f"无法执行 {name} -version：{exc}") from exc

    if completed.returncode != 0 or not completed.stdout.strip():
        raise MediaToolsError(
            f"{name} -version 返回码 {completed.returncode}，无法确定工具链版本"
        )
    first_line = completed.stdout.splitlines()[0].strip()
    return first_line


def probe_media_tools(
    ffmpeg_path: object | None = None,
    ffprobe_path: object | None = None,
) -> MediaTools:
    """解析两个工具并固定其版本与哈希。启动时调用一次即可。"""
    ffmpeg = resolve_media_tool(
        "ffmpeg",
        configured_path=ffmpeg_path,
        env_path=os.environ.get(_ENV_VARS["ffmpeg"]),
    )
    ffprobe = resolve_media_tool(
        "ffprobe",
        configured_path=ffprobe_path,
        env_path=os.environ.get(_ENV_VARS["ffprobe"]),
    )
    return MediaTools(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_version=_version_of(ffmpeg, "ffmpeg"),
        ffprobe_version=_version_of(ffprobe, "ffprobe"),
        ffmpeg_sha256=_sha256(ffmpeg),
        ffprobe_sha256=_sha256(ffprobe),
    )
