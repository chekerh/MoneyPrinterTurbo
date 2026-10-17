"""跨平台发布适配器注册表。

`task.py` 不再直接调用某个平台的服务，而是把「视频 + 元数据 + 频道」交给这里，
由注册表决定这个 niche 上哪些平台已经就绪并逐个分发。新增平台只需要写一个
:class:`PublisherAdapter` 并 :func:`register`，发布链路的其余部分不用改。

约定
----
* 一个频道（niche）一份凭据。适配器的 ``connect`` 是唯一需要人操作的步骤，
  且只执行一次；之后 ``publish`` 必须是纯后台调用，不得打开浏览器窗口或
  抢占交互会话。
* ``publish`` 永远返回 ``{"success": bool, "result": {platform: {...}}}``，
  仅全部目标成功才返回 success=True，并保留每个平台的独立结果。
* 适配器不得吞掉异常——失败要以 ``{"success": False, "error": str(exc)}``
  的形式返回，因为发布状态会写入任务记录供用户查询。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from loguru import logger

from app.services import youtube_upload
from app.services.task_artifacts import _write_json_atomic
from app.services.upload_post import upload_post_service


@dataclass
class PublishMeta:
    """一次发布所需要的全部文案与可见性设置。

    ``title`` 与 ``caption`` 不是一回事：YouTube 有独立的长标题，而 TikTok /
    Instagram / Facebook 用的是带话题标签的短文案。把两者混成一个字段会导致
    YouTube 的标题被当成社交平台文案发出去。
    """

    #: 平台视频标题（YouTube ``snippet.title``）。
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy_status: str = "public"
    thumbnail_path: str = ""
    #: 跨平台共享的短文案，给 TikTok / Instagram / Facebook 用。
    caption: str = ""
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PublisherAdapter(Protocol):
    """所有发布后端都要满足的接口。"""

    #: 主平台标识，用于结果索引与 UI 展示。
    platform: str
    #: 该适配器覆盖的全部平台标识（含自身），用于生成平台化文案。
    platforms: list[str]

    def is_connected(self, channel: str) -> bool:
        """这个 niche 是否已经具备发布所需的凭据。"""

    def connect(self, channel: str) -> dict:
        """执行一次性授权，返回结构化结果。"""

    def describe(self, channel: str) -> dict:
        """返回凭据状态与远端账号信息，不修改任何状态。"""

    def publish(self, channel: str, video_path: str, meta: PublishMeta) -> dict:
        """把视频发布到该平台。"""


class YouTubeAdapter:
    """YouTube Data API v3，纯 HTTPS，不启动浏览器。"""

    platform = "youtube"
    platforms = ["youtube"]

    def is_connected(self, channel: str) -> bool:
        return youtube_upload.is_configured(channel)

    def connect(self, channel: str) -> dict:
        return youtube_upload.authorize(channel)

    def describe(self, channel: str) -> dict:
        return youtube_upload.channel_info(channel)

    def publish(self, channel: str, video_path: str, meta: PublishMeta) -> dict:
        return youtube_upload.publish_video(
            video_path,
            title=meta.title,
            description=meta.description,
            tags=meta.tags or None,
            privacy_status=meta.privacy_status,
            thumbnail_path=meta.thumbnail_path,
            channel=channel,
        )


class UploadPostAdapter:
    """upload-post.com 桥接通道，负责 TikTok / Instagram / Facebook。

    这些平台没有面向个人账号的公开发布 API，只能通过第三方托管账号，
    因此凭据是服务级的 API key 而不是按频道的 OAuth。频道维度仍然保留，
    这样以后换成官方 API 时只需替换这个适配器的 ``is_connected``/``publish``。
    """

    platform = "upload_post"
    platforms = ["tiktok", "instagram", "facebook"]

    def is_connected(self, channel: str) -> bool:
        return upload_post_service.is_configured()

    def connect(self, channel: str) -> dict:
        return {
            "success": False,
            "platform": self.platform,
            "error": (
                "Upload-Post is configured with an API key in config.toml "
                "(upload_post_enabled / upload_post_api_key / upload_post_username); "
                "there is no per-niche OAuth step."
            ),
        }

    def describe(self, channel: str) -> dict:
        return {
            "success": upload_post_service.is_configured(),
            "platform": self.platform,
            "enabled": upload_post_service.enabled,
            "configured": bool(
                upload_post_service.api_key and upload_post_service.username
            ),
            "auto_upload": upload_post_service.auto_upload,
            "platforms": list(upload_post_service.platforms),
        }

    def publish(self, channel: str, video_path: str, meta: PublishMeta) -> dict:
        # 只发 adapter 支持、且 config 里显式开启的平台。
        configured = set(upload_post_service.platforms or [])
        targets = [p for p in self.platforms if p in configured]
        wanted = set(meta.extra.get("platforms") or [])
        if wanted:
            targets = [p for p in targets if p in wanted]
        if not targets:
            return {
                "success": False,
                "platform": self.platform,
                "error": "no configured TikTok/Instagram/Facebook targets",
            }
        return upload_post_service.upload_video(
            video_path,
            # 社交平台用共享文案，没有文案时退回平台标题。
            meta.caption or meta.title,
            platforms=targets,
            youtube_extra=None,
        )


_ADAPTERS: dict[str, PublisherAdapter] = {}


def register(adapter: PublisherAdapter) -> None:
    _ADAPTERS[adapter.platform] = adapter


register(YouTubeAdapter())
register(UploadPostAdapter())


def adapters() -> list[PublisherAdapter]:
    return list(_ADAPTERS.values())


def get(platform: str) -> PublisherAdapter | None:
    return _ADAPTERS.get(platform)


def connected_platforms(channel: str) -> list[str]:
    """这个 niche 上所有可用平台的扁平标识列表。"""
    found: list[str] = []
    for adapter in adapters():
        try:
            if adapter.is_connected(channel):
                if adapter.platform == "upload_post":
                    found.extend(
                        p
                        for p in adapter.platforms
                        if p in upload_post_service.platforms
                    )
                else:
                    found.extend(adapter.platforms)
        except Exception as exc:  # noqa: BLE001 - 探测失败不应阻断发布判定
            logger.warning(
                f"platform {adapter.platform} probe failed for {channel!r}: {exc}"
            )
    return found


def connected_adapters(channel: str) -> list[PublisherAdapter]:
    return [adapter for adapter in adapters() if adapter.is_connected(channel)]


def describe(channel: str) -> dict[str, dict]:
    """所有平台的凭据状态，供 UI 一次性渲染。"""
    status: dict[str, dict] = {}
    for adapter in adapters():
        try:
            status[adapter.platform] = adapter.describe(channel)
        except Exception as exc:  # noqa: BLE001
            status[adapter.platform] = {"success": False, "error": str(exc)}
    return status


def connect(platform: str, channel: str) -> dict:
    adapter = get(platform)
    if adapter is None:
        return {"success": False, "error": f"unknown platform: {platform!r}"}
    try:
        return adapter.connect(channel)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"connect failed: platform={platform}, channel={channel!r}")
        return {"success": False, "platform": platform, "error": str(exc)}


def publish(
    channel: str,
    video_path: str,
    meta: PublishMeta,
    platforms: list[str] | None = None,
) -> dict:
    """把视频发到这个 niche 上所有已连接的平台。"""
    wanted = set(platforms) if platforms else None
    results: dict[str, dict] = {}

    for adapter in adapters():
        covered = set(adapter.platforms)
        if wanted is not None and not (covered & wanted):
            continue
        if not adapter.is_connected(channel):
            if wanted is not None and (covered & wanted):
                results[adapter.platform] = {
                    "success": False,
                    "error": f"{adapter.platform} is not connected for "
                    f"niche {channel!r}",
                }
            continue
        try:
            result = adapter.publish(channel, video_path, meta)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"publish failed: platform={adapter.platform}, channel={channel!r}"
            )
            result = {"success": False, "error": str(exc), "outcome_unknown": True}
        if isinstance(result, dict):
            results[adapter.platform] = result

    if not results:
        return {
            "success": False,
            "error": (
                f"no publishing channel configured for niche {channel!r} "
                f"(connect a platform first)"
            ),
        }
    success = all(r.get("success") for r in results.values())
    response = {"success": success, "result": results}
    if not success:
        response["error"] = "; ".join(
            f"{platform}: {r.get('error') or 'upload failed'}"
            for platform, r in results.items()
            if not r.get("success")
        )
    return response


def publish_once(
    channel: str, video_path: str, meta: PublishMeta, platforms: list[str] | None = None
) -> dict:
    """Persist successes across sessions and refuse concurrent/uncertain uploads.

    A lock left by a killed process requires checking the remote account before
    removing it. Never automatically repeat an upload with an unknown outcome.
    """
    video = Path(video_path)
    if not video.is_file():
        return {"success": False, "error": f"video file is missing: {video.name}"}
    wanted = list(
        dict.fromkeys(
            platforms if platforms is not None else connected_platforms(channel)
        )
    )
    if not wanted:
        return {
            "success": False,
            "error": f"No publishing account connected for {channel!r}. Connect YouTube in channel settings or configure Upload-Post.",
        }
    receipt_path = video.with_suffix(".publish.json")
    lock_path = video.with_suffix(".publish.lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {
            "success": False,
            "error": "Upload in progress or interrupted. Check the platform before clearing the .publish.lock file.",
        }
    except OSError as exc:
        return {"success": False, "error": f"Cannot create upload receipt lock: {exc}"}
    try:
        os.close(lock_fd)
        digest = hashlib.sha256()
        with video.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        identity = digest.hexdigest()
        receipt = {
            "version": 1,
            "channel": channel,
            "sha256": identity,
            "result": {},
            "pending": [],
            "completed_platforms": [],
        }
        if receipt_path.exists():
            try:
                saved = json.loads(receipt_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(saved, dict)
                    or saved.get("version") != 1
                    or not isinstance(saved.get("result"), dict)
                ):
                    raise ValueError("invalid structure")
                if saved.get("channel") != channel or saved.get("sha256") != identity:
                    raise ValueError("channel or video changed")
                if any(not isinstance(r, dict) for r in saved["result"].values()):
                    raise ValueError("invalid platform result")
                receipt = saved
            except (ValueError, TypeError) as exc:
                return {
                    "success": False,
                    "error": f"Invalid upload receipt; check the platform before retrying: {exc}",
                }
        if receipt.get("pending"):
            return {
                "success": False,
                "error": "Previous upload outcome is unknown; check the platform before retrying.",
            }
        results = receipt["result"]
        completed = receipt.get("completed_platforms", [])
        remaining = []
        for platform in wanted:
            adapter = next((a for a in adapters() if platform in a.platforms), None)
            backend = adapter.platform if adapter else platform
            previous = results.get(backend, {})
            if previous.get("outcome_unknown"):
                return {
                    "success": False,
                    "error": f"{backend}: previous upload outcome is unknown; check the platform before retrying.",
                    "result": results,
                }
            # Legacy receipts were per adapter. New receipts retain exact targets
            # so uploading TikTok cannot mark a future Instagram upload complete.
            already_done = (
                platform in completed
                if "completed_platforms" in receipt
                else bool(previous.get("success"))
            )
            if not already_done:
                remaining.append(platform)
        if remaining:
            receipt["pending"] = remaining
            _write_json_atomic(receipt_path, receipt)
            try:
                response = publish(
                    channel=channel,
                    video_path=video_path,
                    meta=replace(meta, extra={**meta.extra, "platforms": remaining}),
                    platforms=remaining,
                )
            except Exception:
                # Pending remains on disk if a provider throws or the process dies.
                raise
            if not isinstance(response, dict):
                return {
                    "success": False,
                    "error": "Invalid publishing response; upload outcome is unknown.",
                }
            response_results = response.get("result") or {}
            if (
                not isinstance(response_results, dict)
                or any(not isinstance(r, dict) for r in response_results.values())
                or (response.get("success") and not response_results)
            ):
                return {
                    "success": False,
                    "error": "Invalid publishing receipt response; upload outcome is unknown.",
                }
            results.update(response_results)
            for platform in remaining:
                adapter = next((a for a in adapters() if platform in a.platforms), None)
                backend = adapter.platform if adapter else platform
                if response_results.get(backend, {}).get("success"):
                    completed.append(platform)
            receipt["completed_platforms"] = list(dict.fromkeys(completed))
            receipt["pending"] = []
            _write_json_atomic(receipt_path, receipt)
            if not response.get("success"):
                return {**response, "result": results}
        return {"success": True, "result": results}
    except (OSError, ValueError) as exc:
        return {"success": False, "error": f"Cannot persist upload receipt: {exc}"}
    finally:
        lock_path.unlink(missing_ok=True)
