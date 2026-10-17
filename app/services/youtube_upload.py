"""每个频道一个 OAuth 凭据的 YouTube Data API v3 发布通道。

设计要点
--------
* **每个 niche 一个 token**：``storage/credentials/youtube/<channel>.json``。
  一个 Google 账号可以持有多个频道，但 OAuth token 与频道是**永久绑定**的，
  所以「登录一次」必须每个频道各做一次。refresh token 之后自动续期，
  平时不再需要打开浏览器。
* **绑定校验**：授权时用 ``channels.list(mine=True)`` 读出 token 实际绑定的
  频道并写进 sidecar 元数据；发布前再次比对。绑错频道会把内容发到错误的
  频道上，属于不可逆事故，所以这里宁可失败也不猜。
* **完全后台**：发布路径只有 HTTPS 请求，不启动任何浏览器，因此后台任务、
  Docker、锁屏状态都能正常工作，不会抢占正在使用的桌面。

后续接入 TikTok / Instagram 时，参见 :mod:`app.services.publishing` 的适配器约定。
"""

import json
import os
import re
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from loguru import logger

from app.config import config
from app.utils import utils

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
#: 授权时用 0 让系统分配空闲端口，避免连续连接多个频道时端口被占用。
_LOCAL_SERVER_PORT = 0
_UPLOAD_CHUNK_BYTES = 128 * 1024
#: YouTube 自定义缩略图上限 2 MB。
_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
#: token 文件里保存频道绑定信息用的键，写在 token 文件内而非 sidecar。
_META_KEY = "mpt_channel"
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(channel: str) -> str:
    """把频道名变成安全的目录名。"""
    cleaned = _SLUG_RE.sub("-", str(channel or "").strip().lower()).strip("-")
    return cleaned or "general"


def client_credentials() -> tuple[str, str]:
    return (
        str(config.app.get("youtube_upload_client_id", "") or "").strip(),
        str(config.app.get("youtube_upload_client_secret", "") or "").strip(),
    )


def credentials_root() -> str:
    return utils.storage_dir(os.path.join("credentials", "youtube"), create=True)


def token_path(channel: str) -> str:
    return os.path.join(credentials_root(), f"{_slug(channel)}.json")


def is_configured(channel: str = "") -> bool:
    """OAuth 客户端已配置，且（指定频道时）该频道已有 token。"""
    client_id, client_secret = client_credentials()
    if not (client_id and client_secret):
        return False
    if not channel:
        return bool(_connected_slugs())
    return os.path.exists(token_path(channel))


def _connected_slugs() -> list[str]:
    try:
        return sorted(
            name[:-5]
            for name in os.listdir(credentials_root())
            if name.endswith(".json")
        )
    except OSError:
        return []


def connected_channels() -> list[str]:
    """已保存 token 的频道列表（目录名形式，与 :func:`_slug` 一致）。"""
    return _connected_slugs()


def _write_token(path: str, credentials: Credentials, meta: dict) -> None:
    data = json.loads(credentials.to_json())
    data[_META_KEY] = meta
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.chmod(path, 0o600)


def _read_token(path: str) -> tuple[Credentials, dict]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    meta = data.pop(_META_KEY, {}) or {}
    return Credentials.from_authorized_user_info(data, YOUTUBE_SCOPES), meta


def authorize(channel: str, expected_channel_id: str = "") -> dict:
    """为 ``channel`` 走一次 OAuth 授权，并把 token 存到该频道名下。

    这是唯一需要打开浏览器的步骤，每个频道执行一次即可，之后发布全部走 API。
    授权完成后会立刻读出 token 实际绑定的频道；和 ``expected_channel_id``
    不一致时仍然保存，但把 ``channel_mismatch`` 标为 True 让调用方决定。
    """
    client_id, client_secret = client_credentials()
    if not client_id or not client_secret:
        raise ValueError(
            "youtube_upload_client_id and youtube_upload_client_secret must be set "
            "before authorizing (run ./setup_youtube.sh)"
        )

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                "http://localhost:%d" % _LOCAL_SERVER_PORT,
                "http://127.0.0.1:%d" % _LOCAL_SERVER_PORT,
            ],
        }
    }
    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=YOUTUBE_SCOPES
    )
    # prompt="consent" 每次都显示账号/频道选择页，这是能绑到不同频道的前提。
    credentials = flow.run_local_server(port=_LOCAL_SERVER_PORT, prompt="consent")

    target = token_path(channel)
    meta: dict = {
        "authorized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "niche": str(channel or ""),
    }
    _write_token(target, credentials, meta)

    bound = channel_info(channel)
    # 把实际绑定的频道写回 token 元数据，后续发布前据此发现漂移。
    if bound.get("success") and bound.get("channel_id"):
        meta["channel_id"] = bound["channel_id"]
        meta["channel_name"] = bound.get("channel_name")
        _write_token(target, credentials, meta)

    result = {
        "success": True,
        "channel": channel,
        "token": target,
        "channel_id": bound.get("channel_id"),
        "channel_name": bound.get("channel_name"),
    }
    logger.success(
        f"youtube authorized for niche {channel!r} -> "
        f"channel {bound.get('channel_name')!r} ({bound.get('channel_id')})"
    )

    if expected_channel_id and bound.get("channel_id") != expected_channel_id:
        result["channel_mismatch"] = True
        result["expected_channel_id"] = expected_channel_id
        logger.warning(
            f"niche {channel!r} expected channel {expected_channel_id} but the "
            f"token is bound to {bound.get('channel_id')!r}"
        )
    return result


def _service_for(channel: str):
    path = token_path(channel)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no YouTube credentials for niche {channel!r} "
            f"(run the connect flow first): {path}"
        )
    credentials, _meta = _read_token(path)
    if not credentials.valid:
        credentials.refresh(Request())
        # 只在过期后回写，保留 _META_KEY。
        try:
            with open(path, encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, ValueError):
            existing = {}
        existing.pop(_META_KEY, None)
        existing.update(json.loads(credentials.to_json()))
        existing[_META_KEY] = _meta
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(existing, handle)
        os.chmod(path, 0o600)
    return build("youtube", "v3", credentials=credentials)


def list_my_channels() -> dict:
    """列出这个 Google 账号可以发布到的全部频道。

    授权前用来帮用户确认「4 个频道分别叫什么」，授权后用来核对绑定关系。
    复用任意一个已保存的 token 即可查询。
    """
    slugs = _connected_slugs()
    if not slugs:
        return {
            "success": False,
            "error": "no connected niche yet — authorize one first",
            "channels": [],
        }
    try:
        service = _service_for(slugs[0])
        response = service.channels().list(
            part="id,snippet", mine=True, maxResults=50
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to list YouTube channels")
        return {"success": False, "error": str(exc), "channels": []}

    channels = [
        {
            "channel_id": item.get("id"),
            "channel_name": (item.get("snippet") or {}).get("title"),
        }
        for item in (response.get("items") or [])
    ]
    return {"success": True, "channels": channels}


def _bound_channel_id(channel: str) -> str:
    path = token_path(channel)
    if not os.path.exists(path):
        return ""
    _credentials, meta = _read_token(path)
    return str(meta.get("channel_id") or "")


def channel_info(channel: str) -> dict:
    if not is_configured(channel):
        return {
            "success": False,
            "channel": channel,
            "error": f"YouTube not connected for niche {channel!r}",
        }
    try:
        service = _service_for(channel)
        response = service.channels().list(
            part="id,snippet", mine=True, maxResults=1
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"failed to load YouTube channel info for {channel!r}")
        return {"success": False, "channel": channel, "error": str(exc)}

    items = response.get("items") or []
    if not items:
        return {
            "success": False,
            "channel": channel,
            "error": "no channel found for this account",
        }
    item = items[0]
    channel_id = item.get("id")
    expected = str(config.app.get("youtube_upload_channel_id", "") or "").strip()

    # 授权时记下的绑定频道是权威值，后续漂移都要显式报错。
    recorded = _bound_channel_id(channel)
    mismatch = bool(expected and channel_id != expected)
    if recorded and recorded != channel_id:
        mismatch = True

    return {
        "success": True,
        "channel": channel,
        "channel_id": channel_id,
        "channel_name": (item.get("snippet") or {}).get("title"),
        "expected_channel_id": expected or None,
        "channel_mismatch": mismatch,
    }


def upload_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list | None = None,
    privacy_status: str = "",
    category_id: str = "",
    thumbnail_path: str = "",
    channel: str = "",
) -> dict:
    if not os.path.isfile(file_path):
        return {"success": False, "error": f"video file does not exist: {file_path}"}

    info = channel_info(channel)
    if not info.get("success"):
        return {"success": False, "error": info.get("error")}
    if info.get("channel_mismatch"):
        return {
            "success": False,
            "error": (
                f"refusing to publish: token for niche {channel!r} is bound to "
                f"channel {info.get('channel_id')!r} "
                f"({info.get('channel_name')!r}) but {info.get('expected_channel_id')!r} "
                f"was expected. Reconnect this niche and pick the right channel."
            ),
        }

    service = _service_for(channel)
    privacy_status = privacy_status or str(
        config.app.get("youtube_upload_privacy_status", "public") or "public"
    )
    category_id = category_id or str(
        config.app.get("youtube_upload_category_id", "27") or "27"
    )

    body = {
        "snippet": {
            "title": (title or "")[:100],
            "description": description or "",
            "tags": [str(tag) for tag in (tags or [])][:500],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }
    media = MediaFileUpload(
        file_path,
        resumable=True,
        chunksize=_UPLOAD_CHUNK_BYTES,
        mimetype="video/mp4",
    )
    request = service.videos().insert(
        part="snippet,status", body=body, media_body=media
    )
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and status.progress() is not None:
            progress = int(status.progress() * 100)
            if progress % 10 == 0:
                logger.info(f"youtube upload progress [{channel}]: {progress}%")
    video_id = response.get("id")
    logger.success(
        f"youtube video uploaded [{channel}] -> {info.get('channel_name')} ({video_id})"
    )

    thumbnail_error = ""
    if thumbnail_path:
        # 自定义缩略图只能上传视频**之后**设置：YouTube 的 videos().insert
        # 不接受缩略图，必须拿到 video_id 再调 thumbnails().set。
        try:
            set_thumbnail(service, video_id, thumbnail_path)
            logger.success(f"youtube thumbnail set from: {thumbnail_path}")
        except Exception as exc:  # noqa: BLE001 - 缩略图失败不该让视频算失败
            thumbnail_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"youtube video uploaded but thumbnail failed: {thumbnail_error}"
            )

    return {
        "success": True,
        "channel": channel,
        "channel_name": info.get("channel_name"),
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail_set": bool(thumbnail_path) and not thumbnail_error,
        "thumbnail_error": thumbnail_error,
    }


def set_thumbnail(service, video_id: str, thumbnail_path: str) -> None:
    """给已上传的视频设置自定义缩略图。

    YouTube 要求缩略图是 JPG/PNG/GIF 且不超过 2 MB、宽高不低于 640x360。
    这里只做校验和上传，不做转码——超限时明确报错，而不是让 YouTube
    返回一个含义不明的 400。
    """
    path = os.path.abspath(os.path.expanduser(str(thumbnail_path)))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"thumbnail file does not exist: {path}")

    suffix = os.path.splitext(path)[1].lower()
    if suffix not in (".jpg", ".jpeg", ".png"):
        raise ValueError(
            f"unsupported thumbnail format {suffix!r}: "
            "YouTube accepts .jpg, .jpeg or .png"
        )

    size = os.path.getsize(path)
    if size > _THUMBNAIL_MAX_BYTES:
        raise ValueError(
            f"thumbnail is {size} bytes, over YouTube's "
            f"{_THUMBNAIL_MAX_BYTES} byte limit"
        )

    mime_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    media = MediaFileUpload(path, resumable=False, mimetype=mime_type)
    service.thumbnails().set(videoId=video_id, media_body=media).execute()


def publish_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list | None = None,
    privacy_status: str = "",
    category_id: str = "",
    thumbnail_path: str = "",
    channel: str = "",
) -> dict:
    if not is_configured(channel):
        hint = (
            f"no YouTube credentials for niche {channel!r}"
            if client_credentials()[0]
            else "YouTube API publishing is not configured (run ./setup_youtube.sh)"
        )
        return {"success": False, "error": hint}
    try:
        return upload_video(
            file_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            category_id=category_id,
            thumbnail_path=thumbnail_path,
            channel=channel,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"youtube upload failed for niche {channel!r}")
        # A network exception may occur after the remote upload completed.
        return {"success": False, "error": str(exc), "channel": channel, "outcome_unknown": True}


class YouTubeUploadService:
    def is_configured(self, channel: str = "") -> bool:
        return is_configured(channel)

    def connected_channels(self) -> list[str]:
        return connected_channels()

    def publish_video(self, *args, **kwargs) -> dict:
        return publish_video(*args, **kwargs)

    def channel_info(self, channel: str) -> dict:
        return channel_info(channel)

    def list_my_channels(self) -> dict:
        return list_my_channels()

    def authorize(self, channel: str, expected_channel_id: str = "") -> dict:
        return authorize(channel, expected_channel_id=expected_channel_id)


youtube_upload_service = YouTubeUploadService()
