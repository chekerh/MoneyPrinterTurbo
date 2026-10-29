"""赛道（niche）的运行时注册表。

此前赛道画像写死在 :mod:`app.services.niche_brand` 的 ``NICHES`` 字典里，
WebUI 只能从中选一个，新增/修改/删除赛道都得改代码、改 ``plans/`` 目录、
再重启。这个模块把赛道定义收敛成一份 JSON（``storage/niches.json``），
WebUI 直接 CRUD，运行时的 ``niche_brand`` 从这里读。

设计取舍：

* ``NicheProfile`` 还是定义在 :mod:`app.services.niche_brand` 里（避免破坏
  现有导入）；本模块只负责序列化与持久化。
* 存储文件**不存在**时，从内置的四个赛道种子一次；第一次写入时落盘。
  之后文件就是唯一真源——内置赛道在文件里删掉就不会再"幽灵复活"。
* ``topics`` / ``automation`` / ``system_prompt`` / ``content_style`` /
  ``channel_description`` 这些原来散落在 ``plans/<channel>/`` 的内容，也一并
  收进注册表，方便在页面上编辑；保存时会同步写回 ``plans/`` 目录，
  供 ``daily_youtube_short.sh`` 与 ``plans.py`` 继续使用（向后兼容）。
* 删除赛道时**只**移动 ``plans/<channel>`` 到 ``plans/_archive/``，从不碰
  ``storage/credentials``、``outputs/`` 与 ``logs/``，避免把已发布的成片或
  OAuth token 一起抹掉。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.utils.utils import storage_dir

STORE_VERSION = 1
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_AUTO_ENV_KEYS = (
    "VIDEO_ASPECT",
    "VOICE_NAME",
    "VOICE_RATE",
    "VIDEO_LANGUAGE",
    "PARAGRAPH_NUMBER",
    "BGM_TYPE",
    "BGM_VOLUME",
)

__all__ = [
    "NicheRegistryError",
    "load_profiles",
    "get_profile",
    "get_record",
    "upsert_niche",
    "delete_niche",
    "topics_for",
    "validate_channel",
    "sync_plan_files",
]


class NicheRegistryError(RuntimeError):
    """赛道注册表损坏、校验失败或不可写。"""


# --------------------------------------------------------------------------- #
# 路径与读写
# --------------------------------------------------------------------------- #


def _store_path() -> Path:
    override = os.environ.get("MPT_NICHE_STORE", "").strip()
    if override:
        return Path(override)
    return Path(storage_dir()) / "niches.json"


def _now() -> float:
    return time.time()


def _builtin_profiles() -> dict[str, Any]:
    # 延迟导入，避免与 niche_brand 的循环依赖。
    from app.services.niche_brand import _BUILTIN_NICHES, NicheProfile  # noqa: F401

    return dict(_BUILTIN_NICHES)


def _profile_to_dict(profile: Any) -> dict[str, Any]:
    from app.services.niche_brand import NicheProfile

    if not isinstance(profile, NicheProfile):
        raise NicheRegistryError("profile 必须是 NicheProfile")
    data = {
        "channel": profile.channel,
        "display_name": profile.display_name,
        "brand_line": profile.brand_line,
        "teaser_shapes": list(profile.teaser_shapes),
        "avoid": list(profile.avoid),
        "min_seconds": int(profile.min_seconds),
        "target_words": int(profile.target_words),
        "longform_target_seconds": int(profile.longform_target_seconds),
        "longform_band": str(profile.longform_band or ""),
    }
    return data


def _profile_from_dict(raw: Any) -> Any:
    from app.services.niche_brand import NicheProfile

    if not isinstance(raw, dict):
        raise NicheRegistryError("niche 记录必须是对象")
    try:
        profile = NicheProfile(
            channel=str(raw["channel"]).strip(),
            display_name=str(raw.get("display_name", "")).strip(),
            brand_line=str(raw.get("brand_line", "")).strip(),
            teaser_shapes=tuple(str(x).strip() for x in (raw.get("teaser_shapes") or []) if str(x).strip()),
            avoid=tuple(str(x).strip() for x in (raw.get("avoid") or []) if str(x).strip()),
            min_seconds=int(raw.get("min_seconds", 60) or 60),
            target_words=int(raw.get("target_words", 220) or 220),
            longform_target_seconds=int(raw.get("longform_target_seconds", 0) or 0),
            longform_band=str(raw.get("longform_band", "") or "").strip(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NicheRegistryError(f"niche 记录字段无效：{exc}") from exc
    # 校验放在构造之后，保证所有记录（含种子）都过同一道门。
    _validate_profile(profile, raw.get("longform_band", ""))
    return profile


def _validate_profile(profile: Any, _band_hint: str = "") -> None:
    from app.services import longform

    if not _CHANNEL_RE.match(profile.channel):
        raise NicheRegistryError(
            f"赛道 slug 不合法：{profile.channel!r}（只允许小写字母、数字与连字符，2-64 位）"
        )
    if not profile.display_name.strip():
        raise NicheRegistryError("display_name 不能为空")
    if not profile.brand_line.strip():
        raise NicheRegistryError("brand_line 不能为空")
    if not profile.teaser_shapes:
        raise NicheRegistryError("至少需要一条 teaser shape")
    if profile.min_seconds <= 0:
        raise NicheRegistryError("min_seconds 必须大于 0")
    if profile.target_words <= 0:
        raise NicheRegistryError("target_words 必须大于 0")
    band = (profile.longform_band or "").strip()
    if band and band not in ("short", "medium", "long"):
        raise NicheRegistryError(f"longform_band 不合法：{band!r}")
    if profile.longform_target_seconds:
        if not band:
            raise NicheRegistryError("设置了 longform_target_seconds 就必须指定 longform_band")
        length_band = longform.resolve_band(band)
        if not (
            length_band.min_seconds
            <= profile.longform_target_seconds
            <= length_band.max_seconds
        ):
            raise NicheRegistryError(
                f"longform_target_seconds {profile.longform_target_seconds} 超出 "
                f"{band} 档位范围 [{length_band.min_seconds}, {length_band.max_seconds}]"
            )


def validate_channel(channel: str) -> str:
    key = str(channel or "").strip().lower()
    if not _CHANNEL_RE.match(key):
        raise NicheRegistryError(
            f"赛道 slug 不合法：{channel!r}"
        )
    return key


def _load_raw() -> dict[str, Any]:
    path = _store_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NicheRegistryError(f"无法读取赛道注册表 {path}：{exc}") from exc
        if not isinstance(data, dict) or "niches" not in data:
            raise NicheRegistryError(f"赛道注册表结构无法识别：{path}")
        if not isinstance(data["niches"], dict):
            raise NicheRegistryError(f"niches 字段必须是对象：{path}")
        data.setdefault("version", STORE_VERSION)
        return data
    # 首次：用内置赛道种子，但不落盘——等第一次写入时再落。
    return {
        "version": STORE_VERSION,
        "niches": {
            key: _profile_to_dict(profile) for key, profile in _builtin_profiles().items()
        },
    }


def _save_raw(data: dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_profiles() -> dict[str, Any]:
    """返回 ``{channel: NicheProfile}``，合并注册表里的扩展字段由调用方取 raw。"""
    raw = _load_raw()
    out: dict[str, Any] = {}
    for key, value in (raw.get("niches") or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            value = dict(value)
            value.setdefault("channel", key)
            profile = _profile_from_dict(value)
        except NicheRegistryError:
            continue
        out[profile.channel] = profile
    if not out:
        # 注册表空了就回退到内置，避免整个应用失去赛道。
        out.update(_builtin_profiles())
    return out


def get_profile(channel: str) -> Any | None:
    profiles = load_profiles()
    key = str(channel or "").strip().lower().replace(" ", "-").replace("_", "-")
    return profiles.get(key)


def _clean_topic(topic: str) -> str:
    # 主题列表长期沿用手写编号（"1. 主题"）。``plans.load_topics`` 读的时候会
    # 剥掉编号，界面这边如果原样显示，主题选择框里就会出现一列 "1." "2."，
    # 选中的标题也会把编号带进视频主题。统一在这里剥一次，两处看到的才是同一句。
    return re.sub(r"^\s*\d+\s*[.)]\s*", "", str(topic)).strip()


def topics_for(channel: str) -> list[str]:
    raw = _load_raw()
    record = (raw.get("niches") or {}).get(str(channel or "").strip())
    stored: list[str] = []
    if isinstance(record, dict):
        topics = record.get("topics")
        if isinstance(topics, list):
            stored = [t for t in (_clean_topic(topic) for topic in topics) if t]
    if stored:
        return stored
    # 内置赛道在第一次保存之前，注册表里没有 topics，但作品集里往往已经有
    # plans/<channel>/topics_30day.txt。不回读的话，"用这个赛道做视频"对内置
    # 频道只能给出空主题列表——恰好是用户最想直接用的那几个。
    try:
        from app.services.project_layout import resolve_project_layout

        path = resolve_project_layout().plans_root / str(channel or "").strip() / "topics_30day.txt"
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001 - 没配作品集就只返回注册表内容
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(_clean_topic(line))
    return [topic for topic in out if topic]


def get_record(channel: str) -> dict[str, Any] | None:
    raw = _load_raw()
    record = (raw.get("niches") or {}).get(str(channel or "").strip())
    if not isinstance(record, dict):
        return None
    return dict(record)


def _portfolio_root() -> Path | None:
    try:
        from app.services.project_layout import resolve_project_layout

        return resolve_project_layout().portfolio_root
    except Exception:  # noqa: BLE001 - 没配作品集根就跳过文件同步
        return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _render_automation_env(automation: dict[str, Any] | None) -> str:
    if not isinstance(automation, dict):
        automation = {}
    lines = []
    for key in _AUTO_ENV_KEYS:
        if key in automation and str(automation.get(key, "")).strip() != "":
            lines.append(f"{key}={automation[key]}")
    # 保留用户自定义的额外键
    for key, value in automation.items():
        if key in _AUTO_ENV_KEYS:
            continue
        if str(value).strip() == "":
            continue
        lines.append(f"{key}={value}")
    return "\n".join(lines) + ("\n" if lines else "")


def sync_plan_files(
    channel: str,
    profile: Any,
    record: dict[str, Any],
) -> None:
    """把注册表里的内容同步到 ``plans/<channel>/``，供 shell 流水线使用。"""
    root = _portfolio_root()
    if root is None:
        return
    channel_dir = root / "plans" / channel
    channel_dir.mkdir(parents=True, exist_ok=True)

    topics = record.get("topics")
    if isinstance(topics, list):
        _write_text(channel_dir / "topics_30day.txt", "\n".join(str(t).strip() for t in topics if str(t).strip()) + "\n")

    automation = record.get("automation")
    if isinstance(automation, dict):
        _write_text(channel_dir / "automation.env", _render_automation_env(automation))

    for field, filename in (
        ("system_prompt", "system_prompt.txt"),
        ("content_style", "content_style.md"),
        ("channel_description", "channel_description.md"),
    ):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            _write_text(channel_dir / filename, value if value.endswith("\n") else value + "\n")

    # README 用 display_name 兜底，避免 plans.list_channels 因缺少 topics 而隐藏频道。
    readme = channel_dir / "README.md"
    if not readme.exists():
        _write_text(readme, f"# {profile.display_name or channel}\n")


def upsert_niche(
    profile: Any,
    *,
    topics: list[str] | None = None,
    automation: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    content_style: str | None = None,
    channel_description: str | None = None,
    enabled: bool = True,
) -> Any:
    from app.services.niche_brand import NicheProfile, refresh_niches

    if not isinstance(profile, NicheProfile):
        raise NicheRegistryError("profile 必须是 NicheProfile")
    _validate_profile(profile)
    channel = profile.channel

    raw = _load_raw()
    niches = raw.setdefault("niches", {})
    if not isinstance(niches, dict):
        raise NicheRegistryError("niches 字段必须是对象")
    existing = niches.get(channel) if isinstance(niches.get(channel), dict) else {}
    record: dict[str, Any] = _profile_to_dict(profile)
    record["enabled"] = bool(enabled)
    record["created_at"] = float(existing.get("created_at", _now())) if existing else _now()
    record["updated_at"] = _now()
    if topics is not None:
        record["topics"] = [str(t).strip() for t in topics if str(t).strip()]
    elif isinstance(existing.get("topics"), list):
        record["topics"] = list(existing["topics"])
    if automation is not None:
        record["automation"] = dict(automation)
    elif isinstance(existing.get("automation"), dict):
        record["automation"] = dict(existing["automation"])
    if system_prompt is not None:
        record["system_prompt"] = system_prompt
    elif isinstance(existing.get("system_prompt"), str):
        record["system_prompt"] = existing["system_prompt"]
    if content_style is not None:
        record["content_style"] = content_style
    elif isinstance(existing.get("content_style"), str):
        record["content_style"] = existing["content_style"]
    if channel_description is not None:
        record["channel_description"] = channel_description
    elif isinstance(existing.get("channel_description"), str):
        record["channel_description"] = existing["channel_description"]
    # 默认 automation：保证新赛道也能被 shell 流水线跑起来。
    if "automation" not in record:
        record["automation"] = {
            "VIDEO_ASPECT": "9:16",
            "VIDEO_LANGUAGE": "en-US",
            "PARAGRAPH_NUMBER": "1",
        }
    niches[channel] = record
    raw["niches"] = niches
    _save_raw(raw)
    sync_plan_files(channel, profile, record)
    refresh_niches()
    return profile


def delete_niche(channel: str) -> None:
    key = validate_channel(channel)
    raw = _load_raw()
    niches = raw.get("niches")
    if not isinstance(niches, dict):
        raise NicheRegistryError("niches 字段必须是对象")
    if key not in niches:
        raise NicheRegistryError(f"赛道不存在：{key}")
    del niches[key]
    raw["niches"] = niches
    _save_raw(raw)
    _archive_plan_dir(key)
    from app.services import niche_brand

    niche_brand.refresh_niches()


def _archive_plan_dir(channel: str) -> None:
    root = _portfolio_root()
    if root is None:
        return
    src = root / "plans" / channel
    if not src.exists():
        return
    archive = root / "plans" / "_archive"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = archive / f"{channel}-{stamp}"
    try:
        shutil.move(str(src), str(dst))
    except OSError:
        # 归档失败不阻塞删除注册表记录，但留下原目录也不致命。
        pass