"""集中解析并校验应用使用的目录根。

仓库里有两棵彼此独立的目录树：嵌套的 MoneyPrinterTurbo 应用根，以及外层持有
``plans/`` 与 ``outputs/`` 的作品集根。历史上各处代码各自用 ``dirname`` 推导，
在容器或非默认部署下会指向不同位置。本模块把"根从哪里来"收敛成一处，并且
只接受显式配置：

* 应用根默认取既有的 :func:`app.utils.utils.root_dir`，它已经基于 ``realpath``；
* 作品集根必须显式传入或来自 ``MPT_PORTFOLIO_ROOT``，**不**从应用根推导；
* 任何根都必须是绝对路径、不得包含符号链接成分；
* 作品集根不能落在应用根之内，且 ``storage_root`` / ``plans_root`` /
  ``outputs_root`` 三个数据根不得互相覆盖。应用根**可以**落在作品集根之内
  ——两棵树共用一个父目录是常见布局，真正要防的是数据根互相覆盖。

解析过程不创建任何目录。写入方按需创建，避免仅仅导入模块就改变磁盘状态。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProjectLayout",
    "ProjectLayoutError",
    "PORTFOLIO_ROOT_ENV_VAR",
    "resolve_project_layout",
    "same_filesystem",
]

#: 作品集根的环境变量名。设置它等于显式声明"作品集在哪里"。
PORTFOLIO_ROOT_ENV_VAR = "MPT_PORTFOLIO_ROOT"


class ProjectLayoutError(RuntimeError):
    """根目录配置缺失、相对化、被符号链接污染或互相嵌套。"""


@dataclass(frozen=True)
class ProjectLayout:
    """一次解析得到的全部根目录，不可变以便在进程内安全共享。"""

    application_root: Path
    storage_root: Path
    thumbnail_root: Path
    descriptor_root: Path
    portfolio_root: Path
    plans_root: Path
    outputs_root: Path


def _default_application_root() -> Path:
    # 局部导入：既有的 root_dir 已经是全仓库唯一的应用根定义，
    # 局部导入可让本模块在 app.utils.utils 尚未初始化完成时也能被单独导入。
    from app.utils.utils import root_dir

    return Path(root_dir())


def _coerce_root(value: object, field: str) -> Path:
    """把配置值转成 Path，并拒绝空白、相对、``~`` 和未归一化的成分。"""
    if value is None:
        raise ProjectLayoutError(f"{field} 未配置")

    text = str(value).strip()
    if not text:
        raise ProjectLayoutError(f"{field} 未配置：不能是空白字符串")

    # os.path.expanduser 会把 "~" 变成当前用户的家目录，这属于隐式猜测。
    if text.startswith("~"):
        raise ProjectLayoutError(f"{field} 必须是已展开的绝对路径，不能使用 ~")

    path = Path(text)
    if not path.is_absolute():
        raise ProjectLayoutError(f"{field} 必须是绝对路径，收到 {text!r}")

    # "." 和 ".." 必须显式拒绝而不是规范化。Path.relative_to 按成分比较前缀，
    # 路径里藏一个 ".." 就能让包含关系判断失效——例如
    # <base>/x/../app/sub 会被判定为不在 <base>/app 之内，而它实际在其内部。
    # 归一化会把这个问题藏起来，直接拒绝则让配置错误立刻暴露。
    for part in path.parts:
        if part in (".", ".."):
            raise ProjectLayoutError(
                f"{field} 不能包含 '.' 或 '..' 成分，请写成归一化后的绝对路径：{text!r}"
            )

    return path


def _first_symlink_component(path: Path) -> Path | None:
    """返回 ``path`` 中最靠近根的符号链接成分；没有则返回 ``None``。

    逐段拼接并用 ``is_symlink`` 判定，而不是 ``resolve()``：后者会跟随链接，
    正好掩盖掉我们要拒绝的情况。对尚不存在的成分 ``is_symlink`` 返回 False，
    因此本函数对"还没建出来的根"同样适用。
    """
    if not path.is_absolute():
        # 相对路径下 Path(path.anchor) 是 "."，parts[1:] 会丢掉首段，
        # 结果是"看起来检查过了"的错误答案。宁可响亮地失败。
        raise ProjectLayoutError(f"符号链接检查要求绝对路径，收到 {path}")

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return current
    return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _paths_overlap(first: Path, second: Path) -> bool:
    """两个路径是否相同，或其中一个是另一个的祖先。

    逐成分比较而不是字符串前缀：``/a/plans-archive`` 不是 ``/a/plans`` 的子目录，
    但 ``str.startswith`` 会说是。
    """
    if first == second:
        return True
    return _is_within(first, second) or _is_within(second, first)


#: 推导出来的数据根，按"谁覆盖谁会出事"分组。同一组内任意两个不可重叠；
#: thumbnail_root / descriptor_root 都在 storage_root 之内，因此只查 storage_root
#: 就把它们一并覆盖了，不必重复枚举。
_CONFLICTING_ROOT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("storage_root", "plans_root", "outputs_root"),
)


def _reject_overlapping_derived_roots(layout: ProjectLayout) -> None:
    """数据根互相覆盖时失败关闭，并点名是哪一对。

    只在"确实会出事"时拒绝：``plans`` 压住 ``storage`` 会让归档成片被当作中间
    产物清掉，``storage`` 压住 ``outputs`` 会让唯一长期留存的视频被清掉。这两种
    顺序都要挡住，两个方向都算重叠。
    """
    for group in _CONFLICTING_ROOT_GROUPS:
        members = [(field, getattr(layout, field)) for field in group]
        for index, (first_field, first_path) in enumerate(members):
            for second_field, second_path in members[index + 1 :]:
                if _paths_overlap(first_path, second_path):
                    raise ProjectLayoutError(
                        f"{first_field} 与 {second_field} 重叠，不能是同一个目录"
                        f"或彼此的子目录：{first_path} / {second_path}。"
                        "请把作品集根与应用根分开配置，"
                        "否则归档成片或成片目录会被当成中间产物清理掉。"
                    )


def _nearest_existing(path: Path) -> Path | None:
    """向上找到最近的、真实存在的祖先目录。

    权限不足会让 ``Path.exists()`` 向上抛 ``OSError``（pathlib 只吞 ENOENT 一类），
    这里必须自己吞掉：调用方要的是一个布尔判定，不是一次异常。
    """
    current = path
    while True:
        try:
            if current.exists():
                return current
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def same_filesystem(first: object, second: object) -> bool:
    """粗筛两个路径是否可能落在同一文件系统上。

    **这不是保证，只是发布前的快速筛子。** 原子改名要求同盘，但设备号在
    macOS 上区分不了 APFS 的各个卷：``/``、``/System/Volumes/Data``、
    ``/Users``、``/private/var`` 在本机都报同一个 ``st_dev``。所以即使这里
    返回 ``True``，跨卷的 ``os.replace`` 仍可能抛 ``EXDEV``。真正的保证是
    调用方捕获 ``EXDEV`` 并改走复制或拒绝发布；本函数只用来在昂贵的写入之前
    排掉明显不同盘的组合。

    路径尚不存在时回退到最近的已存在祖先，因为发布前先创建文件只为检查一次
    是不合理的。相对路径和空值无法给出有意义的判断，一律返回 ``False``。
    """
    paths: list[Path] = []
    for value in (first, second):
        text = str(value).strip()
        if not text:
            return False
        path = Path(text)
        if not path.is_absolute():
            return False
        paths.append(path)

    devices = []
    for path in paths:
        anchor = _nearest_existing(path)
        if anchor is None:
            return False
        try:
            devices.append(os.stat(anchor).st_dev)
        except OSError:
            return False

    return devices[0] == devices[1]


def _portfolio_root_from_config() -> str:
    """从 ``config.toml`` 的 ``[app] portfolio_root`` 读取作品集根。

    局部导入且吞掉所有异常：``app.config.config`` 在导入时会做文件 IO
    （首次运行还会复制示例配置），布局解析不应该因此失败或产生副作用。
    配置读不到就当作"未配置"，由调用方看到明确的失败。
    """
    try:
        from app.config import config as app_config

        return str(app_config.app.get("portfolio_root", "") or "")
    except Exception:  # noqa: BLE001 - 配置不可用等同于未配置
        return ""


def resolve_project_layout(
    application_root: object | None = None,
    portfolio_root: object | None = None,
) -> ProjectLayout:
    """解析出 :class:`ProjectLayout`，配置不合法时抛出 :class:`ProjectLayoutError`。

    作品集根的优先级：显式参数 → ``MPT_PORTFOLIO_ROOT`` 环境变量 →
    ``config.toml`` 的 ``[app] portfolio_root``。三者都没有则失败关闭，
    **绝不**从应用根推导。环境变量排在配置之前，用于单次运行的临时覆盖。

    :param application_root: 应用根。``None`` 时使用既有的 ``root_dir()``。
    :param portfolio_root: 作品集根。``None`` 时按上面的顺序继续查找。
    """
    if application_root is None:
        application_root = _default_application_root()
    # 只有"完全没传"才继续往下找。显式传了空白串是调用方的错误，必须直接报错：
    # 悄悄换成另一个来源的值，会让调用方以为自己配的目录生效了。
    if portfolio_root is None:
        portfolio_root = os.environ.get(PORTFOLIO_ROOT_ENV_VAR)
        if portfolio_root is None or not str(portfolio_root).strip():
            portfolio_root = _portfolio_root_from_config()
        if portfolio_root is None or not str(portfolio_root).strip():
            # 失败关闭，但必须说清楚怎么才能打开。缩略图与作品集规划都依赖这个根，
            # 报错若只写"未配置"，用户只会看到功能凭空消失，以为是坏了。
            raise ProjectLayoutError(
                "portfolio_root 未配置：缩略图与作品集功能已停用。"
                '请在 config.toml 的 [app] 下设置 portfolio_root = "/你的/作品集目录"，'
                f"或用环境变量 {PORTFOLIO_ROOT_ENV_VAR} 临时指定。"
                "该目录必须已存在，且不能与应用目录互相包含。"
            )

    app_path = _coerce_root(application_root, "application_root")
    portfolio_path = _coerce_root(portfolio_root, "portfolio_root")

    if not app_path.is_dir():
        raise ProjectLayoutError(f"application_root 不是已存在的目录：{app_path}")

    if not portfolio_path.is_dir():
        raise ProjectLayoutError(f"portfolio_root 不是已存在的目录：{portfolio_path}")

    for path, field in ((app_path, "application_root"), (portfolio_path, "portfolio_root")):
        link = _first_symlink_component(path)
        if link is not None:
            raise ProjectLayoutError(f"{field} 含有符号链接成分：{link}")

    if app_path == portfolio_path:
        raise ProjectLayoutError(
            f"application_root 与 portfolio_root 不能是同一目录：{app_path}"
        )
    if _is_within(portfolio_path, app_path):
        raise ProjectLayoutError(
            f"portfolio_root 不能位于 application_root 之内：{portfolio_path}"
        )

    storage_root = app_path / "storage"
    layout = ProjectLayout(
        application_root=app_path,
        storage_root=storage_root,
        thumbnail_root=storage_root / "thumbnails",
        descriptor_root=storage_root / "result_descriptors",
        portfolio_root=portfolio_path,
        plans_root=portfolio_path / "plans",
        outputs_root=portfolio_path / "outputs",
    )

    # 允许 application_root 位于 portfolio_root 之内：应用仓库嵌在作品集根下是
    # 常见且安全的布局（两棵树共用一个父目录），而过去这条禁令把整种布局挡在
    # 门外，只能靠"猜根"绕过去。真正要防的不是两个根互相嵌套，而是**推导出来的
    # 数据根互相覆盖**——那一来才是真出事：plans_root 落进 storage_root 时，归档
    # 成片会被当成中间产物清掉；storage_root 落进 outputs_root 时，SSD 清理会把
    # 唯一长期留存的视频删掉。所以下面按成对重叠来查，而不是按"根是否嵌套"来查：
    # 前者精确地覆盖了后者想防的危害，又不会误伤合法布局。
    _reject_overlapping_derived_roots(layout)

    # 五个推导出来的根同样要查。它们由"已验证的父根 + 固定名字"拼成，结构上
    # 不会逃出父根；但父根下的那一层本身可能是指向别处的软链（历史遗留正是
    # 这个问题），那样 thumbnail_root 就会落到作品集树里，plans_root 会落到
    # 树外。既然这个模块就是为了收拾这类历史状态，就不能对推导出来的根免检。
    for field in (
        "storage_root",
        "thumbnail_root",
        "descriptor_root",
        "plans_root",
        "outputs_root",
    ):
        link = _first_symlink_component(getattr(layout, field))
        if link is not None:
            raise ProjectLayoutError(f"{field} 含有符号链接成分：{link}")

    return layout
