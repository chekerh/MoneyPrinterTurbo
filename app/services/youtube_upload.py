import os

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
_LOCAL_SERVER_PORT = 8180
_UPLOAD_CHUNK_BYTES = 128 * 1024
#: YouTube 自定义缩略图上限 2 MB。
_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024


def client_credentials() -> tuple[str, str]:
    return (
        str(config.app.get("youtube_upload_client_id", "") or "").strip(),
        str(config.app.get("youtube_upload_client_secret", "") or "").strip(),
    )


def token_path() -> str:
    return os.path.join(utils.storage_dir("credentials", create=True), "youtube_token.json")


def is_configured() -> bool:
    client_id, client_secret = client_credentials()
    return bool(client_id and client_secret and os.path.exists(token_path()))


def authorize() -> dict:
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
            "redirect_uris": ["http://localhost:%d" % _LOCAL_SERVER_PORT],
        }
    }
    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=YOUTUBE_SCOPES
    )
    credentials = flow.run_local_server(
        port=_LOCAL_SERVER_PORT, prompt="consent"
    )
    target = token_path()
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())
    os.chmod(target, 0o600)
    logger.info(f"saved youtube credentials: {target}")
    return {"success": True, "token": target}


def get_service():
    client_id, client_secret = client_credentials()
    credentials = Credentials.from_authorized_user_file(token_path(), YOUTUBE_SCOPES)
    if not credentials.valid:
        credentials.refresh(Request())
        with open(token_path(), "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
    return build("youtube", "v3", credentials=credentials)


def channel_info() -> dict:
    if not is_configured():
        return {
            "success": False,
            "error": "YouTube API publishing is not configured (run ./setup_youtube.sh)",
        }
    try:
        service = get_service()
        response = service.channels().list(
            part="id,snippet", mine=True, maxResults=1
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to load YouTube channel info")
        return {"success": False, "error": str(exc)}
    items = response.get("items") or []
    if not items:
        return {"success": False, "error": "no channel found for this account"}
    item = items[0]
    return {
        "success": True,
        "channel_id": item.get("id"),
        "channel_name": (item.get("snippet") or {}).get("title"),
    }


def upload_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list | None = None,
    privacy_status: str = "public",
    category_id: str = "27",
    thumbnail_path: str = "",
) -> dict:
    if not os.path.isfile(file_path):
        return {"success": False, "error": f"video file does not exist: {file_path}"}
    service = get_service()
    body = {
        "snippet": {
            "title": (title or "")[:100],
            "description": (description or ""),
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
                logger.info(f"youtube upload progress: {progress}%")
    video_id = response.get("id")
    logger.success(f"youtube video uploaded: {video_id}")

    thumbnail_error = ""
    if thumbnail_path:
        # 自定义缩略图只能上传视频**之后**设置：YouTube 的 videos().insert
        # 不接受缩略图，必须拿到 video_id 再调 videos().update。
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
    if suffix not in (".jpg", ".jpeg", ".png", ".gif"):
        raise ValueError(
            f"unsupported thumbnail format {suffix!r}: "
            "YouTube accepts .jpg, .png or .gif"
        )

    size = os.path.getsize(path)
    if size > _THUMBNAIL_MAX_BYTES:
        raise ValueError(
            f"thumbnail is {size} bytes, over YouTube's "
            f"{_THUMBNAIL_MAX_BYTES} byte limit"
        )

    media = MediaFileUpload(path, resumable=False, mimetype=f"image/{suffix.lstrip('.')}")
    service.videos().update(videoId=video_id, part="snippet", media_body=media).execute()


def publish_video(
    file_path: str,
    title: str,
    description: str = "",
    tags: list | None = None,
    privacy_status: str = "public",
    category_id: str = "27",
    thumbnail_path: str = "",
) -> dict:
    if not is_configured():
        return {
            "success": False,
            "error": "YouTube API publishing is not configured (run ./setup_youtube.sh)",
        }
    try:
        return upload_video(
            file_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            category_id=category_id,
            thumbnail_path=thumbnail_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("youtube upload failed")
        return {"success": False, "error": str(exc)}


class YouTubeUploadService:
    def is_configured(self) -> bool:
        return is_configured()

    def publish_video(self, *args, **kwargs) -> dict:
        return publish_video(*args, **kwargs)

    def channel_info(self) -> dict:
        return channel_info()

    def authorize(self) -> dict:
        return authorize()


youtube_upload_service = YouTubeUploadService()