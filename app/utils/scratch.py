"""生成过程中的中间产物只放在内存盘里，用完立刻删。

需求很直接：**SSD 上只留最终成片**。配音、字幕、脚本、下载的素材、
拼接用的片段、AI 生成的 BGM——这些中间产物既占空间，也把用户的内容
留在了磁盘上。

macOS 没有 tmpfs，但可以用 `diskutil image attach ram://` 挂一个真正的
内存盘。实测写入 ~400 MB/s、读取 ~18 GB/s，数据只在 RAM 里，重启即消失。

这里提供三件事：

* :func:`scratch_space` —— 全进程唯一的内存盘管理者；
* :meth:`ScratchSpace.scoped` —— 上下文管理器，**退出即删**，异常也删；
* :meth:`ScratchSpace.purge` —— 清掉所有残留（崩溃、强制退出后的兜底）。

内存盘创建失败（容器内、无权限、hdiutil 不可用）时**不能静默降级**：
那等于又回到"写 SSD 上"。但也不能因此拒绝服务。所以退回到系统临时目录
并大声告警，让用户知道这次运行会碰磁盘。
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

__all__ = [
    "ScratchError",
    "ScratchSpace",
    "scratch_space",
    "set_scratch_mode",
]

logger = logging.getLogger(__name__)

VOLUME_NAME = "MPT-SCRATCH"
DEFAULT_SIZE_MB = 4096
_MODES = ("ram", "disk", "off")


class ScratchError(RuntimeError):
    """内存盘无法建立，且调用方要求必须成功。"""


class ScratchSpace:
    """一个进程内的内存盘（或退化目录），所有中间产物都写在这里。"""

    def __init__(
        self,
        mode: str = "ram",
        size_mb: int = DEFAULT_SIZE_MB,
        mount_point: Path | None = None,
    ):
        if mode not in _MODES:
            raise ScratchError(f"未知的 scratch 模式：{mode!r}，可选 {_MODES}")
        self.mode = mode
        self.size_mb = int(size_mb)
        # 显式指定挂载点，让测试（以及想把内存盘放在别处的运维）不必共用
        # 同一个真实路径——否则多个 ScratchSpace 会互相看见对方的文件。
        self._mount_point_override = Path(mount_point) if mount_point else None
        self._lock = threading.RLock()
        self._root: Path | None = None
        self._device: str | None = None
        self._on_ram_disk = False
        self._mounted_by_us = False
        self._fallback_reason = ""

    # -- 生命周期 ---------------------------------------------------------

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def on_ram_disk(self) -> bool:
        """中间产物是否真的在内存里。为 ``False`` 时本次运行会碰磁盘。"""
        return self._on_ram_disk

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    def ensure_ready(self) -> Path:
        """确保可用并返回根目录。可重复调用。"""
        with self._lock:
            if self._root is not None and self._root.is_dir():
                return self._root

            if self.mode == "off":
                self._root = None
                return self._root  # type: ignore[return-value]

            if self.mode == "ram":
                try:
                    self._root = self._mount_ram_disk()
                    self._on_ram_disk = True
                    self._fallback_reason = ""
                    atexit.register(self.shutdown)
                    logger.info(
                        "scratch: 中间产物挂载到内存盘 %s（%d MB，仅存于 RAM）",
                        self._root, self.size_mb,
                    )
                    return self._root
                except Exception as exc:  # noqa: BLE001 - 任何失败都要能降级
                    self._fallback_reason = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "scratch: 内存盘不可用（%s），本次运行退回临时目录，"
                        "中间产物会写入磁盘。可用 scratch_mode = \"off\" 明确"
                        "接受这一点，或修复内存盘后重试。",
                        self._fallback_reason,
                    )

            # 降级：系统临时目录
            self._root = Path(tempfile.mkdtemp(prefix="mpt-scratch-"))
            self._on_ram_disk = False
            return self._root

    def _mount_point(self) -> Path:
        if self._mount_point_override is not None:
            self._mount_point_override.mkdir(parents=True, exist_ok=True)
            return self._mount_point_override
        base = Path(os.path.expanduser("~")) / ".cache" / "mpt-scratch"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _mount_ram_disk(self) -> Path:
        device = self._attach()
        self._device = device
        try:
            subprocess.run(
                ["newfs_hfs", "-v", VOLUME_NAME, device],
                check=True, capture_output=True,
            )
            mount_point = self._mount_point()
            subprocess.run(
                ["diskutil", "mount", "-mountPoint", str(mount_point), device],
                check=True, capture_output=True,
            )
        except Exception:
            self._detach()
            raise
        self._mounted_by_us = True
        return mount_point

    def _attach(self) -> str:
        result = subprocess.run(
            ["diskutil", "image", "attach", "--noMount", f"ram://{self.size_mb * 1024 * 1024}"],
            check=True, capture_output=True, text=True,
        )
        device = (result.stdout or "").strip().splitlines()[-1].strip()
        if not device.startswith("/dev/"):
            raise ScratchError(f"无法解析内存盘设备名：{result.stdout!r}")
        return device

    def _detach(self) -> None:
        if self._device:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["diskutil", "unmount", "force", self._device],
                    capture_output=True,
                )
            self._device = None
        self._mounted_by_us = False

    def shutdown(self) -> None:
        """卸载内存盘并删除退化目录。进程退出时自动调用。"""
        with self._lock:
            root = self._root
            if self._on_ram_disk:
                self._detach()
            elif root is not None and root.exists():
                shutil.rmtree(root, ignore_errors=True)
            self._root = None
            self._on_ram_disk = False

    # -- 使用 -------------------------------------------------------------

    @contextlib.contextmanager
    def scoped(self, prefix: str = "job"):
        """
        分配一个临时子目录，**退出时必定删除**（正常、异常、KeyboardInterrupt）。

        这是"用完立刻删"的落点：调用方把中间产物写进来，剩下的事交给这里。
        """
        base = self.ensure_ready()
        if base is None:
            raise ScratchError(
                "scratch_mode = \"off\" 时无法分配中间目录："
                "该模式下中间产物会写回应用目录。"
            )
        safe_prefix = "".join(c for c in prefix if c.isalnum() or c in "-_")[:40] or "job"
        target = base / f"{safe_prefix}-{uuid.uuid4().hex[:12]}"
        target.mkdir(parents=True, exist_ok=False)
        try:
            yield target
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def purge(self) -> int:
        """
        删除根目录下所有残留，返回释放的条目数。

        内存盘本身有上限（``scratch_size_mb``）：下载素材缓存是**跨任务共享**的，
        刻意不在单个任务结束时删除——并发任务会互相踩。共享缓存的代价是占用
        内存盘的额度，好处是绝不落 SSD；进程退出时 :meth:`shutdown` 卸载内存盘，
        全部内容随之消失。
        """
        base = self.ensure_ready()
        if base is None:
            return 0
        removed = 0
        for child in list(base.iterdir()):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
            removed += 1
        return removed


_space: ScratchSpace | None = None
_space_lock = threading.Lock()


def scratch_space() -> ScratchSpace:
    """进程内唯一的内存盘管理者。"""
    global _space
    with _space_lock:
        if _space is None:
            mode, size = _configured()
            _space = ScratchSpace(mode=mode, size_mb=size)
        return _space


def set_scratch_mode(mode: str, size_mb: int | None = None) -> None:
    """测试或运维用：重建内存盘（会先卸载旧的）。"""
    global _space
    with _space_lock:
        if _space is not None:
            _space.shutdown()
        size = size_mb if size_mb is not None else DEFAULT_SIZE_MB
        _space = ScratchSpace(mode=mode, size_mb=size)


def _configured() -> tuple[str, int]:
    """读取配置，失败时用保守默认值（ram）。"""
    mode, size = "ram", DEFAULT_SIZE_MB
    try:
        from app.config import config

        mode = str(config.app.get("scratch_mode", mode) or mode)
        size = int(config.app.get("scratch_size_mb", size) or size)
    except Exception:  # noqa: BLE001 - 配置不可用不该阻断内存盘
        pass
    if mode not in _MODES:
        logger.warning("scratch: 忽略无效的 scratch_mode=%r，回退到 ram", mode)
        mode = "ram"
    return mode, size
