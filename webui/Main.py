import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import requests
import streamlit as st
from loguru import logger
from streamlit_tour import Tour

# WebUI 作为独立入口运行时，需要让项目根目录优先于第三方依赖，
# 避免依赖中的同名 app 包遮蔽 MoneyPrinterTurbo 自己的 app 包。
root_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

# 内容计划工具位于仓库 tools/ 目录。WebUI 复用同一份计划读取逻辑，保证
# 页面展示的待生成/已发布状态与 daily_youtube_short.sh 命令行流水线一致。
# plans / archive_video 现在随仓库一起提供，放在 <repo>/tools 下。
# 早先它们在**仓库之外**（上一级目录），于是本机能跑、干净克隆跑不了：
# 干净环境里 `import plans` 直接 ModuleNotFoundError，整个 WebUI 起不来。
# 仍然回退到上一级目录，是为了兼容还没迁移的本地工作区。
tools_dir = os.path.join(root_dir, "tools")
if not os.path.isdir(tools_dir):
    tools_dir = os.path.join(os.path.dirname(root_dir), "tools")
if tools_dir not in sys.path:
    sys.path.insert(0, tools_dir)

import plans
import archive_video

from app.config import config
from app.models import const
from app.services.project_layout import resolve_project_layout
from app.services.result_descriptor import (
    ResultDescriptorError,
    read_result_descriptor,
)
from app.services.thumbnail_files import ThumbnailFilesError
from webui.thumbnail_panel import (
    _should_request_refresh as should_refresh,
    render_thumbnail_panel,
    watch_for_completion,
)
from app.models.llm_provider import (
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    get_llm_provider,
    normalize_provider_override,
)
from app.models.schema import (
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
    VideoTransitionMode,
)
from app.services.plan_meta import plan_meta_path
from app.services import bgm as bgm_service
from app.services import (
    cache_manager,
    campaign as campaign_service,
    llm,
    loomloom,
    video,
    volcengine_seedance,
    voice,
    webui_task,
)
from app.services import elevenlabs_music as elevenlabs_music_service
from app.services import sonilo as sonilo_service
from app.services import subtitle_preview as subtitle_preview_service
from app.services import copyright_audit as copyright_audit_service
from app.services import orbs
from app.services import publish_jobs
from app.services import posting_package, youtube_upload
from app.services import video_poster as video_poster_service
from app.services import state as sm
from app.services import task as tm
from app.services import version_checker
from app.utils.logging_utils import configure_terminal_logger
from app.utils import utils

st.set_page_config(
    page_title="MoneyPrinterTurbo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="auto",
    menu_items={
        "Report a bug": "https://github.com/harry0703/MoneyPrinterTurbo/issues",
        "About": "# MoneyPrinterTurbo\nSimply provide a topic or keyword for a video, and it will "
        "automatically generate the video copy, video materials, video subtitles, "
        "and video background music before synthesizing a high-definition short "
        "video.\n\nhttps://github.com/harry0703/MoneyPrinterTurbo",
    },
)


# Streamlit 1.59 会在页面右上角默认展示 Deploy、skills nudge 等平台入口。
# MoneyPrinterTurbo 是面向终端用户的本地工具，这些入口会造成顶部大块空白，
# 也会让新用户误以为需要安装额外组件。这里统一隐藏 Streamlit 平台工具栏，
# 并压缩主容器顶部留白，只保留项目自己的标题、语言选择和业务设置区域。
style_file = Path(__file__).with_name("styles.css")
streamlit_style = f"<style>{style_file.read_text(encoding='utf-8')}</style>"
st.markdown(streamlit_style, unsafe_allow_html=True)
# 定义资源目录
font_dir = os.path.join(root_dir, "resource", "fonts")
song_dir = os.path.join(root_dir, "resource", "songs")
i18n_dir = os.path.join(root_dir, "webui", "i18n")
config_file = os.path.join(root_dir, "webui", ".streamlit", "webui.toml")
# 语言列表必须在会话状态初始化前可用，首次访问时才能把浏览器 locale 映射到
# 项目真正支持的语言；自动识别结果只进入当前会话，不修改全局配置。
locales = utils.load_locales(i18n_dir)
DEFAULT_CHATTERBOX_BASE_URL = "http://127.0.0.1:4123/v1"
DEFAULT_CHATTERBOX_MODEL = "chatterbox"
DEFAULT_CHATTERBOX_VOICES = ["default-Female"]
ONBOARDING_TOUR_KEY = "mpt-onboarding-v1"
CUSTOM_LLM_ENDPOINT_ID = "custom"
VOICE_MODE_TTS = "tts"
VOICE_MODE_UPLOAD = "upload"
VOICE_MODE_NONE = "none"
LOOMLOOM_MAX_POLL_FAILURES = 5
# Upload-Post 的 API Key 与发布用户分别在两个页面管理，并且发布用户名称
# 不等于登录邮箱。集中维护入口可以避免多语言文案各自硬编码 URL 后发生偏差，
# 也方便用户从 WebUI 直接完成首次配置和后续账号维护。
UPLOAD_POST_API_KEYS_URL = "https://app.upload-post.com/api-keys"
UPLOAD_POST_MANAGE_USERS_URL = "https://app.upload-post.com/manage-users"
# “默认”是 WebUI 专用哨兵，不会写入 config.toml，也不会传给 FFmpeg。
# 后端在 video_codec 未配置时继续采用稳定的 libx264；单独保留该哨兵可以区分
# “跟随项目默认策略”和“用户明确固定 libx264”，便于未来安全调整默认策略。
DEFAULT_VIDEO_CODEC_OPTION = "__default__"
DEFAULT_SUBTITLE_SETTINGS = {
    "subtitle_enabled": True,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "subtitle_position": "bottom",
    "custom_position": 70.0,
    "text_fore_color": "#FFFFFF",
    "font_size": 60,
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "subtitle_background_enabled": False,
    "subtitle_background_color": "#000000",
    "rounded_subtitle_background": False,
    "caption_word_highlight": True,
}
LOCAL_MATERIAL_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
}
CUSTOM_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
_FINAL_VIDEO_PATTERN = re.compile(
    r"^final-(?P<index>\d+)\.(?P<extension>mp4|mov|mkv|webm)$",
    re.IGNORECASE,
)
_DOWNLOAD_FILENAME_INVALID_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# ffprobe 超时。探测只读取文件头，正常短视频在亚秒级返回；超过 3 秒基本可以
# 判定为文件损坏或 ffprobe 异常，直接当作「无法解析」而不是拖住 2 秒轮询。
_MEDIA_PROBE_TIMEOUT_SECONDS = 3
# 成片写入完成后的静默窗口。任务面板每 2 秒轮询一次，窗口取 3 秒可以让
# 「刚写完就被扫到」和「还在写」这两种情况稳定区分开。
_VIDEO_SETTLE_SECONDS = 3
# 视频预览尺寸上限。竖屏成片按最大高度反推宽度，横屏成片按最大宽度封顶，
# 这样 9:16 和 16:9 都不会铺满整行，播放器也不会退化成固定高度的横向黑框。
_VIDEO_PLAYER_MAX_HEIGHT = 420
_VIDEO_PLAYER_MAX_WIDTH = 480
_VIDEO_PLAYER_MIN_WIDTH = 200
_VIDEO_PLAYER_FALLBACK_WIDTH = 240
_VIDEO_DIMENSIONS_CACHE: dict = {}
#: 尺寸缓存的条目上限。键里带字节数和 mtime，所以仍在写入的成片每次探测都产生
#: 一条新记录；不设上限的话，跑得越久占用越大。
_VIDEO_DIMENSIONS_CACHE_MAX_ENTRIES = 256
_RUNTIME_CONFIG_SECTIONS = {
    "app": config.app,
    "azure": config.azure,
    "chatterbox": config.chatterbox,
    "elevenlabs": config.elevenlabs,
    "minimax_tts": config.minimax_tts,
    "siliconflow": config.siliconflow,
    "fish_audio": config.fish_audio,
    "ui": config.ui,
}
# 设置预设与密钥备份使用各自的文件标识。导入时先校验 schema 和版本，
# 避免把任务记录、config.toml 或其它 JSON 误当成本功能的导出文件。
SETTINGS_PRESET_SCHEMA = "moneyprinterturbo.settings-preset"
SETTINGS_PRESET_VERSION = 1
SETTINGS_PRESET_FILE_NAME = "moneyprinterturbo-settings.json"
KEY_BACKUP_SCHEMA = "moneyprinterturbo.key-backup"
KEY_BACKUP_VERSION = 1
KEY_BACKUP_FILE_NAME = "moneyprinterturbo-keys.json"
# 预设只描述生成参数。素材、配音和配乐都是本机文件路径，预设通常要在另一台
# 机器或另一个容器里导入，带上这些路径只会指向不存在的文件。
PRESET_EXCLUDED_PARAM_KEYS = frozenset(
    {
        "video_materials",
        "custom_audio_file",
        "bgm_file",
    }
)
# 密钥按配置项名称后缀识别。新增 Provider 只要沿用现有命名，就会自动进入
# 备份，不需要再维护第二份密钥清单。
CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "api_keys",
    "api_token",
    "access_key",
    "secret_key",
    "speech_key",
)
# 只恢复密钥而不恢复配套配置项时，凭据仍然不可用。这些配套项与密钥一起备份。
CREDENTIAL_COMPANION_KEYS = {
    # Azure 语音必须同时知道区域。
    "azure": ("speech_region",),
    # Provider 的额外字段由 Registry 声明，例如 Cloudflare AI Gateway 的
    # Account ID 和 Gateway ID。只恢复 API Key 而丢掉这些字段时，换到另一台
    # 机器后该 Provider 仍然无法调用。从 Registry 读取可以让以后新增的
    # Provider 自动进入备份，不需要在这里维护第二份字段清单。
    "app": tuple(
        provider.config_key(field.config_suffix)
        for provider in LLM_PROVIDER_REGISTRY
        for field in provider.extra_fields
    ),
}

NON_LLM_COMPANION_KEYS = {
    "app": ("upload_post_username",)
}
# 同一个密钥在不同面板可能使用各自的控件 key：音频面板直接编辑 Gemini 和
# MiMo 的 LLM 密钥，胜算云密钥的控件没有 _input 后缀。恢复备份时必须清除
# 每一个别名，否则遗留的旧值会在下一次 rerun 覆盖刚刚恢复的密钥。
CREDENTIAL_WIDGET_STATE_ALIASES = {
    ("app", "gemini_api_key"): ("gemini_tts_api_key_input",),
    ("app", "mimo_api_key"): ("mimo_tts_api_key_input",),
    ("app", "loomloom_api_token"): ("loomloom_user_api_token",),
}
# ui 分区只保存界面偏好，不含任何凭据，备份时整体跳过。
KEY_BACKUP_EXCLUDED_SECTIONS = frozenset({"ui"})


# -----------------------------------------------------------------------------
# 启动配置、会话状态与本地化
# -----------------------------------------------------------------------------


def _set_runtime_config(section_name, key, value):
    """
    更新 WebUI 配置，但不等待正在生成视频的后台任务。

    后台任务结束前，配置层只保留同一配置项的最新值；任务释放配置锁时会自动
    应用并保存。页面控件值仍由 Streamlit session_state 维护，因此暂存期间的
    rerun 不会把用户刚输入的内容重置为旧配置。
    """
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    updated = config.update_config_nonblocking(config_section, key, value)
    if not updated:
        logger.debug(f"deferred WebUI config update: section={section_name}, key={key}")
    return updated


def _delete_runtime_config(section_name, key):
    """删除 WebUI 配置项；后台任务占用配置时延后执行。"""
    config_section = _RUNTIME_CONFIG_SECTIONS[section_name]
    deleted = config.delete_config_nonblocking(config_section, key)
    if not deleted:
        logger.debug(f"deferred WebUI config delete: section={section_name}, key={key}")
    return deleted


def _save_runtime_config():
    """请求保存 WebUI 配置；后台任务占用配置时立即返回。"""
    saved = config.try_save_config()
    if not saved:
        logger.debug("deferred WebUI config save until active task completes")
    return saved


def _saved_ui_choice(key, options, default):
    """读取一个持久化选择，并把旧配置或手工编辑的非法值降级为默认值。"""
    options = list(options)
    saved = config.ui.get(key, default)
    numeric_default = isinstance(default, (int, float)) and not isinstance(
        default, bool
    )
    # bool 是 int 的子类，``True == 1``。手工把数值选项写成 TOML
    # 布尔值时必须拒绝，不能让它伪装成第一个数值 option。
    if numeric_default and isinstance(saved, bool):
        return default
    for option in options:
        if saved == option:
            # 返回 options 中的真实值，顺便把 TOML 1.0 等价归一化为
            # 整数选项 1，避免下游参数类型随配置写法漂移。
            return option

    # TOML 中的数值通常保留原类型；仍兼容用户手工写成字符串的情况。
    if numeric_default and isinstance(saved, str):
        try:
            converted = type(default)(saved)
        except (TypeError, ValueError):
            converted = None
        for option in options:
            if converted == option:
                return option
    return default


def _saved_ui_number(key, default, minimum, maximum, number_type=float):
    """读取并限幅持久化数值，避免非法配置破坏 Streamlit slider。"""
    try:
        saved = config.ui.get(key, default)
        if isinstance(saved, bool):
            raise ValueError("boolean is not a numeric setting")
        value = number_type(saved)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError, OverflowError):
        value = default
    return min(maximum, max(minimum, value))


def _saved_ui_bool(key, default):
    """兼容 TOML 布尔值和常见手工字符串，拒绝含义不明的旧值。"""
    value = config.ui.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _saved_ui_color(key, default):
    """只把标准六位十六进制颜色传给 Streamlit color picker。"""
    value = str(config.ui.get(key, default) or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    return default


def _saved_ui_text(key, default="", max_length=None):
    """读取持久化文本并遵守对应 WebUI 控件的长度上限。"""
    value = str(config.ui.get(key, default) or default)
    if max_length is not None:
        value = value[:max_length]
    return value


def _run_llm_read_operation(operation_name, operation):
    """
    使用稳定的当前 LLM 配置执行只读请求，并避免等待视频生成任务。

    能立即取得配置锁时继续沿用原来的互斥保护；锁已被后台视频任务持有时，
    全局配置在任务结束前不会发生变化，因此可以安全复制当前配置，并叠加页面
    尚未落盘的 Provider、模型和密钥。这样新文案使用界面中的最新选择，同时
    不会改变正在生成的视频任务。
    """
    with config.try_runtime_config_lock() as lock_acquired:
        # 配置层在复制全局值和叠加待更新值期间持有队列锁，因此快照只能看到
        # 更新前或更新后的完整状态，不会混用两组 Provider 参数。
        app_config_snapshot = config.snapshot_config_with_pending(config.app)
        if lock_acquired:
            return operation(app_config_snapshot)

    logger.info(
        f"run read-only LLM operation with active task configuration: "
        f"operation={operation_name}"
    )
    return operation(app_config_snapshot)


def _parse_chatterbox_voices(voices):
    # Chatterbox 是自托管服务，音色列表由用户在 WebUI 中手动输入。
    # 这里统一兼容 TOML 数组和输入框里的逗号分隔字符串，避免下拉框、
    # 试听按钮和后续生成流程使用不同格式导致状态不一致。
    if isinstance(voices, str):
        return [v.strip() for v in voices.split(",") if v.strip()]
    return [str(v).strip() for v in voices or [] if str(v).strip()]


def _sync_chatterbox_config_from_session_state():
    # Streamlit 的按钮会触发整页 rerun，而 Chatterbox 配置输入框位于
    # “试听语音合成”按钮之后。如果试听时只读取 config.chatterbox，可能拿不到
    # 用户刚在输入框里填入的 base_url/model/voices。先从 session_state 同步一次，
    # 可以保证按钮逻辑和输入框显示逻辑使用同一份最新配置。
    _set_runtime_config(
        "chatterbox",
        "base_url",
        (
            st.session_state.get(
                "chatterbox_base_url_input",
                config.chatterbox.get("base_url") or DEFAULT_CHATTERBOX_BASE_URL,
            )
            or ""
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "api_key",
        st.session_state.get(
            "chatterbox_api_key_input", config.chatterbox.get("api_key", "")
        ),
    )
    _set_runtime_config(
        "chatterbox",
        "model_id",
        (
            st.session_state.get(
                "chatterbox_model_input",
                config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
            )
            or DEFAULT_CHATTERBOX_MODEL
        ).strip(),
    )
    _set_runtime_config(
        "chatterbox",
        "voices",
        _parse_chatterbox_voices(
            st.session_state.get(
                "chatterbox_voices_input",
                config.chatterbox.get("voices") or DEFAULT_CHATTERBOX_VOICES,
            )
        ),
    )


def _detect_audio_mime(audio_file: str, audio_bytes: bytes) -> str:
    # 有些 OpenAI-compatible TTS 服务，例如 travisvn/chatterbox-tts-api，
    # 即使请求 response_format=mp3，也会返回 WAV 内容。WebUI 试听如果固定
    # 使用 audio/mp3，浏览器可能无法播放，因此这里按文件头识别真实格式。
    header = audio_bytes[:12]
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or header[:2] in (
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
    ):
        return "audio/mp3"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    ext = os.path.splitext(audio_file)[1].lower()
    return {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(ext, "audio/mp3")


def _build_uploaded_file_path(uploaded_file, target_dir, allowed_extensions, prefix):
    """为浏览器上传文件生成受控的服务端保存路径。"""
    original_name = os.path.basename(str(uploaded_file.name or ""))
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in allowed_extensions:
        logger.warning(
            f"reject unsupported uploaded file extension: {original_name or '<empty>'}"
        )
        raise ValueError("unsupported uploaded file type")

    normalized_target_dir = os.path.realpath(target_dir)
    os.makedirs(normalized_target_dir, exist_ok=True)
    # 不复用浏览器传入的文件名，避免路径分隔符、控制字符或同名覆盖。UUID 只用于
    # 服务端落盘，不改变用户在上传控件中看到的原始名称。
    file_path = os.path.realpath(
        os.path.join(normalized_target_dir, f"{prefix}-{uuid4().hex}{extension}")
    )
    if os.path.commonpath([normalized_target_dir, file_path]) != normalized_target_dir:
        logger.warning(f"invalid uploaded file path: {file_path}")
        raise ValueError("invalid uploaded file path")
    return file_path


def _initialize_session_state():
    """集中初始化跨 rerun 保留的页面状态。"""
    if not st.session_state.get("cross_post_recovery_checked"):
        # WebUI 可以不经过 FastAPI 独立运行，因此也需要在首次会话初始化时处理
        # 进程重启留下的发布状态。恢复失败时不写标记，后续 rerun 会再次尝试。
        recovered = tm.recover_interrupted_cross_posts()
        if recovered is not None:
            st.session_state["cross_post_recovery_checked"] = True

    saved_ui_language = config.ui.get("language", "")
    browser_locale = st.context.locale
    initial_ui_language = utils.resolve_ui_language(
        saved_language=saved_ui_language,
        browser_locale=browser_locale,
        supported_languages=locales.keys(),
    )

    defaults = {
        "video_subject": "",
        "video_script": "",
        "video_terms": "",
        "paragraph_number_input": _saved_ui_number(
            "paragraph_number",
            1,
            llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
            llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
            int,
        ),
        "video_script_prompt": _saved_ui_text(
            "video_script_prompt",
            max_length=llm.MAX_SCRIPT_PROMPT_LENGTH,
        ),
        "custom_system_prompt": _saved_ui_text(
            "custom_system_prompt",
            llm.DEFAULT_SCRIPT_SYSTEM_PROMPT,
            llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
        ),
        "match_materials_to_script": bool(
            config.app.get("match_materials_to_script", False)
        ),
        "custom_bgm_file_input": _saved_ui_text("custom_bgm_file"),
        "sonilo_bgm_prompt_input": _saved_ui_text(
            "sonilo_bgm_prompt",
            max_length=sonilo_service.MAX_PROMPT_LENGTH,
        ),
        "elevenlabs_music_prompt_input": _saved_ui_text(
            "elevenlabs_music_prompt",
            max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
        ),
        "subtitle_enabled_checkbox": _saved_ui_bool("subtitle_enabled", True),
        "stroke_color_picker": _saved_ui_color("stroke_color", "#000000"),
        "stroke_width_slider": _saved_ui_number(
            "stroke_width", 1.5, 0.0, 10.0
        ),
        "loomloom_candidate_count": _saved_ui_number(
            "loomloom_candidate_count",
            3,
            1,
            loomloom.MAX_SCRIPT_CANDIDATES,
            int,
        ),
        "loomloom_script_duration_seconds": _saved_ui_number(
            "loomloom_script_duration_seconds", 60, 10, 600, int
        ),
        "ui_language": initial_ui_language,
        # 已落盘的本地素材允许用户只修改文案后继续复用。
        "local_video_materials": [],
        # 生成按钮回调先登记任务，使顶部入口能立即显示运行中数量。
        "active_generation_tasks": {},
        # 最近一次从当前页面提交的任务。生成改为后台执行后，页面 Fragment
        # 通过这个 ID 查询状态；刷新时不再依赖正在执行的旧页面脚本。
        "current_generation_task_id": "",
        # LoomLoom 询价与执行必须跨 Streamlit rerun 保留完全相同的输入和
        # clientRequestId，避免网络重试产生重复付费任务。
        "loomloom_script_batch": None,
        "loomloom_script_quote": None,
        "loomloom_script_input_signature": "",
        "loomloom_client_request_id": "",
        "loomloom_run_id": "",
        "loomloom_run_status": "",
        "loomloom_run_error": "",
        "loomloom_poll_failure_count": 0,
        "loomloom_poll_retry_after": 0.0,
        "loomloom_poll_paused": False,
        "loomloom_script_candidates": (),
        "loomloom_candidate_errors": (),
        "loomloom_selected_candidate": 0,
        "loomloom_video_batch": None,
        "loomloom_video_quote": None,
        "loomloom_video_input_signature": "",
        "loomloom_video_client_request_id": "",
        "loomloom_video_confirm_charge": False,
        "wavespeed_confirm_charge": False,
        "volcengine_seedance_confirm_charge": False,
        # AI 视频按素材段计费，默认只生成一段，用户确认效果后再主动增加数量。
        "loomloom_video_scene_count": _saved_ui_number(
            "loomloom_video_scene_count",
            1,
            1,
            loomloom.MAX_VIDEO_SCENES,
            int,
        ),
        # 活动内容工作台：保留跨 rerun 的抓取结果、简报与文案包。
        "campaign_source": None,
        "campaign_brief": None,
        "campaign_posts": None,
        "campaign_link_input": "",
        "campaign_post_count": 5,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


_initialize_session_state()


def tr(key):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key)
    if value is not None:
        return value
    # 新功能优先维护中英文。其它语言缺少单项翻译时统一回退英文，避免在多个
    # locale 中复制相同英文后长期失去同步；英文也没有该键时才显示原始 key。
    return locales.get("en", {}).get("Translation", {}).get(key, key)


# -----------------------------------------------------------------------------
# 任务管理：历史扫描、运行状态、参数恢复与列表交互
# -----------------------------------------------------------------------------


def _format_task_time(timestamp):
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _format_task_subject(subject, max_length=30):
    subject = str(subject or "").replace("\n", " ").strip()
    if len(subject) <= max_length:
        return subject or "-"
    return f"{subject[:max_length]}..."


def _safe_load_task_script(task_path):
    script_file = os.path.join(task_path, "script.json")
    if not os.path.isfile(script_file):
        return {}

    try:
        with open(script_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"failed to read task script data: {script_file}, {e}")
        return {}


def _probe_video_dimensions(video_file: str):
    """返回视频的 ``(宽, 高)``；文件缺失、损坏或 ffprobe 不可用时返回 ``None``。

    结果按「路径 + 大小 + mtime」缓存。同一文件在同一进程内只探测一次，因此
    2 秒轮询的任务面板不会反复起 ffprobe 子进程；文件被重写后缓存自动失效。
    """
    try:
        stat_result = os.stat(video_file)
    except OSError:
        return None

    cache_key = (video_file, stat_result.st_size, stat_result.st_mtime_ns)
    if cache_key in _VIDEO_DIMENSIONS_CACHE:
        return _VIDEO_DIMENSIONS_CACHE[cache_key]

    dimensions = None
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                video_file,
            ],
            capture_output=True,
            text=True,
            timeout=_MEDIA_PROBE_TIMEOUT_SECONDS,
        )
        width, height = (probe.stdout or "").strip().split("x")[:2]
        width, height = int(width), int(height)
        if width > 0 and height > 0:
            dimensions = (width, height)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # 截断或 0 字节的成片会让 ffprobe 无法解析出尺寸，这正是「文件已损坏」
        # 的信号，调用方据此拒绝播放而不是渲染一个坏掉的播放器。
        logger.debug(f"failed to probe video dimensions: {video_file}, {e}")

    _VIDEO_DIMENSIONS_CACHE[cache_key] = dimensions
    # 缓存键含 (路径, 字节数, mtime_ns)，因此**正在写入的成片每被探测一次就会
    # 留下一条新条目** —— 2 秒轮询 × 2 小时渲染 = 每部片子几千条，永不回收。
    # 这里按插入顺序淘汰最旧的，超过上限即收缩。尺寸探测很便宜（一次 ffprobe，
    # 已被上面的键去重），宁可偶尔重算也不能让内存随任务数无界增长。
    while len(_VIDEO_DIMENSIONS_CACHE) > _VIDEO_DIMENSIONS_CACHE_MAX_ENTRIES:
        oldest_key = next(iter(_VIDEO_DIMENSIONS_CACHE))
        del _VIDEO_DIMENSIONS_CACHE[oldest_key]
    return dimensions


def _video_player_width(video_file: str) -> int:
    """按视频真实宽高比换算播放器宽度，使预览不会被拉伸成巨大的黑框。

    ``st.video`` 只有在传入具体像素宽度时，浏览器才会按视频自身的宽高比推导
    高度；``width="stretch"`` 会得到一个固定高度的横向盒子，竖屏成片在其中被
    压成一条窄带。这里以固定的最大高度反推宽度，并限制上限，避免横屏视频
    铺满整行。
    """
    dimensions = _probe_video_dimensions(video_file)
    if not dimensions:
        return _VIDEO_PLAYER_FALLBACK_WIDTH
    width, height = dimensions
    scaled = round(_VIDEO_PLAYER_MAX_HEIGHT * width / height)
    return max(_VIDEO_PLAYER_MIN_WIDTH, min(_VIDEO_PLAYER_MAX_WIDTH, scaled))


def _render_video_player(video_file: str):
    """渲染一个按真实比例显示的视频播放器；文件损坏时给出明确提示。

    三处预览（任务面板、生成结果页、媒体库）共用这一个入口，保证比例上限和
    损坏提示完全一致，避免同一文件在不同面板表现不同。注意 ``st.video`` 不接受
    ``key`` 参数，多个播放器只能靠所在容器区分。
    """
    if not os.path.isfile(video_file):
        st.warning(tr("Video File Missing").format(name=os.path.basename(video_file)))
        return
    if _probe_video_dimensions(video_file) is None:
        st.error(tr("Video File Unreadable").format(name=os.path.basename(video_file)))
        return
    st.video(video_file, width=_video_player_width(video_file))


# 定义刻意放在**第一个使用点之前**（_task_final_video_dir）。
#
# Streamlit 脚本是自上而下执行的，而被 @st.cache_resource 包住的函数在
# "还没有 ScriptRunContext" 的裸执行模式（例如从别的进程 import 本模块、
# 或某些缓存重放路径）下会在定义当轮就真正执行一遍函数体。原来它定义在文件
# 中部，而 _task_final_video_dir 在它之前就被调用过，于是那条路径上拿到的
# 是一个尚未定义的全局名：NameError: '_cached_project_layout' is not defined。
# 把定义挪到使用点之前，这条脆弱的时序依赖就没有了。
@st.cache_resource(show_spinner=False)
def _cached_project_layout():
    """解析应用根与作品集根。作品集根缺失时不抛，交给调用方按不可用处理。"""
    try:
        return resolve_project_layout()
    except Exception as exc:  # noqa: BLE001 - 配置缺失是可预期状态
        logger.warning(f"project layout unavailable, thumbnails disabled: {exc}")
        return None


def _task_final_video_dir(task_id: str) -> str:
    """
    某个任务最终成片的权威目录：``<outputs_root>/<task_id>``。

    最终成片是**唯一长期留存的视频**，从 ``b2b52cc`` 起写在作品集的 ``outputs``
    下，而不是任务工作目录 —— 后者默认位于内存盘，任务结束就消失了。任务目录
    现在只留中间产物（``combined-1.mp4``、音频、字幕、脚本）。

    布局解析不可用时返回空串，调用方据此退回"成片可能还在任务目录"的老位置。
    """
    layout = _cached_project_layout()
    if layout is None:
        return ""
    return str(layout.outputs_root / str(task_id))


def _task_final_video_dirs(task_path: str) -> list[str]:
    """
    该任务的成片可能出现的位置，按权威性从高到低排列。

    先作品集里的成片目录（当前写入位置），再任务目录（``b2b52cc`` 之前的老任务
    仍可能把成片留在那里）。两处都不存在时调用方自然拿不到成片。
    """
    task_id = os.path.basename(os.path.normpath(task_path))
    candidates = []
    output_dir = _task_final_video_dir(task_id)
    if output_dir:
        candidates.append(output_dir)
    if task_path not in candidates:
        candidates.append(task_path)
    return candidates


def _is_allowed_task_video(candidate: str) -> bool:
    """
    成片是否位于允许读取/打开的根之内。

    打开与预览按钮的路径来自目录扫描或运行期状态，仍然必须限制在受控目录内，
    避免一次 UI 操作被异常路径扩展成任意本地文件读取能力。允许两个根：作品集
    里的成片目录（成片的实际位置）和任务目录（老任务）。两个根都由布局或应用
    自身的目录函数给出，不是用户可控的输入。
    """
    roots = []
    layout = _cached_project_layout()
    if layout is not None:
        roots.append(str(layout.outputs_root))
    roots.append(utils.task_dir())
    return any(_is_within_root(candidate, root) for root in roots)


def _find_final_task_video(task_path: str) -> str:
    """
    返回该任务序号最小的、且已确认写入完成的最终成片。

    合成流程还会产生 combined、temp-clip 和 MoviePy 临时文件，这些文件不能
    表示任务已成功完成，因此这里只接受 ``final-<序号>.<扩展名>``。

    搜索范围覆盖成片的两个可能位置：作品集里的 ``outputs/<task_id>/``（当前
    写入位置）和任务目录（老任务）。只搜任务目录会让归档整条链路静默失效 ——
    任务面板却仍然显示"已完成"，因为那一列读的是运行期状态而不是这里的返回值。

    仅凭文件名判断会让「刚开始写入」的成片立刻被当成完成态：ffmpeg 先创建
    目标文件，2 秒轮询的任务面板就会看到它，随后归档把半截文件复制进媒体库。
    因此这里额外要求文件可被 ffprobe 解析出尺寸，并确认两次轮询之间大小没有
    变化。代价是每个候选文件多一次 stat 与一次 ffprobe（结果有缓存），收益是
    归档链路不会再产出损坏成片。
    """
    for search_dir in _task_final_video_dirs(task_path):
        try:
            files = os.listdir(search_dir)
        except OSError:
            continue

        candidates = []
        for file_name in files:
            match = _FINAL_VIDEO_PATTERN.fullmatch(file_name)
            if match:
                candidates.append((int(match.group("index")), file_name))

        if not candidates:
            continue

        _, file_name = min(candidates, key=lambda item: item[0])
        video_file = os.path.join(search_dir, file_name)
        if _is_settled_video_file(video_file):
            return video_file

    return ""


def _is_settled_video_file(video_file: str) -> bool:
    """确认成片已经写完：可解析出尺寸，且大小在两次轮询之间保持稳定。

    单独的大小稳定性判断不足以排除「刚好写完但仍是坏文件」的情况，所以两者
    都要求。稳定窗口用文件 mtime 与当前时间的差值近似：写入中的文件 mtime 一直
    是「刚刚」，写完一段时间后才会超过阈值。
    """
    dimensions = _probe_video_dimensions(video_file)
    if dimensions is None:
        return False

    try:
        stat_result = os.stat(video_file)
    except OSError:
        return False

    if stat_result.st_size <= 0:
        return False

    # mtime 明显早于本次检查，说明文件在上一轮扫描之后没有再被写过。
    return (time.time() - stat_result.st_mtime) > _VIDEO_SETTLE_SECONDS


def _build_restore_upload_requirements(params: Mapping) -> dict:
    """
    记录历史任务中无法由 Streamlit 自动恢复的上传文件依赖。

    浏览器不允许程序重新填充 file_uploader，因此恢复任务时需要单独记录本地
    素材和自定义音频依赖，并在用户重新生成前检查是否已经主动补充或替换。
    """
    return {
        "local_materials": params.get("video_source") == "local",
        "custom_audio": bool(params.get("custom_audio_file")),
        "original_voice_name": params.get("voice_name") or "",
    }


def _get_unmet_restore_upload_requirements(
    requirements: Mapping | None,
    *,
    video_source: str,
    voice_name: str,
    has_local_materials: bool,
    has_custom_audio: bool,
    voice_mode: str | None = None,
) -> set[str]:
    """返回当前表单仍未满足的历史上传文件依赖。"""
    requirements = requirements or {}
    unmet = set()

    if (
        requirements.get("local_materials")
        and video_source == "local"
        and not has_local_materials
    ):
        unmet.add("local_materials")

    if requirements.get("custom_audio") and not has_custom_audio:
        if voice_mode is not None:
            # 新版 WebUI 使用显式配音方式。用户切换到自动配音或无配音，表示
            # 已主动替换历史上传音频；只有继续选择上传模式时才要求重新上传。
            if voice_mode == VOICE_MODE_UPLOAD:
                unmet.add("custom_audio")
        elif voice_name == requirements.get("original_voice_name", ""):
            # 保留旧调用方按音色判断的兼容行为，避免影响 API 和已有测试工具。
            unmet.add("custom_audio")

    return unmet


def _queue_task_restore(task_id):
    # 任务列表运行在 fragment 中，不能直接修改已经创建的主表单控件状态。
    # 这里只记录候选任务并触发整页 rerun，确认和参数恢复由主页面统一处理。
    st.session_state["task_restore_candidate_id"] = task_id
    st.session_state["task_manager_popover_nonce"] = (
        st.session_state.get("task_manager_popover_nonce", 0) + 1
    )
    st.rerun(scope="app")


def _normalize_task_state(state):
    if state in (
        const.TASK_STATE_COMPLETE,
        const.TASK_STATE_FAILED,
        const.TASK_STATE_PROCESSING,
    ):
        return state
    try:
        return int(state)
    except (TypeError, ValueError):
        return state


def _active_generation_tasks():
    tasks = st.session_state.setdefault("active_generation_tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
        st.session_state["active_generation_tasks"] = tasks
    return tasks


def _add_active_generation_task(task_id, subject=None):
    tasks = _active_generation_tasks()
    task = tasks.setdefault(task_id, {})
    task["subject"] = subject or task.get("subject") or task_id
    task["mtime"] = task.get("mtime") or datetime.now().timestamp()


def _remove_active_generation_task(task_id):
    tasks = _active_generation_tasks()
    if task_id in tasks:
        del tasks[task_id]
    if st.session_state.get("pending_generation_task_id") == task_id:
        del st.session_state["pending_generation_task_id"]


def _prepare_generation_task():
    # st.button 的 on_click 会在页面脚本重新执行前触发。这里提前生成任务 ID，
    # 顶部任务管理入口就能在同一次 rerun 中显示“生成中”数量。
    task_id = str(uuid4())
    st.session_state["pending_generation_task_id"] = task_id
    subject = st.session_state.get("video_subject") or st.session_state.get(
        "video_script"
    )
    _add_active_generation_task(task_id, subject=subject)


def _task_state_label(state, has_video):
    normalized_state = _normalize_task_state(state)
    if normalized_state == const.TASK_STATE_COMPLETE:
        return tr("Task Status Complete")
    if normalized_state == const.TASK_STATE_FAILED:
        return tr("Task Status Failed")
    if normalized_state == const.TASK_STATE_PROCESSING:
        return tr("Task Status Processing")
    if has_video:
        return tr("Task Status Complete")
    return tr("Task Status History")


def _task_state_filter_key(task):
    normalized_state = _normalize_task_state(task.get("state"))
    if normalized_state == const.TASK_STATE_PROCESSING:
        return "processing"
    if normalized_state == const.TASK_STATE_FAILED:
        return "failed"
    if normalized_state == const.TASK_STATE_COMPLETE or task["video_file"]:
        return "complete"
    return "history"


def _scan_history_tasks(limit=30):
    tasks_root = utils.task_dir()
    if not os.path.isdir(tasks_root):
        return []

    # 任务管理 fragment 每两秒刷新一次。先只读取低成本的目录元数据并截取最近
    # 的任务，再解析 script.json 和视频列表，避免历史任务很多时反复扫描全部内容。
    task_entries = []
    try:
        with os.scandir(tasks_root) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    task_entries.append(
                        (
                            entry.stat(follow_symlinks=False).st_mtime,
                            entry.name,
                            entry.path,
                        )
                    )
                except OSError as e:
                    # 单个任务目录可能正在被删除，不应因此让整个任务面板失效。
                    logger.debug(f"skip unavailable task directory: {entry.path}, {e}")
    except OSError as e:
        logger.warning(f"failed to scan task directory: {tasks_root}, {e}")
        return []

    task_entries.sort(key=lambda item: item[0], reverse=True)
    tasks = []
    for mtime, name, task_path in task_entries[:limit]:
        script_data = _safe_load_task_script(task_path)
        params_data = script_data.get("params", {}) if script_data else {}
        video_file = _find_final_task_video(task_path)
        subject = (
            params_data.get("video_subject")
            or script_data.get("script", "")[:40]
            or name
        )
        tasks.append(
            {
                "task_id": name,
                "subject": subject,
                "state": const.TASK_STATE_COMPLETE if video_file else None,
                "progress": 100 if video_file else 0,
                "mtime": mtime,
                "task_path": task_path,
                "video_file": video_file,
                "source": "history",
            }
        )

    return tasks


def _collect_task_summaries(limit=20):
    history_tasks = {task["task_id"]: task for task in _scan_history_tasks(limit=50)}

    try:
        runtime_tasks, _ = sm.state.get_all_tasks(1, 50)
    except Exception as e:
        logger.warning(f"failed to load runtime tasks: {e}")
        runtime_tasks = []

    for task in runtime_tasks:
        task_id = task.get("task_id", "")
        if not task_id:
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_task = history_tasks.get(task_id, {})
        video_files = task.get("videos") or []
        video_file = (
            video_files[0] if video_files else history_task.get("video_file", "")
        )
        subject = (
            task.get("video_subject")
            or history_task.get("subject")
            or (task.get("script", "")[:40] if task.get("script") else "")
            or task_id
        )

        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": subject,
            "state": task.get("state"),
            "cross_post_state": task.get("cross_post_state"),
            "progress": int(task.get("progress", 0) or 0),
            "mtime": os.path.getmtime(task_path)
            if os.path.isdir(task_path)
            else history_task.get("mtime", 0),
            "task_path": task_path,
            "video_file": video_file,
            "source": "runtime",
        }

    for task_id, active_task in _active_generation_tasks().items():
        history_task = history_tasks.get(task_id, {})
        if history_task and _task_state_filter_key(history_task) in {
            "complete",
            "failed",
        }:
            # 会话中的 active 标记只负责覆盖任务刚提交到状态存储前的极短窗口。
            # 后台任务结束后必须以真实终态为准，不能把失败任务重新显示为生成中。
            continue

        task_path = os.path.join(utils.task_dir(), task_id)
        history_tasks[task_id] = {
            "task_id": task_id,
            "subject": active_task.get("subject")
            or history_task.get("subject")
            or task_id,
            "state": const.TASK_STATE_PROCESSING,
            "progress": history_task.get("progress", 0),
            "mtime": active_task.get("mtime")
            or history_task.get("mtime", datetime.now().timestamp()),
            "task_path": task_path,
            "video_file": history_task.get("video_file", ""),
            "source": "active",
        }

    tasks = list(history_tasks.values())
    ordered = sorted(tasks, key=lambda item: item["mtime"], reverse=True)
    # 同时把**未截断的总数**带出去。只回传列表的话，调用方无从知道磁盘上到底
    # 有多少个任务，于是"显示 12 / 共 20"里的 20 其实是 limit 截断后的长度——
    # 磁盘上有 200 个任务时界面会说"共 20 个"，仍然在骗人。
    _TASK_SUMMARY_TOTAL = "task_summary_total"
    for summary in ordered:
        summary[_TASK_SUMMARY_TOTAL] = len(ordered)
    return ordered[:limit]


def _is_headless_server():
    # Docker 或无桌面的服务器部署中，WebUI 进程接触不到用户的桌面环境：
    # xdg-open / webbrowser 只会在容器内静默失败。此时应改为浏览器内预览
    # 视频、以路径提示代替打开目录。macOS/Windows 桌面部署不受影响。
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _is_within_root(candidate: str, root: str) -> bool:
    """判断 ``candidate`` 是否严格位于 ``root`` 之内。

    使用 ``realpath`` + ``commonpath`` 而不是字符串前缀比较：``storage/tasks``
    的前缀匹配会把同级的 ``storage/tasks-backup`` 误判为子目录。解析符号链接
    之后再比较，才能真正防止「用一个指向外部的软链接目录绕过限制」。
    """
    try:
        resolved_root = os.path.realpath(root)
        resolved_candidate = os.path.realpath(candidate)
    except OSError:
        return False
    if resolved_candidate == resolved_root:
        return False
    try:
        return os.path.commonpath([resolved_root, resolved_candidate]) == resolved_root
    except ValueError:
        # 不同盘符 / 不同挂载点时 commonpath 会抛 ValueError，视为越界。
        return False


def _reveal_in_file_manager(
    target: str,
    allowed_root: str,
    label: str,
    display_base: str,
) -> bool:
    """在系统文件管理器中打开 ``target``，并把它限制在 ``allowed_root`` 之内。

    这是任务目录和频道归档目录唯一的打开入口。历史上两处各写了一份，一份用
    ``webbrowser.open``、一份用 ``subprocess``，且归档那一版完全没有目录约束，
    于是两个按钮打开的地方不一样、越界检查也不一样。合并后两个入口共享同一
    套校验和同一套无桌面降级逻辑。

    ``allowed_root`` 是安全边界，``display_base`` 只是无桌面降级时用来算相对
    路径的基准。两者必须分开：任务目录的边界是 ``storage/tasks``，但要展示的
    路径从 ``storage`` 起算才有 ``./storage/tasks/<id>`` 这种可读形式；归档
    目录的边界和展示基准又刚好都是 ``outputs``。分开传入才能让两个入口都
    在根目录被替换（测试、挂载）时仍然正确。

    返回是否真的调用了系统打开动作；无桌面环境下降级为路径提示并返回 ``False``。
    """
    if not _is_within_root(target, allowed_root):
        logger.warning(f"refused to open path outside {allowed_root}: {target}")
        return False

    resolved = os.path.realpath(target)
    if not os.path.exists(resolved):
        return False

    if _is_headless_server():
        # 容器内拿不到宿主机桌面，但 storage / outputs 通常以卷挂载映射回宿主机，
        # 给出相对仓库根的路径就能定位到文件。
        # display_base 必须和 resolved 一样先解析符号链接：macOS 上 /var 是
        # /private/var 的软链接，只解析一边会让 relpath 变成一串 ../..。
        try:
            relative = os.path.relpath(resolved, os.path.realpath(display_base))
        except ValueError:
            relative = resolved
        st.toast(f"{label}: ./{relative}", icon=":material/folder_open:")
        return False

    # 打开目录时用系统文件管理器；目标是文件时交给系统默认程序。
    folder = resolved if os.path.isdir(resolved) else os.path.dirname(resolved)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", folder], stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            if os.path.isdir(resolved):
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", resolved])
        else:
            subprocess.Popen(["xdg-open", folder], stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.error(f"failed to open path in file manager: {resolved}, {e}")
        return False
    return True


def _task_display_base() -> str:
    """任务目录降级提示的相对路径基准，即仓库根目录。

    提示要展示成 ``./storage/tasks/<id>``，所以基准是仓库根而不是 ``storage``。
    任务目录比归档目录深一层（``storage/tasks`` vs ``outputs``），因此这里要
    往上退两级；两个入口的目标深度不同，基准不能靠统一的层级规则推导。
    """
    tasks_root = os.path.abspath(utils.task_dir())
    return os.path.dirname(os.path.dirname(tasks_root))


def _archive_display_base() -> str:
    """归档目录降级提示的相对路径基准，即仓库根目录（``outputs`` 的上一层）。"""
    return os.path.dirname(os.path.abspath(plans.OUTPUTS_DIR))


def _open_task_path(task_path):
    """打开任务工作目录（``storage/tasks/<id>``），内含成片与全部中间产物。"""
    return _reveal_in_file_manager(
        task_path,
        utils.task_dir(),
        tr("Open Task Folder"),
        _task_display_base(),
    )


def _open_archive_folder_for_task(task_path):
    """打开任务已归档到的频道目录（``outputs/<channel>/<YYYY-MM>/videos``）。

    归档目录才是长期保存、用于发布的位置；任务目录可以随时被清理。任务尚未
    归档时没有可打开的目标，返回 ``False`` 让调用方禁用该按钮。
    """
    archive_dir = _resolve_task_archive_dir(task_path)
    if not archive_dir:
        return False
    return _reveal_in_file_manager(
        archive_dir,
        plans.OUTPUTS_DIR,
        tr("Open Archive Folder"),
        _archive_display_base(),
    )


@st.cache_data(show_spinner=False, max_entries=256)
def _resolve_task_archive_dir(task_path: str) -> str:
    """由任务目录反查它归档到的频道视频目录；未归档或已丢失时返回空串。

    任务目录里的 ``plan_meta.json`` 记录了 channel / topic，而归档文件名同时
    包含计划序号、归档日期和主题 slug。这里不重新推导文件名，而是直接在归档
    根目录里查找与任务 ``task_id`` 关联的 ``.txt`` 边车文件——边车里就有
    ``task_id``，因此这是唯一不需要猜测的关联方式。

    结果按 ``task_path`` 记忆化。任务面板每 2 秒刷新一次，一行一次调用，于是
    每个任务每分钟触发 30 次 ``os.walk`` + 整棵频道树里所有 ``.txt`` 边车的读取
    ——12 行就是每分钟 360 次遍历、上千次文件读，而结果只在"某个视频被归档"时
    才变。缓存把它压到每任务每次会话一次。

    缓存会在归档新成片后失效：归档路径只写 ``.plan_archived``，不碰这里的键，
    所以 ``_auto_archive_plan_tasks`` 成功后必须显式清理，否则用户刚看到归档
    结果，"打开归档目录"仍然会灰着。
    """
    meta_path = os.path.join(task_path, "plan_meta.json")
    channel = ""
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                channel = str((json.load(handle) or {}).get("channel") or "").strip()
        except (OSError, ValueError):
            channel = ""
    if not channel:
        return ""

    task_id = os.path.basename(os.path.normpath(task_path))
    if not task_id:
        return ""

    channel_root = plans._outputs_channel_dir(channel)
    if not os.path.isdir(channel_root):
        return ""

    for dirpath, _dirnames, filenames in os.walk(channel_root):
        if os.path.basename(os.path.normpath(dirpath)) != "videos":
            continue
        for name in filenames:
            if not name.endswith(".txt"):
                continue
            sidecar = os.path.join(dirpath, name)
            try:
                with open(sidecar, "r", encoding="utf-8") as handle:
                    body = handle.read()
            except OSError:
                continue
            if f"task_id: {task_id}" in body:
                return dirpath
    return ""


def _open_task_video(video_file):
    normalized_file = os.path.realpath(video_file)

    # 视频路径来自任务目录扫描或运行期状态。这里仍然限制只能打开受控根内的
    # 文件（作品集的成片目录与任务目录），避免 UI 操作被异常路径扩展成任意
    # 本地文件打开能力。
    if not _is_allowed_task_video(normalized_file):
        logger.warning(f"invalid task video path: {normalized_file}")
        return
    if not os.path.isfile(normalized_file):
        logger.warning(f"task video does not exist: {normalized_file}")
        return

    if _is_headless_server():
        # 无桌面环境时在任务面板内嵌播放器预览，代替调用系统播放器。
        st.session_state["task_preview_video_file"] = normalized_file
        return

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", normalized_file])
        elif sys.platform.startswith("win"):
            os.startfile(normalized_file)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", normalized_file])
    except Exception as e:
        logger.error(f"failed to open task video: {normalized_file}, {e}")


def _delete_task(task_id, task_path, task_state=None):
    # 页面展示的状态可能落后于后台任务。删除前同时检查传入状态、当前会话的
    # 活跃任务和最新状态，避免任务刚开始或已产出中间视频时被误删。
    current_task = None
    try:
        current_task = sm.state.get_task(task_id)
    except Exception as e:
        logger.exception(f"failed to verify task state before deletion: {task_id}, {e}")
        return False

    task_snapshot = dict(current_task or {})
    task_snapshot.setdefault("state", task_state)
    if task_id in _active_generation_tasks():
        task_snapshot["state"] = const.TASK_STATE_PROCESSING

    if tm.is_task_busy(task_snapshot):
        logger.warning(f"refused to delete running task: {task_id}")
        return False

    tasks_root = os.path.abspath(utils.task_dir())
    normalized_path = os.path.abspath(task_path)

    # 删除任务会移除任务状态和本地生成文件。这里必须限定在 storage/tasks
    # 下，避免异常 task_path 造成误删其它本地目录。
    if not normalized_path.startswith(tasks_root + os.sep):
        logger.warning(f"invalid task folder path for deletion: {normalized_path}")
        return False

    try:
        if hasattr(sm.state, "delete_task"):
            sm.state.delete_task(task_id)
        if os.path.isdir(normalized_path):
            shutil.rmtree(normalized_path)
        video_removed, video_note = _delete_task_output_video(task_id)
        logger.info(f"deleted task: {task_id}, removed_output_video={video_removed}")
        st.session_state["task_delete_video_note"] = video_note
        return True
    except Exception as e:
        logger.exception(f"failed to delete task: {task_id}, {e}")
        return False


def _delete_task_output_video(task_id: str) -> tuple[bool, str]:
    """
    删除任务时一并清理作品集里的成片目录 ``outputs/<task_id>/``。

    不清理的话，每次删除任务都会在 SSD 上留下 28–93 MB 的孤儿：任务目录没了，
    而 ``_scan_history_tasks`` 只扫任务目录、``list_channel_videos`` 只扫名为
    ``videos`` 的目录，于是这个文件从**每一个**界面都再也够不着。

    但**未归档的成片不能删**：成片是唯一长期留存的视频，一次渲染要两个多小时。
    只有当自动归档已经把一份副本放进频道媒体库时，原始副本才可以一起清掉；
    否则保留，并把这件事告诉用户，由他们决定。``.plan_skipped`` 标记代表这个
    任务被主动丢弃（主题在别处已录制），那种情况下同样有一份媒体库副本。

    返回 ``(是否已删除, 给用户看的说明)``。
    """
    layout = _cached_project_layout()
    if layout is None:
        return False, tr("Task Delete Kept Video No Layout")

    video_dir = layout.outputs_root / str(task_id)
    if not video_dir.is_dir():
        return False, ""

    task_path = os.path.join(utils.task_dir(), str(task_id))
    has_library_copy = any(
        os.path.isfile(os.path.join(task_path, marker))
        for marker in (".plan_archived", ".plan_skipped")
    )
    if not has_library_copy:
        # 唯一的副本还在这里，删掉就等于丢掉两个多小时的渲染结果。
        return False, tr("Task Delete Kept Video Unarchived").format(
            path=str(video_dir)
        )

    shutil.rmtree(video_dir, ignore_errors=True)
    return True, tr("Task Delete Removed Video")


def _count_processing_tasks(tasks):
    # 顶部任务管理入口只需要展示“生成中”任务数量。
    # 这里复用内部状态 key 判断，避免依赖多语言展示文案导致不同语言下统计不一致。
    processing_task_ids = {
        task["task_id"]
        for task in tasks
        if _task_state_filter_key(task) == "processing"
    }
    return len(processing_task_ids)


def _get_task_queue_position(task, all_tasks):
    """获取任务在队列中的位置（仅针对处理中/等待中的任务）。"""
    if _task_state_filter_key(task) not in ("processing", "pending", "queued"):
        return 0
    position = 0
    for t in all_tasks:
        if _task_state_filter_key(t) in ("processing", "pending", "queued"):
            position += 1
            if t["task_id"] == task["task_id"]:
                return position
    return 0


def _estimate_task_eta(task):
    """估算任务剩余时间（基于进度和已耗时）。"""
    progress = task.get("progress", 0)
    mtime = task.get("mtime", 0)
    if progress <= 0 or progress >= 100 or mtime <= 0:
        return ""
    elapsed = time.time() - mtime
    if elapsed < 10:  # 太短不显示
        return ""
    estimated_total = elapsed * 100 / progress
    remaining = max(0, estimated_total - elapsed)
    if remaining < 60:
        return f"(~{int(remaining)}s)"
    elif remaining < 3600:
        return f"(~{int(remaining/60)}m)"
    else:
        return f"(~{int(remaining/3600)}h{int((remaining%3600)/60)}m)"


def _get_task_thumbnail(task):
    """获取任务的缩略图路径（优先使用输出目录中的缩略图，其次任务目录）。"""
    task_path = task.get("task_path", "")
    video_file = task.get("video_file", "")
    
    # 优先检查输出目录中的缩略图
    if video_file and os.path.isfile(video_file):
        video_dir = os.path.dirname(video_file)
        base_name = os.path.splitext(os.path.basename(video_file))[0]
        thumb_path = os.path.join(video_dir, f"{base_name}.jpg")
        if os.path.isfile(thumb_path):
            return thumb_path
        # 尝试其他常见缩略图格式
        for ext in ('.png', '.webp'):
            alt_path = os.path.join(video_dir, f"{base_name}{ext}")
            if os.path.isfile(alt_path):
                return alt_path
    
    # 回退到任务目录中的缩略图
    if task_path and os.path.isdir(task_path):
        for ext in ('.jpg', '.png', '.webp'):
            for fname in os.listdir(task_path):
                if fname.lower().endswith(ext) and 'thumb' in fname.lower():
                    return os.path.join(task_path, fname)
    
    return None


def _task_manager_label(processing_count):
    label = tr("Task Manager")
    if processing_count <= 0:
        return label
    return f"{label} · {processing_count}"


def _artifact_base_url() -> str:
    """产物类端点（/download、/tasks）所在的服务器地址。

    部署形态是**两个进程**：``python main.py`` 起 uvicorn，挂载 API、``/download``
    与 ``/tasks``，并在 ``/`` 上提供上游静态落地页；而 WebUI 是单独用
    ``streamlit run webui/Main.py`` 起的另一个端口。``app/asgi.py`` 从头到尾**没有**
    把 Streamlit 挂进去。

    因此 WebUI 里的相对链接 ``/download?...`` 打在 WebUI 自己的端口上，命中的是
    Streamlit 的 HTML 兜底页：状态码 200、内容是整份 index.html。点"下载成片"
    存下来的是一个网页，而不是视频——而且它看起来是成功的。
    """
    from app.utils.access_policy import resolve_bind_host

    try:
        host = resolve_bind_host(
            config.listen_host,
            allow_non_loopback=bool(
                getattr(config, "allow_non_loopback_bind", False)
            ),
        )
    except Exception:  # noqa: BLE001 - 拿不到就退回回环地址
        host = "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 - 通配地址不能写进 URL
        host = "127.0.0.1"
    return f"http://{host}:{config.listen_port}"


def _generated_video_download_url(video_path: str, file_name: str | None = None) -> str:
    """成片的流式下载链接。

    ``path`` 来自浏览器端，属于不可信输入，因此这里只做百分号编码；真正的路径校验
    在 /download 端点里（必须是允许根目录下的真实文件）。``file_name`` 只是给
    浏览器一个建议文件名，同样不参与定位。
    """
    from urllib.parse import quote

    url = f"{_artifact_base_url()}/download?path={quote(video_path)}"
    if file_name:
        # st.link_button 没有 file_name 参数（传了会直接 TypeError），所以建议文件名
        # 只能走 URL 交给端点，由它交给 FileResponse 当作 Content-Disposition。
        url = f"{url}&file_name={quote(file_name)}"
    return url


def _build_video_download_name(subject, index, total):
    """根据视频主题生成跨平台安全的下载文件名。"""
    safe_subject = _DOWNLOAD_FILENAME_INVALID_PATTERN.sub(" ", str(subject or ""))
    safe_subject = re.sub(r"\s+", " ", safe_subject).strip(" .")[:80].rstrip(" .")
    if not safe_subject:
        safe_subject = "video"

    suffix = f"-{index}" if total > 1 else ""
    return f"{safe_subject}{suffix}.mp4"


# 任务表列宽。表头与每一行必须引用同一份，否则某一列只出现在表头或只出现在
# 行里，视觉上就会整体错位。
#
# popover 宽 1000px，扣掉列间距后可用约 900px。每一列都按表头文字的实测宽度
# 留够余量：「Queue #」「Thumbnail」窄于文字就会断成「Queu / e #」，
# 「Completed」「2026-09-27 19:32」同理。操作列要放下 5 个 44px 触达目标
# （含间距约 250px），主题列拿剩余空间并用单行省略号消化长标题。
# 旧版把操作按钮渲染进了「主题」列，导致表头的「操作」列悬空、按钮被挤在
# 主题文字下方；这里把宽度让给操作列后两者各自独立成列。
_TASK_TABLE_COLUMNS = [0.6, 0.85, 1.0, 1.3, 2.0, 0.75, 2.7]

# 单次渲染的最大行数。12 行是刻意压低的：每行会触发 _resolve_task_archive_dir 的
# 目录遍历，而这个面板每 2 秒刷新一次（见 _render_task_manager_entry）。上限必须
# 是个具名常量而不是裸数字 —— 它现在同时被"显示多少行"和"告知用户藏了多少行"
# 两处引用，裸数字迟早会只改一处。
_TASK_TABLE_ROW_LIMIT = 12


def _render_task_table(filtered_tasks, key_prefix):
    with st.container(key=f"task_table_header_{key_prefix}"):
        header_cols = st.columns(_TASK_TABLE_COLUMNS, vertical_alignment="center")
        header_cols[0].caption(tr("Queue #"))
        header_cols[1].caption(tr("Thumbnail"))
        header_cols[2].caption(tr("Task Status"))
        header_cols[3].caption(tr("Task Updated At"))
        header_cols[4].caption(tr("Task Subject"))
        header_cols[5].caption(tr("Task Progress"))
        header_cols[6].caption(tr("Task Actions"))

    if not filtered_tasks:
        st.info(tr("No Tasks Match Filter"))
        return

    visible_tasks = filtered_tasks[:_TASK_TABLE_ROW_LIMIT]
    # 表格只渲染前 12 行，但必须说清楚被藏了多少行。不说的话，筛出 40 个任务时
    # 界面只显示 12 个且毫无提示，看起来就像"只有 12 个任务"——用户会以为剩下的
    # 任务丢了，或者以为筛选没生效。
    #
    # total 取自未截断的总数，而不是 len(filtered_tasks)：后者已经被
    # _collect_task_summaries 的 limit 截断过，直接用会得到"共 20 个"这种
    # 仍然是假话的提示。筛选态下没有对应的全量计数，此时如实退回到筛选后的长度。
    total = max(
        (int(task.get("task_summary_total") or 0) for task in filtered_tasks),
        default=0,
    )
    hidden_total = total if total >= len(filtered_tasks) else len(filtered_tasks)
    if hidden_total > len(visible_tasks):
        st.caption(
            tr("Showing Newest Of Tasks").format(
                shown=len(visible_tasks), total=hidden_total
            )
        )
    list_height = min(390, max(96, len(visible_tasks) * 58))
    with st.container(height=list_height, border=False):
        for idx, task in enumerate(visible_tasks):
            task_id = task["task_id"]
            has_video = bool(task["video_file"] and os.path.isfile(task["video_file"]))
            is_processing = _task_state_filter_key(task) == "processing"
            is_busy = is_processing or tm.is_task_busy(task)
            has_restore_data = os.path.isfile(
                os.path.join(task["task_path"], "script.json")
            )
            archive_dir = _resolve_task_archive_dir(task["task_path"])
            safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

            # 使用 Streamlit 原生 bordered container + columns 保留每行操作。
            # 相比自定义 HTML/CSS 表格，这种方式对 Streamlit 版本变更更稳；
            # 相比 dataframe，又能保留播放、打开目录、删除等行内动作。
            with st.container(
                key=f"task_row_{key_prefix}_{safe_task_key}", border=True
            ):
                row_cols = st.columns(
                    _TASK_TABLE_COLUMNS,
                    vertical_alignment="center",
                )
                # Queue position (only for processing/queued tasks)
                queue_pos = _get_task_queue_position(task, filtered_tasks)
                row_cols[0].write(f"#{queue_pos}" if queue_pos else "—")
                
                # Thumbnail preview
                thumbnail_path = _get_task_thumbnail(task)
                if thumbnail_path and os.path.isfile(thumbnail_path):
                    row_cols[1].image(thumbnail_path, width=60)
                else:
                    row_cols[1].write("—")
                
                # 正在跑的任务给一个动效，而不是又一行"处理中"文字。
                #
                # 状态列已经有文字说明了，动效在这里承担的是另一件事：**指示这一行还
                # 活着**。任务面板每两秒刷新一次，纯文字的"处理中"看上去和"卡住了"
                # 一模一样；一个在动的东西才说明还在推进。
                if _task_state_filter_key(task) == "processing":
                    state_cols = st.columns([1, 5], vertical_alignment="center")
                    with state_cols[0]:
                        orbs.orb("generate", size=20, label=tr("Task Status Processing"))
                    state_cols[1].write(
                        _task_state_label(task["state"], has_video)
                    )
                else:
                    row_cols[2].write(_task_state_label(task["state"], has_video))
                row_cols[3].write(_format_task_time(task["mtime"]))
                # 主题渲染成带 title 的单行文本：CSS 负责省略号截断，保证每行
                # 高度一致（多行主题会让整行的垂直居中失效），完整主题通过
                # 原生 tooltip 保留。
                full_subject = " ".join(str(task["subject"] or "").split())
                row_cols[4].markdown(
                    f'<span class="mpt-task-subject" title="{html.escape(full_subject)}">'
                    f"{html.escape(full_subject or '-')}</span>",
                    unsafe_allow_html=True,
                )
                
                # Enhanced progress display with ETA for processing tasks
                if _task_state_filter_key(task) == "processing":
                    eta_str = _estimate_task_eta(task)
                    row_cols[5].write(f"{task['progress']}% {eta_str}")
                else:
                    row_cols[5].write(f"{task['progress']}%")

                # 操作按钮独占第 7 列，与表头的「操作」列对齐。旧版把它们
                # 渲染进第 5 列（主题），于是主题文字下方挤着一排按钮，而
                # 表头的「操作」列下方完全是空白。
                action_cols = row_cols[6].columns(
                    5,
                    vertical_alignment="center",
                    gap="small",
                )
                with action_cols[0]:
                    play_label = tr("Play")
                    if st.button(
                        play_label,
                        key=f"play_task_{key_prefix}_{task_id}",
                        width='stretch',
                        icon=":material/play_arrow:",
                        help=play_label,
                        disabled=not has_video,
                    ):
                        _open_task_video(task["video_file"])

                with action_cols[1]:
                    # 归档目录才是长期保存、用于发布的位置，因此它作为默认的
                    # 「打开文件夹」；任务工作目录保留为紧邻的次要入口，两者
                    # 不再共用一个含义模糊的按钮。
                    archive_label = tr("Open Archive Folder")
                    archive_help = (
                        archive_label
                        if archive_dir
                        else tr("Task Not Archived Yet")
                    )
                    if st.button(
                        archive_label,
                        key=f"open_archive_{key_prefix}_{task_id}",
                        width='stretch',
                        icon=":material/folder_open:",
                        help=archive_help,
                        disabled=not archive_dir,
                    ):
                        _open_archive_folder_for_task(task["task_path"])

                with action_cols[2]:
                    open_label = tr("Open Task Folder")
                    if st.button(
                        open_label,
                        key=f"open_task_{key_prefix}_{task_id}",
                        width='stretch',
                        icon=":material/source:",
                        help=open_label,
                    ):
                        _open_task_path(task["task_path"])

                with action_cols[3]:
                    restore_label = tr("Regenerate Task")
                    if st.button(
                        restore_label,
                        key=f"restore_task_{key_prefix}_{task_id}",
                        width='stretch',
                        icon=":material/replay:",
                        help=restore_label,
                        disabled=is_processing or not has_restore_data,
                    ):
                        _queue_task_restore(task_id)

                with action_cols[4]:
                    delete_label = tr("Delete Task")
                    delete_help = (
                        f"{delete_label} ({tr('Task Status Processing')})"
                        if is_busy
                        else delete_label
                    )
                    # 删除任务会 shutil.rmtree 整个任务目录，且没有回收站、没有撤销。
                    # 直接放在行内按钮上一次点击就生效，误点无法挽回，所以在真正
                    # rmtree 之前加一道确认：用 st.popover 把"确认/取消"收在同一
                    # 个按钮里，不占用表格额外的一行，也不像 nonce 复选框那样需要
                    # 多一次 rerun 才能看到状态。任务在生成中时整段都不渲染。
                    with st.popover(
                        delete_label,
                        width="stretch",
                        key=f"delete_task_popover_{key_prefix}_{safe_task_key}",
                        help=delete_help,
                        disabled=is_busy,
                    ):
                        # 这里曾经尝试过"关闭时不渲染确认区"来省掉每 2 秒 60 个
                        # 控件的重建，结论是**这条路走不通**，留在注释里免得再试一次：
                        #   1. 内容完全为空时，Streamlit 会把这个 popover 直接禁用，
                        #      按钮变灰、删除功能整个消失；
                        #   2. 换成一个 st.empty() 占位，按钮恢复正常，但真实打开时
                        #      弹层里只剩一块空白——popover 的展开状态并不会写进
                        #      session_state[popover_key]，那道闸门在真实浏览器里
                        #      永远读到 False。AppTest 里能通过是因为测试自己把那个
                        #      键设成了 True，与真实交互不是一回事。
                        st.caption(
                            tr("Confirm Delete Task Caption").format(
                                subject=task["subject"] or task_id
                            )
                        )
                        confirm_cols = st.columns(2)
                        if confirm_cols[0].button(
                            tr("Confirm Delete"),
                            key=f"delete_task_confirm_{key_prefix}_{safe_task_key}",
                            width="stretch",
                            type="primary",
                            icon=":material/delete:",
                        ):
                            if _delete_task(task_id, task["task_path"], task["state"]):
                                video_note = st.session_state.pop(
                                    "task_delete_video_note", ""
                                )
                                st.toast(tr("Task Deleted"))
                                if video_note:
                                    # 未归档的成片被保留，或布局不可用导致没能清理。
                                    # 不说出来的话，用户以为磁盘已经干净了。
                                    st.warning(video_note)
                                st.rerun()
                            else:
                                st.error(tr("Task Delete Failed"))
                        if confirm_cols[1].button(
                            tr("Cancel"),
                            key=f"delete_task_cancel_{key_prefix}_{safe_task_key}",
                            width="stretch",
                        ):
                            # popover 内容只在打开时渲染，点"取消"本身不需要任何
                            # 状态：Streamlit 会在这次 rerun 后把 popover 关掉。
# 整页 rerun：这些函数都不是 @st.fragment，scope="fragment" 在这里必定抛
                            # StreamlitAPIException（点"发布""刷新缓存统计"都会炸）。
                            st.rerun()


def _render_task_manager_panel(tasks=None):
    tasks = tasks if tasks is not None else _collect_task_summaries()
    if not tasks:
        st.info(tr("No Tasks Yet"))
        return

    # Streamlit 1.59 支持有状态 Tabs 的惰性渲染。切换时只重新构建当前列表，
    # 避免定时 Fragment 每两秒重复创建四套任务行和操作按钮。
    status_tabs = [
        ("all", tr("All Tasks")),
        ("processing", tr("Task Status Processing")),
        ("complete", tr("Task Status Complete")),
        ("failed", tr("Task Status Failed")),
    ]
    tabs = st.tabs(
        [label for _, label in status_tabs],
        key="task_manager_status_tabs",
        on_change="rerun",
    )
    for (status_key, _), tab in zip(status_tabs, tabs):
        if not tab.open:
            continue
        with tab:
            filtered_tasks = [
                task
                for task in tasks
                if status_key == "all" or _task_state_filter_key(task) == status_key
            ]
            _render_task_table(filtered_tasks, status_key)

    _render_task_video_preview()


def _render_task_video_preview():
    # 无桌面部署下“播放”按钮的浏览器内回退：在任务面板底部渲染播放器。
    preview_file = st.session_state.get("task_preview_video_file")
    if not preview_file:
        return

    if not _is_allowed_task_video(preview_file) or not os.path.isfile(
        preview_file
    ):
        st.session_state.pop("task_preview_video_file", None)
        return

    st.divider()
    preview_cols = st.columns([5, 1], vertical_alignment="center")
    task_name = os.path.basename(os.path.dirname(preview_file))
    preview_cols[0].caption(f"{os.path.basename(preview_file)} · {task_name}")
    closed = preview_cols[1].button(
        tr("Close"),
        key="close_task_video_preview",
        width='stretch',
        icon=":material/close:",
        help=tr("Close"),
    )
    if closed:
        st.session_state.pop("task_preview_video_file", None)
        return
    _render_video_player(preview_file)

    # 预览的不一定是任务成片，也可能是媒体库归档到频道目录的文件。缩略图面板
    # 只认任务：父目录名不是任务 ID 时安静跳过，避免给频道文件建出无意义的
    # 缩略图目录。任务 ID 恒为服务端生成的 UUID，这里只做格式校验。
    try:
        preview_task_id = str(UUID(task_name))
    except (ValueError, AttributeError, TypeError):
        return
    _render_thumbnail_section(preview_task_id, surface="preview")


def _plan_topic_recorded(channel, topic) -> bool:
    """任务对应主题是否已在任意月份的月度计划中标记为已录制。"""
    normalized = str(topic or "").strip().lower()
    if not normalized:
        return False
    for _plan_dir, row in plans.iter_all_plan_rows(channel):
        if row["recorded"] and str(row["topic"] or "").strip().lower() == normalized:
            return True
    return False


def _auto_archive_plan_tasks() -> bool:
    """把已完成、尚未归档的 WebUI 计划任务归档进频道输出目录。

    仅处理写有 ``plan_meta.json`` 且有最终成片的历史任务；主题已经录制过的
    不再重复归档，保证幂等。返回是否归档了至少一个任务，调用方据此刷新整页。
    """
    archived = False
    for task in _scan_history_tasks(limit=50):
        if not task["video_file"]:
            continue
        task_path = task["task_path"]
        task_id = task["task_id"]
        # An upload still owns the original file until its worker finishes.
        try:
            runtime_task = sm.state.get_task(task_id)
        except Exception as exc:
            logger.warning(f"defer archiving while task status is unavailable: {task_id}: {exc}")
            continue
        if tm.is_task_busy(runtime_task):
            continue
        # .plan_archived = 已归档；.plan_skipped = 主题在别处已录制，主动丢弃。
        # 两者都表示"这个任务不再需要处理"，没有标记就会每 2 秒重扫一次。
        if any(
            os.path.isfile(os.path.join(task_path, marker))
            for marker in (".plan_archived", ".plan_skipped")
        ):
            continue
        meta_path = os.path.join(task_path, "plan_meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(
                f"skip plan task with invalid metadata: "
                f"task_id={task['task_id']}, error={exc}"
            )
            continue
        channel = str((meta or {}).get("channel") or "").strip()
        topic = str((meta or {}).get("topic") or "").strip()
        if not channel or not topic:
            continue
        if _plan_topic_recorded(channel, topic):
            # 主题在任意月份的月度计划里已标记为录制，于是**静默**丢弃这个任务的
            # 成片：既不写标记（于是每 2 秒重扫一次，永远重试），也不记日志，
            # 界面上什么提示都没有。用户刚花两个多小时渲出来的成片就此蒸发，
            # 而唯一线索是它没出现在媒体库里。
            #
            # 常见成因是跨月：新的月度计划所有行都是 pending，
            # first_pending_topic 于是重新给出 #1，而 #1 上个月已经做过。
            # 所以这里必须留痕，并且写一个"已跳过"标记，让重扫收敛到一次。
            logger.info(
                f"skipped archiving already-recorded plan topic: "
                f"task_id={task_id}, channel={channel}, topic={topic}"
            )
            try:
                with open(
                    os.path.join(task_path, ".plan_skipped"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    json.dump(
                        {
                            "task_id": task_id,
                            "channel": channel,
                            "topic": topic,
                            "reason": "already-recorded",
                        },
                        handle,
                        ensure_ascii=False,
                    )
            except OSError as exc:
                logger.warning(
                    f"failed to write plan skip marker: task_id={task_id}, error={exc}"
                )
            continue
        try:
            base = archive_video.archive_channel_video(
                channel=channel,
                task_dir=task_path,
                topic=topic,
                out_dir=plans.current_plan_dir(channel),
                plans_root=plans.PLANS_DIR,
                copy_media=True,
                # 用 video_file 实际所在的目录，而不是"成片应该在哪"的那个目录。
                # 两个解析器对同一个任务给出不同答案时（成片搬迁前留下的老任务
                # 仍在任务目录里），按规范目录去取会报"成片缺失"并无限期重试。
                final_video_dir=os.path.dirname(task["video_file"]),
            )
        except archive_video.ArchiveError as exc:
            # 归档失败（成片缺失、无法解析、复制不完整）时不写 .plan_archived，
            # 任务会在后续轮询里继续重试，直到成片真正写完。
            logger.warning(
                f"deferred archiving plan video: task_id={task['task_id']}, "
                f"channel={channel}, topic={topic}, reason={exc}"
            )
            continue
        except Exception as exc:
            logger.warning(
                f"failed to archive plan video: task_id={task['task_id']}, "
                f"channel={channel}, topic={topic}, error={exc}"
            )
            continue
        logger.info(
            f"auto-archived plan video: channel={channel}, topic={topic}, "
            f"task_id={task['task_id']}, base={base}"
        )
        # 主题不在 topics_30day.txt 时归档会落入「未计划录制」，iter_all_plan_rows
        # 不会把它读成已录制记录；用按任务持久化的标记保证只归档一次，避免反复 rerun。
        # 标记只在归档函数确认成片已完整落盘之后才写。
        try:
            with open(os.path.join(task_path, ".plan_archived"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"task_id": task["task_id"]}, ensure_ascii=False))
        except OSError as exc:
            logger.warning(
                f"failed to write plan archive marker: task_id={task['task_id']}, error={exc}"
            )
        # 归档目录解析是记忆化的，而它只写 .plan_archived、不碰缓存键。不清掉的话
        # 这一行"打开归档目录"会一直灰着，直到会话结束——用户刚看到成片进了媒体库，
        # 按钮却还是不能点。
        _resolve_task_archive_dir.clear()
        archived = True
    return archived


@st.fragment(run_every="2s")
def _render_task_manager_entry():
    """顶栏的任务管理器入口：只负责开关与计数，真正的表由主区域承载。

    之前任务表整个塞在 ``st.popover`` 里，于是每两秒重建一次——12 行任务 × 每行五个
    操作控件，加上状态页签，上百个控件一秒重建 30 遍，而绝大多数轮次用户根本没在看。

    试过"弹层关闭时不渲染弹层内容"，**走不通**，两个坑都踩过：
    内容为空时 Streamlit 会直接把这个 popover 禁用（任务管理器和每一行的删除按钮
    同时变灰，删除功能整个消失）；换成 ``st.empty()`` 占位后按钮恢复了，但
    popover 的展开状态不写进 ``session_state``，那道闸门在真实浏览器里永远读到
    False——AppTest 里能过是因为测试自己把键设成了 True，与真实交互不是一回事。

    所以不再跟 popover 的内部状态较劲：改用自己控制的开关，并把表放到主区域。
    这同时解决了一个体验问题——十几行带按钮的表格塞在弹层里本来就要滚动、会被
    裁切、点外面就关掉，在主区域展开反而更顺手。
    """
    if _auto_archive_plan_tasks():
        # 归档会新增媒体库条目并推进月度计划，触发一次整页刷新让各面板
        # 立即反映归档结果；未被归档的主题在后续轮询中会跳过，不会反复刷新。
        st.rerun(scope="app")

    task_summaries = _collect_task_summaries()
    processing_task_count = _count_processing_tasks(task_summaries)
    is_open = bool(st.session_state.get("task_manager_open", False))
    with st.container(key="task_manager_entry", width="content"):
        if st.button(
            _task_manager_label(processing_task_count),
            key="task_manager_toggle",
            type="primary" if is_open else "secondary",
            icon=(
                ":material/expand_less:" if is_open else ":material/expand_more:"
            ),
            width="content",
        ):
            st.session_state["task_manager_open"] = not is_open
            st.rerun()


def _render_task_manager_section():
    """主区域里的任务表。只在用户展开时渲染。"""
    if not st.session_state.get("task_manager_open", False):
        return
    task_summaries = _collect_task_summaries()
    if not task_summaries:
        st.caption(tr("No Tasks Yet"))
        return
    running = [t for t in task_summaries if _task_state_filter_key(t) == "processing"]
    with st.container(key="task_manager_section", border=True):
        heading, close_col = st.columns([6, 1], vertical_alignment="center")
        if running:
            # 有任务在跑时，标题旁放一个动效。
            #
            # 任务表每两秒刷新一次，纯文字的"处理中"和"卡住了"在界面上长得一模一样。
            # 一个在动的东西才说明它还活着——而这正是用户盯着这一屏时唯一想知道的事。
            head_left, title_col = st.columns([1, 8], vertical_alignment="center")
            with head_left:
                orbs.orb("generate", size=20, label=tr("Task Status Processing"))
            with title_col:
                st.markdown(f"**{tr('Task Manager')}**")
        else:
            heading.markdown(f"**{tr('Task Manager')}**")
        with close_col:
            if st.button(
                tr("Close"),
                key="task_manager_close",
                icon=":material/close:",
                width="stretch",
            ):
                st.session_state["task_manager_open"] = False
                st.rerun()
        _render_task_manager_panel(task_summaries)


def _load_task_restore_payload(task_id):
    tasks_root = os.path.realpath(utils.task_dir())
    task_path = os.path.realpath(os.path.join(tasks_root, str(task_id)))
    try:
        if os.path.commonpath([tasks_root, task_path]) != tasks_root:
            raise ValueError("task path is outside the task directory")
    except ValueError as e:
        logger.warning(f"invalid task restore path: {task_id}, {e}")
        return None

    script_data = _safe_load_task_script(task_path)
    raw_params = script_data.get("params")
    if not isinstance(raw_params, dict):
        logger.warning(f"task has no restorable parameters: {task_id}")
        return None

    params_input = dict(raw_params)
    if script_data.get("script"):
        params_input["video_script"] = script_data["script"]
    if script_data.get("search_terms"):
        params_input["video_terms"] = script_data["search_terms"]

    try:
        params = VideoParams.model_validate(params_input).model_dump(mode="json")
    except Exception as e:
        logger.warning(f"failed to validate task restore parameters: {task_id}, {e}")
        return None

    return {
        "task_id": str(task_id),
        "subject": params.get("video_subject") or script_data.get("script") or task_id,
        "params": params,
    }


def _infer_tts_server_from_voice(voice_name):
    if voice.is_no_voice(voice_name):
        return voice.NO_VOICE_NAME
    if voice.is_siliconflow_voice(voice_name):
        return "siliconflow"
    if voice.is_gemini_voice(voice_name):
        return "gemini-tts"
    if voice.is_mimo_voice(voice_name):
        return "mimo-tts"
    if voice.is_minimax_voice(voice_name):
        return "minimax-tts"
    if voice.is_elevenlabs_voice(voice_name):
        return "elevenlabs"
    if voice.is_chatterbox_voice(voice_name):
        return "chatterbox"
    if voice.is_fish_audio_voice(voice_name):
        return "fish_audio"
    if voice.is_azure_v2_voice(voice_name):
        return "azure-tts-v2"
    return "azure-tts-v1"


def _set_stable_widget_value(key, value):
    if value is not None:
        st.session_state[localized_widget_key(key)] = value


def _apply_pending_task_restore():
    payload = st.session_state.pop("task_restore_payload", None)
    if not payload:
        return False

    _apply_restored_params(payload["params"])
    st.session_state["task_restore_succeeded"] = True
    logger.info(f"restored task configuration: {payload['task_id']}")
    return True


def _apply_restored_params(params):
    """
    把一份完整的生成参数写回页面控件状态。

    历史任务恢复和设置预设导入使用同一份参数模型，因此共用同一个实现，避免
    新增字段时只更新其中一条路径。调用方必须在渲染任何控件之前执行，否则
    Streamlit 会拒绝修改已经实例化的控件状态。
    """
    video_terms = params.get("video_terms") or ""
    if isinstance(video_terms, list):
        video_terms = ", ".join(str(term) for term in video_terms)

    # 文案与高级脚本设置。
    st.session_state["video_subject"] = params.get("video_subject") or ""
    st.session_state["video_script"] = params.get("video_script") or ""
    st.session_state["video_terms"] = str(video_terms)
    _set_stable_widget_value(
        "script_language_select", params.get("video_language") or ""
    )
    st.session_state["paragraph_number_input"] = params.get("paragraph_number", 1)
    st.session_state["video_script_prompt"] = params.get("video_script_prompt") or ""
    st.session_state["custom_system_prompt"] = (
        params.get("custom_system_prompt") or llm.DEFAULT_SCRIPT_SYSTEM_PROMPT
    )

    # 视频设置。素材上传控件不能由服务端写入，因此本地素材需要用户重新选择。
    video_source = params.get("video_source") or "pexels"
    _set_stable_widget_value("video_source_select", video_source)
    _set_stable_widget_value(
        "video_concat_mode_select", params.get("video_concat_mode") or "random"
    )
    _set_stable_widget_value(
        "video_transition_mode_select",
        params.get("video_transition_mode") or VideoTransitionMode.none.value,
    )
    _set_stable_widget_value(
        f"video_aspect_for_{video_source}",
        params.get("video_aspect") or VideoAspect.portrait.value,
    )
    _set_stable_widget_value(
        "video_clip_duration_select", params.get("video_clip_duration", 3)
    )
    _set_stable_widget_value(
        "video_clip_speed_slider",
        # API 可以写入超过 WebUI 范围的速度，任务生成阶段会安全归一化，但
        # 历史记录仍可能保留原值。恢复任务前再次归一化，避免给 Streamlit
        # slider 注入越界值、NaN 或无穷值导致控件状态异常。
        utils.normalize_clip_speed(params.get("video_clip_speed", 1.0)),
    )
    _set_stable_widget_value("video_count_select", params.get("video_count", 1))
    st.session_state["match_materials_to_script"] = bool(
        params.get("match_materials_to_script", False)
    )

    # 音频设置。TTS server 未写入旧任务，根据历史 voice_name 推断。
    voice_name = params.get("voice_name") or voice.NO_VOICE_NAME
    tts_server = _infer_tts_server_from_voice(voice_name)
    if params.get("custom_audio_file"):
        voice_mode = VOICE_MODE_UPLOAD
    elif voice.is_no_voice(voice_name):
        voice_mode = VOICE_MODE_NONE
    else:
        voice_mode = VOICE_MODE_TTS
    _set_stable_widget_value("voice_mode_control", voice_mode)
    if tts_server != voice.NO_VOICE_NAME:
        _set_stable_widget_value("tts_server_select", tts_server)
        _set_stable_widget_value(f"speech_synthesis_select_{tts_server}", voice_name)
    _set_stable_widget_value("voice_volume_select", params.get("voice_volume", 1.0))
    _set_stable_widget_value("voice_rate_select", params.get("voice_rate", 1.0))
    bgm_type = params.get("bgm_type") or ""
    _set_stable_widget_value("bgm_type_select", bgm_type)
    _set_stable_widget_value("bgm_volume_select", params.get("bgm_volume", 0.2))
    st.session_state["custom_bgm_file_input"] = params.get("bgm_file") or ""
    st.session_state["sonilo_bgm_prompt_input"] = (
        params.get("video_music_prompt") or params.get("sonilo_bgm_prompt") or ""
    )
    st.session_state["elevenlabs_music_prompt_input"] = (
        params.get("video_music_prompt") or ""
    )

    # 字幕设置。对旧任务中的越界数值做最小限幅，避免 Slider 无法初始化。
    st.session_state["subtitle_enabled_checkbox"] = bool(
        params.get("subtitle_enabled", True)
    )
    _set_stable_widget_value("font_name_select", params.get("font_name") or "")
    _set_stable_widget_value(
        "subtitle_position_select", params.get("subtitle_position") or "bottom"
    )
    custom_position = min(100.0, max(0.0, float(params.get("custom_position", 70.0))))
    st.session_state["custom_position_input"] = str(custom_position)
    st.session_state["font_color_picker"] = params.get("text_fore_color") or "#FFFFFF"
    st.session_state["font_size_slider"] = min(
        100, max(30, int(params.get("font_size", 60)))
    )
    st.session_state["stroke_color_picker"] = params.get("stroke_color") or "#000000"
    st.session_state["stroke_width_slider"] = min(
        10.0, max(0.0, float(params.get("stroke_width", 1.5)))
    )
    background_color = params.get("text_background_color")
    background_enabled = bool(background_color)
    st.session_state["subtitle_background_enabled_checkbox"] = background_enabled
    if isinstance(background_color, str):
        st.session_state["subtitle_background_color_picker"] = background_color
    st.session_state["rounded_subtitle_background_checkbox"] = bool(
        params.get("rounded_subtitle_background", False) and background_enabled
    )

    st.session_state.pop("local_video_materials_uploader", None)
    # 历史任务只保存素材路径，不能保证这些文件在当前环境仍然存在。
    # 同时清空当前页面已缓存的上传素材，避免恢复后误用另一个任务的文件。
    st.session_state["local_video_materials"] = []
    st.session_state.pop("custom_audio_file_uploader", None)
    st.session_state.pop("custom_bgm_uploader", None)
    st.session_state.pop("custom_bgm_validation", None)
    st.session_state["task_restore_upload_requirements"] = (
        _build_restore_upload_requirements(params)
    )

    return True


def _dismiss_task_restore_dialog():
    st.session_state.pop("task_restore_candidate_id", None)


@st.dialog(
    tr("Regenerate Task"),
    width="small",
    on_dismiss=_dismiss_task_restore_dialog,
)
def _render_task_restore_dialog(task_id):
    payload = _load_task_restore_payload(task_id)
    if payload is None:
        st.error(tr("Task Restore Failed"))
        if st.button(tr("Cancel"), key="cancel_invalid_task_restore"):
            st.session_state.pop("task_restore_candidate_id", None)
            st.rerun(scope="app")
        return

    st.write(tr("Regenerate Task Confirmation"))
    st.caption(_format_task_subject(payload["subject"], max_length=80))
    cancel_col, load_col = st.columns(2)
    if cancel_col.button(
        tr("Cancel"),
        key="cancel_task_restore",
        width='stretch',
    ):
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")
    if load_col.button(
        tr("Load Task Configuration"),
        key="confirm_task_restore",
        type="primary",
        width='stretch',
    ):
        st.session_state["task_restore_payload"] = payload
        st.session_state.pop("task_restore_candidate_id", None)
        st.rerun(scope="app")


def _dismiss_settings_dialog():
    """关闭设置弹窗，并确保下一次整页 rerun 不会再次自动打开。"""
    st.session_state["settings_dialog_open"] = False


def _render_brand(available_update: str | None = None):
    """渲染项目名称、当前版本和可选的更新入口。"""
    update_link = ""
    if available_update:
        update_label = html.escape(
            tr("Update Available").format(version=available_update)
        )
        # Streamlit 会继续用 Markdown 解析传入的 HTML。这里保持链接为单行，
        # 避免多行字符串的缩进被识别成代码块，导致页面直接显示 HTML 源码。
        update_link = (
            '<a class="mpt-brand__update" '
            f'href="{version_checker.LATEST_RELEASE_PAGE_URL}" '
            'target="_blank" rel="noopener noreferrer" '
            f'aria-label="{update_label}" title="{update_label}">'
            f"{update_label}</a>"
        )
    st.markdown(
        f"""
        <h1 class="mpt-brand">
            <span class="mpt-brand__name">MoneyPrinterTurbo</span>
            <a class="mpt-brand__version"
               href="https://github.com/harry0703/MoneyPrinterTurbo"
               target="_blank"
               rel="noopener noreferrer"
               aria-label="Open MoneyPrinterTurbo on GitHub"
               title="Open project on GitHub">v{html.escape(str(config.project_version))}</a>
            {update_link}
        </h1>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every="1s")
def _render_pending_version_check():
    """检查未完成时只刷新品牌区域，避免阻塞或反复执行整页表单。"""
    snapshot = version_checker.poll_available_update(config.project_version)
    if snapshot.complete:
        # 检查完成后刷新一次整页，让顶部栏改为静态渲染并停止 fragment 轮询。
        # 该刷新发生在后台请求完成之后，不会延迟初始页面的其它内容。
        st.rerun(scope="app")
    _render_brand()


def _nav_title_and_hint(view):
    """当前视图在左侧导航里登记的标题与说明键。"""
    for _group_title, _items in _NAV_GROUPS:
        for _key, _title, _icon, _hint in _items:
            if _key == view:
                return _title, _hint
    return "Nav Generate", "Nav Generate Hint"


def _render_top_bar_context():
    """顶部栏里的当前页面名 + 一句说明，对应参考设计的工具标题行。"""
    view = st.session_state.get("nav_view", _DEFAULT_VIEW)
    title_key, hint_key = _nav_title_and_hint(view)
    st.markdown(
        f'<div class="mpt-top-context">'
        f'<p class="mpt-top-context__title">{html.escape(tr(title_key))}</p>'
        f'<p class="mpt-top-context__sub">{html.escape(tr(hint_key))}</p>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_top_bar():
    """渲染品牌、任务管理、设置和语言切换组成的页面顶部栏。"""
    # 顶部栏分为品牌区和操作区两个独立区域。窄屏下由 Streamlit
    # 将两个区域整体换行，操作区内部再根据剩余宽度自动换行。
    with st.container(key="top_bar"):
        brand_col, actions_col = st.columns(
            [3.5, 2.0],
            vertical_alignment="center",
            gap="small",
        )

    with brand_col:
        update_snapshot = version_checker.poll_available_update(config.project_version)
        if update_snapshot.complete:
            _render_brand(update_snapshot.available_version)
        else:
            _render_pending_version_check()
        _render_top_bar_context()

    with actions_col:
        with st.container(
            key="top_bar_actions",
            horizontal=True,
            horizontal_alignment="right",
            vertical_alignment="center",
            gap="small",
            width="stretch",
        ):
            _render_task_manager_entry()

            # 重播引导。默认只播一次，而它教的仅是手动生成那条路径——计划流、
            # 媒体库、发布这些每天真正在用的操作一次都没覆盖，用户需要能再看一遍。
            # 换 tour key 就是换一条 localStorage 记录，组件会重新播。
            if st.button(
                tr("Replay Onboarding Tour"),
                key="replay_onboarding_tour_button",
                type="secondary",
                icon=":material/help:",
                width="content",
                help=tr("Replay Onboarding Tour Help"),
            ):
                st.session_state["onboarding_tour_replay_nonce"] = (
                    st.session_state.get("onboarding_tour_replay_nonce", 0) + 1
                )
                st.session_state[f"{ONBOARDING_TOUR_KEY}-auto-started"] = False
                st.rerun()

            if st.button(
                tr("Settings"),
                key="open_settings_dialog_button",
                type="secondary",
                icon=":material/settings:",
                width="content",
            ):
                # 设置现在是左侧导航里的一屏，不再是一层弹窗。这个按钮保留下来有两个
                # 作用：它是新手引导第一步指向的目标，也是已经在生成页的用户最快的
                # 入口——不用先找到侧栏。
                st.session_state[_NAV_REQUEST_KEY] = "settings"
                st.rerun()

            language_codes = list(locales.keys())
            selected_index = 0
            for i, code in enumerate(language_codes):
                if code == st.session_state.get("ui_language", ""):
                    selected_index = i

            selected_language_code = st.selectbox(
                "Language / 语言",
                options=language_codes,
                index=selected_index,
                format_func=lambda code: locales[code].get("Language", code),
                key="top_language_code_selector",
                label_visibility="collapsed",
                width=180,
            )
            if selected_language_code:
                previous_language = st.session_state.get("ui_language", "")
                if selected_language_code != previous_language:
                    logger.info(
                        "UI language changed by user: "
                        f"previous_language={previous_language or '<empty>'}, "
                        f"selected_language={selected_language_code}"
                    )
                    st.session_state["ui_language"] = selected_language_code
                    # 浏览器自动识别只影响当前会话；只有用户主动切换下拉框时才
                    # 写入 config.toml，后续新会话将优先使用该明确选择。
                    _set_runtime_config("ui", "language", selected_language_code)
                    _save_runtime_config()
                    # 切换语言后强制刷新，避免 selectbox 继续展示旧语言文案。
                    st.rerun()


support_locales = [
    "zh-CN",
    "zh-HK",
    "zh-TW",
    "de-DE",
    "en-US",
    "es-ES",
    "fr-FR",
    "it-IT",
    "ru-RU",
    "vi-VN",
    "th-TH",
    "tr-TR",
]


# -----------------------------------------------------------------------------
# 通用 UI 组件、资源缓存与日志
# -----------------------------------------------------------------------------


@st.cache_data(ttl=30, show_spinner=False)
def get_all_fonts():
    # 字体目录很少变化，但 Streamlit 每次控件交互都会 rerun 页面。短周期缓存
    # 可以避免连续重复 os.walk，同时保证新增字体后最多 30 秒即可被发现。
    fonts = []
    for root, dirs, files in os.walk(font_dir):
        for file in files:
            if file.endswith(".ttf") or file.endswith(".ttc"):
                fonts.append(file)
    fonts.sort()
    return fonts


@st.cache_data(ttl=30, show_spinner=False)
def get_all_songs():
    # 背景音乐与字体使用相同的短周期策略，不做永久缓存，兼顾 rerun 性能和
    # 用户运行期间手动添加音乐文件的场景。
    songs = []
    for root, dirs, files in os.walk(song_dir):
        for file in files:
            if file.endswith(".mp3"):
                songs.append(file)
    return songs


def open_task_folder(task_id):
    try:
        # task_id 应始终是服务端生成的 UUID。这里先做格式校验，避免异常值
        # 通过路径拼接访问任务目录之外的位置，也避免后续打开目录时触发
        # 平台 shell 对特殊字符的解释。
        normalized_task_id = str(UUID(str(task_id)))
        tasks_root = os.path.abspath(os.path.join(root_dir, "storage", "tasks"))
        path = os.path.abspath(os.path.join(tasks_root, normalized_task_id))

        # 即使 UUID 校验通过，也再次确认最终路径仍在任务根目录内，避免
        # 未来调用方调整 task_id 来源时引入路径穿越风险。
        if not path.startswith(tasks_root + os.sep):
            logger.warning(f"invalid task folder path: {path}")
            return

        if os.path.isdir(path):
            webbrowser.open(f"file://{path}")
    except Exception as e:
        logger.exception(f"failed to open task folder: task_id={task_id}, error={e}")


@st.cache_resource
def init_log():
    # 基础日志 Handler 属于进程级资源，而不是页面会话状态。Streamlit 每次组件
    # 交互都会 rerun 页面脚本，代码热重载也可能让缓存失效。日志初始化只能
    # 精确替换终端 Handler，不能清空正在生成任务使用的 WebUI 临时 Handler。
    _lvl = "DEBUG"

    return configure_terminal_logger(
        sys.stdout,
        level=_lvl,
        colorize=True,
    )


init_log()


def tr_optional(key, fallback_language=""):
    loc = locales.get(st.session_state["ui_language"], {})
    value = loc.get("Translation", {}).get(key, "")
    if not value and fallback_language:
        fallback_loc = locales.get(fallback_language, {})
        value = fallback_loc.get("Translation", {}).get(key, "")
    return value if value else ""


def render_onboarding_tour():
    # 引导只覆盖三个稳定入口，不尝试控制 Dialog、Tabs 或业务表单。这样既能让
    # 新用户理解完整流程，也不会把引导状态与 Streamlit 的动态组件生命周期耦合。
    steps = [
        Tour.bind(
            "open_settings_dialog_button",
            title=tr("Onboarding Model Settings Title"),
            desc=tr("Onboarding Model Settings Description"),
            side="bottom",
            align="end",
        ),
        Tour.bind(
            "main_settings_grid",
            title=tr("Onboarding Creation Settings Title"),
            desc=tr("Onboarding Creation Settings Description"),
            side="top",
            align="center",
        ),
        Tour.bind(
            "generate_video_button",
            title=tr("Onboarding Generate Video Title"),
            desc=tr("Onboarding Generate Video Description"),
            side="top",
            align="center",
        ),
    ]

    # streamlit-tour 1.1.0 没有在 Python 构造参数中暴露导航文案，但底层
    # Driver.js 支持在每一步的 popover 配置中覆盖按钮文本。这里统一注入本地化
    # 文案，并对内容做 HTML 转义，因为组件会通过 innerHTML 渲染这些字段。
    previous_text = html.escape(tr("Onboarding Previous"))
    next_text = html.escape(tr("Onboarding Next"))
    done_text = html.escape(tr("Onboarding Done"))
    for index, step in enumerate(steps):
        step.popover["prevBtnText"] = f"&larr; {previous_text}"
        # Driver.js 会在合并单步配置时覆盖已经替换过变量的进度模板，因此直接
        # 写入当前步骤和总步骤数，避免页面显示未解析的 {{current}} 占位符。
        step.popover["progressText"] = f"{index + 1} / {len(steps)}"
        if index == len(steps) - 1:
            step.popover["doneBtnText"] = done_text
        else:
            step.popover["nextBtnText"] = f"{next_text} &rarr;"

    # 引导默认只播一次，完成状态由组件写进浏览器 localStorage。但"只播一次"
    # 意味着用户看完一遍之后再也无法复习，而它教的又只是手动生成那条路径
    # —— 真正每天在用的计划流、媒体库、发布一次都没讲。这里给一个重播入口：
    # 换一个新的 tour key 即可。streamlit-tour 用 key 索引 localStorage，
    # 新 key 就是一条全新的完成状态，不需要从 Python 侧清理浏览器存储。
    replay_nonce = st.session_state.get("onboarding_tour_replay_nonce", 0)
    tour = Tour(
        steps=steps,
        key=f"{ONBOARDING_TOUR_KEY}-{replay_nonce}",
        show_progress=True,
        animate=True,
        overlay_opacity=0.55,
        one_time_tour=True,
    )

    # 每个 Streamlit 会话只主动启动一次。是否已经完成则由组件通过浏览器
    # localStorage 判断，避免页面 rerun 或普通控件交互反复弹出引导。
    # nonce 参与 key 后，重播需要把 auto-start 标志也一起清掉。
    auto_start_key = f"{ONBOARDING_TOUR_KEY}-auto-started"
    if not st.session_state.get(auto_start_key, False):
        st.session_state[auto_start_key] = True
        tour.start()


@st.fragment(run_every="500ms")
def _thumbnail_job_watch(task_id: str, thumbnail_root: str):
    """
    有在途缩略图作业时轮询，全部结束后触发一次整页刷新。

    没有这一层的话，后台作业跑完了界面也不会更新：结果页是静态渲染的，
    用户必须自己点一下页面才能看到新缩略图，看起来就像功能坏了。
    空作业列表不刷新，否则页面会无限自我刷新。
    """
    from app.services.thumbnail_jobs import get_job_manager

    try:
        records = get_job_manager(Path(thumbnail_root)).list_jobs(task_id)
    except Exception as exc:  # noqa: BLE001 - 轮询失败不该打断结果页
        logger.debug(f"thumbnail job watch skipped: task_id={task_id}, error={exc}")
        return

    if not should_refresh(records):
        return

    st.rerun(scope="app")


def _thumbnail_unavailable_notice(reason: object) -> None:
    """
    作品集根没配时说明功能为什么没出现、怎么打开。

    缩略图是附加能力，所以布局解析失败时绝不抛错、也绝不猜一个目录来写
    （那会把产物落到错误的树里）。但静默不渲染和"功能坏了"在用户眼里没区别，
    所以这里给一条折叠的说明：该功能处于关闭状态，以及具体要改哪个配置。
    """
    message = str(reason).strip() or "portfolio_root 未配置"
    with st.expander(tr("Thumbnail unavailable"), expanded=False):
        st.caption(message)
        st.caption(
            tr(
                "Set portfolio_root in the [app] section of config.toml to enable "
                "thumbnails and portfolio planning."
            )
        )


def _render_thumbnail_section(task_id: str, surface: str = "snapshot"):
    """
    在已完成结果下方渲染缩略图面板。

    任何失败都只影响这一段：缩略图是附加能力，不能让它把整个结果页打不开。
    读不到描述符时说明这个任务还没有成片身份记录，直接安静地不渲染。

    :param surface: 同一任务可能同时出现在结果快照与任务预览里，控件键必须按
        展示位置隔离，否则 Streamlit 会报重复控件 ID 而整页崩掉。
    """
    try:
        layout = _cached_project_layout()
        if layout is None:
            _thumbnail_unavailable_notice(
                "portfolio_root 未配置，缩略图与作品集功能处于关闭状态。"
            )
            return

        descriptor_path = (
            layout.descriptor_root / task_id / "result_descriptor.json"
        )
        if not descriptor_path.is_file():
            return

        descriptor = read_result_descriptor(descriptor_path)
    except (ResultDescriptorError, ThumbnailFilesError) as exc:
        logger.info(f"thumbnail section skipped: task_id={task_id}, reason={exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 附加功能不应影响主流程
        logger.warning(
            f"unexpected error preparing thumbnail section: "
            f"task_id={task_id}, error={exc}"
        )
        return

    if not descriptor.videos:
        return

    thumbnail_root = layout.thumbnail_root / task_id
    try:
        if watch_for_completion(task_id, thumbnail_root):
            _thumbnail_job_watch(task_id, str(thumbnail_root))
    except Exception as exc:  # noqa: BLE001 - 附加功能不应影响主流程
        logger.debug(f"thumbnail job watch failed: task_id={task_id}, error={exc}")

    try:
        with st.container(key=f"thumbnail_panel_{surface}_{task_id}"):
            render_thumbnail_panel(
                descriptor=descriptor,
                manifest_path=layout.thumbnail_root / task_id / "manifest.json",
                thumbnail_root=layout.thumbnail_root / task_id,
                # 成片目录是 layout.outputs_root/<task_id>，不是 storage/tasks/<task_id>。
                # 最终成片是唯一长期留存的视频，b2b52cc 起就写在那里；任务目录只剩
                # 中间产物（可能还在内存盘上）。传错目录会让每个缩略图作业都找不到
                # 源文件而失败，而失败态此前什么都不渲染，用户只看到"点了没反应"。
                source_root=layout.outputs_root / task_id,
                tr=tr,
                key_prefix=f"{surface}_{task_id}_",
            )
    except Exception as exc:  # noqa: BLE001 - 同上，附加功能不应影响主流程
        logger.warning(
            f"failed to render thumbnail panel: task_id={task_id}, error={exc}"
        )


def _render_generation_logs(task_id):
    """渲染后台任务日志快照，不从工作线程访问 Streamlit 会话状态。"""
    if config.ui.get("hide_log", False):
        return

    log_records = webui_task.get_task_logs(task_id)
    if not log_records:
        return

    st.code("\n".join(log_records))


def _render_generation_task_snapshot(task_id, task):
    """根据状态存储中的快照渲染进度、失败原因或最终成片。"""
    if not task:
        st.info(tr("Generating Video"))
        _render_generation_logs(task_id)
        return

    state = _normalize_task_state(task.get("state"))
    progress = max(0, min(100, int(task.get("progress", 0) or 0)))
    if state == const.TASK_STATE_PROCESSING:
        st.info(tr("Generating Video"))
        st.progress(
            progress,
            text=f"{tr('Task Progress')}: {progress}%",
        )
        _render_generation_logs(task_id)
        return

    if state == const.TASK_STATE_FAILED:
        error = str(task.get("error") or "").strip()
        message = tr("Video Generation Failed")
        st.error(f"{message}: {error}" if error else message)
        _render_generation_logs(task_id)
        return

    video_files = task.get("videos") or []
    if state != const.TASK_STATE_COMPLETE or not video_files:
        st.error(tr("Video Generation Failed"))
        _render_generation_logs(task_id)
        return

    st.success(tr("Video Generation Completed"))
    posting_state = task.get("cross_post_state")
    if posting_state == const.CROSS_POST_STATE_FAILED:
        st.warning(f"{tr('Video Publish Failed')}: {task.get('cross_post_error') or ''}")
    elif posting_state == const.CROSS_POST_STATE_COMPLETE:
        st.success(tr("Video Published"))
    elif posting_state in (const.CROSS_POST_STATE_PENDING, const.CROSS_POST_STATE_PROCESSING):
        st.caption(tr("Publishing"))
    elif task.get("cross_post_skip_reason"):
        st.caption(task["cross_post_skip_reason"])
    for warning in task.get("warnings") or []:
        if isinstance(warning, Mapping) and warning.get("code") == "sonilo_bgm_failed":
            st.warning(
                tr("Sonilo BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        elif (
            isinstance(warning, Mapping)
            and warning.get("code") == "elevenlabs_bgm_failed"
        ):
            st.warning(
                tr("ElevenLabs BGM Fallback Warning").format(
                    index=warning.get("video_index", "")
                )
            )
        else:
            st.warning(str(warning))

    try:
        player_cols = st.columns(len(video_files) * 2 + 1)
        for i, url in enumerate(video_files):
            with player_cols[i * 2 + 1]:
                _render_video_player(url)
                if not os.path.isfile(url):
                    logger.warning(
                        f"generated video is unavailable for download: "
                        f"task_id={task_id}, video_file={url}"
                    )
                    continue

                download_label = tr("Download Video")
                if len(video_files) > 1:
                    download_label = f"{download_label} {i + 1}"
                download_name = _build_video_download_name(
                    task.get("video_subject"),
                    i + 1,
                    len(video_files),
                )
                # 这里刻意用 link_button 指向 /download，而不是把文件读进内存交给
                # st.download_button。结果页一屏可能有 3 条成片、每条 50 MB 上下，
                # 旧写法等于用户只是**看一眼**结果页，进程就先多吃进 160 MB——
                # 而这 160 MB 在按钮被点之前一次都没被用到。/download 以
                # FileResponse 边读边发，常驻内存只有分块缓冲。
                #
                # st.link_button 只接受它自己那几个参数：早期版本给它传了
                # file_name / mime，于是每一次渲染结果页都抛 TypeError，而上面那圈
                # try/except 把异常吞成一条日志——成片与下载按钮就此整段消失，
                # 界面看起来只是"这次没有产出视频"。建议文件名改由 URL 传给端点。
                st.link_button(
                    download_label,
                    url=_generated_video_download_url(url, download_name),
                    key=f"download_generated_video_{task_id}_{i}",
                    icon=":material/download:",
                    width='stretch',
                )
    except Exception as exc:
        logger.exception(
            f"failed to render generated video preview: task_id={task_id}, "
            f"video_files={video_files}, error={exc}"
        )

    _render_thumbnail_section(task_id)
    first_video = next((path for path in video_files if os.path.isfile(path)), None)
    if first_video:
        _render_posting_copy(first_video, task.get("video_subject") or "", "", f"task_{task_id}")

    _render_generation_logs(task_id)
    if st.session_state.get("handled_generation_task_id") != task_id:
        # Fragment 可能重复渲染同一个完成任务。无论是否开启自动打开目录，
        # 每个任务都只处理一次完成事件，避免重复弹出资源管理器或重复写入日志。
        st.session_state["handled_generation_task_id"] = task_id
        if config.ui.get("open_task_folder_on_completion", True):
            open_task_folder(task_id)
        logger.info(f"{tr('Video Generation Completed')}: task_id={task_id}")


@st.fragment(run_every=webui_task.TASK_LOG_REFRESH_INTERVAL_SECONDS)
def _render_running_generation_task(task_id):
    """只在任务运行期间轮询；结束后切回静态结果，停止不必要的定时刷新。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query WebUI generation task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        # 完整页面脚本现在没有耗时生成逻辑，可以安全 rerun 并把结果改为静态
        # 渲染。这样任务结束后不会让浏览器永久保留一个两秒轮询的 Fragment。
        st.rerun(scope="app")

    _render_generation_task_snapshot(task_id, task)


def _render_current_generation_task():
    """在生成按钮下方恢复当前页面最近提交任务的可查询 UI。"""
    task_id = st.session_state.get("current_generation_task_id", "")
    if not task_id:
        return

    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.exception(
            f"failed to query current WebUI task: task_id={task_id}, error={exc}"
        )
        st.error(tr("Video Generation Failed"))
        return

    state = _normalize_task_state((task or {}).get("state"))
    if state in {const.TASK_STATE_COMPLETE, const.TASK_STATE_FAILED}:
        _remove_active_generation_task(task_id)
        _render_generation_task_snapshot(task_id, task)
        return

    _render_running_generation_task(task_id)


def get_llm_provider_tips(provider_id, **kwargs):
    # LLM provider 说明文案统一使用 `llm_provider_tips.<provider_id>` 规则。
    # 这样新增 provider 时只需要在 locale 中补文案；没有文案时不展示提示块，
    # 避免 Main.py 里继续堆叠大量中英文硬编码说明。
    provider = get_llm_provider(provider_id)
    if provider is None:
        return ""

    # Provider 配置说明目前统一维护中文和英文两套规范模板；其它界面语言
    # 统一使用英文，避免在 locale 中复制英文后长期不同步。后续某个语种完成
    # 全量翻译后，再将它加入这里的独立维护范围。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    tips = (
        locales.get(tips_language, {}).get("Translation", {}).get(provider.tips_key, "")
    )
    if not tips:
        return tips

    service_endpoint = provider.preferred_service_endpoint(
        prefer_international=tips_language == "en"
    )
    api_key_url = (
        service_endpoint.api_key_url
        if service_endpoint
        else provider.effective_api_key_url()
    )
    format_context = {
        "api_key_url": api_key_url,
        "default_model": provider.default_model,
        "default_base_url": (
            service_endpoint.base_url
            if service_endpoint
            else provider.effective_default_base_url
        ),
        "model_docs_url": service_endpoint.model_docs_url if service_endpoint else "",
        **{
            f"default_{field.config_suffix}": field.default_value
            for field in provider.extra_fields
        },
        **kwargs,
    }
    try:
        return tips.format(**format_context)
    except Exception as e:
        logger.warning(f"format llm provider tips failed: {provider_id}, {e}")
        return tips


def format_llm_connection_error(provider_id, base_url, error):
    """为可明确定位的鉴权错误补充配置检查建议，同时保留原始响应。"""
    error_text = str(error or "").strip()
    normalized_error = error_text.lower()
    authentication_markers = (
        "401",
        "authentication",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    provider = get_llm_provider(provider_id)
    if provider is None or not provider.service_endpoints or not any(
        marker in normalized_error for marker in authentication_markers
    ):
        return error_text

    message = tr_optional(
        provider.authentication_error_key,
        fallback_language="en",
    )
    if not message:
        return error_text
    return message.format(base_url=base_url or "-", error=error_text)


def get_llm_provider_label(provider):
    return tr_optional(provider.label_key) or provider.default_label


def get_tts_provider_tips(provider_id):
    # TTS 配置说明与 LLM Provider 采用相同维护策略：只维护中英文，
    # 其它界面语言统一回退英文，避免复制后长期不同步。
    ui_language = st.session_state.get("ui_language", "en")
    tips_language = ui_language if ui_language in {"zh", "en"} else "en"
    return (
        locales.get(tips_language, {})
        .get("Translation", {})
        .get(f"tts_provider_tips.{provider_id}", "")
    )


def localized_widget_key(name, *parts):
    # 部分 Streamlit selectbox 使用稳定 key 记住选择状态，但展示文本来自 locale。
    # 语言切换时把语言也放进 key，可以强制重建控件，避免选中项仍显示旧语言。
    language = st.session_state.get("ui_language", config.ui.get("language", ""))
    suffix_parts = [name, language, *[str(part) for part in parts if part]]
    return "_".join(suffix_parts)


def stable_selectbox(label, options, default_value, key, format_func=None, **kwargs):
    # Streamlit 1.59 对 selectbox 的状态复用更敏感：如果控件没有固定 key，
    # 或者真实选项只是一组临时下标，页面 rerun 后容易被重新计算的 index 覆盖，
    # 表现为用户第一次选择不生效、需要再选一次。这个 helper 统一用稳定业务值
    # 作为真实选项，并在 session_state 里保存该值；展示文案只通过 format_func
    # 转换，避免翻译文案、选项顺序或上游配置变化影响选择状态。
    options = list(options)
    if not options:
        raise ValueError(f"selectbox options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    accepts_custom_value = bool(kwargs.get("accept_new_options"))
    has_valid_custom_value = (
        accepts_custom_value
        and isinstance(selected_value, str)
        and bool(selected_value.strip())
    )
    if selected_value not in options and not has_valid_custom_value:
        # 如果上游选项发生变化（例如切换 TTS provider 后声音列表变了），
        # 旧值已经不合法。控件创建前直接初始化 session_state，之后只让 key
        # 管理状态，不再同时传入 index。这样可以避免 Streamlit 在 rerun 时
        # 用重新计算的 index 覆盖用户刚选择的值，导致第一次选择不生效。
        st.session_state[widget_key] = default_value

    if format_func is None:
        format_func = str

    return st.selectbox(
        label,
        options=options,
        format_func=format_func,
        key=widget_key,
        **kwargs,
    )


def stable_pills(label, options, default_value, key, format_func=None, selection_mode="single", **kwargs):
    """把少量离散选项渲染成横向筹码（chips），并保留与 ``stable_selectbox`` 相同的状态语义。

    三个左右的选项用下拉框是浪费：用户要"点开、看清、再点一下"才能确认自己选了
    什么，而选项就摆在那里。筹码一次就能选完，也让"当前选择"始终可见——画幅、
    转场、字幕位置这类每天都要调的项尤其如此。

    状态处理刻意与 ``stable_selectbox`` 对齐：session_state 里存业务值而不是下标，
    展示文案只经 ``format_func`` 转换。这样翻译文案、选项顺序或上游配置变化都不会
    把用户的选择冲掉——Streamlit 1.59 对控件状态复用非常敏感，历史上正是这一点导致
    "第一次选择不生效、要再选一次"。
    """
    options = list(options)
    if not options:
        raise ValueError(f"pills options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    selected_value = st.session_state.get(widget_key)
    if selection_mode == "single" and selected_value not in options:
        st.session_state[widget_key] = default_value
    elif selection_mode == "multi" and selected_value is not None:
        # 上游选项变少时，剔掉已不存在的值；全被剔掉则回到默认值。
        if not isinstance(selected_value, (list, tuple)):
            selected_value = [selected_value]
        pruned = [value for value in selected_value if value in options]
        st.session_state[widget_key] = pruned or [default_value]

    if format_func is None:
        format_func = str

    return st.pills(
        label,
        options=options,
        format_func=format_func,
        selection_mode=selection_mode,
        key=widget_key,
        **kwargs,
    )


def sync_script_order_concat_mode():
    """在文案顺序匹配开启时固定使用顺序拼接，并在关闭后恢复原选择。"""
    widget_key = localized_widget_key("video_concat_mode_select")
    previous_key = "video_concat_mode_before_script_order_match"
    match_script_order = bool(st.session_state.get("match_materials_to_script", False))

    if match_script_order:
        current_mode = st.session_state.get(widget_key, VideoConcatMode.random.value)
        if current_mode != VideoConcatMode.sequential.value:
            st.session_state[previous_key] = current_mode
        st.session_state[widget_key] = VideoConcatMode.sequential.value
        return

    previous_mode = st.session_state.pop(previous_key, None)
    if previous_mode in {
        VideoConcatMode.sequential.value,
        VideoConcatMode.random.value,
    }:
        st.session_state[widget_key] = previous_mode


def reset_script_system_prompt():
    """将高级脚本设置中的系统提示词恢复为当前版本的默认内容。"""
    st.session_state["custom_system_prompt"] = llm.DEFAULT_SCRIPT_SYSTEM_PROMPT


def reset_subtitle_settings():
    """恢复 WebUI 字幕控件和持久化配置中的默认值。"""
    defaults = DEFAULT_SUBTITLE_SETTINGS
    st.session_state["subtitle_enabled_checkbox"] = defaults["subtitle_enabled"]
    _set_stable_widget_value("font_name_select", defaults["font_name"])
    _set_stable_widget_value("subtitle_position_select", defaults["subtitle_position"])
    st.session_state["custom_position_input"] = str(defaults["custom_position"])
    st.session_state["font_color_picker"] = defaults["text_fore_color"]
    st.session_state["font_size_slider"] = defaults["font_size"]
    st.session_state["stroke_color_picker"] = defaults["stroke_color"]
    st.session_state["stroke_width_slider"] = defaults["stroke_width"]
    st.session_state["subtitle_background_enabled_checkbox"] = defaults[
        "subtitle_background_enabled"
    ]
    st.session_state["subtitle_background_color_picker"] = defaults[
        "subtitle_background_color"
    ]
    st.session_state["rounded_subtitle_background_checkbox"] = defaults[
        "rounded_subtitle_background"
    ]

    # 同步会持久化的 UI 选项，确保恢复后刷新页面仍保持默认设置。
    for key in (
        "subtitle_enabled",
        "font_name",
        "subtitle_position",
        "custom_position",
        "text_fore_color",
        "font_size",
        "stroke_color",
        "stroke_width",
        "subtitle_background_enabled",
        "subtitle_background_color",
        "rounded_subtitle_background",
    ):
        _set_runtime_config("ui", key, defaults[key])


@st.dialog(tr("Final Prompt Preview"), width="large")
def render_script_prompt_preview(prompt):
    """展示将要发送给大模型的完整脚本生成提示词。"""
    st.code(prompt, language="markdown", wrap_lines=True)


def stable_segmented_control(
    label, options, default_value, key, format_func=None, **kwargs
):
    """使用稳定业务值创建单选分段控件，避免语言切换后状态被展示文案覆盖。"""
    options = list(options)
    if not options:
        raise ValueError(f"segmented control options cannot be empty: {key}")

    if default_value not in options:
        default_value = options[0]

    widget_key = localized_widget_key(key)
    if st.session_state.get(widget_key) not in options:
        st.session_state[widget_key] = default_value

    return st.segmented_control(
        label,
        options=options,
        selection_mode="single",
        required=True,
        format_func=format_func or str,
        key=widget_key,
        **kwargs,
    )


@st.cache_data(ttl=300, show_spinner=False)
def get_groq_model_ids(api_key: str, base_url: str) -> list[str]:
    if not api_key:
        return []

    normalized_base_url = (
        (base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    )
    models_url = f"{normalized_base_url}/models"

    try:
        response = requests.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        model_ids = []
        for item in data:
            if isinstance(item, dict):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id.strip():
                    model_ids.append(model_id.strip())

        return sorted(set(model_ids))
    except Exception as e:
        logger.warning(f"failed to fetch groq models: {e}")
        return []


def _get_material_api_keys(config_key):
    """将配置中的素材 API Key 统一转换为 WebUI 可编辑字符串。"""
    api_keys = config.app.get(config_key, [])
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    return ", ".join(api_keys)


def _save_material_api_keys(config_key, value):
    """保存逗号分隔的素材 API Key，并允许用户显式清空旧配置。"""
    normalized_value = value.replace(" ", "")
    _set_runtime_config(
        "app",
        config_key,
        normalized_value.split(",") if normalized_value else [],
    )


def _format_file_size(size_bytes):
    """将字节数格式化为适合设置页展示的紧凑容量文本。"""
    size = float(max(0, size_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


@st.cache_data(ttl=30, show_spinner=False)
def _get_video_cache_stats(max_age_days=None):
    """
    短周期缓存目录统计，避免设置弹窗内普通控件交互反复扫描大量文件。

    缓存键包含清理天数，因此切换范围只会为每个范围扫描一次；主动刷新或清理
    完成后会显式清空，最多 30 秒的缓存不会影响实际删除时的二次扫描。
    """
    return cache_manager.get_video_cache_stats(max_age_days=max_age_days)


def _render_cache_management_settings(panel):
    """渲染默认在线视频素材缓存的统计、预览和安全清理操作。"""
    with panel:
        cleanup_message = st.session_state.pop("video_cache_cleanup_message", None)
        if cleanup_message:
            message_type, message = cleanup_message
            if message_type == "success":
                st.success(message)
            else:
                st.warning(message)

        st.caption(tr("Video Cache Directory"))
        st.code(cache_manager.video_cache_dir(), language="text")

        total_stats = _get_video_cache_stats()
        metric_count, metric_size, metric_oldest = st.columns(3)
        metric_count.metric(tr("Cache File Count"), total_stats.file_count)
        metric_size.metric(
            tr("Cache Total Size"), _format_file_size(total_stats.total_size)
        )
        oldest_text = (
            datetime.fromtimestamp(total_stats.oldest_mtime).strftime("%Y-%m-%d")
            if total_stats.oldest_mtime is not None
            else "-"
        )
        metric_oldest.metric(tr("Oldest Cache Date"), oldest_text)

        st.caption(tr("Video Cache Management Help"))
        cleanup_options = (30, 7, 90, None)
        cleanup_labels = {
            30: tr("Cache Older Than 30 Days"),
            7: tr("Cache Older Than 7 Days"),
            90: tr("Cache Older Than 90 Days"),
            None: tr("All Video Cache"),
        }
        max_age_days = st.selectbox(
            tr("Cache Cleanup Range"),
            options=cleanup_options,
            format_func=lambda value: cleanup_labels[value],
            key="video_cache_cleanup_range",
        )
        cleanup_preview = _get_video_cache_stats(max_age_days=max_age_days)
        st.info(
            tr("Cache Cleanup Preview").format(
                count=cleanup_preview.file_count,
                size=_format_file_size(cleanup_preview.total_size),
            )
        )

        confirm_nonce = st.session_state.get("video_cache_cleanup_confirm_nonce", 0)
        confirmed = st.checkbox(
            tr("Confirm Cache Cleanup"),
            key=f"video_cache_cleanup_confirm_{confirm_nonce}",
        )
        refresh_col, open_col, cleanup_col = st.columns(3)
        if refresh_col.button(
            tr("Refresh Cache Stats"),
            key="refresh_video_cache_stats",
            width='stretch',
            icon=":material/refresh:",
        ):
            _get_video_cache_stats.clear()
            # 整页 rerun：这些函数都不是 @st.fragment，scope="fragment" 在这里必定抛
            # StreamlitAPIException（点“发布”“刷新缓存统计”都会炸）。
            st.rerun()

        if open_col.button(
            tr("Open Cache Directory"),
            key="open_video_cache_directory",
            width='stretch',
            icon=":material/folder_open:",
        ):
            webbrowser.open(Path(cache_manager.video_cache_dir()).as_uri())

        cleanup_disabled = not confirmed or cleanup_preview.file_count == 0
        if cleanup_col.button(
            tr("Clean Cache Now"),
            key="clean_video_cache_now",
            type="primary",
            disabled=cleanup_disabled,
            width='stretch',
            icon=":material/delete_sweep:",
            help=tr("Clean Cache Now Help"),
        ):
            result = cache_manager.clean_video_cache(max_age_days=max_age_days)
            message_key = (
                "Cache Cleanup Completed With Failures"
                if result.failed_count
                else "Cache Cleanup Completed"
            )
            st.session_state["video_cache_cleanup_message"] = (
                "warning" if result.failed_count else "success",
                tr(message_key).format(
                    count=result.deleted_count,
                    size=_format_file_size(result.deleted_size),
                    failed=result.failed_count,
                ),
            )
            # Streamlit 不允许在控件实例化后修改同名 session_state。通过递增
            # nonce 让下一次 fragment rerun 创建未勾选的新控件，避免清理完成后
            # 危险确认状态被继续保留。
            st.session_state["video_cache_cleanup_confirm_nonce"] = confirm_nonce + 1
            _get_video_cache_stats.clear()
            # 整页 rerun：这些函数都不是 @st.fragment，scope="fragment" 在这里必定抛
            # StreamlitAPIException（点“发布”“刷新缓存统计”都会炸）。
            st.rerun()


# -----------------------------------------------------------------------------
# 设置预设导出导入与密钥备份
# -----------------------------------------------------------------------------


def _is_credential_config_key(key):
    """判断一个配置项名称是否表示凭据。"""
    return str(key).endswith(CREDENTIAL_KEY_SUFFIXES)


def _is_backup_config_key(section_name, key):
    """凭据本身及其配套配置项都属于密钥备份范围。"""
    if _is_credential_config_key(key):
        return True
    if key in CREDENTIAL_COMPANION_KEYS.get(section_name, ()):
        return True
    return key in NON_LLM_COMPANION_KEYS.get(section_name, ())


def _credential_widget_state_keys(section_name, key):
    """
    返回某个凭据配置项对应的全部 Streamlit 控件 key。

    密码输入框都带 key，Streamlit 中 session_state 的值优先于控件的 value
    参数。恢复备份后必须清除这些残留控件状态，否则页面会继续显示旧密钥，
    并在下一次 rerun 把旧值重新写回配置，让恢复看起来没有生效。多个面板
    共用同一个密钥时会各自持有控件状态，因此返回默认 key 和全部别名。
    """
    if section_name == "app":
        default_widget_key = f"{key}_input"
    else:
        default_widget_key = f"{section_name}_{key}_input"
    return (
        default_widget_key,
        *CREDENTIAL_WIDGET_STATE_ALIASES.get((section_name, key), ()),
    )


def _normalize_backup_value(value):
    """归一化备份值，丢弃空字符串和空列表，避免恢复时覆盖成空配置。"""
    if isinstance(value, list):
        items = [
            str(item).strip()
            for item in value
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        return items or None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return None


def _collect_key_backup(config_sections):
    """从运行期配置分区中收集所有已填写的密钥及其配套配置项。"""
    backup = {}
    for section_name, section in config_sections.items():
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        entries = {}
        for key, value in section.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                entries[key] = normalized_value
        if entries:
            backup[section_name] = entries
    return backup


def _count_backup_keys(backup):
    """统计备份中的配置项数量，用于界面提示和禁用空导出。"""
    return sum(len(entries) for entries in backup.values())


def _build_key_backup_payload(config_sections, app_version):
    """构造密钥备份文件内容。"""
    return {
        "schema": KEY_BACKUP_SCHEMA,
        "version": KEY_BACKUP_VERSION,
        "app_version": str(app_version),
        "keys": _collect_key_backup(config_sections),
    }


def _load_transfer_payload(raw_bytes, schema, version):
    """
    解析导出文件，并校验它确实来自本功能的同一版本。

    用户可能上传任意 JSON。这里只接受声明了正确 schema 和版本的文件，让错误
    提示停留在导入入口，而不是把无法识别的内容写进配置或控件状态。
    Windows 编辑器可能保存带 BOM 的 JSON，因此按 utf-8-sig 解码。
    """
    payload = json.loads(raw_bytes.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("exported file must contain a JSON object")
    if payload.get("schema") != schema:
        raise ValueError(f"unexpected schema: {payload.get('schema')!r}")
    if payload.get("version") != version:
        raise ValueError(f"unsupported version: {payload.get('version')!r}")
    return payload


def _parse_key_backup(raw_bytes, config_sections):
    """
    解析密钥备份文件，只保留当前版本认识的分区和配置项。

    备份文件可以手工编辑，也可能来自更新的版本。未知分区或非密钥配置项一律
    忽略，避免通过导入功能改写与凭据无关的配置。
    """
    payload = _load_transfer_payload(raw_bytes, KEY_BACKUP_SCHEMA, KEY_BACKUP_VERSION)
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("key backup file has no keys object")

    restored = {}
    for section_name, entries in keys.items():
        if section_name not in config_sections:
            continue
        if section_name in KEY_BACKUP_EXCLUDED_SECTIONS:
            continue
        if not isinstance(entries, dict):
            continue
        section_entries = {}
        for key, value in entries.items():
            if not _is_backup_config_key(section_name, key):
                continue
            normalized_value = _normalize_backup_value(value)
            if normalized_value is not None:
                section_entries[key] = normalized_value
        if section_entries:
            restored[section_name] = section_entries

    if not restored:
        raise ValueError("key backup file contains no restorable keys")
    return restored


def _build_settings_preset_payload(params, app_version):
    """构造生成参数预设文件内容。"""
    preset_params = {
        key: value
        for key, value in params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    return {
        "schema": SETTINGS_PRESET_SCHEMA,
        "version": SETTINGS_PRESET_VERSION,
        "app_version": str(app_version),
        "params": preset_params,
    }


def _parse_settings_preset(raw_bytes):
    """
    解析预设文件并交给 VideoParams 校验。

    预设可以在其它机器上生成，也可能被手工编辑。统一走模型校验可以复用既有
    的取值范围约束，非法预设在导入时就被拒绝，而不是在生成任务时才失败。
    """
    payload = _load_transfer_payload(
        raw_bytes, SETTINGS_PRESET_SCHEMA, SETTINGS_PRESET_VERSION
    )
    preset_params = payload.get("params")
    if not isinstance(preset_params, dict):
        raise ValueError("settings preset file has no params object")

    params_input = {
        key: value
        for key, value in preset_params.items()
        if key not in PRESET_EXCLUDED_PARAM_KEYS
    }
    # video_subject 是 VideoParams 的必填字段，但预设允许只保存风格设置。
    params_input.setdefault("video_subject", "")
    return VideoParams.model_validate(params_input).model_dump(mode="json")


def _apply_key_backup(restored_keys):
    """把解析后的密钥写回运行期配置，并清除对应控件的残留状态。"""
    restored_count = 0
    for section_name, entries in restored_keys.items():
        for key, value in entries.items():
            _set_runtime_config(section_name, key, value)
            for widget_key in _credential_widget_state_keys(section_name, key):
                st.session_state.pop(widget_key, None)
            restored_count += 1
    # ElevenLabs 音色列表按密钥缓存，换用另一份备份后必须重新拉取。
    for cache_key in list(st.session_state.keys()):
        if str(cache_key).startswith("elevenlabs_voices_"):
            del st.session_state[cache_key]
    return restored_count


def _apply_pending_settings_preset():
    """在渲染任何控件之前应用已导入的预设。"""
    preset_params = st.session_state.pop("settings_preset_payload", None)
    if not preset_params:
        return False

    _apply_restored_params(preset_params)
    logger.info("applied imported settings preset")
    return True


def _render_settings_transfer(params):
    """渲染生成参数预设的导出与导入入口。"""
    with st.expander(tr("Settings Preset"), expanded=False):
        st.caption(tr("Settings Preset Help"))
        preset_payload = _build_settings_preset_payload(
            params.model_dump(mode="json"), config.project_version
        )
        st.download_button(
            tr("Export Settings"),
            data=json.dumps(preset_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=SETTINGS_PRESET_FILE_NAME,
            mime="application/json",
            width='stretch',
            key="export_settings_preset_button",
            icon=":material/download:",
        )
        uploaded_preset = st.file_uploader(
            tr("Import Settings"),
            type=["json"],
            key="settings_preset_uploader",
        )
        if uploaded_preset is None:
            return
        # 上传的文件在之后每次 rerun 都会重新出现。记录已处理的文件标识，
        # 避免用户改完控件后被同一个预设反复覆盖。
        if st.session_state.get("settings_preset_file_id") == uploaded_preset.file_id:
            return

        st.session_state["settings_preset_file_id"] = uploaded_preset.file_id
        try:
            preset_params = _parse_settings_preset(uploaded_preset.getvalue())
        except Exception as e:
            logger.warning(f"failed to import settings preset: {e}")
            st.error(tr("Settings Preset Import Failed"))
            return

        st.session_state["settings_preset_payload"] = preset_params
        st.rerun()


def _render_key_backup_settings(panel):
    """渲染密钥备份的导出与恢复入口。"""
    with panel:
        backup_message = st.session_state.pop("key_backup_message", None)
        if backup_message:
            message_type, message = backup_message
            if message_type == "success":
                st.success(message)
            else:
                st.error(message)

        st.caption(tr("Key Backup Help"))
        st.warning(tr("Key Backup Warning"))

        backup_payload = _build_key_backup_payload(
            _RUNTIME_CONFIG_SECTIONS, config.project_version
        )
        backup_key_count = _count_backup_keys(backup_payload["keys"])
        st.caption(tr("Key Backup Summary").format(count=backup_key_count))
        st.download_button(
            tr("Export Keys"),
            data=json.dumps(backup_payload, ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
            file_name=KEY_BACKUP_FILE_NAME,
            mime="application/json",
            disabled=backup_key_count == 0,
            width='stretch',
            key="export_key_backup_button",
            icon=":material/download:",
        )

        uploaded_backup = st.file_uploader(
            tr("Import Keys"),
            type=["json"],
            key="key_backup_uploader",
        )
        if uploaded_backup is None:
            return
        if st.session_state.get("key_backup_file_id") == uploaded_backup.file_id:
            return

        st.session_state["key_backup_file_id"] = uploaded_backup.file_id
        try:
            restored_keys = _parse_key_backup(
                uploaded_backup.getvalue(), _RUNTIME_CONFIG_SECTIONS
            )
        except Exception as e:
            logger.warning(f"failed to import key backup: {e}")
            st.session_state["key_backup_message"] = (
                "error",
                tr("Key Restore Failed"),
            )
        else:
            restored_count = _apply_key_backup(restored_keys)
            _save_runtime_config()
            logger.info(f"restored keys from backup file: count={restored_count}")
            st.session_state["key_backup_message"] = (
                "success",
                tr("Keys Restored").format(count=restored_count),
            )
        # 主页面上的 TTS 密钥输入框也需要读取恢复后的配置，因此整页刷新。
        # 设置弹窗的打开状态保存在 session_state 中，刷新后会重新展开。
        st.rerun(scope="app")


# -----------------------------------------------------------------------------
# 设置与提示词弹窗
# -----------------------------------------------------------------------------


# 设置属于低频操作，使用中等尺寸 Dialog 避免长期占用主页面纵向空间，
# 同时控制阅读行宽，避免弹窗在宽屏设备上显得过于松散。
# Dialog 继承 fragment 行为，内部控件交互只重绘弹窗；函数末尾单独保存配置，
# 关闭时通过回调触发整页同步，确保生成流程读取最新 Provider 和界面设置。
def _render_settings_body():
    """设置项的正文。左侧导航的「设置」页与顶部栏的设置弹窗共用这一段。

    两者刻意共用：设置项有二十多个，任何一处改而另一处漏掉，用户就会在"页面里有、
    弹窗里没有"这种极难自查的分叉里迷路。
    """
    with st.container():
        # 历史 hide_config 只用于隐藏旧基础设置面板。改为固定设置入口后，该值
        # 不再有用户可见意义，统一迁移为 false，避免旧配置影响后续版本。
        _set_runtime_config("app", "hide_config", False)
        (
            middle_config_panel,
            right_config_panel,
            key_backup_panel,
            cache_config_panel,
            publish_config_panel,
            left_config_panel,
        ) = st.tabs(
            [
                tr("LLM Settings Tab"),
                tr("Material API Tab"),
                tr("Key Backup Tab"),
                tr("Cache Management Tab"),
                tr("Auto-Publish Settings"),
                tr("Interface Settings Tab"),
            ]
        )

        with publish_config_panel:
            st.subheader(tr("YouTube API Publishing"))
            st.caption(tr("YouTube API Setup Help"))
            for field, label in (
                ("youtube_upload_client_id", "YouTube OAuth Client ID"),
                ("youtube_upload_client_secret", "YouTube OAuth Client Secret"),
            ):
                current = str(config.app.get(field, "") or "")
                value = st.text_input(tr(label), value=current, type="password", key=f"{field}_input")
                if value != current:
                    _set_runtime_config("app", field, value.strip())
            privacy_options = ["public", "unlisted", "private"]
            saved_privacy = config.app.get("youtube_upload_privacy_status", "public")
            if saved_privacy not in privacy_options:
                saved_privacy = "public"
            privacy = st.selectbox(
                tr("YouTube Privacy Status"), privacy_options,
                index=privacy_options.index(saved_privacy), key="youtube_api_privacy",
            )
            if privacy != saved_privacy:
                _set_runtime_config("app", "youtube_upload_privacy_status", privacy)
            auto_publish = st.checkbox(
                tr("YouTube Auto Publish"),
                value=bool(config.app.get("youtube_upload_auto_publish", True)),
                key="youtube_api_auto_publish",
            )
            if auto_publish != bool(config.app.get("youtube_upload_auto_publish", True)):
                _set_runtime_config("app", "youtube_upload_auto_publish", auto_publish)
            st.divider()
            st.write(tr("Automatically publish generated videos to social media using upload-post.com"))
            st.info(
                tr("Upload-Post Setup Guide").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL,
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL,
                )
            )

            is_enabled = config.app.get("upload_post_enabled", False)
            is_auto = config.app.get("upload_post_auto_upload", False)

            # 两个键各自独立:enabled 允许外部流程调用 Upload-Post,
            # auto_upload 才决定渲染完成后是否自动发布。合并成一个复选框会在
            # 两键不一致的配置下,仅打开设置对话框就把 enabled 改写为 False。
            upload_post_enabled = st.checkbox(
                tr("Enable Upload-Post Integration"),
                value=is_enabled,
                key="upload_post_enabled_checkbox"
            )
            if upload_post_enabled != is_enabled:
                _set_runtime_config("app", "upload_post_enabled", upload_post_enabled)

            upload_post_auto_upload = st.checkbox(
                tr("Enable Auto-Publish"),
                value=is_auto,
                key="upload_post_auto_upload_checkbox"
            )
            if upload_post_auto_upload != is_auto:
                _set_runtime_config("app", "upload_post_auto_upload", upload_post_auto_upload)

            upload_post_api_key = st.text_input(
                tr("Upload-Post API Key"),
                value=config.app.get("upload_post_api_key", ""),
                type="password",
                help=tr("Upload-Post API Key Help").format(
                    api_keys_url=UPLOAD_POST_API_KEYS_URL
                ),
                key="upload_post_api_key_input"
            )
            if upload_post_api_key != config.app.get("upload_post_api_key", ""):
                _set_runtime_config("app", "upload_post_api_key", upload_post_api_key)

            upload_post_username = st.text_input(
                tr("Upload-Post Profile Username"),
                value=config.app.get("upload_post_username", ""),
                help=tr("Upload-Post Profile Username Help").format(
                    manage_users_url=UPLOAD_POST_MANAGE_USERS_URL
                ),
                key="upload_post_username_input"
            )
            if upload_post_username != config.app.get("upload_post_username", ""):
                _set_runtime_config("app", "upload_post_username", upload_post_username)

            upload_post_platforms = st.multiselect(
                tr("Platforms"),
                options=["tiktok", "instagram", "youtube"],
                default=config.app.get("upload_post_platforms", ["tiktok", "instagram"]),
                help="Select platforms to publish to",
                key="upload_post_platforms_multiselect"
            )
            if upload_post_platforms != config.app.get("upload_post_platforms", ["tiktok", "instagram"]):
                _set_runtime_config("app", "upload_post_platforms", upload_post_platforms)

            if "youtube" in upload_post_platforms:
                yt_status_options = ["public", "private", "unlisted"]
                yt_saved = config.app.get("upload_post_youtube_privacy_status", "public")
                if yt_saved not in yt_status_options:
                    yt_saved = "public"
                upload_post_youtube_privacy_status = st.selectbox(
                    tr("YouTube Privacy Status"),
                    options=yt_status_options,
                    index=yt_status_options.index(yt_saved),
                    key="upload_post_youtube_privacy_status_selectbox"
                )
                if upload_post_youtube_privacy_status != config.app.get("upload_post_youtube_privacy_status", "public"):
                    _set_runtime_config("app", "upload_post_youtube_privacy_status", upload_post_youtube_privacy_status)

        # 左侧面板 - 日志与完成行为
        with left_config_panel:
            hide_log = st.checkbox(
                tr("Hide Log"),
                value=config.ui.get("hide_log", False),
                key="hide_log_checkbox",
            )
            _set_runtime_config("ui", "hide_log", hide_log)

            # 任务完成时自动弹出 Finder 窗口。这个行为每完成一个视频就触发一次，
            # 此前只有 config.toml 里的开关、全应用没有任何界面入口，于是用户既
            # 不知道它在发生，也没法在界面上关掉它——"界面一跳一跳"里它占很大一份。
            # 读侧 _render_running_generation_task 用 .get(key, True) 兜底，这里
            # 同用 True，两边对"键不存在"的解释必须一致，否则开关存下去的值和
            # 实际行为会差一档。
            open_folder = st.checkbox(
                tr("Open Output Folder On Completion"),
                value=config.ui.get("open_task_folder_on_completion", True),
                key="open_task_folder_on_completion_checkbox",
                help=tr("Open Output Folder On Completion Help"),
            )
            if open_folder != config.ui.get("open_task_folder_on_completion", True):
                _set_runtime_config(
                    "ui", "open_task_folder_on_completion", open_folder
                )

        _render_cache_management_settings(cache_config_panel)
        # 密钥恢复会写回配置并清除密码控件状态，必须在下面渲染这些控件之前执行。
        _render_key_backup_settings(key_backup_panel)

        # 中间面板 - LLM 设置

        with middle_config_panel:
            # 下拉顺序、默认 label 和稳定 provider id 全部来自 Registry；locale
            # 只覆盖展示文案，不再让 Main.py 维护第二份 Provider 列表。
            llm_provider_ids = [
                provider.provider_id for provider in LLM_PROVIDER_REGISTRY
            ]
            llm_provider_labels = {
                provider.provider_id: get_llm_provider_label(provider)
                for provider in LLM_PROVIDER_REGISTRY
            }
            saved_llm_provider = config.app.get(
                "llm_provider", DEFAULT_LLM_PROVIDER_ID
            ).lower()
            if saved_llm_provider not in llm_provider_ids:
                saved_llm_provider = DEFAULT_LLM_PROVIDER_ID

            llm_provider = stable_selectbox(
                tr("LLM Provider"),
                options=llm_provider_ids,
                default_value=saved_llm_provider,
                key="llm_provider_select",
                format_func=lambda provider_id: llm_provider_labels[provider_id],
            )
            # 配置表单和 Provider 说明并排展示，减少长说明在窄列中的换行，
            # 同时充分利用基础设置面板的横向空间。
            llm_form_panel, llm_help_panel = st.columns(
                [0.9, 1.1],
                gap="large",
                vertical_alignment="top",
            )
            llm_helper = llm_help_panel.container()
            _set_runtime_config("app", "llm_provider", llm_provider)
            llm_provider_spec = get_llm_provider(llm_provider)
            if llm_provider_spec is None:
                # 正常情况下下拉选项全部来自 Registry，不会进入该分支；保留
                # 明确错误用于诊断损坏的 session state 或后续接入遗漏。
                raise RuntimeError(f"unsupported llm provider: {llm_provider}")

            llm_api_key = config.app.get(llm_provider_spec.config_key("api_key"), "")
            configured_llm_base_url = config.app.get(
                llm_provider_spec.config_key("base_url"), ""
            )
            llm_default_base_url = llm_provider_spec.effective_default_base_url
            llm_base_url = configured_llm_base_url or llm_default_base_url
            llm_model_name = llm_provider_spec.resolve_model_name(
                config.app.get(llm_provider_spec.config_key("model_name"), "")
            )

            provider_tip_context = {}
            selected_service_endpoint = None
            if llm_provider_spec.service_endpoints:
                # Kimi 等 Provider 的中国站和国际站使用不同账号体系。只让用户
                # 选择服务区域，再由 Registry 同步 API 申请入口和 Base URL，
                # 避免手工组合错误。已有空 Base URL 配置继续沿用中国站，只有
                # 尚未填写 Key 的全新配置才根据界面语言推荐对应入口。
                selected_service_endpoint = (
                    llm_provider_spec.select_service_endpoint(
                        configured_llm_base_url,
                        has_api_key=bool(str(llm_api_key).strip()),
                        prefer_international=(
                            st.session_state.get("ui_language", "en") != "zh"
                        ),
                    )
                )
                endpoint_options = [
                    endpoint.endpoint_id
                    for endpoint in llm_provider_spec.service_endpoints
                ] + [CUSTOM_LLM_ENDPOINT_ID]
                default_endpoint_id = (
                    selected_service_endpoint.endpoint_id
                    if selected_service_endpoint
                    else CUSTOM_LLM_ENDPOINT_ID
                )
                endpoint_labels = {
                    endpoint.endpoint_id: (
                        tr_optional(
                            llm_provider_spec.endpoint_label_key(endpoint.endpoint_id),
                            fallback_language="en",
                        )
                        or endpoint.default_label
                    )
                    for endpoint in llm_provider_spec.service_endpoints
                }
                endpoint_labels[CUSTOM_LLM_ENDPOINT_ID] = (
                    tr_optional("Custom API Endpoint", fallback_language="en")
                    or "Custom API Endpoint"
                )
                with llm_form_panel:
                    selected_endpoint_id = stable_selectbox(
                        tr_optional(
                            llm_provider_spec.endpoint_selector_label_key,
                            fallback_language="en",
                        )
                        or tr("API Platform"),
                        options=endpoint_options,
                        default_value=default_endpoint_id,
                        key=f"{llm_provider}_service_endpoint_select",
                        format_func=lambda endpoint_id: endpoint_labels[endpoint_id],
                        help=(
                            tr_optional(
                                llm_provider_spec.endpoint_selector_help_key,
                                fallback_language="en",
                            )
                            or None
                        ),
                    )
                selected_service_endpoint = next(
                    (
                        endpoint
                        for endpoint in llm_provider_spec.service_endpoints
                        if endpoint.endpoint_id == selected_endpoint_id
                    ),
                    None,
                )
                if selected_service_endpoint:
                    llm_base_url = selected_service_endpoint.base_url
                    provider_tip_context.update(
                        {
                            "api_key_url": selected_service_endpoint.api_key_url,
                            "default_base_url": selected_service_endpoint.base_url,
                            "model_docs_url": selected_service_endpoint.model_docs_url,
                        }
                    )
                else:
                    # 自定义模式只保留用户明确保存的地址，不将某个标准区域伪装
                    # 成自定义值。输入为空时配置不会持久化，下一次仍回到兼容默认。
                    llm_base_url = str(configured_llm_base_url or "").strip()

            if llm_provider == "ollama":
                llm_default_base_url = config.get_default_ollama_base_url()
                if not llm_base_url:
                    llm_base_url = llm_default_base_url
                docker_hint = ""
                if config.is_running_in_container():
                    docker_hint = tr_optional(
                        "llm_provider_tips.ollama.docker_hint",
                        fallback_language="en",
                    )
                provider_tip_context["docker_hint"] = docker_hint

            tips = get_llm_provider_tips(llm_provider, **provider_tip_context)
            if tips:
                with llm_helper:
                    st.info(tips)

            st_llm_api_key = llm_api_key
            if llm_provider_spec.show_api_key:
                st_llm_api_key = llm_form_panel.text_input(
                    tr("API Key"),
                    value=llm_api_key,
                    type="password",
                    key=f"{llm_provider}_api_key_input",
                )

            st_llm_base_url = llm_base_url
            if llm_provider_spec.show_base_url:
                st_llm_base_url = llm_form_panel.text_input(
                    tr("Base Url"),
                    value=llm_base_url,
                    key=(
                        f"{llm_provider}_base_url_"
                        f"{selected_service_endpoint.endpoint_id}_input"
                        if selected_service_endpoint
                        else f"{llm_provider}_base_url_custom_input"
                    ),
                    disabled=selected_service_endpoint is not None,
                )
            st_llm_model_name = ""
            if llm_provider == "groq":
                effective_api_key = st_llm_api_key or llm_api_key
                effective_base_url = st_llm_base_url or llm_base_url
                groq_models = get_groq_model_ids(
                    api_key=effective_api_key,
                    base_url=effective_base_url,
                )

                if groq_models:
                    selected_index = 0
                    if llm_model_name in groq_models:
                        selected_index = groq_models.index(llm_model_name)

                    st_llm_model_name = llm_form_panel.selectbox(
                        tr("Model Name"),
                        options=groq_models,
                        index=selected_index,
                        key="groq_model_name_select",
                    )
                else:
                    st_llm_model_name = llm_form_panel.text_input(
                        tr("Model Name"),
                        value=llm_model_name,
                        key="groq_model_name_input",
                    )
                    if effective_api_key:
                        llm_form_panel.caption(tr("Groq Model List Load Failed"))
                    else:
                        llm_form_panel.caption(
                            tr("Groq API Key Required for Model List")
                        )
            else:
                st_llm_model_name = llm_form_panel.text_input(
                    tr("Model Name"),
                    value=llm_model_name,
                    key=f"{llm_provider}_model_name_input",
                )
            # 输入框展示 Registry 默认值，但配置只保存真实的用户覆盖值。
            # 这样默认模型、Base URL 更新后，未自定义的用户能够自动跟随。
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("api_key"),
                st_llm_api_key,
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("base_url"),
                normalize_provider_override(
                    st_llm_base_url,
                    llm_default_base_url,
                ),
            )
            _set_runtime_config(
                "app",
                llm_provider_spec.config_key("model_name"),
                normalize_provider_override(
                    st_llm_model_name,
                    llm_provider_spec.default_model,
                ),
            )

            # Provider 专用字段也由 Registry 声明。例如 Cloudflare AI Gateway
            # 需要 Account ID；以后新增类似字段时无需再在 Main.py 增加判断。
            for field in llm_provider_spec.extra_fields:
                field_config_key = llm_provider_spec.config_key(field.config_suffix)
                field_value = llm_form_panel.text_input(
                    tr(field.label_key),
                    value=(config.app.get(field_config_key, "") or field.default_value),
                    type="password" if field.secret else "default",
                    key=f"{llm_provider}_{field.config_suffix}_input",
                )
                _set_runtime_config(
                    "app",
                    field_config_key,
                    normalize_provider_override(
                        field_value,
                        field.default_value,
                    ),
                )

            if llm_form_panel.button(
                tr("Test LLM Connection"),
                key="test_llm_connection_button",
                width='stretch',
                type="secondary",
                icon=":material/network_check:",
            ):
                with config.try_runtime_config_lock() as lock_acquired:
                    if not lock_acquired:
                        llm_form_panel.warning(tr("Runtime Configuration Busy"))
                    else:
                        with llm_form_panel.spinner(tr("Testing LLM Connection")):
                            connection_ok, connection_error, connection_elapsed = (
                                llm.test_connection()
                            )

                if not lock_acquired:
                    connection_ok = None
                elif connection_ok:
                    llm_form_panel.success(
                        tr("LLM Connection Test Succeeded").format(
                            provider=llm_provider_labels[llm_provider],
                            model=st_llm_model_name or "-",
                            elapsed=f"{connection_elapsed:.2f}",
                        )
                    )
                else:
                    connection_error = format_llm_connection_error(
                        llm_provider,
                        st_llm_base_url,
                        connection_error,
                    )
                    llm_form_panel.error(
                        tr("LLM Connection Test Failed").format(error=connection_error)
                    )

        # 右侧面板 - API 密钥设置
        with right_config_panel:
            pexels_api_key = _get_material_api_keys("pexels_api_keys")
            pexels_api_key = st.text_input(
                tr("Pexels API Key"),
                value=pexels_api_key,
                type="password",
                key="pexels_api_keys_input",
            )
            _save_material_api_keys("pexels_api_keys", pexels_api_key)

            pixabay_api_key = _get_material_api_keys("pixabay_api_keys")
            pixabay_api_key = st.text_input(
                tr("Pixabay API Key"),
                value=pixabay_api_key,
                type="password",
                key="pixabay_api_keys_input",
            )
            _save_material_api_keys("pixabay_api_keys", pixabay_api_key)

            coverr_api_key = _get_material_api_keys("coverr_api_keys")
            coverr_api_key = st.text_input(
                tr("Coverr API Key"),
                value=coverr_api_key,
                type="password",
                key="coverr_api_keys_input",
            )
            _save_material_api_keys("coverr_api_keys", coverr_api_key)

            wavespeed_api_key = _get_material_api_keys("wavespeed_api_keys")
            wavespeed_api_key = st.text_input(
                tr("WaveSpeed API Key"),
                value=wavespeed_api_key,
                type="password",
                key="wavespeed_api_keys_input",
            )
            _save_material_api_keys("wavespeed_api_keys", wavespeed_api_key)

            seedance_api_key = st.text_input(
                tr("Volcano Engine Ark API Key"),
                value=str(config.app.get("volcengine_seedance_api_key", "") or ""),
                type="password",
                key="volcengine_seedance_api_key_input",
            )
            _set_runtime_config(
                "app", "volcengine_seedance_api_key", seedance_api_key.strip()
            )
            seedance_model = st.text_input(
                tr("Volcano Engine Seedance Model"),
                value=str(
                    config.app.get(
                        "volcengine_seedance_model",
                        volcengine_seedance.DEFAULT_MODEL_ID,
                    )
                    or volcengine_seedance.DEFAULT_MODEL_ID
                ),
                key="volcengine_seedance_model_input",
            )
            _set_runtime_config(
                "app", "volcengine_seedance_model", seedance_model.strip()
            )
            seedance_base_url = st.text_input(
                tr("Volcano Engine Ark Base URL"),
                value=str(
                    config.app.get(
                        "volcengine_seedance_base_url",
                        volcengine_seedance.DEFAULT_BASE_URL,
                    )
                    or volcengine_seedance.DEFAULT_BASE_URL
                ),
                key="volcengine_seedance_base_url_input",
            )
            _set_runtime_config(
                "app", "volcengine_seedance_base_url", seedance_base_url.strip()
            )

    _save_runtime_config()


# -----------------------------------------------------------------------------
# 主生成表单：文案、视频、音频与字幕面板
# -----------------------------------------------------------------------------


# 设置属于低频操作，顶部栏那个入口仍然用中等尺寸 Dialog：它多半是"临时改个 key
# 就走"，弹窗关掉即回到原来的页面，不必把主页面整个换掉。
@st.dialog(
    tr("Settings"),
    width="medium",
    on_dismiss=_dismiss_settings_dialog,
)
def _render_settings_dialog():
    _render_settings_body()


def _create_loomloom_script_backend():
    """从当前 WebUI/config.toml 配置创建批量文案客户端。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.LoomLoomSettings.from_mapping(app_config_snapshot)
    return loomloom.LoomLoomScriptBackend(settings)


def _create_loomloom_video_backend():
    """使用项目默认 SkillBot 和当前有效凭证创建视频客户端。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    settings = loomloom.video_settings_from_mapping(app_config_snapshot)
    return loomloom.LoomLoomVideoBackend(settings)


def _effective_loomloom_api_token():
    """读取 WebUI 尚未落盘或 config.toml 中的胜算云 API Key。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    return loomloom.resolve_api_token(app_config_snapshot)


def _effective_script_generation_backend():
    """读取包含 WebUI 待保存修改的文案生成方式。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    backend = str(
        app_config_snapshot.get("script_generation_backend", "local") or "local"
    ).strip()
    return backend if backend in {"local", "loomloom"} else "local"


def _render_loomloom_api_token_input():
    """仅在未选择胜算云 Provider 时显示独立 LoomLoom 密钥输入。"""
    app_config_snapshot = config.snapshot_config_with_pending(config.app)
    if str(app_config_snapshot.get("llm_provider", "") or "").lower() == "shengsuanyun":
        st.caption(tr("Shengsuan Cloud API Key Reused"))
        return loomloom.resolve_api_token(app_config_snapshot)

    configured_token = loomloom.resolve_api_token(app_config_snapshot)
    st.session_state.setdefault("loomloom_user_api_token", configured_token)
    api_token = st.text_input(
        tr("Shengsuan Cloud API Key"),
        type="password",
        key="loomloom_user_api_token",
        help=tr("Shengsuan Cloud API Key Help"),
        placeholder=tr("Shengsuan Cloud API Key Placeholder"),
    ).strip()
    _set_runtime_config("app", "loomloom_api_token", api_token)
    return _effective_loomloom_api_token()


def _loomloom_video_scene_prompts(video_terms, subject, scene_count):
    """按素材关键词生成有限数量的场景描述，供视频模型逐段生成素材。"""
    if isinstance(video_terms, str):
        terms = [
            term.strip() for term in re.split(r"[,，\n]", video_terms) if term.strip()
        ]
    elif isinstance(video_terms, list):
        terms = [
            str(term or "").strip() for term in video_terms if str(term or "").strip()
        ]
    else:
        terms = []
    fallback = str(subject or "").strip()
    if not terms and fallback:
        terms = [fallback]
    if not terms:
        return ()
    return tuple(
        (
            terms[index % len(terms)]
            if index < len(terms)
            else f"{terms[index % len(terms)]}; alternative camera angle {index + 1}"
        )
        for index in range(int(scene_count))
    )


def _loomloom_video_signature(batch, credential_fingerprint):
    """将全部计费输入和凭证摘要纳入签名，参数变化后强制重新报价。"""
    payload = {
        "inputRows": [dict(row) for row in batch.input_rows],
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _current_loomloom_video_quote_context(params):
    """根据当前页面参数构建默认 SkillBot 的视频报价批次。"""
    token = _effective_loomloom_api_token()
    scene_count = int(st.session_state.get("loomloom_video_scene_count", 1) or 1)
    prompts = _loomloom_video_scene_prompts(
        params.video_terms,
        params.video_subject or params.video_script,
        scene_count,
    )
    if not token or not prompts:
        return None, ""
    try:
        batch = _create_loomloom_video_backend().prepare_video_batch(
            subject=params.video_subject or params.video_script,
            scene_prompts=prompts,
            aspect_ratio=str(
                params.video_aspect.value
                if isinstance(params.video_aspect, VideoAspect)
                else params.video_aspect
            ),
        )
    except (loomloom.LoomLoomError, ValueError):
        return None, ""
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return batch, _loomloom_video_signature(batch, fingerprint)


def _render_loomloom_video_settings(params):
    """渲染默认视频 SkillBot 的报价、报价失效和付费确认流程。"""
    st.caption(tr("Shengsuan Cloud AI Video Help"))
    if _effective_script_generation_backend() != "loomloom":
        _render_loomloom_api_token_input()
    elif (
        str(
            config.snapshot_config_with_pending(config.app).get("llm_provider", "")
            or ""
        ).lower()
        == "shengsuanyun"
    ):
        st.caption(tr("Shengsuan Cloud API Key Reused"))

    token = _effective_loomloom_api_token()

    scene_count = st.number_input(
        tr("AI Video Scene Count"),
        min_value=1,
        max_value=loomloom.MAX_VIDEO_SCENES,
        step=1,
        key="loomloom_video_scene_count",
    )
    _set_runtime_config("ui", "loomloom_video_scene_count", int(scene_count))
    batch, input_signature = _current_loomloom_video_quote_context(params)
    if not token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_videos",
        width='stretch',
        type="secondary",
        icon=":material/request_quote:",
        disabled=not token or batch is None,
        help=tr("LoomLoom Video Quote Help"),
    ):
        try:
            quote_result = _create_loomloom_video_backend().quote(batch)
        except (loomloom.LoomLoomError, ValueError) as exc:
            logger.warning(f"failed to quote LoomLoom videos: error={exc}")
            st.error(str(exc))
        else:
            st.session_state["loomloom_video_batch"] = batch
            st.session_state["loomloom_video_quote"] = quote_result
            st.session_state["loomloom_video_input_signature"] = input_signature
            st.session_state["loomloom_video_client_request_id"] = (
                f"mpt-video-{uuid4()}"
            )
            st.session_state["loomloom_video_confirm_charge"] = False
            logger.info(
                "LoomLoom video quote ready: "
                f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
            )

    quote_result = st.session_state.get("loomloom_video_quote")
    quoted_batch = st.session_state.get("loomloom_video_batch")
    if quote_result is not None and quoted_batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "AI Video Quote Summary Singular"
                if quote_result.task_count == 1
                else "AI Video Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_video_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        st.checkbox(
            tr("Confirm AI Video Charge"),
            key="loomloom_video_confirm_charge",
            help=tr("Confirm AI Video Charge Help"),
            disabled=not quote_is_current,
        )


def _loomloom_script_signature(
    *,
    subject,
    language,
    candidate_count,
    duration_seconds,
    style,
    credential_fingerprint,
):
    payload = {
        "subject": str(subject or "").strip(),
        "language": str(language or "auto").strip() or "auto",
        "candidateCount": int(candidate_count),
        "durationSeconds": int(duration_seconds),
        "style": str(style or "").strip(),
        "credentialFingerprint": str(credential_fingerprint or "").strip(),
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _render_local_script_generation(params):
    """保留 MoneyPrinterTurbo 原有的本地 LLM 脚本生成路径。"""
    if not st.button(
        tr("Generate Video Script and Keywords"),
        key="auto_generate_script",
        width='stretch',
        type="secondary",
        icon=":material/auto_awesome:",
    ):
        return

    if not params.video_subject:
        st.toast(tr("Please Enter the Video Subject First"))
        st.warning(tr("Please Enter the Video Subject First"))
        return

    with st.spinner(tr("Generating Video Script and Keywords")):

        def generate_script_and_terms(app_config_snapshot):
            script = llm.generate_script(
                video_subject=params.video_subject,
                language=params.video_language,
                paragraph_number=params.paragraph_number,
                video_script_prompt=params.video_script_prompt,
                custom_system_prompt=params.custom_system_prompt,
                app_config=app_config_snapshot,
            )
            terms = llm.generate_terms(
                params.video_subject,
                script,
                amount=8 if params.match_materials_to_script else 5,
                match_script_order=params.match_materials_to_script,
                app_config=app_config_snapshot,
            )
            return script, terms

        script, terms = _run_llm_read_operation(
            "generate_script_and_terms",
            generate_script_and_terms,
        )
        if "Error: " in script:
            st.error(tr(script))
        elif "Error: " in terms:
            st.error(tr(terms))
        else:
            st.session_state["video_script"] = script
            st.session_state["video_terms"] = ", ".join(terms)


def _render_loomloom_candidates():
    candidates = tuple(st.session_state.get("loomloom_script_candidates") or ())
    errors = tuple(st.session_state.get("loomloom_candidate_errors") or ())
    if errors:
        st.warning(
            tr("LoomLoom Candidate Errors").format(
                count=len(errors),
                details="; ".join(
                    f"#{error.row_index + 1}: {error.message}" for error in errors
                ),
            )
        )
    if not candidates:
        return

    selected_index = st.radio(
        tr("Choose Script Candidate"),
        options=list(range(len(candidates))),
        key="loomloom_selected_candidate",
        format_func=lambda index: (
            f"#{candidates[index].row_index + 1} {candidates[index].script[:80]}"
        ),
    )
    selected = candidates[selected_index]
    st.code(selected.script, language=None, wrap_lines=True)
    st.caption(", ".join(selected.video_terms))
    if st.button(
        tr("Use Selected Candidate"),
        key="loomloom_apply_candidate",
        type="primary",
        width='stretch',
    ):
        st.session_state["video_script"] = selected.script
        st.session_state["video_terms"] = ", ".join(selected.video_terms)
        st.toast(tr("LoomLoom Candidate Applied"))


def _handle_loomloom_poll_error(run_id, exc):
    """对脚本任务轮询错误做有限退避，确定性错误立即停止轮询。"""
    logger.warning(f"failed to poll LoomLoom run: run_id={run_id}, error={exc}")
    failure_count = int(st.session_state.get("loomloom_poll_failure_count", 0) or 0) + 1
    retryable = isinstance(exc, loomloom.LoomLoomAPIError) and exc.retryable
    if not retryable or failure_count >= LOOMLOOM_MAX_POLL_FAILURES:
        st.session_state["loomloom_run_error"] = str(exc)
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        # 查询失败不等于远端付费任务失败。保留 run_id 并暂停自动轮询，让用户
        # 可以继续查询同一个任务；如果直接丢弃 ID 后重新提交，可能重复付费。
        st.session_state["loomloom_poll_paused"] = True
        st.rerun(scope="app")
        return

    retry_delay = min(2**failure_count, 30)
    st.session_state["loomloom_poll_failure_count"] = failure_count
    st.session_state["loomloom_poll_retry_after"] = time.monotonic() + retry_delay
    st.warning(
        tr("LoomLoom Poll Retry Warning").format(
            attempt=failure_count,
            max_attempts=LOOMLOOM_MAX_POLL_FAILURES,
        )
    )


@st.fragment(run_every="2s")
def _render_loomloom_run_progress():
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if not run_id or st.session_state.get("loomloom_poll_paused", False):
        return
    retry_after = float(st.session_state.get("loomloom_poll_retry_after", 0.0) or 0.0)
    retry_wait_seconds = max(0, int(math.ceil(retry_after - time.monotonic())))
    if retry_wait_seconds > 0:
        st.info(
            tr("LoomLoom Poll Retry Pending").format(
                seconds=retry_wait_seconds,
            )
        )
        return
    try:
        backend = _create_loomloom_script_backend()
        run = backend.get_run(run_id)
    except loomloom.LoomLoomError as exc:
        _handle_loomloom_poll_error(run_id, exc)
        return

    st.session_state["loomloom_run_status"] = run.status
    if run.status == "completed":
        try:
            result = backend.get_script_results(run_id)
        except loomloom.LoomLoomError as exc:
            _handle_loomloom_poll_error(run_id, exc)
            return
        st.session_state["loomloom_poll_failure_count"] = 0
        st.session_state["loomloom_poll_retry_after"] = 0.0
        st.session_state["loomloom_poll_paused"] = False
        st.session_state["loomloom_script_candidates"] = result.candidates
        st.session_state["loomloom_candidate_errors"] = result.errors
        st.session_state["loomloom_selected_candidate"] = 0
        st.session_state["loomloom_run_id"] = ""
        st.rerun(scope="app")
        return
    if run.status in {"failed", "cancelled", "canceled"}:
        st.session_state["loomloom_run_error"] = run.first_error_message or run.status
        st.session_state["loomloom_run_id"] = ""
        st.session_state["loomloom_poll_paused"] = False
        st.rerun(scope="app")
        return

    st.session_state["loomloom_poll_failure_count"] = 0
    st.session_state["loomloom_poll_retry_after"] = 0.0
    st.info(
        tr("LoomLoom Run Progress").format(
            completed=run.completed_tasks,
            total=run.total_tasks,
        )
    )


def _render_loomloom_script_generation(params):
    st.caption(tr("LoomLoom Batch Script Generation Help"))
    effective_token = _render_loomloom_api_token_input()
    if not effective_token:
        st.warning(tr("Shengsuan Cloud API Key Required"))

    candidate_col, duration_col = st.columns(2)
    candidate_count = candidate_col.number_input(
        tr("Script Candidate Count"),
        min_value=1,
        max_value=loomloom.MAX_SCRIPT_CANDIDATES,
        step=1,
        key="loomloom_candidate_count",
    )
    duration_seconds = duration_col.number_input(
        tr("Target Script Duration Seconds"),
        min_value=10,
        max_value=600,
        step=10,
        key="loomloom_script_duration_seconds",
    )
    _set_runtime_config("ui", "loomloom_candidate_count", int(candidate_count))
    _set_runtime_config(
        "ui", "loomloom_script_duration_seconds", int(duration_seconds)
    )
    input_signature = _loomloom_script_signature(
        subject=params.video_subject,
        language=params.video_language,
        candidate_count=candidate_count,
        duration_seconds=duration_seconds,
        style=params.video_script_prompt,
        credential_fingerprint=(
            hashlib.sha256(effective_token.encode("utf-8")).hexdigest()
            if effective_token
            else ""
        ),
    )

    if st.button(
        tr("Get LoomLoom Quote"),
        key="loomloom_quote_scripts",
        width='stretch',
        type="secondary",
        icon=":material/request_quote:",
        disabled=not effective_token or bool(st.session_state.get("loomloom_run_id")),
        help=tr("LoomLoom Script Quote Help"),
    ):
        if not params.video_subject:
            st.toast(tr("Please Enter the Video Subject First"))
            st.warning(tr("Please Enter the Video Subject First"))
        else:
            try:
                backend = _create_loomloom_script_backend()
                batch = backend.prepare_script_batch(
                    subject=params.video_subject,
                    candidate_count=int(candidate_count),
                    language=params.video_language,
                    duration_seconds=int(duration_seconds),
                    style=params.video_script_prompt,
                )
                quote_result = backend.quote(batch)
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to quote LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_script_batch"] = batch
                st.session_state["loomloom_script_quote"] = quote_result
                st.session_state["loomloom_script_input_signature"] = input_signature
                st.session_state["loomloom_client_request_id"] = f"mpt-{uuid4()}"
                st.session_state["loomloom_run_id"] = ""
                st.session_state["loomloom_run_status"] = "quoted"
                st.session_state["loomloom_run_error"] = ""
                st.session_state["loomloom_poll_failure_count"] = 0
                st.session_state["loomloom_poll_retry_after"] = 0.0
                st.session_state["loomloom_poll_paused"] = False
                st.session_state["loomloom_script_candidates"] = ()
                st.session_state["loomloom_candidate_errors"] = ()
                st.session_state["loomloom_confirm_charge"] = False
                logger.info(
                    "LoomLoom script quote ready: "
                    f"tasks={quote_result.task_count}, currency={quote_result.currency}, "
                    f"estimated_payable_t={quote_result.estimated_buyer_payable_t}"
                )

    quote_result = st.session_state.get("loomloom_script_quote")
    batch = st.session_state.get("loomloom_script_batch")
    if quote_result is not None and batch is not None:
        display_amount = (
            quote_result.estimated_buyer_payable_amount
            or f"{quote_result.estimated_buyer_payable_t} T"
        )
        st.success(
            tr(
                "LoomLoom Quote Summary Singular"
                if quote_result.task_count == 1
                else "LoomLoom Quote Summary"
            ).format(
                tasks=quote_result.task_count,
                amount=display_amount,
                currency=quote_result.currency,
            )
        )
        quote_is_current = (
            st.session_state.get("loomloom_script_input_signature") == input_signature
        )
        if not quote_is_current:
            st.warning(tr("LoomLoom Quote Changed Warning"))
        confirm_charge = st.checkbox(
            tr("Confirm LoomLoom Charge"),
            key="loomloom_confirm_charge",
            disabled=not quote_is_current,
        )
        run_in_progress = bool(st.session_state.get("loomloom_run_id"))
        if st.button(
            tr("Run LoomLoom Batch"),
            key="loomloom_execute_scripts",
            width='stretch',
            type="primary",
            disabled=(not quote_is_current or not confirm_charge or run_in_progress),
            help=tr("LoomLoom Run Help"),
        ):
            try:
                execution = _create_loomloom_script_backend().execute(
                    batch,
                    client_request_id=st.session_state["loomloom_client_request_id"],
                    listing_version_id=quote_result.listing_version_id,
                    confirm=True,
                )
            except (loomloom.LoomLoomError, ValueError) as exc:
                logger.warning(f"failed to execute LoomLoom scripts: error={exc}")
                st.error(str(exc))
            else:
                st.session_state["loomloom_run_id"] = execution.run_id
                st.session_state["loomloom_run_status"] = "running"
                st.session_state["loomloom_poll_paused"] = False
                # 一次报价只允许启动一次付费批次。后台状态只依赖 run_id，提交
                # 后即可丢弃报价与幂等请求 ID；失败后用户需要重新报价再重试。
                st.session_state["loomloom_script_batch"] = None
                st.session_state["loomloom_script_quote"] = None
                st.session_state["loomloom_script_input_signature"] = ""
                st.session_state["loomloom_client_request_id"] = ""
                logger.info(
                    f"LoomLoom script run submitted: run_id={execution.run_id}, "
                    f"tasks={len(batch.input_rows)}"
                )
                st.toast(tr("LoomLoom Run Submitted"))

    run_error = str(st.session_state.get("loomloom_run_error", "") or "").strip()
    if run_error:
        st.error(tr("LoomLoom Run Failed").format(error=run_error))
    run_id = str(st.session_state.get("loomloom_run_id", "") or "").strip()
    if run_id and st.session_state.get("loomloom_poll_paused", False):
        retry_col, stop_col = st.columns(2)
        if retry_col.button(
            tr("Resume LoomLoom Status Check"),
            key="loomloom_resume_status_check",
            width='stretch',
            type="secondary",
        ):
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_failure_count"] = 0
            st.session_state["loomloom_poll_retry_after"] = 0.0
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
        if stop_col.button(
            tr("Stop Tracking LoomLoom Run"),
            key="loomloom_stop_tracking_run",
            width='stretch',
            type="secondary",
            help=tr("Stop Tracking LoomLoom Run Help"),
        ):
            # 这里只停止本地状态查询，不声称取消远端执行。用户确认放弃跟踪后
            # 才清理 run_id，下一次付费运行仍需重新报价和确认。
            st.session_state["loomloom_run_id"] = ""
            st.session_state["loomloom_run_error"] = ""
            st.session_state["loomloom_poll_paused"] = False
            st.rerun(scope="app")
    # 只有真实运行中的批次才启动两秒轮询，报价阶段和结果展示阶段不创建
    # 定时 fragment，避免用户停留在页面时产生无意义的网络请求和 rerun。
    if run_id and not st.session_state.get("loomloom_poll_paused", False):
        _render_loomloom_run_progress()
    _render_loomloom_candidates()


def _render_script_settings(panel, params):
    """渲染文案设置并更新生成参数。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Video Script Settings"))
            params.video_subject = st.text_area(
                tr("Video Subject"),
                placeholder=tr("Video Subject Placeholder"),
                height=96,
                key="video_subject",
            ).strip()

            # 赛道选择：决定品牌句、钩子写法与目标长度。每条视频都会带上
            # 该赛道的固定品牌句，识别度就是靠这个建立的。
            from app.services.niche_brand import NICHES

            niche_options = [(tr("No Niche"), "")]
            for channel, profile in sorted(NICHES.items()):
                niche_options.append((profile.display_name, channel))
            niche_values = [value for _, value in niche_options]
            params.channel = stable_selectbox(
                tr("Niche"),
                options=niche_values,
                default_value=(
                    params.channel if params.channel in niche_values else ""
                ),
                format_func=lambda value: dict(niche_options).get(value, value),
                key="channel",
            )
            if params.channel:
                profile = NICHES[params.channel]
                st.caption(
                    tr("Niche Preview").format(
                        name=profile.display_name,
                        line=profile.brand_line,
                        words=profile.target_words,
                    )
                )

            video_languages = [
                (tr("Auto Detect"), ""),
            ]
            for code in support_locales:
                video_languages.append((code, code))

            selected_language_code = stable_selectbox(
                tr("Script Language"),
                options=[value for _, value in video_languages],
                default_value=_saved_ui_choice(
                    "video_language",
                    [value for _, value in video_languages],
                    "",
                ),
                key="script_language_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_languages
                )[value],
            )
            params.video_language = selected_language_code
            _set_runtime_config("ui", "video_language", params.video_language)

            # 使用带 key 的局部容器限定折叠入口样式，保持 expander 的原生交互，
            # 同时避免样式误伤页面顶部的“基础设置”等其他折叠区域。
            with st.container(key="advanced_settings_script"):
                with st.expander(tr("Advanced Script Settings"), expanded=False):
                    script_backend_options = ["local", "loomloom"]
                    script_backend_labels = {
                        "local": tr("Local LLM Script Generation"),
                        "loomloom": tr("Shengsuan Cloud Batch Script Generation"),
                    }
                    script_generation_backend = stable_selectbox(
                        tr("Script Generation Method"),
                        options=script_backend_options,
                        default_value=_effective_script_generation_backend(),
                        key="script_generation_backend_select",
                        format_func=lambda value: script_backend_labels[value],
                        help=tr("Script Generation Method Help"),
                    )
                    _set_runtime_config(
                        "app", "script_generation_backend", script_generation_backend
                    )

                    params.paragraph_number = st.slider(
                        tr("Script Paragraph Number"),
                        min_value=llm.MIN_SCRIPT_PARAGRAPH_NUMBER,
                        max_value=llm.MAX_SCRIPT_PARAGRAPH_NUMBER,
                        key="paragraph_number_input",
                    )
                    _set_runtime_config(
                        "ui", "paragraph_number", params.paragraph_number
                    )
                    params.video_script_prompt = st.text_area(
                        tr("Custom Script Requirements"),
                        height=100,
                        max_chars=llm.MAX_SCRIPT_PROMPT_LENGTH,
                        placeholder=tr("Custom Script Requirements Placeholder"),
                        key="video_script_prompt",
                    ).strip()
                    _set_runtime_config(
                        "ui", "video_script_prompt", params.video_script_prompt
                    )

                    system_prompt = st.text_area(
                        tr("Custom System Prompt"),
                        height=240,
                        max_chars=llm.MAX_SCRIPT_SYSTEM_PROMPT_LENGTH,
                        key="custom_system_prompt",
                    ).strip()
                    # 默认内容由服务层统一维护。界面虽然直接展示默认提示词，但只有
                    # 用户实际修改后才随任务传递，避免历史任务固化旧版本默认规则。
                    params.custom_system_prompt = (
                        ""
                        if system_prompt == llm.DEFAULT_SCRIPT_SYSTEM_PROMPT.strip()
                        else system_prompt
                    )
                    _set_runtime_config(
                        "ui", "custom_system_prompt", params.custom_system_prompt
                    )

                    restore_prompt_col, preview_prompt_col = st.columns(2)
                    if restore_prompt_col.button(
                        tr("Restore Default System Prompt"),
                        key="restore_default_system_prompt",
                        icon=":material/restart_alt:",
                        on_click=reset_script_system_prompt,
                        width='stretch',
                    ):
                        st.toast(tr("Default System Prompt Restored"))
                    if preview_prompt_col.button(
                        tr("Preview Final Prompt"),
                        key="preview_final_script_prompt",
                        icon=":material/preview:",
                        width='stretch',
                    ):
                        render_script_prompt_preview(
                            llm.build_script_prompt(
                                video_subject=params.video_subject,
                                language=params.video_language,
                                paragraph_number=params.paragraph_number,
                                video_script_prompt=params.video_script_prompt,
                                custom_system_prompt=params.custom_system_prompt,
                            )
                        )

            if _effective_script_generation_backend() == "loomloom":
                _render_loomloom_script_generation(params)
            else:
                _render_local_script_generation(params)
            params.video_script = st.text_area(
                tr("Video Script"),
                help=tr("Video Script Help"),
                height=180,
                key="video_script",
            )
            using_loomloom_scripts = (
                _effective_script_generation_backend() == "loomloom"
            )
            if using_loomloom_scripts:
                st.caption(tr("LoomLoom Video Terms Reuse Help"))
            elif st.button(
                tr("Generate Video Keywords"),
                key="auto_generate_terms",
                width='stretch',
                type="secondary",
                icon=":material/auto_awesome:",
            ):
                if not params.video_script:
                    # 视频关键词需要基于文案提取，文案为空时提前提示并跳过模型调用。
                    st.toast(tr("Please Enter the Video Subject"))
                    st.warning(tr("Please Enter the Video Subject"))
                else:
                    with st.spinner(tr("Generating Video Keywords")):
                        terms = _run_llm_read_operation(
                            "generate_terms",
                            lambda app_config_snapshot: llm.generate_terms(
                                params.video_subject,
                                params.video_script,
                                amount=8 if params.match_materials_to_script else 5,
                                match_script_order=params.match_materials_to_script,
                                app_config=app_config_snapshot,
                            ),
                        )
                        if "Error: " in terms:
                            st.error(tr(terms))
                        else:
                            st.session_state["video_terms"] = ", ".join(terms)

            params.video_terms = st.text_area(
                tr("Video Keywords"),
                help=tr("Video Keywords Help"),
                key="video_terms",
            )


def _render_video_settings(panel, params):
    """渲染视频设置并返回本次选择的本地素材。"""
    uploaded_files = []
    with panel:
        with st.container(border=True):
            st.write(tr("Video Settings"))
            video_concat_modes = [
                (tr("Sequential"), "sequential"),
                (tr("Random"), "random"),
            ]
            video_sources = [
                (tr("Pexels"), "pexels"),
                (tr("Pixabay"), "pixabay"),
                (tr("Coverr"), "coverr"),
                (tr("WaveSpeed AI Video"), "wavespeed"),
                (tr("Volcano Engine Seedance"), "volcengine_seedance"),
                (tr("Shengsuan Cloud AI Video"), "loomloom"),
                (tr("Local file"), "local"),
            ]

            saved_video_source_name = config.app.get("video_source", "pexels")

            params.video_source = stable_selectbox(
                tr("Video Source"),
                options=[value for _, value in video_sources],
                default_value=saved_video_source_name,
                key="video_source_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_sources
                )[value],
            )
            _set_runtime_config("app", "video_source", params.video_source)

            if params.video_source == "wavespeed":
                st.caption(tr("WaveSpeed AI Video Help"))
            if params.video_source == "volcengine_seedance":
                st.caption(tr("Volcano Engine Seedance Help"))

            if params.video_source == "local":
                # Streamlit 的文件类型校验对扩展名大小写敏感，这里同时放行大小写两种形式。
                local_file_types = sorted(
                    extension.removeprefix(".")
                    for extension in LOCAL_MATERIAL_EXTENSIONS
                )
                uploaded_files = st.file_uploader(
                    tr("Upload Local Files"),
                    type=local_file_types
                    + [file_type.upper() for file_type in local_file_types],
                    accept_multiple_files=True,
                    key="local_video_materials_uploader",
                )

            # 文案顺序匹配会从关键词生成到最终合成全程保持叙事顺序，因此开启时
            # 顺序拼接是唯一符合实际执行逻辑的选项。同步控件值可避免界面仍显示
            # “随机拼接”，同时保留用户原选择，关闭后自动恢复。
            sync_script_order_concat_mode()
            selected_concat_mode = stable_selectbox(
                tr("Video Concat Mode"),
                options=[value for _, value in video_concat_modes],
                default_value=_saved_ui_choice(
                    "video_concat_mode",
                    [value for _, value in video_concat_modes],
                    VideoConcatMode.random.value,
                ),
                key="video_concat_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_concat_modes
                )[value],
                disabled=bool(st.session_state.get("match_materials_to_script", False)),
            )
            params.video_concat_mode = VideoConcatMode(selected_concat_mode)

            params.match_materials_to_script = st.checkbox(
                tr("Match Materials to Script Order"),
                help=tr("Match Materials to Script Order Help"),
                key="match_materials_to_script",
                on_change=sync_script_order_concat_mode,
            )
            _set_runtime_config(
                "app",
                "match_materials_to_script",
                params.match_materials_to_script,
            )
            # 顺序匹配开启时，sequential 是派生出的强制值，不应覆盖用户在关闭
            # 该功能时选择的拼接偏好；关闭后仍能恢复此前的 random/sequential。
            if not params.match_materials_to_script:
                _set_runtime_config(
                    "ui", "video_concat_mode", params.video_concat_mode.value
                )

            # 视频转场模式
            video_transition_modes = [
                (tr("None"), VideoTransitionMode.none.value),
                (tr("Shuffle"), VideoTransitionMode.shuffle.value),
                (tr("FadeIn"), VideoTransitionMode.fade_in.value),
                (tr("FadeOut"), VideoTransitionMode.fade_out.value),
                (tr("SlideIn"), VideoTransitionMode.slide_in.value),
                (tr("SlideOut"), VideoTransitionMode.slide_out.value),
                (tr("ZoomIn"), VideoTransitionMode.zoom_in.value),
                (tr("ZoomOut"), VideoTransitionMode.zoom_out.value),
            ]
            selected_transition_mode = stable_selectbox(
                tr("Video Transition Mode"),
                options=[value for _, value in video_transition_modes],
                default_value=_saved_ui_choice(
                    "video_transition_mode",
                    [value for _, value in video_transition_modes],
                    VideoTransitionMode.none.value,
                ),
                key="video_transition_mode_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_transition_modes
                )[value],
            )
            params.video_transition_mode = VideoTransitionMode(selected_transition_mode)
            _set_runtime_config(
                "ui",
                "video_transition_mode",
                params.video_transition_mode.value,
            )

            video_aspect_ratios = [
                (tr("Portrait"), VideoAspect.portrait.value),
                (tr("Landscape"), VideoAspect.landscape.value),
            ]
            # Coverr 库 99% 是 16:9 横屏,默认竖屏会让画面被大量黑边包围。
            # 用 source-specific widget key 让每个 source 各自记忆 aspect 选择:
            #   - 首次切到 coverr → 默认 Landscape(index=1)
            #   - 其他 source 沿用 Portrait(index=0)
            #   - 用户在某 source 下手动改过 aspect,session_state 会记住,
            #     下次回到同一 source 时尊重用户选择,不会再被强制覆盖。
            default_aspect_index = 1 if params.video_source == "coverr" else 0
            video_aspect_values = [value for _, value in video_aspect_ratios]
            video_aspect_config_key = f"video_aspect_{params.video_source}"
            # 只有两个选项，用下拉框等于把选择藏起来。筹码一次点完，且当前选择始终可见。
            selected_aspect_ratio = stable_pills(
                tr("Video Ratio"),
                options=video_aspect_values,
                default_value=_saved_ui_choice(
                    video_aspect_config_key,
                    video_aspect_values,
                    video_aspect_ratios[default_aspect_index][1],
                ),
                key=f"video_aspect_for_{params.video_source}",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_aspect_ratios
                )[value],
            )
            # 兜底到默认画幅：切到别的视图再切回来时，Streamlit 会丢掉本轮没有渲染的
            # 控件状态，于是 st.pills 可能返回 None。None 传进 VideoAspect 会直接
            # 抛 ValueError 把整页打断——这里退回默认画幅，页面照常可用。
            selected_aspect_ratio = selected_aspect_ratio or video_aspect_values[
                default_aspect_index
            ]
            params.video_aspect = VideoAspect(selected_aspect_ratio)
            _set_runtime_config(
                "ui", video_aspect_config_key, params.video_aspect.value
            )

            video_clip_durations = [2, 3, 4, 5, 6, 7, 8, 9, 10]
            params.video_clip_duration = stable_selectbox(
                tr("Clip Duration"),
                options=video_clip_durations,
                default_value=_saved_ui_choice(
                    "video_clip_duration", video_clip_durations, 3
                ),
                key="video_clip_duration_select",
                help=tr("Clip Duration Help"),
            )
            _set_runtime_config(
                "ui", "video_clip_duration", params.video_clip_duration
            )
            clip_speed_key = localized_widget_key("video_clip_speed_slider")
            # session_state 可能来自旧任务、API 参数或旧版页面状态。控件创建前
            # 统一归一化，既保留合法选择，也确保 slider 始终收到 0.5～2.0
            # 范围内的有限浮点数。
            st.session_state[clip_speed_key] = utils.normalize_clip_speed(
                st.session_state.get(
                    clip_speed_key,
                    _saved_ui_number("video_clip_speed", 1.0, 0.5, 2.0),
                )
            )
            params.video_clip_speed = st.slider(
                tr("Clip Speed"),
                min_value=0.5,
                max_value=2.0,
                step=0.05,
                format="%.2fx",
                key=clip_speed_key,
                help=tr("Clip Speed Help"),
            )
            _set_runtime_config("ui", "video_clip_speed", params.video_clip_speed)
            video_count_options = [1, 2, 3, 4, 5]
            params.video_count = stable_selectbox(
                tr("Number of Videos Generated Simultaneously"),
                options=video_count_options,
                default_value=_saved_ui_choice(
                    "video_count", video_count_options, 1
                ),
                key="video_count_select",
            )
            _set_runtime_config("ui", "video_count", params.video_count)

            video_codec_options = [
                (tr("Default Video Encoder"), DEFAULT_VIDEO_CODEC_OPTION),
                ("libx264 (CPU)", "libx264"),
                ("NVIDIA NVENC (h264_nvenc)", "h264_nvenc"),
                ("AMD AMF (h264_amf)", "h264_amf"),
                ("Intel QSV (h264_qsv)", "h264_qsv"),
                ("Windows MediaFoundation (h264_mf)", "h264_mf"),
                ("macOS VideoToolbox (h264_videotoolbox)", "h264_videotoolbox"),
            ]
            saved_video_codec = config.app.get(
                "video_codec", DEFAULT_VIDEO_CODEC_OPTION
            )
            saved_video_codec_values = [item[1] for item in video_codec_options]
            if saved_video_codec not in saved_video_codec_values:
                # 旧版本或手工配置可能留下无效值。UI 回到“默认”而不是替用户
                # 固定某个编码器，后端仍会按稳定策略解析为 libx264。
                saved_video_codec = DEFAULT_VIDEO_CODEC_OPTION
            selected_video_codec = stable_selectbox(
                tr("Video Encoder"),
                options=saved_video_codec_values,
                default_value=saved_video_codec,
                key="video_encoder_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in video_codec_options
                )[value],
                help=tr("Video Encoder Help"),
            )
            if selected_video_codec == DEFAULT_VIDEO_CODEC_OPTION:
                # 默认模式不持久化具体编码器，让配置表达“跟随项目默认值”。
                _delete_runtime_config("app", "video_codec")
            else:
                _set_runtime_config("app", "video_codec", selected_video_codec)

            if params.video_source == "loomloom":
                _render_loomloom_video_settings(params)

            if params.video_source == "wavespeed":
                _render_wavespeed_video_settings(params)
            if params.video_source == "volcengine_seedance":
                _render_seedance_video_settings(params)
    return uploaded_files


def _render_wavespeed_video_settings(params):
    """
    渲染 WaveSpeed 生成数量估算与计费确认。

    生成按条计费，提交前必须让用户看到大致会生成多少段。估算完全在本地
    完成：用配音时长估算区间除以片段时长得到需要覆盖的片段数。素材流程
    本身按需逐段生成、凑够所需时长即停，因此实际生成数以运行时为准，
    估算只用于量级提示，不参与任务执行。
    """
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""),
        params.voice_rate,
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("WaveSpeed Billing Notice").format(min=min_clips, max=max_clips)
        )
    else:
        st.warning(tr("WaveSpeed Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm WaveSpeed Charge"),
        key="wavespeed_confirm_charge",
        help=tr("Confirm WaveSpeed Charge Help"),
    )


def _render_seedance_video_settings(params):
    """展示预计付费任务数量，并要求用户明确确认方舟生成费用。"""
    clip_duration = max(int(params.video_clip_duration or 1), 1)
    video_count = max(int(params.video_count or 1), 1)
    estimated_range = _estimate_voiceover_duration_range(
        str(params.video_script or ""), params.voice_rate
    )
    if estimated_range:
        min_clips = max(math.ceil(estimated_range[0] * video_count / clip_duration), 1)
        max_clips = max(
            math.ceil(estimated_range[1] * video_count / clip_duration), min_clips
        )
        st.warning(
            tr("Volcano Engine Seedance Billing Notice").format(
                min=min_clips, max=max_clips
            )
        )
    else:
        st.warning(tr("Volcano Engine Seedance Billing Notice Without Script"))
    st.checkbox(
        tr("Confirm Volcano Engine Seedance Charge"),
        key="volcengine_seedance_confirm_charge",
        help=tr("Confirm Volcano Engine Seedance Charge Help"),
    )


def _estimate_voiceover_duration_range(
    text: str, voice_rate: float
) -> tuple[float, float] | None:
    """
    在本地估算完整配音时长，返回保守的上下界秒数。

    该估算只用于帮助用户在调用付费 TTS 前判断文案量级，不参与任务执行。
    中文、日文和韩文按字符速度估算，其它使用空格分词的语言按单词速度估算，
    再计入常见标点停顿。不同 Provider、音色和语气会造成实际偏差，因此界面
    必须展示区间而不是伪精确的单一结果。
    """
    normalized_text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized_text:
        return None

    script_chars = re.findall(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        normalized_text,
    )
    remaining_text = re.sub(
        r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]",
        " ",
        normalized_text,
    )
    words = re.findall(r"\b[\w]+(?:[-'’][\w]+)*\b", remaining_text, re.UNICODE)
    punctuation_count = len(re.findall(r"[,，.。!?！？;；:：]", normalized_text))

    # 4.2 字/秒和 2.6 词/秒接近日常解说语速；标点按 0.12 秒加入轻微停顿。
    # voice_rate 只作为估算修正项。部分生成式 TTS 不严格执行倍率，所以最终
    # 仍保留 ±15% 区间，避免让用户误以为该值等同于服务端真实结果。
    base_seconds = len(script_chars) / 4.2 + len(words) / 2.6 + punctuation_count * 0.12
    if base_seconds <= 0:
        return None

    normalized_rate = max(float(voice_rate or 1.0), 0.1)
    estimated_seconds = base_seconds / normalized_rate
    return (
        round(max(estimated_seconds * 0.85, 1.0), 1),
        round(max(estimated_seconds * 1.15, 1.0), 1),
    )


def _get_voice_preview_sample(voice_name: str) -> str:
    """返回适合当前音色的短试听文案，不使用用户的完整视频文案。"""
    # ElevenLabs 音色缺少明确语言字段时，根据展示名称中的越南语字符选择
    # 试听文案，避免用明显不匹配的语言判断音色效果。
    if voice.is_elevenlabs_voice(voice_name):
        parts = voice_name.split(":", 2)
        display = parts[2] if len(parts) >= 3 else ""
        vietnamese_chars = set("àáâãèéêìíòóôõùúýăđơưÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐƠƯ")
        if any(char in vietnamese_chars for char in display):
            return "Xin chào, đây là đoạn âm thanh thử nghiệm giọng nói."
    return tr("Voice Example")


def _voice_preview_fingerprint(
    *,
    preview_type: str,
    content: str,
    tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
    provider_signature: dict,
) -> str:
    """生成试听缓存指纹，任一配音参数变化后自动让旧试听结果失效。"""
    payload = {
        "preview_type": preview_type,
        "content": content,
        "tts_server": tts_server,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_volume": voice_volume,
        "provider_signature": provider_signature,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _credential_signature(value: str) -> str:
    """
    生成只用于缓存失效判断的凭证摘要。

    摘要不会写入配置、日志或任务文件。用户修改 API Key 后摘要会变化，从而
    强制重新调用当前配音服务，避免旧试听缓存让无效的新凭证看起来可用。
    """
    normalized_value = str(value or "")
    if not normalized_value:
        return ""
    return hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()


def _get_voice_preview_provider_signature(tts_server: str) -> dict:
    """
    返回会影响试听结果的非敏感 Provider 配置。

    API Key 只以单向摘要参与缓存指纹，原始凭证不会进入缓存或日志。模型、
    服务地址、区域或凭证发生变化时都必须重新生成试听，否则界面可能继续播放
    旧 Provider 配置下的音频，让用户误判当前设置已经生效。
    """
    if tts_server == "azure-tts-v2":
        return {
            "speech_region": config.azure.get("speech_region", ""),
            "credential": _credential_signature(config.azure.get("speech_key", "")),
        }
    if tts_server == "siliconflow":
        return {
            "credential": _credential_signature(config.siliconflow.get("api_key", ""))
        }
    if tts_server == "gemini-tts":
        return {
            "credential": _credential_signature(config.app.get("gemini_api_key", ""))
        }
    if tts_server == "mimo-tts":
        return {"credential": _credential_signature(config.app.get("mimo_api_key", ""))}
    if tts_server == "minimax-tts":
        return {
            "base_url": voice.get_minimax_tts_endpoint(),
            "model_id": config.minimax_tts.get("model_id", ""),
            "voice_id": config.minimax_tts.get("voice_id", ""),
            "credential": _credential_signature(voice.get_minimax_tts_api_key()),
        }
    if tts_server == "elevenlabs":
        return {
            "model_id": config.elevenlabs.get("model_id", ""),
            "credential": _credential_signature(config.elevenlabs.get("api_key", "")),
        }
    if tts_server == "chatterbox":
        return {
            "base_url": config.chatterbox.get("base_url", ""),
            "model_id": config.chatterbox.get("model_id", ""),
            "credential": _credential_signature(config.chatterbox.get("api_key", "")),
        }
    return {}


def _synthesize_voice_preview(
    *,
    content: str,
    preview_type: str,
    selected_tts_server: str,
    voice_name: str,
    voice_rate: float,
    voice_volume: float,
) -> dict | None:
    """生成一次试听并转为内存缓存，临时文件不会跨会话长期保留。"""
    if selected_tts_server == "chatterbox":
        _sync_chatterbox_config_from_session_state()

    temp_dir = utils.storage_dir("temp", create=True)
    audio_file = os.path.join(temp_dir, f"tmp-voice-{str(uuid4())}.mp3")
    logger.info(
        f"generating {preview_type} voice preview: "
        f"voice={voice_name}, rate={voice_rate}, volume={voice_volume}, "
        f"text_length={len(content)}"
    )
    try:
        with config.try_runtime_config_lock() as lock_acquired:
            if not lock_acquired:
                return {"busy": True}
            sub_maker = voice.tts(
                text=content,
                voice_name=voice_name,
                voice_rate=voice_rate,
                voice_file=audio_file,
                voice_volume=voice_volume,
            )
        if not sub_maker or not os.path.exists(audio_file):
            logger.error(f"{preview_type} voice preview did not produce an audio file")
            return None

        with open(audio_file, "rb") as file:
            audio_bytes = file.read()
        if not audio_bytes:
            logger.error(f"voice preview audio file is empty: {audio_file}")
            return None

        duration = voice.get_audio_duration(audio_file)
        if (
            not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            logger.warning(
                f"voice preview duration is unavailable: "
                f"preview_type={preview_type}, voice={voice_name}"
            )
            duration = None

        return {
            "audio_bytes": audio_bytes,
            "mime_type": _detect_audio_mime(audio_file, audio_bytes),
            "duration": duration,
            "preview_type": preview_type,
            "sub_maker": sub_maker,
        }
    finally:
        # 浏览器播放器使用内存字节，文件读取完即可清理，避免频繁试听积累临时文件。
        try:
            os.remove(audio_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # 清理失败不应覆盖真正的 TTS 响应或异常，但需要保留路径和系统错误，
            # 方便排查权限、只读文件系统等环境问题。
            logger.warning(
                f"failed to delete voice preview file {audio_file}: {str(exc)}"
            )


def _render_voice_preview(params, friendly_names, selected_tts_server, voice_name):
    """渲染低成本短试听、完整文案时长估算和按需完整配音预览。"""
    if not friendly_names:
        return

    script_content = str(params.video_script or "").strip()
    estimated_range = _estimate_voiceover_duration_range(
        script_content,
        params.voice_rate,
    )
    if estimated_range:
        st.caption(
            tr("Estimated Voiceover Duration").format(
                min=estimated_range[0],
                max=estimated_range[1],
            )
        )
    else:
        st.caption(tr("Voiceover Script Required"))

    sample_content = _get_voice_preview_sample(voice_name)
    provider_signature = _get_voice_preview_provider_signature(selected_tts_server)
    preview_columns = st.columns(2)
    short_preview_requested = preview_columns[0].button(
        tr("Play Voice"),
        key="play_voice_button",
        icon=":material/graphic_eq:",
        width='stretch',
    )
    full_preview_requested = preview_columns[1].button(
        tr("Generate Full Voiceover Preview"),
        key="generate_full_voiceover_preview_button",
        icon=":material/article:",
        help=tr("Full Voiceover Preview Cost Hint"),
        width='stretch',
        disabled=not bool(script_content),
    )

    preview_type = ""
    preview_content = ""
    if short_preview_requested:
        preview_type = "sample"
        preview_content = sample_content
    elif full_preview_requested:
        preview_type = "full"
        preview_content = script_content

    sample_fingerprint = _voice_preview_fingerprint(
        preview_type="sample",
        content=sample_content,
        tts_server=selected_tts_server,
        voice_name=voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=provider_signature,
    )
    full_fingerprint = (
        _voice_preview_fingerprint(
            preview_type="full",
            content=script_content,
            tts_server=selected_tts_server,
            voice_name=voice_name,
            voice_rate=params.voice_rate,
            voice_volume=params.voice_volume,
            provider_signature=provider_signature,
        )
        if script_content
        else ""
    )

    if preview_type:
        requested_fingerprint = (
            sample_fingerprint if preview_type == "sample" else full_fingerprint
        )
        cached_preview = st.session_state.get("voice_preview_audio")
        if (
            not cached_preview
            or cached_preview.get("fingerprint") != requested_fingerprint
        ):
            try:
                with st.spinner(tr("Synthesizing Voice")):
                    preview_result = _synthesize_voice_preview(
                        content=preview_content,
                        preview_type=preview_type,
                        selected_tts_server=selected_tts_server,
                        voice_name=voice_name,
                        voice_rate=params.voice_rate,
                        voice_volume=params.voice_volume,
                    )
            except Exception as exc:
                logger.exception(f"failed to generate {preview_type} voice preview")
                st.error(tr("Voice Preview Failed").format(error=str(exc)))
            else:
                if preview_result and preview_result.get("busy"):
                    st.warning(tr("Voice Preview Busy"))
                elif preview_result:
                    preview_result["fingerprint"] = requested_fingerprint
                    st.session_state["voice_preview_audio"] = preview_result
                else:
                    st.error(tr("Voice Preview No Audio"))

    cached_preview = st.session_state.get("voice_preview_audio")
    valid_fingerprints = {sample_fingerprint, full_fingerprint}
    if (
        cached_preview
        and cached_preview.get("fingerprint") in valid_fingerprints
        and cached_preview.get("audio_bytes")
    ):
        # 只在用户本次明确点击“试听音色”时自动播放。Streamlit 的其它控件
        # 也会触发页面 rerun；如果对缓存音频永久开启 autoplay，修改任意设置
        # 都可能让旧试听从头播放。完整试听继续保留手动播放，避免较长音频在
        # 生成完成后意外打断用户。
        should_autoplay = bool(
            short_preview_requested
            and cached_preview.get("preview_type") == "sample"
            and cached_preview.get("fingerprint") == sample_fingerprint
        )
        st.audio(
            cached_preview["audio_bytes"],
            format=cached_preview.get("mime_type", "audio/mp3"),
            autoplay=should_autoplay,
        )
        if cached_preview.get("preview_type") == "full":
            duration = cached_preview.get("duration")
            if isinstance(duration, (int, float)) and duration > 0:
                st.caption(
                    tr("Actual Voiceover Duration").format(duration=f"{duration:.1f}")
                )
            else:
                st.warning(tr("Voice Preview Duration Unavailable"))


def _get_reusable_full_voice_preview(params, voice_mode: str) -> dict | None:
    """
    返回与当前生成参数完全匹配的完整试听缓存。

    只复用完整文案试听，短音色样例永远不能进入正式任务。指纹统一覆盖文案、
    Provider、音色、语速、音量和非敏感配置摘要；任何参数变化都会自然回退到
    正常 TTS 流程。字幕时间轴和有效时长同样是必需条件，避免只复用音频后让
    Edge 字幕链路失去 SubMaker。
    """
    if voice_mode != VOICE_MODE_TTS:
        return None

    script_content = str(params.video_script or "").strip()
    selected_tts_server = config.ui.get("tts_server", "azure-tts-v1")
    if (
        not script_content
        or not params.voice_name
        # 正式视频会在 MoviePy 合成阶段统一应用配音音量；部分 Provider 又会
        # 在 TTS 阶段直接写入音量增益。非默认音量下复用试听可能造成二次增益，
        # 因此先保守回退原流程，避免为少量场景引入 Provider 特判。
        or not math.isclose(float(params.voice_volume), 1.0)
    ):
        return None

    expected_fingerprint = _voice_preview_fingerprint(
        preview_type="full",
        content=script_content,
        tts_server=selected_tts_server,
        voice_name=params.voice_name,
        voice_rate=params.voice_rate,
        voice_volume=params.voice_volume,
        provider_signature=_get_voice_preview_provider_signature(selected_tts_server),
    )
    cached_preview = st.session_state.get("voice_preview_audio")
    if (
        not cached_preview
        or cached_preview.get("fingerprint") != expected_fingerprint
        or cached_preview.get("preview_type") != "full"
        or not cached_preview.get("audio_bytes")
        or cached_preview.get("sub_maker") is None
    ):
        return None

    duration = cached_preview.get("duration")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        return None

    return {
        "audio_bytes": bytes(cached_preview["audio_bytes"]),
        "duration": float(duration),
        "sub_maker": cached_preview["sub_maker"],
        "script": script_content,
        "voice_name": params.voice_name,
        "voice_rate": float(params.voice_rate),
        "voice_volume": float(params.voice_volume),
    }


def _sync_minimax_tts_api_key_input():
    """
    同步 MiniMax TTS 密码控件，并返回当前有效 Key。

    TTS 专用 Key 为空时允许复用 MiniMax LLM Key。共享 Key 只用于当前控件和
    请求，不自动复制到 [minimax_tts]，避免同一凭证在配置文件中重复维护。
    """
    widget_key = "minimax_tts_api_key_input"
    configured_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    shared_key = str(
        config.app.get("minimax_api_key", "") or os.getenv("MINIMAX_API_KEY", "") or ""
    ).strip()
    effective_key = configured_key or shared_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # 浏览器重连可能重放空密码状态。恢复已配置凭证，防止空值覆盖配置，
        # 同时确保当前 rerun 的试听请求可以直接使用有效 Key。
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored MiniMax TTS API key after empty session replay")
    elif not had_widget_state:
        st.session_state[widget_key] = effective_key
        entered_key = effective_key

    if entered_key and entered_key != effective_key:
        _set_runtime_config("minimax_tts", "api_key", entered_key)

    return entered_key


def _get_cached_minimax_voices(api_key: str, endpoint: str) -> list[dict[str, str]]:
    """按站点和凭证摘要读取当前会话中的 MiniMax 音色查询结果。"""
    cache = st.session_state.get("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cached_voices = cache.get(cache_key, [])
    return cached_voices if isinstance(cached_voices, list) else []


def _cache_minimax_voices(
    api_key: str,
    endpoint: str,
    voices: list[dict[str, str]],
):
    """缓存主动查询到的音色，避免普通控件 rerun 后重复请求 MiniMax。"""
    cache = st.session_state.setdefault("minimax_tts_voice_catalog_cache", {})
    cache_key = f"{endpoint}|{_credential_signature(api_key)}"
    cache[cache_key] = voices


def _render_minimax_tts_settings() -> tuple[list[str], dict[str, str]]:
    """渲染 MiniMax TTS 配置，并返回统一音色选择器使用的选项和文案。"""
    effective_api_key = _sync_minimax_tts_api_key_input()
    effective_api_key = st.text_input(
        tr("MiniMax TTS API Key"),
        type="password",
        key="minimax_tts_api_key_input",
    ).strip()

    dedicated_key = str(config.minimax_tts.get("api_key", "") or "").strip()
    minimax_tts_endpoints = [voice.MINIMAX_TTS_GLOBAL_URL, voice.MINIMAX_TTS_CN_URL]
    effective_endpoint = voice.get_minimax_tts_endpoint()
    if effective_endpoint not in minimax_tts_endpoints:
        effective_endpoint = voice.MINIMAX_TTS_GLOBAL_URL
    minimax_tts_base_url = stable_selectbox(
        tr("MiniMax TTS Endpoint"),
        options=minimax_tts_endpoints,
        default_value=effective_endpoint,
        key="minimax_tts_endpoint_select",
        # 复用 LLM Key 时必须跟随 LLM 所在区域，避免界面允许选择一个实际
        # 不会生效的地址；填写独立 TTS Key 后即可单独选择站点。
        disabled=not dedicated_key,
    )
    if dedicated_key:
        _set_runtime_config("minimax_tts", "base_url", minimax_tts_base_url)

    configured_model = config.minimax_tts.get(
        "model_id", voice.MINIMAX_TTS_DEFAULT_MODEL
    )
    if configured_model not in voice.MINIMAX_TTS_MODELS:
        configured_model = voice.MINIMAX_TTS_DEFAULT_MODEL
    minimax_tts_model = stable_selectbox(
        tr("MiniMax TTS Model"),
        options=list(voice.MINIMAX_TTS_MODELS),
        default_value=configured_model,
        key="minimax_tts_model_select",
    )
    _set_runtime_config("minimax_tts", "model_id", minimax_tts_model)

    if st.button(
        tr("Load MiniMax Voices"),
        key="load_minimax_voices_button",
        icon=":material/refresh:",
        width='stretch',
    ):
        try:
            available_voices = voice.get_minimax_voice_catalog(
                api_key=effective_api_key,
                endpoint=minimax_tts_base_url,
                voice_type="all",
            )
        except Exception as exc:
            # 这里必须把异常暴露给用户并记录日志。账号区域不匹配、Key 权限不足
            # 或网络失败都很常见，静默返回空列表会让用户误以为账号没有音色。
            logger.warning(f"load MiniMax voices failed: {exc}")
            st.error(tr("MiniMax Voices Load Failed").format(error=str(exc)))
        else:
            _cache_minimax_voices(
                effective_api_key,
                minimax_tts_base_url,
                available_voices,
            )
            st.success(tr("MiniMax Voices Loaded").format(count=len(available_voices)))

    available_voices = _get_cached_minimax_voices(
        effective_api_key,
        minimax_tts_base_url,
    )
    voice_labels = {
        f"minimax:{item['voice_id']}": (
            f"{item['voice_name']} ({item['voice_id']})"
            if item["voice_name"] != item["voice_id"]
            else item["voice_id"]
        )
        for item in available_voices
    }
    configured_voice_id = str(
        config.minimax_tts.get("voice_id", voice.MINIMAX_TTS_DEFAULT_VOICE)
        or voice.MINIMAX_TTS_DEFAULT_VOICE
    ).strip()
    configured_voice = f"minimax:{configured_voice_id}"
    # 尚未点击获取音色、接口暂时不可用或配置使用列表外克隆音色时，仍保留
    # 当前 Voice ID，确保原有生成流程不依赖远端音色查询结果。
    voice_labels.setdefault(configured_voice, configured_voice_id)
    return list(voice_labels), voice_labels


def _sync_elevenlabs_api_key_input():
    """
    同步 ElevenLabs 密码控件、持久化配置和环境变量，并返回当前有效 Key。

    Streamlit 在浏览器标签页连接到重启后的服务时，可能重放一个空的密码控件
    状态。这个空值无法与用户主动清空可靠区分，因此当配置文件或环境变量仍有
    Key 时，优先恢复有效值，防止空状态覆盖配置并确保本次 rerun 能立即加载
    音色。需要彻底删除 Key 时应修改配置文件或环境变量，避免重连误判。
    """
    widget_key = "elevenlabs_api_key_input"
    configured_key = str(config.elevenlabs.get("api_key", "") or "").strip()
    env_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    effective_key = configured_key or env_key
    had_widget_state = widget_key in st.session_state
    entered_key = str(st.session_state.get(widget_key, "") or "").strip()

    if not entered_key and effective_key:
        # 重连后的空状态不能覆盖有效凭证，同时必须在渲染音色列表之前恢复，
        # 否则配置文件虽然没有被清空，当前页面仍会使用空 Key 请求 ElevenLabs。
        st.session_state[widget_key] = effective_key
        entered_key = effective_key
        if had_widget_state:
            logger.debug("restored ElevenLabs API key after empty session replay")
    elif not had_widget_state:
        # 先初始化再创建控件，避免同时传 value 和 session_state 触发 Streamlit
        # 的默认值冲突警告；没有任何 Key 时初始化为空即可。
        st.session_state[widget_key] = entered_key

    if entered_key and entered_key != effective_key:
        # 用户主动输入的新值才落入 config.toml。环境变量作为有效值回填时不会
        # 被复制到文件，容器或部署平台注入的密钥仍只保留在运行环境中。
        for cache_key in list(st.session_state.keys()):
            if str(cache_key).startswith("elevenlabs_voices_"):
                del st.session_state[cache_key]
        _set_runtime_config("elevenlabs", "api_key", entered_key)

    return entered_key


def _render_elevenlabs_api_key_input(label_key):
    """
    渲染 ElevenLabs TTS 与配乐共用的唯一 API Key 输入状态。

    同一页面若为 TTS 和配乐分别使用两个 widget key，Streamlit 会各自保留旧值，
    后渲染的输入框还会覆盖共享配置。这里统一使用一个 key，并集中处理环境变量
    回填、配置更新和音色缓存失效，确保界面显示与后台任务始终读取同一个值。
    """
    _sync_elevenlabs_api_key_input()
    return st.text_input(
        tr(label_key),
        type="password",
        key="elevenlabs_api_key_input",
    ).strip()


def _render_background_music_settings(params, elevenlabs_api_key_rendered=False):
    """渲染背景音乐来源与音量设置，并返回本次待保存的上传文件。"""
    uploaded_bgm_file = None
    previous_bgm_type = st.session_state.get("last_rendered_bgm_type")
    st.divider()
    bgm_options = [
        (tr("No Background Music"), ""),
        (tr("Random Background Music"), "random"),
        (tr("Custom Background Music"), "custom"),
        (tr("Sonilo Background Music"), "sonilo"),
        (tr("ElevenLabs Background Music"), "elevenlabs"),
    ]
    selected_bgm_type = stable_selectbox(
        tr("Background Music Source"),
        options=[value for _, value in bgm_options],
        default_value=_saved_ui_choice(
            "bgm_type",
            [value for _, value in bgm_options],
            "random",
        ),
        key="bgm_type_select",
        format_func=lambda value: dict((v, label) for label, v in bgm_options)[value],
    )
    params.bgm_type = selected_bgm_type
    _set_runtime_config("ui", "bgm_type", params.bgm_type)
    if params.bgm_type == "sonilo":
        configured_key = str(config.app.get("sonilo_api_key", "") or "").strip()
        effective_key = configured_key or os.getenv("SONILO_API_KEY", "").strip()
        entered_key = st.text_input(
            tr("Sonilo API Key"),
            value=effective_key,
            type="password",
            key="sonilo_api_key_input",
        ).strip()
        # 用户要求已配置的 Key 直接回填到密码输入框。配置值优先于环境变量；
        # 仅当用户确实修改输入或本来就使用配置时写回，避免把环境变量中的 Key
        # 在无操作的情况下复制进 config.toml。
        if configured_key or entered_key != effective_key:
            _set_runtime_config("app", "sonilo_api_key", entered_key)
    elif params.bgm_type == "elevenlabs":
        if elevenlabs_api_key_rendered:
            # TTS 区域已经渲染共享输入框时不再创建第二个 widget，避免两个独立
            # session_state 值互相覆盖。说明文字帮助用户定位上方的共用配置。
            st.caption(tr("ElevenLabs API Key Help"))
        else:
            _render_elevenlabs_api_key_input("ElevenLabs Music API Key")

    bgm_volume_options = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    params.bgm_volume = stable_selectbox(
        tr("Background Music Volume"),
        options=bgm_volume_options,
        default_value=_saved_ui_choice("bgm_volume", bgm_volume_options, 0.2),
        key="bgm_volume_select",
        format_func=lambda value: f"{int(value * 100)}%",
        disabled=not params.bgm_type,
    )
    _set_runtime_config("ui", "bgm_volume", params.bgm_volume)
    bgm_enabled = bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)

    if params.bgm_type == "custom":
        uploaded_bgm_file = st.file_uploader(
            tr("Upload Background Music"),
            type=[
                extension.removeprefix(".")
                for extension in bgm_service.SUPPORTED_BGM_EXTENSIONS
            ],
            accept_multiple_files=False,
            key="custom_bgm_uploader",
            help=tr("Upload Background Music Help"),
            # Streamlit 默认会在控件上展示全局 200MB 上限。这里必须与服务层
            # 30MB 硬限制保持一致，避免界面允许选择、提交时才被服务端拒绝。
            max_upload_size=bgm_service.MAX_BGM_UPLOAD_BYTES // (1024 * 1024),
        )
        if uploaded_bgm_file is not None and bgm_enabled:
            try:
                safe_name = bgm_service.sanitize_upload_filename(uploaded_bgm_file.name)
                # Streamlit 在调整音量等任意控件后都会重新执行页面。使用内容哈希
                # 区分上传文件，并在当前会话内缓存完整解码结果，既不能只凭同名、
                # 同大小文件误用旧结果，也避免每次 rerun 都重复调用 FFmpeg。
                validation_key = (
                    safe_name,
                    uploaded_bgm_file.size,
                    hashlib.sha256(uploaded_bgm_file.getbuffer()).hexdigest(),
                )
                cached_validation = st.session_state.get("custom_bgm_validation")
                if (
                    not cached_validation
                    or cached_validation.get("key") != validation_key
                ):
                    try:
                        bgm_service.validate_bgm_upload(
                            uploaded_bgm_file.name, uploaded_bgm_file
                        )
                    except bgm_service.BgmUploadError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "upload",
                        }
                        # 同一个文件指纹的失败结果会进入会话缓存，因此这里只在
                        # 首次真实执行校验时记录一次，避免普通控件 rerun 刷屏。
                        logger.warning(
                            "WebUI background music validation rejected: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    except bgm_service.BgmServiceError as exc:
                        cached_validation = {
                            "key": validation_key,
                            "error": str(exc),
                            "error_type": "service",
                        }
                        logger.error(
                            "WebUI background music validation failed: "
                            f"name={safe_name}, error={str(exc)}"
                        )
                    else:
                        cached_validation = {
                            "key": validation_key,
                            "error": "",
                            "error_type": "",
                        }
                    st.session_state["custom_bgm_validation"] = cached_validation

                if cached_validation.get("error"):
                    if cached_validation.get("error_type") == "service":
                        raise bgm_service.BgmServiceError(cached_validation["error"])
                    raise bgm_service.BgmUploadError(cached_validation["error"])
            except bgm_service.BgmUploadError:
                # 非法文件不能沿用上一次有效上传的名称，否则任务参数可能仍指向
                # 历史 BGM。保留 UploadedFile 返回值，让用户点击生成时仍会被最终
                # 服务端校验拦截，而不是静默生成一条没有背景音乐的视频。
                params.bgm_file = ""
                st.error(tr("Invalid Background Music"))
            except bgm_service.BgmServiceError:
                params.bgm_file = ""
                st.error(tr("Background Music Validation Failed"))
            else:
                # 完整解码校验通过后才展示播放器和“已就绪”。文件仍只在点击
                # 生成时持久化，用户仅预览或随后移除文件不会污染 storage/bgm。
                uploaded_mime_type = str(getattr(uploaded_bgm_file, "type", "") or "")
                preview_mime_type = (
                    uploaded_mime_type
                    if uploaded_mime_type.startswith("audio/")
                    else mimetypes.guess_type(safe_name)[0] or "audio/mpeg"
                )
                st.audio(uploaded_bgm_file, format=preview_mime_type)
                st.info(f"{tr('Background Music Ready')}: {safe_name}")
                params.bgm_file = safe_name

        # Streamlit 会在条件控件暂时不渲染时清理其 widget state。
        # 从其它 BGM 来源切回时用已持久化值恢复；同一来源下
        # 用户主动清空时 previous_bgm_type 不变，因此不会被旧值反弹。
        if previous_bgm_type != "custom":
            st.session_state["custom_bgm_file_input"] = _saved_ui_text(
                "custom_bgm_file"
            )
        custom_bgm_file = st.text_input(
            tr("Custom Background Music File"),
            key="custom_bgm_file_input",
            disabled=uploaded_bgm_file is not None,
        )
        _set_runtime_config(
            "ui", "custom_bgm_file", custom_bgm_file.strip()
        )
        if uploaded_bgm_file is None and custom_bgm_file and bgm_enabled:
            # 文件名由服务层映射到 storage/bgm 或 resource/songs 后校验，
            # UI 不接受两个白名单目录之外的任意路径。
            params.bgm_file = custom_bgm_file.strip()
        elif not bgm_enabled:
            # 上传控件继续保留用户已选择的文件，调高音量后的下一次 rerun 会自动
            # 完整校验；当前任务参数必须清空，避免 0 音量任务保存或解析该文件。
            params.bgm_file = ""

    if params.bgm_type == "sonilo":
        if previous_bgm_type != "sonilo":
            st.session_state["sonilo_bgm_prompt_input"] = _saved_ui_text(
                "sonilo_bgm_prompt",
                max_length=sonilo_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("Sonilo Music Prompt"),
            key="sonilo_bgm_prompt_input",
            max_chars=sonilo_service.MAX_PROMPT_LENGTH,
            help=tr("Sonilo Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "sonilo_bgm_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("Sonilo Multiple Videos Warning"))
        if st.button(
            tr("Test Sonilo Connection"),
            key="test_sonilo_connection_button",
            width='stretch',
        ):
            try:
                sonilo_service.test_connection()
            except sonilo_service.SoniloError as exc:
                logger.warning(f"Sonilo connection test failed: {exc}")
                st.error(tr("Sonilo Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("Sonilo Connection Test Succeeded"))
    elif params.bgm_type == "elevenlabs":
        if previous_bgm_type != "elevenlabs":
            st.session_state["elevenlabs_music_prompt_input"] = _saved_ui_text(
                "elevenlabs_music_prompt",
                max_length=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            )
        params.video_music_prompt = st.text_input(
            tr("ElevenLabs Music Prompt"),
            key="elevenlabs_music_prompt_input",
            max_chars=elevenlabs_music_service.MAX_PROMPT_LENGTH,
            help=tr("ElevenLabs Music Prompt Help"),
        ).strip()
        _set_runtime_config(
            "ui", "elevenlabs_music_prompt", params.video_music_prompt
        )
        if params.video_count > 1:
            st.warning(tr("ElevenLabs Multiple Videos Warning"))
        if st.button(
            tr("Test ElevenLabs Connection"),
            key="test_elevenlabs_music_connection_button",
            width='stretch',
        ):
            try:
                elevenlabs_music_service.test_connection()
            except elevenlabs_music_service.ElevenLabsPaidPlanRequiredError:
                st.error(tr("ElevenLabs Paid Plan Required"))
            except elevenlabs_music_service.ElevenLabsMusicError as exc:
                logger.warning(f"ElevenLabs connection test failed: {exc}")
                st.error(tr("ElevenLabs Connection Test Failed").format(error=str(exc)))
            else:
                st.success(tr("ElevenLabs Connection Test Succeeded"))
    if params.bgm_type == "sonilo" and bgm_enabled and not sonilo_service.is_enabled():
        # 音量为 0 时任务层不会生成或混合 Sonilo 配乐，因此无需提示 Key；
        # 该判断与任务入口共用服务层规则，避免界面提示和实际执行条件分叉。
        st.warning(tr("Sonilo API Key Required"))
    elif (
        params.bgm_type == "elevenlabs"
        and bgm_enabled
        and not elevenlabs_music_service.is_enabled()
    ):
        st.warning(tr("ElevenLabs API Key Required"))
    st.session_state["last_rendered_bgm_type"] = params.bgm_type
    return uploaded_bgm_file


def _render_audio_settings(panel, params):
    """渲染音频设置并返回上传音频与当前配音模式。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Audio Settings"))

            # 配音方式是音频设置的一级状态，负责明确区分自动配音、用户上传和无配音。
            # 旧配置没有 voice_mode 时，根据原 tts_server 的无配音哨兵保持兼容。
            saved_tts_server = config.ui.get("tts_server", "azure-tts-v1")
            saved_voice_mode = config.ui.get("voice_mode")
            if saved_voice_mode not in {
                VOICE_MODE_TTS,
                VOICE_MODE_UPLOAD,
                VOICE_MODE_NONE,
            }:
                saved_voice_mode = (
                    VOICE_MODE_NONE
                    if saved_tts_server == voice.NO_VOICE_NAME
                    else VOICE_MODE_TTS
                )
            voice_mode_options = [VOICE_MODE_TTS, VOICE_MODE_UPLOAD, VOICE_MODE_NONE]
            voice_mode_labels = {
                VOICE_MODE_TTS: tr("Automatic Voiceover"),
                VOICE_MODE_UPLOAD: tr("Upload Voiceover"),
                VOICE_MODE_NONE: tr("No Voiceover"),
            }
            voice_mode = stable_segmented_control(
                tr("Voiceover Mode"),
                options=voice_mode_options,
                default_value=saved_voice_mode,
                key="voice_mode_control",
                format_func=lambda value: voice_mode_labels[value],
                width="stretch",
            )
            _set_runtime_config("ui", "voice_mode", voice_mode)
            tts_mode_enabled = voice_mode == VOICE_MODE_TTS

            # Provider 下拉只负责选择自动配音服务；无配音已经由上方模式控制，
            # 不再作为 TTS Provider 混入列表，避免两个入口表达同一状态。
            tts_servers = [
                ("azure-tts-v1", "Azure TTS V1"),
                ("azure-tts-v2", "Azure TTS V2"),
                ("siliconflow", "SiliconFlow TTS"),
                ("gemini-tts", "Google Gemini TTS"),
                ("mimo-tts", "Xiaomi MiMo TTS"),
                ("minimax-tts", "MiniMax TTS"),
                ("elevenlabs", "ElevenLabs TTS"),
                ("chatterbox", "Chatterbox TTS"),
                ("fish_audio", "Fish Audio TTS"),
            ]

            tts_server_values = [server_value for server_value, _ in tts_servers]
            if saved_tts_server not in tts_server_values:
                saved_tts_server = "azure-tts-v1"

            if tts_mode_enabled:
                selected_tts_server = stable_selectbox(
                    tr("Voiceover Service"),
                    options=tts_server_values,
                    default_value=saved_tts_server,
                    key="tts_server_select",
                    format_func=lambda value: dict(
                        (v, label) for v, label in tts_servers
                    )[value],
                )
            else:
                # 非自动配音模式不渲染 TTS 控件，但保留上次选择，切回后可以继续使用。
                selected_tts_server = saved_tts_server

            _set_runtime_config("ui", "tts_server", selected_tts_server)

            # 服务说明紧跟 Provider 选择，先告诉用户需要准备什么，再进入音色和
            # 凭证配置。没有说明的 Provider 不渲染空提示块。
            if tts_mode_enabled:
                provider_tips = get_tts_provider_tips(selected_tts_server)
                if provider_tips:
                    st.info(provider_tips)

            # MiniMax 只复用下方通用“配音声音”选择器。Provider 配置函数负责
            # 刷新远端音色并返回友好文案，不再额外渲染 Voice ID 和音色下拉框。
            minimax_voices = []
            minimax_voice_labels = {}
            if tts_mode_enabled and selected_tts_server == "minimax-tts":
                minimax_voices, minimax_voice_labels = _render_minimax_tts_settings()

            # 根据选择的TTS服务器获取声音列表
            filtered_voices = []
            saved_voice_name = config.ui.get("voice_name", "")
            elevenlabs_api_key_rendered = False

            if not tts_mode_enabled:
                # 上传音频和无配音模式不加载远程音色，减少无意义的网络请求和界面噪音。
                filtered_voices = []
            elif selected_tts_server == "siliconflow":
                # 获取硅基流动的声音列表
                filtered_voices = voice.get_siliconflow_voices()
            elif selected_tts_server == "gemini-tts":
                # 获取Gemini TTS的声音列表
                filtered_voices = voice.get_gemini_voices()
            elif selected_tts_server == "mimo-tts":
                # 获取 Xiaomi MiMo TTS 的预置音色列表
                filtered_voices = voice.get_mimo_voices()
            elif selected_tts_server == "minimax-tts":
                filtered_voices = minimax_voices
            elif selected_tts_server == "elevenlabs":
                # 音色列表位于 Key 输入框之前渲染，必须先统一恢复重连状态并读取
                # 配置/环境变量，否则页面会用空 Key 加载并缓存空音色列表。
                saved_elevenlabs_api_key = _sync_elevenlabs_api_key_input()
                # 缓存键必须用凭证摘要，不能直接拼原始 Key：session_state 会被
                # Streamlit 序列化后送到浏览器，原始 Key 因此会出现在 devtools、
                # 任何浏览器扩展的 session-state 转储里。MiniMax 那条路径已经用
                # _credential_signature，这里此前是唯一的例外。摘要只用于缓存失效
                # 判断，不写入配置、日志或任务文件。
                cache_key = f"elevenlabs_voices_{_credential_signature(saved_elevenlabs_api_key)}"
                if cache_key not in st.session_state:
                    st.session_state[cache_key] = voice.get_elevenlabs_voices(
                        saved_elevenlabs_api_key
                    )
                filtered_voices = st.session_state[cache_key]
            elif selected_tts_server == "chatterbox":
                # 自托管 Chatterbox 服务的预置音色（来自 [chatterbox] voices 配置）
                _sync_chatterbox_config_from_session_state()
                filtered_voices = voice.get_chatterbox_voices()
            elif selected_tts_server == "fish_audio":
                filtered_voices = voice.get_fish_audio_voices()
            else:
                # 获取Azure的声音列表
                all_voices = voice.get_all_azure_voices(filter_locals=None)

                # 根据选择的TTS服务器筛选声音
                for v in all_voices:
                    if selected_tts_server == "azure-tts-v2":
                        # V2版本的声音名称中包含"v2"
                        if "V2" in v:
                            filtered_voices.append(v)
                    else:
                        # V1版本的声音名称中不包含"v2"
                        if "V2" not in v:
                            filtered_voices.append(v)

            def _friendly(v):
                if voice.is_no_voice(v):
                    return tr("No Voice Selected")
                if voice.is_elevenlabs_voice(v):
                    parts = v.split(":", 2)
                    return parts[2] if len(parts) >= 3 else v
                if voice.is_chatterbox_voice(v):
                    name = v.split(":", 1)[1] if ":" in v else v
                    return name.replace("-Female", "").replace("-Male", "")
                if voice.is_minimax_voice(v):
                    return minimax_voice_labels.get(v, v.split(":", 1)[1])
                if voice.is_fish_audio_voice(v):
                    parts = v.split(":", 2)
                    display_name = parts[2] if len(parts) >= 3 else v
                    return (
                        display_name.replace("Female", tr("Female"))
                        .replace("Male", tr("Male"))
                    )
                return (
                    v.replace("Female", tr("Female"))
                    .replace("Male", tr("Male"))
                    .replace("Neural", "")
                )

            friendly_names = {v: _friendly(v) for v in filtered_voices}

            # Gemini 旧目录把推测的性别放在值里（例如 Charon-Male）。按基础
            # voice name 映射到新的官方风格值，升级后继续保留用户原来的音色。
            if (
                selected_tts_server == "gemini-tts"
                and saved_voice_name not in friendly_names
            ):
                saved_gemini_voice = voice.parse_gemini_voice_name(saved_voice_name)
                saved_voice_name = next(
                    (
                        candidate
                        for candidate in filtered_voices
                        if voice.parse_gemini_voice_name(candidate)
                        == saved_gemini_voice
                    ),
                    saved_voice_name,
                )

            saved_voice_name_index = 0

            # 检查保存的声音是否在当前筛选的声音列表中
            if saved_voice_name in friendly_names:
                saved_voice_name_index = list(friendly_names.keys()).index(
                    saved_voice_name
                )
            else:
                # 如果不在，则根据当前UI语言选择一个默认声音
                for i, v in enumerate(filtered_voices):
                    if v.lower().startswith(st.session_state["ui_language"].lower()):
                        saved_voice_name_index = i
                        break

            # 如果没有找到匹配的声音，使用第一个声音
            if saved_voice_name_index >= len(friendly_names) and friendly_names:
                saved_voice_name_index = 0

            # 确保有声音可选
            if tts_mode_enabled and friendly_names:
                voice_name = stable_selectbox(
                    tr("Voiceover Voice"),
                    options=list(friendly_names.keys()),
                    default_value=list(friendly_names.keys())[saved_voice_name_index],
                    key=f"speech_synthesis_select_{selected_tts_server}",
                    format_func=lambda value: friendly_names.get(
                        value,
                        str(value).removeprefix("minimax:"),
                    ),
                    # MiniMax 支持用户直接输入列表外的克隆或生成音色 ID；其它
                    # Provider 维持原选择器行为，不扩大本次修改的影响范围。
                    accept_new_options=selected_tts_server == "minimax-tts",
                )

                if selected_tts_server == "minimax-tts":
                    custom_voice_id = str(voice_name or "").strip()
                    if custom_voice_id and not voice.is_minimax_voice(custom_voice_id):
                        voice_name = f"minimax:{custom_voice_id}"
                    if voice.is_minimax_voice(voice_name):
                        _set_runtime_config(
                            "minimax_tts",
                            "voice_id",
                            voice_name.split(":", 1)[1],
                        )

                params.voice_name = voice_name
                if not voice.is_no_voice(voice_name):
                    # 占位 sentinel 仅用于非自动模式的禁用展示，不覆盖用户上一次
                    # 真正选择的音色，切回自动配音后可以恢复原设置。
                    _set_runtime_config("ui", "voice_name", voice_name)
            elif tts_mode_enabled:
                # 如果没有声音可选，显示提示信息
                st.warning(
                    tr(
                        "No voices available for the selected TTS server. Please select another server."
                    )
                )
                voice_name = ""
                params.voice_name = ""
                _set_runtime_config("ui", "voice_name", "")
            else:
                # 非自动配音模式不显示音色控件，只复用保存值维持参数结构稳定。
                voice_name = saved_voice_name or voice.NO_VOICE_NAME
                params.voice_name = voice_name

            # 当选择V2版本或者声音是V2声音时，显示服务区域和API key输入框
            if tts_mode_enabled and (
                selected_tts_server == "azure-tts-v2"
                or (voice_name and voice.is_azure_v2_voice(voice_name))
            ):
                saved_azure_speech_region = config.azure.get("speech_region", "")
                saved_azure_speech_key = config.azure.get("speech_key", "")
                azure_speech_region = st.text_input(
                    tr("Speech Region"),
                    value=saved_azure_speech_region,
                    key="azure_speech_region_input",
                )
                azure_speech_key = st.text_input(
                    tr("Speech Key"),
                    value=saved_azure_speech_key,
                    type="password",
                    key="azure_speech_key_input",
                )
                _set_runtime_config("azure", "speech_region", azure_speech_region)
                _set_runtime_config("azure", "speech_key", azure_speech_key)

            if tts_mode_enabled and selected_tts_server == "gemini-tts":
                # Gemini TTS 与 Gemini LLM 共用同一份密钥；在音频面板提供直接入口，
                # 用户无需先切换 LLM Provider 才能完成语音配置。
                gemini_tts_api_key = st.text_input(
                    tr("Gemini API Key"),
                    value=config.app.get("gemini_api_key", ""),
                    type="password",
                    key="gemini_tts_api_key_input",
                )
                _set_runtime_config("app", "gemini_api_key", gemini_tts_api_key)

            # 当选择硅基流动时，显示API key输入框和说明信息
            if tts_mode_enabled and (
                selected_tts_server == "siliconflow"
                or (voice_name and voice.is_siliconflow_voice(voice_name))
            ):
                saved_siliconflow_api_key = config.siliconflow.get("api_key", "")

                siliconflow_api_key = st.text_input(
                    tr("SiliconFlow API Key"),
                    value=saved_siliconflow_api_key,
                    type="password",
                    key="siliconflow_api_key_input",
                )

                _set_runtime_config("siliconflow", "api_key", siliconflow_api_key)

            # 当选择 Xiaomi MiMo TTS 时，复用 MiMo LLM provider 的 API Key。
            # 这样用户如果同时使用 MiMo 生成文案和语音，只需要维护一份密钥。
            if tts_mode_enabled and (
                selected_tts_server == "mimo-tts"
                or (voice_name and voice.is_mimo_voice(voice_name))
            ):
                saved_mimo_api_key = config.app.get("mimo_api_key", "")

                mimo_api_key = st.text_input(
                    tr("MiMo API Key"),
                    value=saved_mimo_api_key,
                    type="password",
                    key="mimo_tts_api_key_input",
                )

                _set_runtime_config("app", "mimo_api_key", mimo_api_key)

            # ElevenLabs API key section
            if tts_mode_enabled and (
                selected_tts_server == "elevenlabs"
                or (voice_name and voice.is_elevenlabs_voice(voice_name))
            ):
                _render_elevenlabs_api_key_input(
                    "ElevenLabs API Key",
                )
                elevenlabs_api_key_rendered = True

                _elevenlabs_models = [
                    "eleven_multilingual_v2",
                    "eleven_flash_v2_5",
                    "eleven_v3",
                ]
                saved_elevenlabs_model = config.elevenlabs.get(
                    "model_id", "eleven_multilingual_v2"
                )
                if saved_elevenlabs_model not in _elevenlabs_models:
                    saved_elevenlabs_model = "eleven_multilingual_v2"
                elevenlabs_model = stable_selectbox(
                    tr("ElevenLabs Model"),
                    options=_elevenlabs_models,
                    default_value=saved_elevenlabs_model,
                    key="elevenlabs_model_select",
                )
                _set_runtime_config("elevenlabs", "model_id", elevenlabs_model)

            # Fish Audio API settings section
            if tts_mode_enabled and (
                selected_tts_server == "fish_audio"
                or (voice_name and voice.is_fish_audio_voice(voice_name))
            ):
                saved_fish_api_key = (
                    config.fish_audio.get("api_key", "")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else ""
                )
                fish_audio_api_key = st.text_input(
                    tr("Fish Audio API Key"),
                    value=saved_fish_api_key,
                    type="password",
                    key="fish_audio_api_key_input",
                )
                _set_runtime_config("fish_audio", "api_key", fish_audio_api_key)

                _fish_audio_models = [
                    "s2.1-pro-free",
                    "s2.1-pro",
                    "s2-pro",
                ]
                saved_fish_model = (
                    config.fish_audio.get("model", "s2.1-pro-free")
                    if hasattr(config, "fish_audio") and isinstance(config.fish_audio, dict)
                    else "s2.1-pro-free"
                )
                if saved_fish_model not in _fish_audio_models:
                    saved_fish_model = "s2.1-pro-free"
                fish_model = stable_selectbox(
                    tr("Fish Audio Model"),
                    options=_fish_audio_models,
                    default_value=saved_fish_model,
                    key="fish_audio_model_select",
                )
                _set_runtime_config("fish_audio", "model", fish_model)

            # Chatterbox API settings section (self-hosted, OpenAI-compatible)
            if tts_mode_enabled and (
                selected_tts_server == "chatterbox"
                or (voice_name and voice.is_chatterbox_voice(voice_name))
            ):
                chatterbox_base_url = st.text_input(
                    tr("Chatterbox Base URL"),
                    value=config.chatterbox.get("base_url")
                    or DEFAULT_CHATTERBOX_BASE_URL,
                    key="chatterbox_base_url_input",
                    placeholder=tr("Chatterbox Base URL Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox", "base_url", (chatterbox_base_url or "").strip()
                )

                chatterbox_api_key = st.text_input(
                    tr("Chatterbox API Key"),
                    value=config.chatterbox.get("api_key", ""),
                    type="password",
                    key="chatterbox_api_key_input",
                )
                _set_runtime_config("chatterbox", "api_key", chatterbox_api_key)

                chatterbox_model = st.text_input(
                    tr("Chatterbox Model"),
                    value=config.chatterbox.get("model_id") or DEFAULT_CHATTERBOX_MODEL,
                    key="chatterbox_model_input",
                )
                _set_runtime_config(
                    "chatterbox",
                    "model_id",
                    (chatterbox_model or DEFAULT_CHATTERBOX_MODEL).strip(),
                )

                _saved_chatterbox_voices = (
                    _parse_chatterbox_voices(config.chatterbox.get("voices"))
                    or DEFAULT_CHATTERBOX_VOICES
                )
                if isinstance(_saved_chatterbox_voices, list):
                    _saved_chatterbox_voices = ", ".join(_saved_chatterbox_voices)
                chatterbox_voices = st.text_input(
                    tr("Chatterbox Voices"),
                    value=str(_saved_chatterbox_voices or ""),
                    key="chatterbox_voices_input",
                    placeholder=tr("Chatterbox Voices Placeholder"),
                )
                _set_runtime_config(
                    "chatterbox",
                    "voices",
                    _parse_chatterbox_voices(chatterbox_voices),
                )

            # 三种模式只渲染当前任务真正需要的控件。自动配音可调音量和语速；
            # 上传音频只需要文件和音量；无配音不再展示无效设置。
            params.voice_name = (
                voice.NO_VOICE_NAME if voice_mode == VOICE_MODE_NONE else voice_name
            )
            params.voice_volume = 1.0
            params.voice_rate = 1.0
            uploaded_audio_file = None
            voice_volume_options = [0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0, 5.0]
            voice_rate_options = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.8, 2.0]

            if tts_mode_enabled:
                voice_control_cols = st.columns(2)
                with voice_control_cols[0]:
                    params.voice_volume = stable_selectbox(
                        tr("Voiceover Volume"),
                        options=voice_volume_options,
                        default_value=_saved_ui_choice(
                            "voice_volume", voice_volume_options, 1.0
                        ),
                        key="voice_volume_select",
                        format_func=lambda value: f"{int(value * 100)}%",
                        help=tr("Voiceover Volume Help"),
                    )

                with voice_control_cols[1]:
                    params.voice_rate = stable_selectbox(
                        tr("Voiceover Speed"),
                        options=voice_rate_options,
                        default_value=_saved_ui_choice(
                            "voice_rate", voice_rate_options, 1.0
                        ),
                        key="voice_rate_select",
                        format_func=lambda value: f"{value:.1f}×",
                        help=tr("Voiceover Speed Help"),
                    )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                _set_runtime_config("ui", "voice_rate", params.voice_rate)

                # 试听必须位于音量和语速控件之后，确保调用使用当前控件值。
                _render_voice_preview(
                    params,
                    friendly_names,
                    selected_tts_server,
                    voice_name,
                )
            elif voice_mode == VOICE_MODE_UPLOAD:
                custom_audio_file_types = sorted(
                    extension.removeprefix(".") for extension in CUSTOM_AUDIO_EXTENSIONS
                )
                uploaded_audio_file = st.file_uploader(
                    tr("Upload Voiceover File"),
                    type=custom_audio_file_types
                    + [file_type.upper() for file_type in custom_audio_file_types],
                    accept_multiple_files=False,
                    key="custom_audio_file_uploader",
                    help=tr("Upload Voiceover File Help"),
                )
                params.voice_volume = stable_selectbox(
                    tr("Voiceover Volume"),
                    options=voice_volume_options,
                    default_value=_saved_ui_choice(
                        "voice_volume", voice_volume_options, 1.0
                    ),
                    key="voice_volume_select",
                    format_func=lambda value: f"{int(value * 100)}%",
                    help=tr("Voiceover Volume Help"),
                )
                _set_runtime_config("ui", "voice_volume", params.voice_volume)
                if uploaded_audio_file:
                    st.audio(uploaded_audio_file, format="audio/mp3")
                    st.info(
                        tr(
                            "Custom audio will be used directly. TTS synthesis will be skipped for this task."
                        )
                    )
            uploaded_bgm_file = _render_background_music_settings(
                params,
                elevenlabs_api_key_rendered=elevenlabs_api_key_rendered,
            )
    return uploaded_audio_file, uploaded_bgm_file, voice_mode


def _render_subtitle_settings(panel, params):
    """渲染字幕设置并更新生成参数。"""
    with panel:
        with st.container(border=True):
            st.write(tr("Subtitle Settings"))
            st.session_state.setdefault(
                "subtitle_enabled_checkbox",
                _saved_ui_bool(
                    "subtitle_enabled",
                    DEFAULT_SUBTITLE_SETTINGS["subtitle_enabled"],
                ),
            )
            params.subtitle_enabled = st.checkbox(
                tr("Enable Subtitles"),
                key="subtitle_enabled_checkbox",
            )
            _set_runtime_config("ui", "subtitle_enabled", params.subtitle_enabled)
            subtitle_settings_disabled = not params.subtitle_enabled

            # 逐词高亮：正在念的词点亮，其余压暗。需要 ASR 的词级时间；
            # 拿不到时自动退回整句原样显示。
            st.session_state.setdefault(
                "caption_word_highlight_checkbox",
                _saved_ui_bool(
                    "caption_word_highlight",
                    DEFAULT_SUBTITLE_SETTINGS.get("caption_word_highlight", True),
                ),
            )
            params.caption_word_highlight = st.checkbox(
                tr("Highlight Spoken Words"),
                key="caption_word_highlight_checkbox",
                disabled=subtitle_settings_disabled,
                help=tr("Highlight Spoken Words Help"),
            )
            _set_runtime_config(
                "ui", "caption_word_highlight", params.caption_word_highlight
            )
            font_names = get_all_fonts()
            saved_font_name = config.ui.get(
                "font_name", DEFAULT_SUBTITLE_SETTINGS["font_name"]
            )
            saved_font_name_index = 0
            if saved_font_name in font_names:
                saved_font_name_index = font_names.index(saved_font_name)
            params.font_name = stable_selectbox(
                tr("Font"),
                options=font_names,
                default_value=font_names[saved_font_name_index] if font_names else "",
                key="font_name_select",
                disabled=subtitle_settings_disabled,
            )
            _set_runtime_config("ui", "font_name", params.font_name)

            subtitle_positions = [
                (tr("Top"), "top"),
                (tr("Center"), "center"),
                (tr("Bottom"), "bottom"),
                (tr("Custom"), "custom"),
            ]
            saved_subtitle_position = config.ui.get(
                "subtitle_position", DEFAULT_SUBTITLE_SETTINGS["subtitle_position"]
            )
            saved_position_index = 2
            for i, (_, pos_value) in enumerate(subtitle_positions):
                if pos_value == saved_subtitle_position:
                    saved_position_index = i
                    break
            selected_subtitle_position = stable_pills(
                tr("Position"),
                options=[value for _, value in subtitle_positions],
                default_value=subtitle_positions[saved_position_index][1],
                key="subtitle_position_select",
                format_func=lambda value: dict(
                    (v, label) for label, v in subtitle_positions
                )[value],
                disabled=subtitle_settings_disabled,
            )
            params.subtitle_position = selected_subtitle_position
            _set_runtime_config("ui", "subtitle_position", params.subtitle_position)

            if params.subtitle_position == "custom":
                saved_custom_position = config.ui.get(
                    "custom_position", DEFAULT_SUBTITLE_SETTINGS["custom_position"]
                )
                st.session_state.setdefault(
                    "custom_position_input", str(saved_custom_position)
                )
                custom_position = st.text_input(
                    tr("Custom Position (% from top)"),
                    key="custom_position_input",
                    disabled=subtitle_settings_disabled,
                )
                try:
                    params.custom_position = float(custom_position)
                    if params.custom_position < 0 or params.custom_position > 100:
                        st.error(tr("Please enter a value between 0 and 100"))
                    else:
                        _set_runtime_config(
                            "ui", "custom_position", params.custom_position
                        )
                except ValueError:
                    st.error(tr("Please enter a valid number"))

            # 非中文语言的颜色标签通常比中文更长。为颜色选择器保留适当宽度，
            # 避免标签换行，同时仍给字号滑块保留足够的可操作空间。
            font_cols = st.columns([0.42, 0.58])
            with font_cols[0]:
                saved_text_fore_color = config.ui.get(
                    "text_fore_color", DEFAULT_SUBTITLE_SETTINGS["text_fore_color"]
                )
                st.session_state.setdefault("font_color_picker", saved_text_fore_color)
                params.text_fore_color = st.color_picker(
                    tr("Font Color"),
                    key="font_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "text_fore_color", params.text_fore_color)

            with font_cols[1]:
                saved_font_size = config.ui.get(
                    "font_size", DEFAULT_SUBTITLE_SETTINGS["font_size"]
                )
                st.session_state.setdefault("font_size_slider", saved_font_size)
                params.font_size = st.slider(
                    tr("Font Size"),
                    30,
                    100,
                    key="font_size_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "font_size", params.font_size)

            stroke_cols = st.columns([0.42, 0.58])
            with stroke_cols[0]:
                st.session_state.setdefault(
                    "stroke_color_picker",
                    _saved_ui_color(
                        "stroke_color", DEFAULT_SUBTITLE_SETTINGS["stroke_color"]
                    ),
                )
                params.stroke_color = st.color_picker(
                    tr("Stroke Color"),
                    key="stroke_color_picker",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_color", params.stroke_color)
            with stroke_cols[1]:
                st.session_state.setdefault(
                    "stroke_width_slider",
                    _saved_ui_number(
                        "stroke_width",
                        DEFAULT_SUBTITLE_SETTINGS["stroke_width"],
                        0.0,
                        10.0,
                    ),
                )
                params.stroke_width = st.slider(
                    tr("Stroke Width"),
                    0.0,
                    10.0,
                    key="stroke_width_slider",
                    disabled=subtitle_settings_disabled,
                )
                _set_runtime_config("ui", "stroke_width", params.stroke_width)

            # 背景开关的本地化名称普遍比颜色标签更长，因此让开关占据略多空间。
            subtitle_bg_cols = st.columns([0.55, 0.45])
            saved_subtitle_background_enabled = config.ui.get(
                "subtitle_background_enabled",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_enabled"],
            )
            st.session_state.setdefault(
                "subtitle_background_enabled_checkbox",
                saved_subtitle_background_enabled,
            )
            with subtitle_bg_cols[0]:
                subtitle_background_enabled = st.checkbox(
                    tr("Enable Subtitle Background"),
                    key="subtitle_background_enabled_checkbox",
                    disabled=subtitle_settings_disabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_enabled",
                subtitle_background_enabled,
            )

            # 背景颜色和圆角样式都从属于字幕背景开关。子控件始终保留在页面中，
            # 父开关关闭时统一禁用，避免一个控件消失而另一个控件禁用造成布局跳动。
            # 颜色值仍保存在 UI 配置中，重新启用背景后可以恢复用户之前的选择；
            # 传给生成服务的参数则设为 False，确保关闭状态不会实际渲染背景。
            saved_subtitle_background_color = config.ui.get(
                "subtitle_background_color",
                DEFAULT_SUBTITLE_SETTINGS["subtitle_background_color"],
            )
            st.session_state.setdefault(
                "subtitle_background_color_picker",
                saved_subtitle_background_color,
            )
            with subtitle_bg_cols[1]:
                selected_subtitle_background_color = st.color_picker(
                    tr("Subtitle Background Color"),
                    key="subtitle_background_color_picker",
                    disabled=subtitle_settings_disabled
                    or not subtitle_background_enabled,
                )
            _set_runtime_config(
                "ui",
                "subtitle_background_color",
                selected_subtitle_background_color,
            )
            params.text_background_color = (
                selected_subtitle_background_color
                if subtitle_background_enabled
                else False
            )

            saved_rounded_subtitle_background = config.ui.get(
                "rounded_subtitle_background",
                DEFAULT_SUBTITLE_SETTINGS["rounded_subtitle_background"],
            )
            # 背景关闭时，圆角背景没有可渲染的底色。这里禁用控件但保留原配置，
            # 用户下次重新开启字幕背景后，可以继续使用之前保存的圆角偏好。
            rounded_background_disabled = (
                subtitle_settings_disabled or not subtitle_background_enabled
            )
            st.session_state.setdefault(
                "rounded_subtitle_background_checkbox",
                saved_rounded_subtitle_background,
            )
            selected_rounded_subtitle_background = st.checkbox(
                tr("Rounded Subtitle Background"),
                help=tr("Rounded Subtitle Background Help"),
                disabled=rounded_background_disabled,
                key="rounded_subtitle_background_checkbox",
            )
            params.rounded_subtitle_background = (
                selected_rounded_subtitle_background
                if subtitle_background_enabled
                else False
            )
            if not subtitle_settings_disabled and subtitle_background_enabled:
                _set_runtime_config(
                    "ui",
                    "rounded_subtitle_background",
                    selected_rounded_subtitle_background,
                )

            if video.subtitle_colors_are_indistinguishable(params):
                # 同色配置仍然是合法的用户选择，因此只在字幕设置区域就近提示，
                # 不阻止生成。用户可以根据实际视觉需求决定是否继续。
                st.warning(tr("Subtitle Colors Are Indistinguishable"))

            subtitle_preview_text = params.video_script or params.video_subject
            selected_font_path = os.path.join(font_dir, params.font_name)
            if (
                params.subtitle_enabled
                and subtitle_preview_text
                and not video.subtitle_font_supports_text(
                    selected_font_path, subtitle_preview_text
                )
            ):
                st.warning(tr("Subtitle Font Does Not Support Text"))

            if st.button(
                tr("Restore Default Subtitle Settings"),
                key="restore_default_subtitle_settings",
                icon=":material/restart_alt:",
                on_click=reset_subtitle_settings,
                width='stretch',
            ):
                st.toast(tr("Default Subtitle Settings Restored"))


def _render_generation_controls(
    params, uploaded_files, uploaded_audio_file, uploaded_bgm_file, voice_mode
):
    """
    校验生成依赖、提交任务，并渲染日志与成片结果。

    返回本次页面执行是否成功提交了新任务。提交前已经请求非阻塞保存，调用方
    据此跳过页面末尾的重复请求。主脚本必须及时结束，定时 Fragment 才能持续
    刷新进度和任务日志。
    """
    restore_upload_requirements = st.session_state.get(
        "task_restore_upload_requirements", {}
    )
    has_local_materials = bool(
        uploaded_files or st.session_state.get("local_video_materials", [])
    )
    has_custom_audio = bool(uploaded_audio_file)
    unmet_restore_requirements = _get_unmet_restore_upload_requirements(
        restore_upload_requirements,
        video_source=params.video_source,
        voice_name=params.voice_name or "",
        has_local_materials=has_local_materials,
        has_custom_audio=has_custom_audio,
        voice_mode=voice_mode,
    )
    if "local_materials" in unmet_restore_requirements:
        st.warning(tr("Task Restore Local Materials Warning"))
    if "custom_audio" in unmet_restore_requirements:
        st.warning(tr("Task Restore Custom Audio Warning"))
    if restore_upload_requirements and not unmet_restore_requirements:
        # 用户已重新上传文件，或主动切换了素材来源/音色。此时历史任务的上传依赖
        # 已经得到明确处理，清除标记，避免后续普通生成继续显示旧提示。
        st.session_state.pop("task_restore_upload_requirements", None)

    _render_settings_transfer(params)

    start_button = st.button(
        tr("Generate Video"),
        width='stretch',
        type="primary",
        key="generate_video_button",
        on_click=_prepare_generation_task,
    )
    # 活动面板的“用作视频主题”按钮会写入该标志，表示用户希望用刚才载入的
    # 活动文案直接开始生成。这里把它等价于一次真实的“生成视频”点击：手动
    # 复用同样的任务准备与校验逻辑，避免出现第二条提交路径无法获得 API 密钥、
    # 本地素材等校验的状态。
    if st.session_state.pop("campaign_auto_generate", False):
        _prepare_generation_task()
        start_button = True
    render_onboarding_tour()
    if start_button:
        _save_runtime_config()
        task_id = st.session_state.get("pending_generation_task_id") or str(uuid4())
        _add_active_generation_task(
            task_id,
            subject=params.video_subject or params.video_script or task_id,
        )
        if not params.video_subject and not params.video_script:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            st.stop()

        if params.video_source not in [
            "pexels",
            "pixabay",
            "coverr",
            "wavespeed",
            "volcengine_seedance",
            "loomloom",
            "local",
        ]:
            _remove_active_generation_task(task_id)
            st.error(tr("Please Select a Valid Video Source"))
            st.stop()

        if params.video_source == "pexels" and not config.app.get(
            "pexels_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pexels API Key"))
            st.stop()

        if params.video_source == "pixabay" and not config.app.get(
            "pixabay_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Pixabay API Key"))
            st.stop()

        if params.video_source == "coverr" and not config.app.get(
            "coverr_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Coverr API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not config.app.get(
            "wavespeed_api_keys", ""
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the WaveSpeed API Key"))
            st.stop()

        if params.video_source == "wavespeed" and not st.session_state.get(
            "wavespeed_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm WaveSpeed Charge Required"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not (
            volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Please Enter the Volcano Engine Ark API Key"))
            st.stop()

        if params.video_source == "volcengine_seedance" and not st.session_state.get(
            "volcengine_seedance_confirm_charge", False
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Confirm Volcano Engine Seedance Charge Required"))
            st.stop()

        loomloom_video_request = None
        if params.video_source == "loomloom":
            current_batch, current_signature = _current_loomloom_video_quote_context(
                params
            )
            quoted_batch = st.session_state.get("loomloom_video_batch")
            quote_result = st.session_state.get("loomloom_video_quote")
            quote_is_current = bool(
                current_batch is not None
                and isinstance(quoted_batch, loomloom.LoomLoomVideoBatch)
                and quote_result is not None
                and st.session_state.get("loomloom_video_input_signature")
                == current_signature
            )
            if not quote_is_current:
                _remove_active_generation_task(task_id)
                st.error(tr("AI Video Quote Required"))
                st.stop()
            if not st.session_state.get("loomloom_video_confirm_charge", False):
                _remove_active_generation_task(task_id)
                st.error(tr("Confirm AI Video Charge Required"))
                st.stop()
            try:
                video_backend = _create_loomloom_video_backend()
                loomloom_video_request = loomloom.LoomLoomConfirmedVideoRequest(
                    settings=video_backend.settings,
                    batch=current_batch,
                    listing_version_id=quote_result.listing_version_id,
                    client_request_id=st.session_state[
                        "loomloom_video_client_request_id"
                    ],
                )
                loomloom_video_request.validate()
            except (loomloom.LoomLoomError, ValueError) as exc:
                _remove_active_generation_task(task_id)
                st.error(str(exc))
                st.stop()

        if (
            params.bgm_type == "sonilo"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not sonilo_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("Sonilo API Key Required"))
            st.stop()

        if (
            params.bgm_type == "elevenlabs"
            and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
            and not elevenlabs_music_service.is_enabled()
        ):
            _remove_active_generation_task(task_id)
            st.error(tr("ElevenLabs API Key Required"))
            st.stop()

        if params.video_source == "local" and not has_local_materials:
            # 本地素材为空时继续执行会先产生 TTS/字幕，最后才在素材预处理阶段失败。
            # 在任务启动前拦截，可以避免无意义的 API 调用和中间文件。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Local Materials First"))
            st.stop()

        if voice_mode == VOICE_MODE_UPLOAD and not uploaded_audio_file:
            # 上传音频是用户显式选择的配音方式，缺少文件时不能静默退回 TTS。
            # 在任务启动前拦截，避免产生与用户选择不一致的成片。
            _remove_active_generation_task(task_id)
            st.error(tr("Please Upload Voiceover File First"))
            st.stop()

        if "custom_audio" in unmet_restore_requirements:
            # 历史自定义音频不能自动回填。用户尚未重新上传且也没有主动更换音色时，
            # 必须阻止静默退回 TTS，否则重新生成的结果会与原任务语音不一致。
            _remove_active_generation_task(task_id)
            st.error(tr("Task Restore Custom Audio Warning"))
            st.stop()

        if uploaded_bgm_file and bgm_service.should_use_bgm(
            params.bgm_type, params.bgm_volume
        ):
            try:
                saved_bgm_name = bgm_service.save_bgm_upload(
                    uploaded_bgm_file.name, uploaded_bgm_file
                )
            except bgm_service.BgmUploadError as exc:
                _remove_active_generation_task(task_id)
                logger.warning(f"WebUI background music upload rejected: {str(exc)}")
                st.error(tr("Invalid Background Music"))
                st.stop()
            except bgm_service.BgmServiceError as exc:
                _remove_active_generation_task(task_id)
                logger.error(f"WebUI background music upload failed: {str(exc)}")
                st.error(tr("Background Music Validation Failed"))
                st.stop()
            # 保存成功后只把文件名写入任务参数。视频服务会在两个 BGM 白名单
            # 目录中重新解析，避免把服务器绝对路径持久化或展示给用户。
            params.bgm_file = saved_bgm_name
        elif uploaded_bgm_file:
            # 0 音量时视频服务不会使用任何 BGM，因此不再把已经预览的上传文件
            # 持久化到 storage。用户之后调高音量时可直接再次点击生成完成保存。
            params.bgm_file = ""

        if uploaded_audio_file:
            task_dir = utils.task_dir(task_id)
            try:
                custom_audio_path = _build_uploaded_file_path(
                    uploaded_audio_file,
                    task_dir,
                    CUSTOM_AUDIO_EXTENSIONS,
                    "custom-audio",
                )
            except ValueError:
                _remove_active_generation_task(task_id)
                st.error(tr("Unsupported Upload File Type"))
                st.stop()
            with open(custom_audio_path, "wb") as f:
                f.write(uploaded_audio_file.getbuffer())
            params.custom_audio_file = custom_audio_path

        if uploaded_files:
            local_videos_dir = utils.storage_dir("local_videos", create=True)
            # 每次重新上传时都以本次选择的素材为准，避免旧素材不断重复追加。
            params.video_materials = []
            persisted_local_materials = []
            for file in uploaded_files:
                try:
                    file_path = _build_uploaded_file_path(
                        file,
                        local_videos_dir,
                        LOCAL_MATERIAL_EXTENSIONS,
                        "material",
                    )
                except ValueError:
                    _remove_active_generation_task(task_id)
                    st.error(tr("Unsupported Upload File Type"))
                    st.stop()
                with open(file_path, "wb") as f:
                    f.write(file.getbuffer())
                    m = MaterialInfo()
                    m.provider = "local"
                    m.url = file_path
                    params.video_materials.append(m)
                    persisted_local_materials.append(
                        {
                            "provider": m.provider,
                            "url": m.url,
                            "duration": m.duration,
                        }
                    )
            # 将已上传并保存到本地的视频素材写入会话，供后续只改文案时直接复用。
            st.session_state["local_video_materials"] = persisted_local_materials
        elif (
            params.video_source == "local" and st.session_state["local_video_materials"]
        ):
            # 当用户没有重新上传文件时，复用最近一次已经保存到磁盘的本地素材列表。
            params.video_materials = []
            for material in st.session_state["local_video_materials"]:
                m = MaterialInfo()
                m.provider = material.get("provider", "local")
                m.url = material.get("url", "")
                m.duration = material.get("duration", 0)
                if m.url:
                    params.video_materials.append(m)

        reusable_voice_preview = _get_reusable_full_voice_preview(
            params,
            voice_mode,
        )
        if reusable_voice_preview:
            # 试听缓存只存在当前 Streamlit 会话。提交前把音频写入目标任务目录，
            # 后台线程随后只读取任务自己的文件；即使页面 rerun、浏览器关闭或
            # 用户试听其它音色，也不会影响已经入队的生成任务。
            preview_audio_file = os.path.join(
                utils.task_dir(task_id),
                "audio.mp3",
            )
            with open(preview_audio_file, "wb") as file:
                file.write(reusable_voice_preview.pop("audio_bytes"))
            reusable_voice_preview["audio_file"] = preview_audio_file
            logger.info(
                f"reuse full voice preview for task: "
                f"task_id={task_id}, duration={reusable_voice_preview['duration']:.2f}s"
            )

        try:
            st.toast(tr("Generating Video"))
            logger.info(tr("Start Generating Video"))
            logger.info(utils.to_json(params))
            webui_task.submit_generation(
                task_id=task_id,
                params=params,
                capture_logs=not config.ui.get("hide_log", False),
                voice_preview=reusable_voice_preview,
                loomloom_video_request=loomloom_video_request,
            )
            if loomloom_video_request is not None:
                # 一个报价只允许提交一次。后台请求自带稳定幂等 ID；提交成功后
                # 清除页面报价，下一次生成必须重新询价和确认。
                st.session_state["loomloom_video_batch"] = None
                st.session_state["loomloom_video_quote"] = None
                st.session_state["loomloom_video_input_signature"] = ""
                st.session_state["loomloom_video_client_request_id"] = ""
        except Exception:
            _remove_active_generation_task(task_id)
            st.error(tr("Video Generation Failed"))
            st.stop()

        st.session_state["current_generation_task_id"] = task_id
        logger.info(f"WebUI generation task submitted: task_id={task_id}")

    _render_current_generation_task()
    return start_button


# -----------------------------------------------------------------------------
# 内容计划与发布：读取 plans/<channel>、生成“今日”视频、经 Chrome 发布
# -----------------------------------------------------------------------------


def _plan_python() -> str:
    return os.path.join(root_dir, ".venv", "bin", "python")


def _plan_uploader_tool() -> str:
    return os.path.join(tools_dir, "publish_video.py")


def _run_plan_uploader(args, timeout=900) -> dict:
    command = [_plan_python(), _plan_uploader_tool()] + args
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "uploader timed out"}
    output = (proc.stdout or "").strip()
    if not output:
        return {
            "success": False,
            "error": (proc.stderr or "uploader produced no output").strip(),
        }
    last_line = output.splitlines()[-1]
    try:
        return json.loads(last_line)
    except (ValueError, TypeError):
        return {"success": False, "error": last_line}


def _run_youtube_cli(args, timeout=300) -> dict:
    """Run a YouTube CLI verb (``--youtube-*``) and parse its JSON line."""
    command = [_plan_python(), os.path.join(root_dir, "cli.py")] + args
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=root_dir,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "YouTube command timed out"}
    output = (proc.stdout or "").strip()
    if not output:
        return {
            "success": False,
            "error": (proc.stderr or "no output").strip()[-400:],
        }
    try:
        return json.loads(output.splitlines()[-1])
    except (ValueError, TypeError):
        return {"success": False, "error": output.splitlines()[-1][:400]}


#: ``automation.env`` 键 → ``VideoParams`` 字段。键是频道自己的配置文件里的写法，
#: 值是模型字段名。
#:
#: 优先级是**频道文件压过表单**：这四个频道都刻意用自己的 VOICE_NAME / BGM_TYPE /
#: BGM_VOLUME 做品牌区分，若改成表单压过文件，这些配置就全部失去意义，之前"设置
#: 被丢弃"的抱怨只是换了个方向继续丢。真正要修的是**沉默**，不是优先级：以前文件
#: 只覆盖 7 个键、其余 46 个设置被整个丢掉且无人提示；现在文件只覆盖它写了的键，
#: 其余设置从表单照常生效，界面也把"这条路径会覆盖哪几项"直接列出来。
_PLAN_ENV_FIELD_MAP: dict[str, str] = {
    "VIDEO_ASPECT": "video_aspect",
    "VIDEO_SOURCE": "video_source",
    "VOICE_NAME": "voice_name",
    "VOICE_RATE": "voice_rate",
    "VOICE_VOLUME": "voice_volume",
    "VIDEO_LANGUAGE": "video_language",
    "PARAGRAPH_NUMBER": "paragraph_number",
    "BGM_TYPE": "bgm_type",
    "BGM_VOLUME": "bgm_volume",
    "VIDEO_CLIP_DURATION": "video_clip_duration",
    "VIDEO_CLIP_SPEED": "video_clip_speed",
    "VIDEO_CONCAT_MODE": "video_concat_mode",
    "VIDEO_TRANSITION_MODE": "video_transition_mode",
    "VIDEO_COUNT": "video_count",
    "SUBTITLE_ENABLED": "subtitle_enabled",
    "SUBTITLE_POSITION": "subtitle_position",
    "FONT_NAME": "font_name",
    "FONT_SIZE": "font_size",
    "TEXT_FORE_COLOR": "text_fore_color",
    "STROKE_COLOR": "stroke_color",
    "STROKE_WIDTH": "stroke_width",
    "CAPTION_WORD_HIGHLIGHT": "caption_word_highlight",
    "MATCH_MATERIALS_TO_SCRIPT": "match_materials_to_script",
}

#: 上面这张表里需要按数字解析的键。写错类型时跳过该键并留日志，而不是让
#: pydantic 在提交任务时才炸——那时用户已经按下"生成"并等了一段时间。
_PLAN_ENV_INT_FIELDS = frozenset(
    {
        "paragraph_number",
        "video_clip_duration",
        "video_count",
        "font_size",
        "stroke_width",
    }
)
_PLAN_ENV_FLOAT_FIELDS = frozenset(
    {"voice_rate", "voice_volume", "bgm_volume", "video_clip_speed"}
)
_PLAN_ENV_BOOL_FIELDS = frozenset(
    {"subtitle_enabled", "caption_word_highlight", "match_materials_to_script"}
)


def _coerce_plan_env_value(field: str, raw: str):
    """把 automation.env 的字符串转成 VideoParams 需要的类型。

    解析失败返回 ``None``，由调用方跳过并记录。值形如 ``9:16`` 的 VIDEO_ASPECT 走
    默认字符串路径即可，它本来就是枚举字符串。
    """
    text = str(raw).strip()
    if field in _PLAN_ENV_BOOL_FIELDS:
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        return None
    if field in _PLAN_ENV_INT_FIELDS:
        try:
            return int(float(text))
        except ValueError:
            return None
    if field in _PLAN_ENV_FLOAT_FIELDS:
        try:
            return float(text)
        except ValueError:
            return None
    return text or None


def _apply_plan_env(params: VideoParams, env: dict) -> tuple[VideoParams, list[str], list[str]]:
    """把频道的 automation.env 叠在表单参数之上。

    返回 ``(参数, 已覆盖的字段, 无法解析而被跳过的键)``。后两个列表给界面用：
    覆盖了什么要说清楚，跳过了什么也要说清楚，否则用户改了一个拼错的键会以为它生效了。
    """
    applied: list[str] = []
    skipped: list[str] = []
    for key, raw in (env or {}).items():
        field = _PLAN_ENV_FIELD_MAP.get(key)
        if field is None:
            continue  # 不是认识的键，保持静默：文件里常留着注释性配置
        value = _coerce_plan_env_value(field, raw)
        if value is None:
            logger.warning(
                f"ignoring unparseable automation.env value: {key}={raw!r} "
                f"(field {field})"
            )
            skipped.append(key)
            continue
        try:
            setattr(params, field, value)
        except Exception as exc:  # noqa: BLE001 - 单个字段非法不该毁掉整次生成
            logger.warning(
                f"ignoring rejected automation.env value: {key}={raw!r} "
                f"(field {field}, error={exc})"
            )
            skipped.append(key)
            continue
        applied.append(field)
    return params, applied, skipped


def _render_effective_parameters_readout(channel: str, env: dict) -> None:
    """
    一行说明这条路径会覆盖表单里的哪几项。

    这里曾经是七个小节、两大段文字，用来解释"界面上的设置在这条路径上会被丢弃"。
    那是把一份 bug 报告直接画进了界面。现在 ``automation.env`` 只覆盖它显式写了的
    键，其余设置照常从表单生效，所以这里只需要回答一个问题：**按下这个按钮，我改的
    设置会被谁盖掉？**
    """
    known = {
        key: value
        for key, value in (env or {}).items()
        if key in _PLAN_ENV_FIELD_MAP
    }
    unknown = {key: value for key, value in (env or {}).items() if key not in _PLAN_ENV_FIELD_MAP}

    summary = tr("Plan Overrides From Channel").format(
        channel=channel,
        count=len(known),
    )
    if not known:
        st.caption(summary)
    else:
        st.caption(summary)
        # 带上取值。只显示键名的话，看到 VOICE_NAME 仍然不知道它被设成了什么，
        # 而这正是"我改的设置到底有没有被用上"要回答的问题。
        st.write(
            ", ".join(f"`{key}={value}`" for key, value in sorted(known.items()))
        )
    if unknown:
        st.caption(tr("Plan Unknown Env Keys").format(count=len(unknown)))
        st.write(", ".join(f"`{key}`" for key in sorted(unknown)))


def _build_plan_params(channel, topic, base_params=None) -> VideoParams:
    """
    计划路径的参数：表单当前值 + 频道 automation.env 的覆盖。

    早先这里是 ``VideoParams(video_subject=topic)`` —— 一个全新的对象，然后只套用
    automation.env 里的七个键。界面上四列设置的其余部分连同它们在配置文件里的取值
    一起被整个丢掉，于是一条花了两个多小时的渲染，用的是一份与界面显示无关的参数。

    现在的顺序是：**以表单为准，再让频道文件覆盖它写了的键**。频道文件优先是因为这
    四个频道正是靠它区分品牌的（各自的 VOICE_NAME / BGM_TYPE / BGM_VOLUME），
    若反过来，频道配置就失去意义了；而"哪些键被覆盖"由上面的 readout 直接显示，
    不再需要靠读代码去猜。

    :param base_params: 表单当前的参数快照。``None`` 时退化为一份默认参数，
        仅用于测试与没有快照的首轮渲染。
    """
    if base_params is None:
        # video_subject 是必填字段，裸 VideoParams() 会抛 ValidationError；
        # 这里传空串，与 _page_generate 的做法一致。
        base_params = VideoParams(video_subject="")
    # 深拷贝：直接改表单那份会让"手动生成"与"计划生成"共享对象，随后一次计划渲染
    # 就把用户正在编辑的表单改掉了。
    params = base_params.model_copy(deep=True)
    params.video_subject = topic

    env = plans.read_automation_env(channel)
    _applied, _covered, _skipped = _apply_plan_env(params, env)

    system_prompt = plans.read_file_text(channel, "system_prompt.txt")
    if system_prompt:
        params.custom_system_prompt = system_prompt
    return params


def _submit_plan_generation(channel, topic) -> None:
    task_id = str(uuid4())
    _add_active_generation_task(task_id, subject=topic)
    # 记录该任务归属于哪个频道/主题。任务完成后，任务管理 fragment 会读取这份
    # 元数据，把成片归档进 outputs/<channel>/<月份>/videos/ 并推进月度计划。
    plan_meta = {"channel": channel, "topic": topic}
    try:
        task_path = utils.task_dir(task_id)
        os.makedirs(task_path, exist_ok=True)
        with open(str(plan_meta_path(task_path)), "w", encoding="utf-8") as handle:
            json.dump(plan_meta, handle, ensure_ascii=False)
    except OSError as exc:
        logger.warning(f"failed to write plan task metadata: channel={channel}, error={exc}")
    params = _build_plan_params(
        channel, topic, st.session_state.get("form_params_snapshot")
    )
    _save_runtime_config()
    try:
        st.toast(tr("Generating Video"))
        logger.info(f"plan generation submitted: channel={channel}, task_id={task_id}")
        webui_task.submit_generation(
            task_id=task_id,
            params=params,
            capture_logs=not config.ui.get("hide_log", False),
        )
    except Exception:
        _remove_active_generation_task(task_id)
        st.error(tr("Video Generation Failed"))
        st.stop()
    st.session_state["current_generation_task_id"] = task_id
    st.rerun()


@st.fragment(run_every="2s")
def _publish_status(key: str):
    """轮询一次发布作业，只重画这一小块。"""
    job = publish_jobs.get_job(key)
    if job is None or not job.done:
        return
    info = job.snapshot()
    if job.status == "done":
        st.success(tr("Video Published"))
        published = job.result or {}
        # API 路径把每个平台的结果收在 result.<platform> 下；保留顶层 url
        # 是为了兼容仍然返回扁平结构的旧结果。
        video_url = (published.get("result") or {}).get("youtube", {}).get(
            "url"
        ) or published.get("url")
        if video_url:
            st.write(video_url)
    else:
        st.error((job.result or {}).get("error", tr("Video Publish Failed")))
    st.caption(tr("Publish Elapsed").format(seconds=info["elapsed"]))
    if st.button(tr("Dismiss"), key=f"publish_dismiss_{key}", width="content"):
        publish_jobs.clear_finished_jobs()
        st.rerun()


def _render_publish_button(*, key, label, button_key, disabled, work, primary=False):
    """发布按钮：只负责启动作业，进度交给 :func:`_publish_status` 轮询。

    同步跑发布会让整页冻结——上传一次几分钟，页面期间完全不可用。因此这里点了
    就返回，作业在后台线程里跑。同一时间同一条片子只允许一个作业：发布是公开可见
    且无法撤回的，重复提交比等待糟糕得多。
    """
    running = publish_jobs.is_running(key)
    if st.button(
        tr("Publishing") if running else label,
        type="primary" if (primary and not running) else "secondary",
        width="stretch",
        disabled=disabled or running,
        key=button_key,
        icon=":material/upload:" if running else None,
    ):
        publish_jobs.start_publish_job(key, label, work)
        # 整页 rerun：这些函数都不是 @st.fragment，scope="fragment" 在这里必定抛
        # StreamlitAPIException（点“发布”“刷新缓存统计”都会炸）。
        st.rerun()

    job = publish_jobs.get_job(key)
    if job is not None:
        with st.container(key=f"publish_status_{key}"):
            if not job.done:
                # 原来是 st.progress(0.0, ...)：上传过程里没有任何可上报的进度，
                # 于是那根条永远停在 0%。一条不动的进度条比没有进度更糟——它承诺了
                # 一个不会到来的推进。换成"正在上传"的动效，如实说明只有一件事发生着。
                status_line = st.columns([1, 6], vertical_alignment="center")
                with status_line[0]:
                    orbs.orb("upload", size=20, label=tr("Uploading To YouTube"))
                status_line[1].caption(tr("Uploading To YouTube"))
            _publish_status(key)


def _publish_via_api(channel, video_path, title, index=None, plan_dir=None) -> dict:
    """Publish via the YouTube Data API: no browser, no visible window."""
    result = _run_plan_uploader(
        [
            "--channel",
            channel,
            "--video",
            video_path,
            "--platforms",
            "youtube",
        ]
    )
    if result.get("success") and index is not None:
        video_url = (
            (result.get("result") or {}).get("youtube", {}).get("url") or
            result.get("url")
        )
        if video_url:
            plans.mark_posted(channel, index, video_url, plan_dir=plan_dir)
    return result


def _render_posting_copy(video_path: str, subject: str, channel: str, key: str):
    """Keep the video and ready-to-copy publishing text in the same view."""
    with st.expander(tr("Posting Package")):
        try:
            package = posting_package.load_for_video(video_path, channel, subject)
        except (OSError, ValueError) as exc:
            st.error(str(exc))
            return
        if package.get("copy_source") == "fallback":
            st.caption(tr("Posting Copy Fallback"))
        for label, content in (
            ("Posting Title", package["title"]),
            ("Posting Description", package["caption"]),
            ("Posting Hashtags", " ".join(package["hashtags"])),
            ("Posting Comment", package["comment"]),
        ):
            st.caption(tr(label))
            if content:
                st.code(content, language=None)
            else:
                st.caption(tr("No Comment Draft"))
        st.download_button(
            tr("Download Posting Text"), posting_package.render_text(package),
            file_name=Path(video_path).stem + ".posting.txt", mime="text/plain",
            key=f"posting_text_{key}",
        )
        receipt_path = Path(video_path).with_suffix(".publish.json")
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                for platform, result in receipt.get("result", {}).items():
                    if result.get("success"):
                        st.success(f"{platform}: {result.get('url') or tr('Video Published')}")
                    else:
                        st.error(f"{platform}: {result.get('error') or tr('Video Publish Failed')}")
                if receipt.get("pending"):
                    st.warning(tr("Posting Outcome Unknown"))
            except (OSError, ValueError, AttributeError):
                st.warning(tr("Posting Receipt Invalid"))


_DRAFT_PREVIEW_STYLE_FIELDS = (
    "video_aspect",
    "subtitle_enabled",
    "subtitle_position",
    "custom_position",
    "font_name",
    "font_size",
    "text_fore_color",
    "text_background_color",
    "stroke_color",
    "stroke_width",
    "rounded_subtitle_background",
)


def _draft_preview_cache_key(params, text: str) -> tuple:
    """只取影响画面的字段做缓存键。

    语音、音乐这类不影响画面的参数如果也进键，用户每调一次音量就得重画一张图。
    """
    return (
        tuple(getattr(params, field, None) for field in _DRAFT_PREVIEW_STYLE_FIELDS),
        text,
    )


def _draft_preview_fallback_params() -> VideoParams:
    """没有表单快照时，按**已保存的界面设置**造一份预览参数。

    快照只有生成页会写，而左侧导航把工具台排在最前面，所以"一次都没进过生成页"
    是正常路径。这时预览必须照样出得来，只是退化到用户保存过的那份设置。

    不能直接用 ``VideoParams()`` 的字段默认值：``subtitle_enabled`` 之类的默认值
    写死在 schema 里（``subtitle_enabled`` 默认 True），不读 ``config.ui``。那样
    就会在一个关着字幕的账号上宣称字幕是开着的。所以这里逐个字段走控件自己用的
    那套读取器（:func:`_saved_ui_bool` 等），保证预览与生成页看到的是同一份值。
    """
    params = VideoParams(video_subject="")
    defaults = DEFAULT_SUBTITLE_SETTINGS

    params.subtitle_enabled = _saved_ui_bool(
        "subtitle_enabled", defaults["subtitle_enabled"]
    )
    params.subtitle_position = _saved_ui_choice(
        "subtitle_position",
        ("top", "center", "bottom", "custom"),
        defaults["subtitle_position"],
    )
    params.font_name = _saved_ui_text(
        "font_name", defaults["font_name"], max_length=200
    )
    params.font_size = int(
        _saved_ui_number("font_size", defaults["font_size"], 8, 200, int)
    )
    params.custom_position = float(
        _saved_ui_number("custom_position", defaults["custom_position"], 0.0, 100.0)
    )
    params.text_fore_color = _saved_ui_color(
        "text_fore_color", defaults["text_fore_color"]
    )
    params.stroke_color = _saved_ui_color("stroke_color", defaults["stroke_color"])
    params.stroke_width = float(
        _saved_ui_number("stroke_width", defaults["stroke_width"], 0.0, 10.0)
    )
    # 字幕背景在 schema 里只有一个 text_background_color 字段，形状是"关=False /
    # 开=颜色串"（兼容历史布尔值，见 subtitle_preview._resolve_background_color）。
    # 颜色和圆角各自的开关/取值仍存在 UI 配置里，所以按控件的同一套读法取回来，
    # 再合成这一个字段。
    background_enabled = _saved_ui_bool(
        "subtitle_background_enabled", defaults["subtitle_background_enabled"]
    )
    background_color = _saved_ui_color(
        "subtitle_background_color", defaults["subtitle_background_color"]
    )
    params.text_background_color = background_color if background_enabled else False
    params.rounded_subtitle_background = _saved_ui_bool(
        "rounded_subtitle_background", defaults["rounded_subtitle_background"]
    )
    params.caption_word_highlight = _saved_ui_bool(
        "caption_word_highlight", defaults["caption_word_highlight"]
    )
    # 宽高比由频道的 automation.env 在调用方覆盖，这里只兜住没有 env 的频道。
    # video_aspect 在 schema 里是普通字符串（VideoAspect 是 str 枚举，但赋值后
    # 不会自动变成枚举实例），所以默认值取枚举的 value 而不是 params 上的属性。
    params.video_aspect = _saved_ui_choice(
        "video_aspect",
        tuple(item.value for item in VideoAspect),
        VideoAspect.portrait.value,
    )
    return params


def _render_draft_preview(container, channel: str, topic: str | None) -> None:
    """把 9:16 草稿预览画进 ``container``。

    预览读取生成页写下的表单快照，再套上该频道的 automation.env 覆盖——也就是
    **这条频道真正会出片的那份参数**。只显示表单值会漏掉 env 里的品牌设置，而那
    恰恰是最需要肉眼确认的一部分。

    为什么读快照不会"慢一轮"：计划与生成现在是两个独立页面，控件状态存在全局
    session_state 里。用户在生成页改完设置的那一次 rerun 已经把快照写好了，切到
    计划页读到的正是他眼前这份设置。之前慢一轮是因为两个面板在**同一个页面**上
    且计划面板排在表单之前——分页之后这个顺序问题不复存在。

    渲染走的是出片同一段排版代码（见 app/services/subtitle_preview.py），所以字号、
    颜色、描边、位置在这里是什么样，出片就是什么样。
    """
    text = str(topic or "").strip()
    if not text:
        return

    params = st.session_state.get("form_params_snapshot")
    if params is None:
        # 甩一句"请先去生成页设置字幕"是不对的：用户并没有做错任何事，那句话只是
        # 把界面自己的导航顺序说成了用户的操作失误。_build_plan_params 处理同一个
        # 场景时早就是这个做法。
        params = _draft_preview_fallback_params()

    # 深拷贝后再套 env 覆盖：直接改快照会让"手动生成"与"计划生成"共享对象。
    preview_params = params.model_copy(deep=True)
    preview_params.video_subject = text
    _apply_plan_env(preview_params, plans.read_automation_env(channel))

    # 字幕关闭时 render_subtitle_draft_png 会给一张纯净底板（这是它刻意的诚实：
    # 不谎报成片里有文字）。但这一页的预览是**字幕样张**，字号、位置、描边、
    # 配色全在那四个控件里——对着纯黑板调这些参数，用户看不到任何反馈，只会觉得
    # 设置没生效。所以这里强制把样张画出来，再用一行说明告诉用户成片里到底有没有
    # 字幕。既看得见样式，又不谎报成片内容。
    subtitles_enabled = bool(preview_params.subtitle_enabled)
    preview_params.subtitle_enabled = True

    key = _draft_preview_cache_key(preview_params, text)
    cached = st.session_state.get("draft_preview_cache")
    if not cached or cached[0] != key:
        try:
            png = subtitle_preview_service.render_subtitle_draft_png(
                preview_params, text
            )
        except Exception as exc:  # noqa: BLE001
            # 预览失败不能挡住生成。字体缺失、参数异常都只该让预览退场。
            logger.warning(f"draft preview unavailable: {type(exc).__name__}: {exc}")
            st.session_state.pop("draft_preview_cache", None)
            with container:
                st.caption(tr("Draft Preview Unavailable"))
            return
        st.session_state["draft_preview_cache"] = (key, png)
        cached = st.session_state["draft_preview_cache"]

    with container:
        st.image(
            cached[1],
            # 竖版预览必须限宽：按容器整宽铺开的话，一张 9:16 会高到把整页顶出视口。
            # 300px 左右读起来才像一块手机屏。
            width=300,
        )
        st.caption(tr("Draft Preview Note"))
        if not subtitles_enabled:
            st.caption(tr("Draft Preview Subtitles Off"))


_PIPELINE_SCHEDULE_BASE_LABEL = "com.moneyprinterturbo.daily-shorts"


def _expected_schedule_labels() -> list:
    """期望存在的定时任务：基础任务 + 其余每个频道一个。"""
    labels = [_PIPELINE_SCHEDULE_BASE_LABEL]
    try:
        channels = plans.list_channels()
    except Exception:  # noqa: BLE001 - 读不到频道列表就只查基础任务
        return labels
    for channel in channels or []:
        if channel and channel != "history-mysteries":
            labels.append(f"{_PIPELINE_SCHEDULE_BASE_LABEL}.{channel}")
    return labels


def _pipeline_schedule_counts():
    """返回 ``(已加载数, 应有数)``；查不到返回 ``None``。只读，不启停任何任务。"""
    if not sys.platform.startswith("darwin"):
        return None
    try:
        labels = _expected_schedule_labels()
        ready = 0
        for label in labels:
            proc = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ready += proc.returncode == 0
        return ready, len(labels)
    except Exception as exc:  # noqa: BLE001 - 查不到就报未知，不挡计划页
        logger.debug(f"scheduler state check failed: {exc}")
        return None


def _pipeline_scheduler_state() -> str:
    """launchd 定时任务状态：loaded / partial / not_loaded / unknown。"""
    counts = _pipeline_schedule_counts()
    if counts is None:
        return "unknown"
    ready, total = counts
    if ready >= total:
        return "loaded"
    if ready <= 0:
        return "not_loaded"
    return "partial"


def _parse_schedule_run_outcome(tail_lines) -> bool | None:
    """从日志尾部判断上次定时运行的成败；看不出返回 ``None``。纯函数。"""
    for line in reversed(list(tail_lines or [])):
        text = str(line)
        if "SUCCESS on attempt" in text:
            return True
        if "attempts failed" in text:
            return False
    return None


def _pipeline_last_run(channel: str):
    """该频道最近一次定时运行的 ``{mtime, ok, name}``；没有返回 ``None``。只读。"""
    log_dir = Path(root_dir).parent / "logs"
    try:
        candidates = sorted(log_dir.glob(f"daily_{channel}_*.log"))
    except Exception:  # noqa: BLE001
        return None
    if not candidates:
        return None
    latest = candidates[-1]
    try:
        mtime = latest.stat().st_mtime
        with open(latest, encoding="utf-8", errors="replace") as handle:
            tail = handle.readlines()[-40:]
    except OSError:
        return None
    return {"mtime": mtime, "ok": _parse_schedule_run_outcome(tail), "name": latest.name}


@st.cache_data(ttl=300, show_spinner=False)
def _cached_youtube_channel_info(channel: str) -> dict:
    """5 分钟缓存的频道连接状态。未配置时直接返回，不产生任何网络请求。"""
    try:
        info = youtube_upload.channel_info(channel)
        return dict(info) if isinstance(info, Mapping) else {"success": False}
    except Exception as exc:  # noqa: BLE001 - 状态查询失败按"未连接"显示
        return {"success": False, "error": str(exc)[:200]}


def _render_pipeline_health_strip(channel: str):
    """计划页顶部的自动化健康条。

    只读：只查 launchd 状态、读日志尾部、读发布连接状态。绝不提交任务、
    绝不启停调度、绝不发布——这一段的存在意义是让"自动化悄悄停了"这种事
    在界面上可见，而不是再去制造一次意外触发。
    """
    try:
        counts = _pipeline_schedule_counts()
        last = _pipeline_last_run(channel)
        publish = _cached_youtube_channel_info(channel)
    except Exception as exc:  # noqa: BLE001 - 健康条永远不能挡住计划页
        logger.debug(f"pipeline health strip skipped: {exc}")
        return

    with st.container(key="pipeline_health", border=True):
        st.markdown(f"**{tr('Pipeline Health')}**")
        cols = st.columns(3)
        with cols[0]:
            if counts is None:
                st.caption(tr("Pipeline Scheduler Unknown"))
            else:
                ready, total = counts
                if ready >= total:
                    st.success(tr("Pipeline Scheduler Running"))
                elif ready <= 0:
                    st.warning(tr("Pipeline Scheduler Stopped"))
                else:
                    st.warning(
                        tr("Pipeline Scheduler Partial").format(
                            ready=ready, total=total
                        )
                    )
        with cols[1]:
            if not last:
                st.caption(tr("Pipeline Last Run Never"))
            else:
                when = datetime.fromtimestamp(last["mtime"]).strftime(
                    "%Y-%m-%d %H:%M"
                )
                if last["ok"] is True:
                    st.success(
                        tr("Pipeline Last Run").format(
                            when=when, result=tr("Pipeline Run Ok")
                        )
                    )
                elif last["ok"] is False:
                    st.error(
                        tr("Pipeline Last Run").format(
                            when=when, result=tr("Pipeline Run Failed")
                        )
                    )
                else:
                    st.caption(
                        tr("Pipeline Last Run").format(
                            when=when, result=tr("Pipeline Run Unknown")
                        )
                    )
        with cols[2]:
            if publish.get("success"):
                st.success(
                    tr("Pipeline Publish Connected").format(
                        name=html.escape(
                            str(publish.get("channel_name") or channel)
                        )
                    )
                )
            else:
                st.warning(tr("Pipeline Publish Missing"))


def _render_youtube_connect_button(channel: str, *, button_key: str, result_key: str):
    """一块"连接 YouTube 频道"按钮：先查状态，已连直接记下；否则走一次 OAuth。

    计划页与登录屏共用这一份流程，行为完全一致（先 `--youtube-status`，
    失败才 `--youtube-authorize`，成功后重查状态）。调用方只决定按钮放在哪、
    用什么 key 隔离各自的会话结果。
    """
    if st.button(
        tr("Channel Sign In"),
        width='stretch',
        disabled=bool(
            (st.session_state.get(result_key) or {}).get("logged_in")
        ),
        key=button_key,
        help=tr("Channel Connect Help"),
    ):
        # 一次性 OAuth：浏览器只会在这里出现一次。授权后 token 绑定该频道，
        # 之后所有发布都走 API，不占用交互会话。
        check = _run_youtube_cli(
            ["--youtube-status", "--channel", channel], timeout=180
        )
        if check.get("success"):
            st.session_state[result_key] = {
                "success": True,
                "logged_in": True,
                **check,
            }
            st.rerun()
        else:
            authorization = _run_youtube_cli(
                ["--youtube-authorize", "--channel", channel], timeout=240
            )
            if authorization.get("success"):
                info = _run_youtube_cli(
                    ["--youtube-status", "--channel", channel], timeout=180
                )
                st.session_state[result_key] = {
                    "logged_in": bool(info.get("success")), **info,
                }
                st.rerun()
            else:
                # 授权失败以前只显示一行原始错误，用户得自己把
                # redirect_uri_mismatch 这种词翻译成"客户端类型建错了"。这里把
                # 服务端给出的原因和下一步直接摆出来。
                if authorization.get("error_fix"):
                    st.error(authorization.get("error_hint") or tr("Channel Login Failed"))
                    st.info(authorization["error_fix"])
                else:
                    st.error(authorization.get("error") or tr("Channel Login Failed"))


def _remembered_youtube_login(channel: str, *, result_key: str):
    """本机已存 token 即视为已记住：打开应用直接显示已连接，不用再点一次。

    会话里已有结果（刚点完按钮）时以会话为准；否则读一次带缓存的连接状态。
    token 本来就落在 ``storage/credentials/youtube/`` 并保持 0600 权限，
    这里只是把它显示出来，没有复制或移动任何凭据。
    """
    login = st.session_state.get(result_key)
    if login:
        return login
    info = _cached_youtube_channel_info(channel)
    if info.get("success"):
        return {"logged_in": True, **info}
    return None


def _render_youtube_login_detail(channel: str, *, result_key: str):
    """已连接明细 / 未连接提示。`None`（从未点过且本机无 token）时不画。"""
    login = _remembered_youtube_login(channel, result_key=result_key)
    if not login:
        return
    if login.get("logged_in"):
        # 频道名来自 Google API，与用户输入一样不可信：不转义的话名字里的
        # 下划线会被 Markdown 渲染成斜体，名字本身就显示错了。
        detail = html.escape(str(login.get("channel_name") or tr("Channel Logged In")))
        channel_id = html.escape(str(login.get("channel_id") or ""))
        st.caption(f"✅ {detail}" + (f" ({channel_id})" if channel_id else ""))
        if login.get("channel_mismatch"):
            st.warning(tr("Channel Mismatch Warning"))
    else:
        st.caption(f"⚠️ {tr('Channel Not Logged In')}")


def _render_plan_panel():
    """渲染计划面板：频道、待办主题、覆盖摘要、草稿预览与本月记录。"""
    channels = [c for c in plans.list_channels() if c]
    if not channels:
        return None
    default_channel = (
        "history-mysteries" if "history-mysteries" in channels else channels[0]
    )
    channel = st.selectbox(
        tr("Plan Channel"),
        channels,
        index=channels.index(default_channel),
        key="plan_channel_select",
    )

    _render_pipeline_health_strip(channel)

    pending = plans.first_pending_topic(channel)
    env = plans.read_automation_env(channel)

    if pending:
        index, topic = pending
        st.markdown(
            f"**{tr('Today Topic')}** #{index}: {topic}"
        )
    else:
        # 这个状态现在每个月都会真正到达：30 条主题一旦全部录过就不会再被给出，
        # 所以文案要说清下一步做什么，而不是只说"都录完了"。旧的"退回第一条待办"
        # 行为会让按钮每个月重新烧一次已做过的主题，渲完再被归档丢弃。
        st.info(tr("All Topics Recorded").format(channel=channel))
        st.caption(tr("All Topics Recorded Next Step").format(channel=channel))

    action_cols = st.columns([1, 1, 1], vertical_alignment="center")
    _render_effective_parameters_readout(channel, env)
    with action_cols[0]:
        if st.button(
            tr("Generate Today Video"),
            width='stretch',
            type="primary",
            disabled=not pending,
            key="plan_generate_today_button",
        ):
            _submit_plan_generation(channel, pending[1])

    with action_cols[1]:
        # 这里曾经有一个「发布最新视频」按钮，已删除。
        #
        # 它的取值是"**任意**任务目录里最新的那个成片"（_latest_generated_video
        # 不区分频道），发布目标却是上面选中的那个频道账号。于是在多频道工作流
        # 里点一下，就可能把 money-finance 的片子发到 history-mysteries 的频道上
        # ——而 youtube_uploader 把可见性硬编码为 public，发出去即刻公开，无法撤回。
        #
        # 它还固定传 index=None，于是月度计划的那一行永远不会被标记为已发布，
        # "生成今日视频"会反复给同一个主题。
        #
        # 跨频道发错的风险现在由 youtube_upload 兜底：token 绑定的频道与
        # youtube_upload_channel_id 不一致时发布会直接失败，不会发出去了。
        #
        # 正确入口是媒体库里每条视频自己的「发布」按钮：那个按钮从所属频道的
        # 归档记录取来源，并且总是带着 plan_dir，所以计划行会被正确标记。
        st.caption(
            tr("Publish Videos From The Library").format(channel=channel)
        )

    with action_cols[2]:
        _render_youtube_connect_button(
            channel,
            button_key="plan_channel_login_button",
            result_key=f"plan_login_result_{channel}",
        )

    _render_youtube_login_detail(channel, result_key=f"plan_login_result_{channel}")

    # 预览与计划表并排：9:16 竖图如果按整幅宽度铺开，会高到把整页内容顶出视口。
    # 收在左侧窄列里，它才读起来像一块手机屏，而不是一张海报。
    preview_col, rows_col = st.columns([1.05, 3.6], vertical_alignment="top")

    with preview_col:
        _render_draft_preview(preview_col, channel, pending[1] if pending else None)

    with rows_col:
        plan_rows = plans.load_monthly_plan(channel)
        if plan_rows:
            recorded = [row for row in plan_rows if row["recorded"]]
            if recorded:
                _render_plan_rows_table(channel, recorded)
            else:
                st.caption(tr("No Recorded Topics Yet"))


# 计划表的列宽。表头与每一行必须用同一套，否则标题和数据会错位——
# 而且错位在有缩略图的时候特别难看，因为缩略图那一列宽度是固定的。
_PLAN_ROW_COLUMNS = [0.62, 2.35, 1.15, 1.15, 1.05]


def _render_plan_row_header():
    """计划表的表头。

    之前这一列只有一个"🔗 / —"，没有任何说明。那个破折号到底是"没发布"还是
    "文件丢了"，光看符号分不出来；而这一列又是判断哪几条能直接发布的唯一依据。
    """
    header = st.columns(_PLAN_ROW_COLUMNS, vertical_alignment="center")
    # 这里不能用 tr("Index")：它在英文里是 "#"，而 st.caption 走 Markdown 渲染，
    # 孤零零一个 "#" 会被当成一级标题，渲染成一片空白。表头因此少了最左边一列。
    header[0].caption(tr("Plan Number"))
    header[1].caption(tr("Topic"))
    header[2].caption(tr("Status"))
    header[3].caption(tr("Thumbnail"))
    header[4].caption(tr("Task Actions"))


def _render_plan_poster(container, video_path: str, topic_key: str):
    """在计划行里画一张封面缩略图，没有封面时给一块说明性的占位。

    缩略图是这一列存在的理由：只看主题文字判断不出成片长什么样，而计划面板本来就是
    用来快速扫一眼"这个月做了什么"的。
    """
    if not video_path:
        container.markdown(
            f"<div class='mpt-thumb mpt-thumb--empty' "
            f"title='{html.escape(tr('Thumbnail No Preview Yet'))}'></div>",
            unsafe_allow_html=True,
        )
        return

    poster = video_poster_service.poster_frame_path(video_path)
    if poster is None:
        container.markdown(
            f"<div class='mpt-thumb mpt-thumb--empty' "
            f"title='{html.escape(tr('Thumbnail No Preview Yet'))}'></div>",
            unsafe_allow_html=True,
        )
        return
    # 必须套一层带 key 的容器：st.image 输出的是 <img>，直接给 img 限高只能靠
    # 选择器命中容器。Streamlit 会把 st-key-<key> 加到容器 class 上，样式就挂在那里。
    with container.container(key=f"plan_thumb_{topic_key}"):
        st.image(str(poster), width="stretch")


def _render_plan_rows_table(channel: str, rows: list[dict]):
    st.caption(tr("Recorded Topics"))
    with st.container(border=True, key="plan_rows_container"):
        _render_plan_row_header()
        for row in rows:
            video_path = plans.find_video_path(channel, row["index"])
            post_disabled = bool(row["posted"]) or not video_path
            cols = st.columns(_PLAN_ROW_COLUMNS, vertical_alignment="center")
            cols[0].write(f"#{row['index']}")
            cols[1].write(row["topic"])
            # 用带文字的状态徽章替掉裸露的符号，并把"已发布"直接链出去。
            if row["posted"]:
                cols[2].markdown(
                    f"<a class='mpt-status mpt-status--posted' "
                    f"href='{html.escape(str(row['posted']))}' "
                    f"target='_blank' rel='noopener'>{html.escape(tr('Posted To YouTube'))}</a>",
                    unsafe_allow_html=True,
                )
            else:
                cols[2].markdown(
                    f"<span class='mpt-status mpt-status--draft'>"
                    f"{html.escape(tr('Not Posted'))}</span>",
                    unsafe_allow_html=True,
                )
            _render_plan_poster(
                cols[3], video_path, f"{channel}_{row['index']}"
            )
            with cols[4]:
                _render_publish_button(
                    key=publish_jobs.job_key("video", channel, video_path),
                    label=tr("Post Video"),
                    disabled=post_disabled,
                    button_key=f"plan_post_row_{channel}_{row['index']}",
                    work=lambda channel=channel, video_path=video_path, row=row:
                    _publish_via_api(channel, video_path, row["topic"], index=row["index"]),
                )



def _media_library_entries(channel):
    entries = plans.list_channel_videos(channel)
    row_index = plans.build_video_row_index(channel)
    for entry in entries:
        match = row_index.get(entry["name"])
        if match:
            plan_dir, row = match
            entry["topic"] = (row["topic"] or entry["topic"]).strip() or entry["name"]
            entry["plan_index"] = row["index"]
            entry["plan_dir"] = plan_dir
            entry["posted"] = bool(row.get("posted"))
            entry["video_url"] = row.get("posted") or ""
        else:
            entry["topic"] = entry["topic"].strip() or entry["name"]
            entry["plan_index"] = None
            entry["plan_dir"] = None
            entry["posted"] = False
            entry["video_url"] = ""
    return entries


# Cap the per-channel dataframe at the newest N rows so the library stays
# responsive once a channel has archived thousands of videos.
_MEDIA_LIBRARY_TABLE_LIMIT = 200


def _render_media_library():
    channels = [c for c in plans.list_channels() if c]
    if not channels:
        return
    tabs = st.tabs([f"{channel}" for channel in channels])
    for tab, channel in zip(tabs, channels):
        with tab:
            entries = _media_library_entries(channel)
            posted_count = sum(1 for entry in entries if entry["posted"])
            metric_cols = st.columns(3, gap="small")
            metric_cols[0].metric(tr("Videos Built"), len(entries))
            metric_cols[1].metric(tr("Posted To YouTube"), posted_count)
            metric_cols[2].metric(tr("Not Posted"), len(entries) - posted_count)
            if not entries:
                st.markdown(
                    f"<p class='mpt-empty-state'>{tr('No Generated Videos Yet')}</p>",
                    unsafe_allow_html=True,
                )
                continue
            preview_names = [
                f"#{str(entry.get('plan_index') or '—')}  ·  {entry['date']}  ·  {entry['name']}"
                for entry in entries
            ]
            pick_name = st.selectbox(
                tr("Preview Video"),
                preview_names,
                key=f"media_library_preview_{channel}",
            )
            pick_index = preview_names.index(pick_name)
            entry = entries[pick_index]
            local = entry["path"]
            readable = _probe_video_dimensions(local) is not None
            if st.checkbox(
                tr("Show Preview"),
                value=False,
                key=f"media_library_show_video_{channel}",
                disabled=not readable,
                help=(
                    tr("Video File Unreadable").format(name=os.path.basename(local))
                    if not readable
                    else None
                ),
            ):
                _render_video_player(local)
            badge = (
                "<span class='mpt-badge mpt-badge--posted'>● Posted</span>"
                if entry["posted"]
                else "<span class='mpt-badge mpt-badge--draft'>● Draft</span>"
            )
            if not readable:
                badge = (
                    "<span class='mpt-badge mpt-badge--broken'>● Unreadable</span>"
                )
            st.markdown(
                f"<p class='mpt-media-caption'>{badge} "
                f"{html.escape(entry['topic'])}</p>",
                unsafe_allow_html=True,
            )
            _render_posting_copy(local, entry["topic"], channel, f"library_{channel}_{pick_index}")
            if not youtube_upload.is_configured(channel):
                st.caption(tr("YouTube Connection Required"))
            action_cols = st.columns([1, 1, 3], gap="small")
            with action_cols[0]:
                _render_publish_button(
                    key=publish_jobs.job_key("video", channel, local),
                    label=tr("Post Video"),
                    button_key=f"media_library_post_{channel}_{pick_index}",
                    disabled=entry["posted"] or not readable,
                    primary=True,
                    work=lambda channel=channel, local=local, entry=entry:
                    _publish_via_api(
                        channel,
                        local,
                        entry["topic"],
                        index=entry["plan_index"],
                        plan_dir=entry["plan_dir"],
                    ),
                )
            with action_cols[1]:
                if st.button(
                    tr("Open Folder"),
                    icon=":material/folder_open:",
                    width='stretch',
                    help=tr("Open Archive Folder"),
                    key=f"media_library_open_{channel}_{pick_index}",
                ):
                    # 媒体库里的成片本身就位于归档目录，因此这里与任务面板的
                    # 「打开归档目录」指向同一棵树，两个面板不再各开各的。
                    _reveal_in_file_manager(
                        local,
                        plans.OUTPUTS_DIR,
                        tr("Open Archive Folder"),
                        _archive_display_base(),
                    )
            st.divider()
            library_rows = []
            shown_entries = entries[:_MEDIA_LIBRARY_TABLE_LIMIT]
            for entry in shown_entries:
                library_rows.append(
                    {
                        tr("Index"): entry["plan_index"] or "—",
                        tr("Date"): entry["date"],
                        tr("Topic"): entry["topic"],
                        tr("Status"): tr("Posted") if entry["posted"] else tr("Draft"),
                    }
                )
            if len(entries) > len(shown_entries):
                st.caption(
                    f"Showing newest {len(shown_entries)} of {len(entries)} videos"
                )
            st.dataframe(
                library_rows,
                width='stretch',
                hide_index=True,
                key=f"media_library_table_{channel}",
            )


def _as_campaign_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _render_campaign_panel():
    """活动内容工作台：从链接/文本理解活动简报，生成可直接发布的文案包。"""
    with st.container(border=True):
        st.markdown(
            f"<p class='mpt-explain'>{html.escape(tr('Campaign Content Help'))}</p>",
            unsafe_allow_html=True,
        )
        saved_campaigns = campaign_service.list_saved_campaigns()
        if saved_campaigns:
            saved_options = campaign_service.saved_campaign_options(saved_campaigns)
            with st.container(key="campaign_saved_loader"):
                loader_cols = st.columns([3, 1], gap="small", vertical_alignment="center")
                with loader_cols[0]:
                    saved_label = st.selectbox(
                        tr("Saved Campaigns"),
                        list(saved_options),
                        key="campaign_saved_select",
                    )
                with loader_cols[1]:
                    if st.button(
                        tr("Load Saved Campaign"),
                        width='stretch',
                        key="campaign_load_saved_button",
                    ):
                        try:
                            loaded = campaign_service.load_saved_campaign(
                                saved_options[saved_label]
                            )
                        except Exception as exc:
                            st.error(f"{tr('Campaign Load Failed')}: {exc}")
                        else:
                            st.session_state["campaign_source"] = loaded["source"].to_dict()
                            st.session_state["campaign_brief"] = loaded["brief"]
                            st.session_state["campaign_posts"] = loaded["posts"]
                            st.toast(tr("Campaign Loaded"))
                            st.rerun()
            st.divider()
        with st.container(key="campaign_input_container"):
            campaign_link = st.text_input(
                tr("Campaign Link Or Text"),
                placeholder=tr("Campaign Link Placeholder"),
                key="campaign_link_input",
            )

        fetch_cols = st.columns([1, 1], gap="small", vertical_alignment="center")
        with fetch_cols[0]:
            if st.button(
                tr("Fetch And Understand"),
                type="primary",
                width='stretch',
                disabled=not (campaign_link or "").strip(),
                key="campaign_fetch_button",
            ):
                raw = (campaign_link or "").strip()
                try:
                    with st.spinner(tr("Fetching Campaign Source")):
                        source = campaign_service.fetch_campaign_source(raw)
                    if not source.text and not source.refs:
                        st.session_state["campaign_source"] = None
                        st.session_state["campaign_brief"] = None
                        st.session_state["campaign_posts"] = None
                        st.warning(tr("Campaign Source Empty"))
                    else:
                        st.session_state["campaign_source"] = source.to_dict()
                        st.session_state["campaign_brief"] = None
                        st.session_state["campaign_posts"] = None
                        st.toast(tr("Campaign Source Fetched"))
                        st.rerun()
                except Exception as exc:
                    st.session_state["campaign_source"] = None
                    st.session_state["campaign_brief"] = None
                    st.session_state["campaign_posts"] = None
                    st.error(f"{tr('Campaign Fetch Failed')}: {exc}")

        source = st.session_state.get("campaign_source")
        if not source:
            st.markdown(
                f"<p class='mpt-empty-state'>{html.escape(tr('No Campaign Source Yet'))}</p>",
                unsafe_allow_html=True,
            )
            return

        st.markdown(f"**{tr('Campaign Source')}**")
        kind_labels = {
            "notion": tr("Kind Notion"),
            "drive_file": tr("Kind Drive File"),
            "drive_folder": tr("Kind Drive Folder"),
            "docs": tr("Kind Drive Docs"),
            "url": tr("Kind Url"),
            "text": tr("Kind Text"),
        }
        kind_label = kind_labels.get(source.get("kind")) or tr("Campaign Source")
        st.caption(
            f"{kind_label}  ·  {source.get('title') or source.get('url') or '(pasted text)'}"
        )
        with st.expander(tr("Campaign Source Text"), expanded=False):
            st.code(source.get("text") or tr("Campaign Source Empty"), language=None, height=400)
        if source.get("refs"):
            with st.expander(tr("Campaign Media References"), expanded=False):
                for ref in source.get("refs", []):
                    st.markdown(f"- {ref.get('title') or ref.get('url')}")
                    if ref.get("url"):
                        st.caption(ref["url"])

        with fetch_cols[1]:
            if st.button(
                tr("Build Campaign Brief"),
                width='stretch',
                key="campaign_brief_button",
            ):
                try:
                    with st.spinner(tr("Building Campaign Brief")):
                        brief = campaign_service.understand_campaign(
                            campaign_service.CampaignSource.from_dict(source)
                        )
                    st.session_state["campaign_brief"] = brief
                    st.session_state["campaign_posts"] = None
                    st.toast(tr("Campaign Brief Built"))
                    st.rerun()
                except Exception as exc:
                    st.error(f"{tr('Campaign Brief Failed')}: {exc}")

        brief = st.session_state.get("campaign_brief")
        if brief:
            st.divider()
            st.markdown(f"**{tr('Campaign Brief')}**")
            st.markdown(f"- **{tr('Campaign Name')}**: {brief.get('campaign_name') or '—'}")
            st.markdown(f"- **{tr('Brand')}**: {brief.get('brand') or '—'}")
            st.markdown(f"- **{tr('Goal')}**: {brief.get('goal') or '—'}")
            st.markdown(f"- **{tr('Audience')}**: {brief.get('audience') or '—'}")
            st.markdown(f"- **{tr('Tone')}**: {brief.get('tone') or '—'}")
            platforms = ", ".join(_as_campaign_list(brief.get("platforms")))
            st.markdown(f"- **{tr('Platforms')}**: {platforms or '—'}")
            hashtags = ", ".join(_as_campaign_list(brief.get("hashtags")))
            hashtag_line = f"#{hashtags.replace(', ', ' #')}" if hashtags else "—"
            st.markdown(f"- **{tr('Hashtags')}**: {hashtag_line}")
            requirements = _as_campaign_list(brief.get("requirements"))
            if requirements:
                st.markdown(f"**{tr('Requirements')}**")
                for req in requirements:
                    st.markdown(f"- {req}")
            source_materials = _as_campaign_list(brief.get("source_materials"))
            if source_materials:
                st.markdown(f"**{tr('Source Materials')}**")
                for url in source_materials:
                    st.markdown(f"- {url}")

            gen_cols = st.columns([1, 1], gap="small", vertical_alignment="center")
            with gen_cols[0]:
                post_count = st.number_input(
                    tr("Campaign Posts Count"),
                    min_value=1,
                    max_value=20,
                    value=int(st.session_state.get("campaign_post_count", 5)),
                    key="campaign_post_count_input",
                )
                st.session_state["campaign_post_count"] = int(post_count)
            with gen_cols[1]:
                if st.button(
                    tr("Generate Campaign Posts"),
                    type="primary",
                    width='stretch',
                    key="campaign_generate_button",
                ):
                    try:
                        with st.spinner(tr("Generating Campaign Posts")):
                            posts = campaign_service.generate_campaign_posts(
                                brief, count=int(post_count)
                            )
                        st.session_state["campaign_posts"] = posts
                        st.toast(tr("Campaign Posts Generated"))
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{tr('Campaign Generate Failed')}: {exc}")

            posts = st.session_state.get("campaign_posts")
            if posts:
                st.divider()
                st.markdown(f"**{tr('Content Pieces')}**")
                for index, post in enumerate(posts, start=1):
                    with st.expander(
                        f"#{index}  {post.get('title') or tr('Untitled Post')}",
                        expanded=index == 1,
                    ):
                        st.markdown(f"**{tr('Hook')}**: {post.get('hook') or '—'}")
                        st.markdown(f"**{tr('Script')}**")
                        st.code(post.get("script") or "", language=None)
                        on_screen = post.get("on_screen_text") or ""
                        st.markdown(
                            f"**{tr('On Screen Text')}**: {on_screen or '—'}"
                        )
                        st.markdown(f"**{tr('CTA')}**: {post.get('cta') or '—'}")
                        st.markdown(f"**{tr('Hashtags')}**: {' '.join('#' + h for h in _as_campaign_list(post.get('hashtags'))) or '—'}")
                        st.markdown(f"**{tr('Ready To Post')}**")
                        st.code(post.get("ready_to_post") or "", language="markdown")
                        if st.button(
                            tr("Use Title As Video Subject"),
                            width='stretch',
                            key=f"campaign_use_subject_{index}",
                        ):
                            st.session_state["video_subject"] = post.get("title") or ""
                            st.session_state["video_script"] = post.get("script") or ""
                            # 单击语义：把活动标题文案载入生成表单，并立即触发视频生成。
                            # 生成控制面板在**生成视图**上，而活动面板在另一个视图，
                            # 因此这里同时切过去：否则标志被设下了却没人消费，用户点了
                            # 按钮却什么也没发生。切过去之后，生成控件在同一轮里读到
                            # 这个标志，沿用与手动点击"生成视频"完全一致的提交流程。
                            st.session_state["campaign_auto_generate"] = True
                            st.toast(tr("Campaign Subject Loaded"))
                            # 必须再跑一轮：上面那个"有待生成就切到生成视图"的判断
                            # 在本次执行的更早处已经过去了，此时标志才被写进去。
                            # 不显式 rerun 的话，本次执行渲染的还是活动视图，
                            # 标志留到下一次交互才被消费——单击出片就成了两次点击。
                            st.rerun()
                accept_cols = st.columns([1, 1], gap="small", vertical_alignment="center")
                with accept_cols[0]:
                    if st.button(
                        tr("Accept Campaign"),
                        type="primary",
                        width='stretch',
                        key="campaign_accept_button",
                    ):
                        try:
                            saved_meta = campaign_service.save_campaign(
                                campaign_service.CampaignSource.from_dict(source),
                                brief,
                                posts,
                            )
                        except Exception as exc:
                            st.error(f"{tr('Campaign Save Failed')}: {exc}")
                        else:
                            st.session_state["campaign_saved_last"] = saved_meta
                            st.toast(tr("Campaign Accepted"))
                            st.rerun()
                with accept_cols[1]:
                    st.download_button(
                        tr("Download Campaign Markdown"),
                        data=campaign_service.build_campaign_markdown(
                            campaign_service.CampaignSource.from_dict(source),
                            brief,
                            posts,
                        ),
                        file_name="campaign-content.md",
                        mime="text/markdown",
                        width='stretch',
                        key="campaign_download_markdown",
                    )
                saved_last = st.session_state.get("campaign_saved_last")
                if saved_last:
                    st.caption(
                        f"✅ {tr('Campaign Saved As')}: {saved_last.get('campaign_id')}"
                    )
            elif posts is not None:
                st.markdown(
                    f"<p class='mpt-empty-state'>{html.escape(tr('Campaign Posts Empty'))}</p>",
                    unsafe_allow_html=True,
                )


def _page_login():
    """左侧导航「登录」：应用打开即落到这一屏，先连好各频道的 YouTube 账号。

    每个频道连一次即可，token 落在 ``storage/credentials/youtube/`` 并保持
    0600 权限；下次打开直接显示已连接，不需要重复操作。
    """
    with st.container(key="page_login"):
        st.markdown(
            f'<div class="mpt-hero">'
            f'<p class="mpt-hero-badge">{html.escape(tr("Login Hero Badge"))}</p>'
            f'<p class="mpt-hero-title">{html.escape(tr("Login Hero Title"))}</p>'
            f'<p class="mpt-hero-sub">{html.escape(tr("Login Hero Sub"))}</p>'
            f"</div>",
            unsafe_allow_html=True,
        )

        client_id = str(config.app.get("youtube_upload_client_id", "") or "").strip()
        client_secret = str(
            config.app.get("youtube_upload_client_secret", "") or ""
        ).strip()
        if not (client_id and client_secret):
            # 以前这里只有一句提示加一个"去设置页"的按钮，于是"连好频道"被切成
            # 两段：先离开登录页去设置填凭据，再回来逐个频道登录。凭据是登录页
            # 自己的前提，就该在这一屏填完——填好后下方卡片立刻变成可登录。
            st.warning(tr("Login Client Missing"))
            with st.expander(tr("Login Client Setup Title"), expanded=True):
                st.caption(tr("Login Client Setup"))
                st.markdown(
                    f'<ol class="mpt-steps">'
                    f"<li>{html.escape(tr('Login Step Project'))}</li>"
                    f"<li>{html.escape(tr('Login Step EnableApi'))}</li>"
                    f"<li>{html.escape(tr('Login Step Consent'))}</li>"
                    f"<li>{html.escape(tr('Login Step Credentials'))}</li>"
                    f"</ol>",
                    unsafe_allow_html=True,
                )
                st.link_button(
                    tr("Login Open Google Console"),
                    url="https://console.cloud.google.com/apis/credentials",
                    key="login_google_console_link",
                    icon=":material/open_in_new:",
                )
                for field, label_key in (
                    ("youtube_upload_client_id", "YouTube OAuth Client ID"),
                    ("youtube_upload_client_secret", "YouTube OAuth Client Secret"),
                ):
                    current = str(config.app.get(field, "") or "")
                    entered = st.text_input(
                        tr(label_key),
                        value=current,
                        type="password",
                        key=f"login_{field}_input",
                    ).strip()
                    if entered != current:
                        _set_runtime_config("app", field, entered)
                _save_runtime_config()
                client_id = str(
                    config.app.get("youtube_upload_client_id", "") or ""
                ).strip()
                client_secret = str(
                    config.app.get("youtube_upload_client_secret", "") or ""
                ).strip()
                if client_id and client_secret:
                    st.success(tr("Login Client Saved"))

        if client_id and client_secret:
            # Desktop 客户端不需要填回调地址；Web 客户端必须填这一条。把它摆在
            # 屏幕上，用户就不必猜 —— redirect_uri_mismatch 的原因九成是这个。
            st.caption(
                tr("Login Redirect Hint").format(
                    uri=youtube_upload.redirect_uri_hint()
                )
            )

        channels = [c for c in plans.list_channels() if c]
        if not channels:
            st.markdown(
                f"<p class='mpt-empty-state'>{html.escape(tr('Login No Channels'))}</p>",
                unsafe_allow_html=True,
            )
            return

        if not (client_id and client_secret):
            # 凭据还没填时，卡片照样画出来（它们是"每个频道"这个概念本身），
            # 但先说清现在点了不会有反应，免得被当成界面坏了。
            st.caption(tr("Login Sign In Blocked"))

        # Google 把授权错误渲染在**浏览器里**，本地回调服务器看不到，异常只会
        # 以超时形式回到应用。也就是说：应用里写的错误诊断，用户永远读不到。
        # 于是把最常见的两个前置条件写在点击之前——否则用户只能对着 Google 的
        # 错误页猜，而那两句话正是全部原因。
        with st.expander(tr("Login Preflight"), expanded=True):
            st.markdown(
                f'<ul class="mpt-steps">'
                f"<li>{html.escape(tr('Login Preflight TestUsers'))}</li>"
                f"<li>{html.escape(tr('Login Preflight Production'))}</li>"
                f"</ul>",
                unsafe_allow_html=True,
            )

        st.caption(tr("Login Remembered Note"))
        columns = st.columns(3)
        for index, channel in enumerate(channels):
            with columns[index % 3]:
                with st.container(key=f"login_card_{channel}", border=True):
                    st.markdown(f"**{html.escape(channel)}**")
                    info = _cached_youtube_channel_info(channel)
                    if info.get("success"):
                        st.success(
                            tr("Pipeline Publish Connected").format(
                                name=html.escape(
                                    str(info.get("channel_name") or channel)
                                )
                            )
                        )
                        if info.get("channel_mismatch"):
                            st.warning(tr("Channel Mismatch Warning"))
                    elif info.get("needs_reauth"):
                        # 连过、但 token 过期了。这不是"还没连"，是"要重新连"，
                        # 而且多半是同意屏幕停在 Testing 导致的。
                        st.error(tr("Pipeline Publish Expired"))
                    else:
                        st.warning(tr("Pipeline Publish Missing"))
                    _render_youtube_connect_button(
                        channel,
                        button_key=f"login_signin_{channel}",
                        result_key=f"login_result_{channel}",
                    )
                    _render_youtube_login_detail(
                        channel, result_key=f"login_result_{channel}"
                    )


def _page_plan():
    """左侧导航「计划」：今天的待办主题、频道覆盖、草稿预览、本月记录。"""
    with st.container(key="page_plan"):
        _render_plan_panel()


def _page_library():
    """左侧导航「成片库」：按频道列出归档成片，并提供发布入口。"""
    with st.container(key="page_library"):
        _render_media_library()


_COPYRIGHT_VERDICT_BADGES = {
    "low": "mpt-badge--posted",
    "medium": "mpt-badge--warn",
    "high": "mpt-badge--broken",
}

_COPYRIGHT_VERDICT_LABELS = {
    "low": "Copyright Verdict Low",
    "medium": "Copyright Verdict Medium",
    "high": "Copyright Verdict High",
}


def _render_copyright_report(report: dict):
    """审计报告：总 verdict + 逐项来源与风险 + 能力边界声明。"""
    verdict = report.get("verdict", "medium")
    st.markdown(
        f'<p><span class="mpt-badge {html.escape(_COPYRIGHT_VERDICT_BADGES.get(verdict, "mpt-badge--draft"))}">'
        f'{html.escape(tr(_COPYRIGHT_VERDICT_LABELS.get(verdict, "Copyright Verdict Medium")))}'
        f"</span></p>",
        unsafe_allow_html=True,
    )
    for item in report.get("items", []):
        with st.container(key=f"copyright_item_{item.get('kind')}", border=True):
            risk = item.get("risk", "medium")
            st.markdown(
                f'<p class="mpt-media-caption">'
                f'<span class="mpt-badge {html.escape(_COPYRIGHT_VERDICT_BADGES.get(risk, "mpt-badge--draft"))}">'
                f'{html.escape(tr(_COPYRIGHT_VERDICT_LABELS.get(risk, "Copyright Verdict Medium")))}'
                f"</span>"
                f"<span>{html.escape(tr(item.get('title_key', '')))}"
                f" · {html.escape(str(item.get('source_name', '')))}"
                f"</span></p>",
                unsafe_allow_html=True,
            )
            note_template = tr(item.get("note_key", ""))
            try:
                note = note_template.format(name=str(item.get("source_name", "")))
            except (KeyError, IndexError, ValueError):
                note = note_template
            st.caption(note)
    st.caption(tr("Copyright Disclaimer"))


def _page_copyright():
    """左侧导航「版权检查」：对一条成片做素材来源审计，发布前看风险。"""
    with st.container(key="page_copyright"):
        st.markdown(
            f'<div class="mpt-hero">'
            f'<p class="mpt-hero-badge">{html.escape(tr("Copyright Hero Badge"))}</p>'
            f'<p class="mpt-hero-title">{html.escape(tr("Copyright Hero Title"))}</p>'
            f'<p class="mpt-hero-sub">{html.escape(tr("Copyright Hero Sub"))}</p>'
            f"</div>",
            unsafe_allow_html=True,
        )

        candidates = [
            task
            for task in _collect_task_summaries(limit=30)
            if task.get("video_file")
        ]
        if not candidates:
            st.markdown(
                f"<p class='mpt-empty-state'>{html.escape(tr('Copyright No Video'))}</p>",
                unsafe_allow_html=True,
            )
            return

        options = [task["task_id"] for task in candidates]
        by_id = {task["task_id"]: task for task in candidates}
        # 任务可能在别处被删掉（任务管理里删成片是日常操作），而下拉框的上次
        # 选择还留在会话里。带着一个已不存在的 id 进 selectbox，生产环境会
        # 直接整页报错；先把它收敛回首个有效选项。
        saved_pick = st.session_state.get("copyright_task_select")
        if saved_pick is not None and saved_pick not in options:
            st.session_state["copyright_task_select"] = options[0]
        # Triage at a glance: each row carries its risk verdict, so the queue
        # reads worst-first instead of forcing a click per video. Same cost
        # class as the task scans above (one small JSON read per task).
        verdicts = {}
        for task in candidates:
            script_data = _safe_load_task_script(task.get("task_path", ""))
            params = script_data.get("params", {}) if script_data else {}
            verdicts[task["task_id"]] = copyright_audit_service.audit_video_assets(
                params
            )["verdict"]
        selected_id = st.selectbox(
            tr("Copyright Pick Video"),
            options=options,
            format_func=lambda task_id: (
                f"{tr(_COPYRIGHT_VERDICT_LABELS.get(verdicts.get(task_id, 'medium'), 'Copyright Verdict Medium'))}"
                f" — {_format_task_subject(by_id[task_id].get('subject'), max_length=36)}"
            ),
            key="copyright_task_select",
        )
        stored = st.session_state.get("copyright_report") or {}
        if stored.get("task_id") != selected_id:
            stored = {}
        if st.button(
            tr("Copyright Check"),
            key="copyright_check_button",
            type="primary",
            icon=":material/shield:",
        ):
            task = by_id[selected_id]
            script_data = _safe_load_task_script(task.get("task_path", ""))
            params = script_data.get("params", {}) if script_data else {}
            stored = {
                "task_id": selected_id,
                "report": copyright_audit_service.audit_video_assets(params),
            }
            st.session_state["copyright_report"] = stored
        if stored.get("report"):
            _render_copyright_report(stored["report"])


def _page_campaign():
    """左侧导航「活动」：从 brief 生成成套可发布内容。"""
    with st.container(key="page_campaign"):
        _render_campaign_panel()


def _page_settings():
    """左侧导航「设置」：模型、素材源、语音、接口与缓存。

    这一屏是**页面**而不是弹窗——弹窗关掉之后主区域会空着，用户会以为页面坏了。
    顶部栏那个设置按钮保留 Dialog 形态，供"临时改一个 key 就走"的场景使用。
    """
    with st.container(key="page_settings"):
        _render_settings_body()


# ── 工具台 ──────────────────────────────────────────────────────────────
#
# 参考设计是一张卡片网格：每张卡一个能力，配一句"它做什么"和一个入口。我们照它的
# 形制做，但**状态必须是真的**——参考设计里 7 张卡长得一模一样，因为它背后是一个
# 封闭产品；本应用的能力要靠本地模型、免费额度的 key、乃至二进制才可能跑起来，
# 于是"能不能用"是每张卡各自不同的一件事。把它显示出来，比事后让用户在点下去之后
# 才发现强。
#
# 每张卡的三种状态：
#   ready       现在就能用
#   needs_setup 缺凭据/缺前置条件，CTA 变成"去配置"并跳设置页
#   waiting     依赖上一步的产物（缩略图要先有完成的��务），说清还差什么
#
# ``orb`` 字段是这张卡对应的思考动效状态。``app/services/orbs.py`` 早就把业务动作
# 映射好了，其中大半此前没有任何调用点——把它们接回真实的操作，卡片上的图标才不是
# 凭空选的装饰。
_TOOLS_BENCH_TOOLS = [
    {
        "key": "script",
        "title": "Bench Script",
        "desc": "Bench Script Desc",
        "icon": ":material/description:",
        "orb": "composing",
        "ready_when": "llm",
        "target": ("generate", "focus_script"),
        "cta": "Bench Try Now",
    },
    {
        "key": "footage",
        "title": "Bench Footage",
        "desc": "Bench Footage Desc",
        "icon": ":material/perm_media:",
        "orb": "searching",
        "ready_when": "media",
        "target": ("generate", "focus_footage"),
        "cta": "Bench Try Now",
    },
    {
        "key": "voice",
        "title": "Bench Voice",
        "desc": "Bench Voice Desc",
        "icon": ":material/record_voice_over:",
        "orb": "listening",
        "ready_when": "tts",
        "target": ("generate", "focus_voice"),
        "cta": "Bench Try Now",
    },
    {
        "key": "subtitles",
        "title": "Bench Subtitles",
        "desc": "Bench Subtitles Desc",
        "icon": ":material/subtitles:",
        # 字幕排版完全在本地跑，不需要任何 key，因此永远就绪。
        "orb": "working",
        "ready_when": "always",
        "target": ("generate", "focus_subtitles"),
        "cta": "Bench Try Now",
    },
    {
        "key": "assembly",
        "title": "Bench Assembly",
        "desc": "Bench Assembly Desc",
        "icon": ":material/movie_edit:",
        "orb": "working",
        "ready_when": "always",
        "target": ("generate", "focus_generate"),
        "cta": "Bench Try Now",
    },
    {
        "key": "thumbnails",
        "title": "Bench Thumbnails",
        "desc": "Bench Thumbnails Desc",
        "icon": ":material/image:",
        "orb": "shaping",
        "ready_when": "has_task",
        "target": ("generate", "open_tasks"),
        "cta": "Bench Try Now",
    },
    {
        "key": "campaign",
        "title": "Bench Campaign",
        "desc": "Bench Campaign Desc",
        "icon": ":material/campaign:",
        "orb": "composing",
        "ready_when": "llm",
        "target": ("campaign", None),
        "cta": "Bench Try Now",
    },
    {
        "key": "plan",
        "title": "Bench Plan",
        "desc": "Bench Plan Desc",
        "icon": ":material/calendar_month:",
        "orb": "solving",
        "ready_when": "always",
        "target": ("plan", None),
        "cta": "Bench Try Now",
    },
    {
        "key": "library",
        "title": "Bench Library",
        "desc": "Bench Library Desc",
        "icon": ":material/video_library:",
        "orb": "breathing",
        "ready_when": "always",
        "target": ("library", None),
        "cta": "Bench Try Now",
    },
]


# 就绪度判定的唯一口径：**当前选中的那一个**有没有配齐，而不是"有没有配过别的"。
#
# 此前三张卡（脚本、素材、配音）都是"任一凭据即就绪"：配了 Coverr 的 key、素材源
# 选的却是 Pexels，卡片照样显示 Ready，点进去生成时才被提交校验拦下。提交校验
# （_render_generation_controls）本来就是按选中项逐项 gate 的，这里的判定必须与它
# 同构，否则"Ready"就是一句谎言。TTS 的 key 还分散在各自的配置段里
# （[elevenlabs]、[siliconflow]、[fish_audio]……），判定必须走运行时真正读的那些
# 解析函数，而不是猜一个 [app] 段里的同名键。
def _has_cred(*values) -> bool:
    """任一值非空即为已配置。图库 key 可能是字符串或数组，两种都接受。"""
    for value in values:
        if isinstance(value, (list, tuple)):
            if any(str(item or "").strip() for item in value):
                return True
        elif str(value or "").strip():
            return True
    return False


_TTS_SERVER_LABELS = {
    "azure-tts-v1": "Azure TTS V1",
    "azure-tts-v2": "Azure TTS V2",
    "siliconflow": "SiliconFlow TTS",
    "gemini-tts": "Google Gemini TTS",
    "mimo-tts": "Xiaomi MiMo TTS",
    "minimax-tts": "MiniMax TTS",
    "elevenlabs": "ElevenLabs TTS",
    "chatterbox": "Chatterbox TTS",
    "fish_audio": "Fish Audio TTS",
}

_MEDIA_SOURCE_LABELS = {
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "coverr": "Coverr",
    "wavespeed": "WaveSpeed",
    "volcengine_seedance": "Seedance",
    "loomloom": "LoomLoom",
    "local": "Local files",
}


def _selected_llm_detail() -> tuple[bool, str]:
    """所选 LLM Provider 是否可调用。返回 ``(就绪, 提供方 id)``。

    镜像 app/services/llm.py 的准入判定：注册表里找不到、要 key 没 key、必填
    附加字段缺失，运行时都会直接抛错。这里提前说出来，而不是等任务失败。
    模型名与地址都有内置回退（resolve_*），只在回退后仍为空时才算缺失。
    """
    try:
        provider_id = str(
            config.app.get("llm_provider", "") or DEFAULT_LLM_PROVIDER_ID
        ).lower()
        provider = get_llm_provider(provider_id)
        if provider is None:
            return False, provider_id
        if provider_id == "ollama":
            # 与运行时一致：地址按运行环境推导，不需要 key。
            return True, provider_id
        if provider.requires_api_key and not _has_cred(
            config.app.get(provider.config_key("api_key"))
        ):
            return False, provider_id
        model_name = provider.resolve_model_name(
            config.app.get(provider.config_key("model_name"), "")
        )
        if provider.requires_model_name and not model_name:
            return False, provider_id
        base_url = provider.resolve_base_url(
            config.app.get(provider.config_key("base_url"), "")
        )
        if provider.requires_base_url and not base_url:
            return False, provider_id
        for field in provider.extra_fields:
            if field.required:
                value = config.app.get(
                    provider.config_key(field.config_suffix), ""
                ) or field.default_value
                if not str(value or "").strip():
                    return False, provider_id
        return True, provider_id
    except Exception as exc:  # noqa: BLE001 - 判定失败按"未就绪"处理
        logger.debug(f"selected llm readiness check failed: {exc}")
        return False, ""


def _missing_key_message(name: str) -> str:
    """未就绪时的说明：先点名缺的是哪一项，再保留通用引导句。"""
    specific = tr("Bench Needs Key For").format(name=name)
    return f"{specific} {tr('Bench Needs Key')}"


def _selected_media_state() -> tuple[str, str]:
    """所选素材源是否可拉取。返回 ``(状态, 缺什么)``。"""
    source = str(config.app.get("video_source", "") or "pexels")
    label = _MEDIA_SOURCE_LABELS.get(source, source)
    if source == "local":
        # 本地素材在生成页现场选择，工具台上无法"就绪"，也不能叫"待配置"
        # （设置页里没有能填的东西）。waiting 会把用户送去生成页，正好是
        # 选文件的地方。
        return "waiting", tr("Bench Needs Local Files")
    try:
        if source == "loomloom":
            ok = _has_cred(config.app.get("loomloom_api_token"))
        elif source == "wavespeed":
            ok = _has_cred(config.app.get("wavespeed_api_keys"))
        elif source == "volcengine_seedance":
            ok = volcengine_seedance.is_enabled(
                config.snapshot_config_with_pending(config.app)
            )
        elif source in ("pexels", "pixabay", "coverr"):
            ok = _has_cred(
                config.app.get(f"{source}_api_keys"),
                config.app.get(f"{source}_api_key"),
            )
        else:
            # 提交校验会以 "Please Select a Valid Video Source" 拒绝它。
            return "needs_setup", _missing_key_message(label)
    except Exception as exc:  # noqa: BLE001 - 判定失败按"未就绪"处理
        logger.debug(f"selected media readiness check failed: {exc}")
        return "needs_setup", _missing_key_message(label)
    if ok:
        return "ready", ""
    return "needs_setup", _missing_key_message(label)


def _selected_tts_state() -> tuple[str, str]:
    """所选配音链路是否可发声。返回 ``(状态, 缺什么)``。"""
    server = str(config.ui.get("tts_server", "") or "azure-tts-v1")
    mode = config.ui.get("voice_mode")
    if mode not in {VOICE_MODE_TTS, VOICE_MODE_UPLOAD, VOICE_MODE_NONE}:
        # 与音频面板的默认逻辑保持一致：无配音哨兵才进无配音，否则自动配音。
        mode = (
            VOICE_MODE_NONE
            if server == voice.NO_VOICE_NAME
            else VOICE_MODE_TTS
        )
    if mode == VOICE_MODE_NONE:
        return "ready", ""
    if mode == VOICE_MODE_UPLOAD:
        # 音频文件在生成页现场上传，同样只能是 waiting，目标正是生成页。
        return "waiting", tr("Bench Needs Upload Audio")
    label = _TTS_SERVER_LABELS.get(server, server)
    try:
        if server in ("azure-tts-v1", "azure-tts-v2"):
            # 运行时两个缺一即抛错，只配 key 不配 region 照样失败。
            ok = _has_cred(
                _safe_config_get(config.azure, "speech_key")
            ) and _has_cred(_safe_config_get(config.azure, "speech_region"))
        elif server == "siliconflow":
            ok = _has_cred(_safe_config_get(config.siliconflow, "api_key"))
        elif server == "gemini-tts":
            ok = _has_cred(config.app.get("gemini_api_key"))
        elif server == "mimo-tts":
            ok = _has_cred(config.app.get("mimo_api_key"))
        elif server == "minimax-tts":
            # 与运行时同一解析顺序：独立段 → [app] 段 → 环境变量。
            ok = _has_cred(voice.get_minimax_tts_api_key())
        elif server == "elevenlabs":
            ok = _has_cred(voice.get_elevenlabs_api_key())
        elif server == "chatterbox":
            # 自托管服务：配了地址即视为配好，服务是否在线是运行时的事。
            ok = _has_cred(_safe_config_get(config.chatterbox, "base_url"))
        elif server == "fish_audio":
            ok = _has_cred(voice.get_fish_audio_api_key())
        else:
            return "needs_setup", _missing_key_message(label)
    except Exception as exc:  # noqa: BLE001 - 判定失败按"未就绪"处理
        logger.debug(f"selected tts readiness check failed: {exc}")
        return "needs_setup", _missing_key_message(label)
    if ok:
        return "ready", ""
    return "needs_setup", _missing_key_message(label)


def _summaries_show_finished(summaries) -> bool:
    """任务摘要里是否存在已完成的成片。纯判定，可独立测试。"""
    for item in summaries or []:
        if not isinstance(item, dict):
            continue
        if item.get("video_file"):
            return True
        if _normalize_task_state(item.get("state")) == const.TASK_STATE_COMPLETE:
            return True
    return False


def _bench_tool_state(tool) -> tuple[str, str]:
    """这张卡现在处于什么状态，返回 ``(状态, 缺什么)``。

    状态判定只有三个来源：所选能力的真实配置（与提交校验同构）、有没有跑完的
    任务（产物）、以及"永远就绪"（纯本地能力）。"配过别的 key"不算就绪——那
    正是之前满屏 Ready 点进去却失败的原因。
    """
    ready_when = tool.get("ready_when", "always")
    if ready_when == "always":
        return "ready", ""

    if ready_when == "llm":
        ok, provider_id = _selected_llm_detail()
        if ok:
            return "ready", ""
        if provider_id:
            return "needs_setup", _missing_key_message(provider_id)
        return "needs_setup", tr("Bench Needs Key")

    if ready_when == "media":
        return _selected_media_state()

    if ready_when == "tts":
        return _selected_tts_state()

    if ready_when == "has_task":
        # 缩略图只能挂在一条**已完成**的任务上（渲染入口只在生成视图的任务快照里），
        # 所以没有任务时这张卡该说的是"还差什么"，而不是"点进去是空的"。
        # 注意不能只读会话里的 active 标记：那份字典只登记"刚提交"，从不写回终态，
        # 在这里读它等于永远 waiting。直接看任务摘要（历史 + 运行态），与任务面板
        # 是同一份事实来源。
        try:
            finished = _summaries_show_finished(_collect_task_summaries(limit=20))
        except Exception as exc:  # noqa: BLE001 - 判定失败按"没有成片"处理
            logger.debug(f"thumbnail readiness check failed: {exc}")
            finished = False
        if finished:
            return "ready", ""
        return "waiting", tr("Bench Needs Finished Task")

    return "ready", ""


def _render_tools_bench():
    """工具台：一屏看全所有能力，以及每一项此刻能不能用。"""
    st.markdown(
        f'<p class="mpt-bench-eyebrow">{html.escape(tr("Bench Eyebrow"))}</p>',
        unsafe_allow_html=True,
    )
    st.markdown(f"## {html.escape(tr('Bench Title'))}")
    st.markdown(
        f'<p class="mpt-bench-subtitle">{html.escape(tr("Bench Subtitle"))}</p>',
        unsafe_allow_html=True,
    )

    columns = st.columns(3)
    for index, tool in enumerate(_TOOLS_BENCH_TOOLS):
        with columns[index % 3]:
            _render_bench_card(tool)


_BENCH_STATE_LABELS = {
    "ready": "Bench State Ready",
    "needs_setup": "Bench State Needs Setup",
    "waiting": "Bench State Waiting",
}


def _render_bench_card(tool):
    """一张工具卡。整张卡可点，状态与 CTA 按真实就绪度变化。"""
    state, missing = _bench_tool_state(tool)
    view, action = tool["target"]
    title = tr(tool["title"])

    with st.container(key=f"bench_card_{tool['key']}"):
        # 参考设计的每张卡顶部都有一块浅色视觉区。本应用没有定制 3D 素材，就用
        # 同一家族的单色渐变 + 首字母字标来表达——比一张通用图库插画诚实，也比
        # 一个空白盒子少很多"图片加载失败"的误会。色调按 data-card 区分。
        st.markdown(
            f'<div class="mpt-bench-visual" data-card="{html.escape(tool["key"])}">'
            f'<span class="mpt-bench-visual__mark">{html.escape(title[:1])}</span>'
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="mpt-bench-body">'
            f'<div class="mpt-bench-head"'
            f' data-card="{html.escape(tool["key"])}"'
            f' data-state="{html.escape(state)}">'
            f'<span class="mpt-bench-title">{html.escape(title)}</span>'
            f'<span class="mpt-bench-flag">'
            f'{html.escape(tr(_BENCH_STATE_LABELS[state]))}</span>'
            f"</div>"
            f'<p class="mpt-bench-desc">{html.escape(tr(tool["desc"]))}</p>'
            f"</div>",
            unsafe_allow_html=True,
        )
        if missing:
            st.caption(missing)

        # 未就绪的卡不跳到那个能力上去——跳过去只会让人对着一个空的控件发呆。
        # 它跳到设置页，并把缺的那项说清楚。这是"能点"与"点了有用"的区别。
        if state == "needs_setup":
            cta_label = tr("Bench Configure")
            target_view = "settings"
        else:
            cta_label = f"{tr(tool['cta'])} →"
            target_view = view

        # 一律用次级按钮。九张卡九个实心主按钮会把整屏变成一片红，而"一屏一个
        # 主操作"正是参考设计让密集界面显得安静的原因。卡片的按钮是入口提示，
        # 不是这一屏要你做的事。
        if st.button(
            cta_label,
            key=f"bench_go_{tool['key']}",
            width="stretch",
            type="secondary",
            icon=tool["icon"],
        ):
            if action == "open_tasks":
                st.session_state["task_manager_open"] = True
            elif action:
                st.session_state["bench_focus"] = action
            st.session_state[_NAV_REQUEST_KEY] = target_view
            st.rerun()


def _page_bench():
    """左侧导航「工具台」：所有能力的一屏总览。"""
    with st.container(key="page_bench"):
        _render_tools_bench()


# 参考设计的工具台里有七张卡，本应用只实现了其中一部分。这里照它的形制列出
# **尚未实现**的能力，每一张都明确标注"待实现"，并写清缺什么。对照关系：
#   Script Writer   → 已有（Bench Script）
#   Prompt Builder  → 待实现（电影感提示词改写）
#   Video Generator → 部分有（素材拼装 + AI 片段服务），纯 AI 成片仍是路线图
#   Image Generator → 待实现（配图/缩略图底图仍需外部准备）
#   AI Ad Generator → 待实现（多角度广告变体）
#   Skit Builder    → 待实现（多镜分场）
#   AI Voiceover    → 已有（Bench Voice）
#   Auto Captions   → 部分有（字幕排版已有，成片级导出预设待实现）
#   Video Editor    → 待实现（时间线剪辑）
_ROADMAP_TOOLS = [
    {
        "key": "prompt_builder",
        "title": "Roadmap Prompt Builder",
        "desc": "Roadmap Prompt Builder Desc",
        "icon": ":material/auto_fix:",
        "needs": "Roadmap Prompt Builder Needs",
    },
    {
        "key": "ai_image",
        "title": "Roadmap AI Image",
        "desc": "Roadmap AI Image Desc",
        "icon": ":material/image:",
        "needs": "Roadmap AI Image Needs",
    },
    {
        "key": "skit_builder",
        "title": "Roadmap Skit Builder",
        "desc": "Roadmap Skit Builder Desc",
        "icon": ":material/theater_comedy:",
        "needs": "Roadmap Skit Builder Needs",
    },
    {
        "key": "timeline_editor",
        "title": "Roadmap Timeline Editor",
        "desc": "Roadmap Timeline Editor Desc",
        "icon": ":material/filmstrip:",
        "needs": "Roadmap Timeline Editor Needs",
    },
    {
        "key": "ad_variants",
        "title": "Roadmap Ad Variants",
        "desc": "Roadmap Ad Variants Desc",
        "icon": ":material/campaign:",
        "needs": "Roadmap Ad Variants Needs",
    },
    {
        "key": "generation_search",
        "title": "Roadmap Search",
        "desc": "Roadmap Search Desc",
        "icon": ":material/search:",
        "needs": "Roadmap Search Needs",
    },
]


def _render_roadmap_bench():
    """未实现能力的占位卡片。"""
    with st.expander(tr("Roadmap Bench"), expanded=False):
        st.caption(tr("Roadmap Bench Note"))
        columns = st.columns(3)
        for index, tool in enumerate(_ROADMAP_TOOLS):
            with columns[index % 3]:
                st.markdown(
                    f'<div class="mpt-bench-card">'
                    f'<p class="mpt-bench-title">{html.escape(tr(tool["title"]))}</p>'
                    f'<p class="mpt-bench-desc">{html.escape(tr(tool["desc"]))}</p>'
                    f'<p class="mpt-bench-badge">{html.escape(tr("Roadmap Badge"))}</p>'
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.button(
                    tr("Roadmap Not Yet"),
                    key=f"roadmap_{tool['key']}",
                    disabled=True,
                    width="stretch",
                    help=tr(tool["needs"]),
                )


def _page_generate():
    """左侧导航「生成」：脚本、素材、音频、字幕四组参数 + 唯一的生成按钮。

    这一页刻意只放参数和一个主动作。计划、成片库、活动各有自己的页面，不再和参数
    挤在同一屏——之前它们是三个折叠面板叠在参数区上方，展开与否都会重绘，真正每天
    要用的"填参数、点生成"被挤到了页面最下面。
    """
    with st.container(key="page_generate"):
        # 参考设计的每个工具页顶部都是一组居中 hero：小徽章 + 大标题 + 一句副标题。
        # 生成页的四个参数面板就是本应用的"工具区"，hero 让它读起来像一个工具，
        # 而不是一张配置表。
        st.markdown(
            f'<div class="mpt-hero">'
            f'<p class="mpt-hero-badge">{html.escape(tr("Generate Hero Badge"))}</p>'
            f'<p class="mpt-hero-title">{html.escape(tr("Generate Hero Title"))}</p>'
            f'<p class="mpt-hero-sub">{html.escape(tr("Generate Hero Sub"))}</p>'
            f"</div>",
            unsafe_allow_html=True,
        )
        with st.container(key="main_settings_grid"):
            panel = st.columns(4)

        params = VideoParams(video_subject="")
        params.match_materials_to_script = bool(
            st.session_state.get("match_materials_to_script", False)
        )
        _render_script_settings(panel[0], params)
        uploaded_files = _render_video_settings(panel[1], params)
        uploaded_audio_file, uploaded_bgm_file, voice_mode = _render_audio_settings(
            panel[2], params
        )
        _render_subtitle_settings(panel[3], params)

        # 计划页在另一个页面读这份快照来画预览，因此每次都要更新。
        st.session_state["form_params_snapshot"] = params.model_copy(deep=True)

        _render_roadmap_bench()

        generation_submitted = _render_generation_controls(
            params,
            uploaded_files,
            uploaded_audio_file,
            uploaded_bgm_file,
            voice_mode,
        )

        # 生成分支在启动后台线程前已经请求过保存。普通控件交互继续请求非阻塞保存；
        # 如果后台任务正在使用配置，配置层会在任务结束时自动应用并落盘最新值。
        if not generation_submitted:
            _save_runtime_config()


# 左侧导航按**工作阶段**分组，而不是按功能罗列。
#
# 分组依据是"这条视频走到哪一步了"：先计划、再生成、最后成片与发布。功能罗列
# （比如"设置 / 计划 / 库 / 活动"）在条目变多之后用户得自己记住每个入口属于哪一段，
# 而分组之后位置本身就说明了顺序。
_NAV_GROUPS = [
    # 登录屏放在最上面并作为落地页：先连好各频道的 YouTube 账号，发布链路才是
    # 通的；否则用户在媒体库点发布时才发现没登录。连过的 token 存在本机，
    # 之后打开直接是已连接状态，不需要重复操作。
    ("Nav Group Account", [
        ("login", "Nav Login", ":material/login:", "Nav Login Hint"),
    ]),
    ("Nav Group Bench", [
        ("bench", "Nav Bench", ":material/grid_view:", "Nav Bench Hint"),
    ]),
    ("Nav Group Plan", [
        ("plan", "Nav Plan", ":material/calendar_month:", "Nav Plan Hint"),
    ]),
    ("Nav Group Create", [
        ("generate", "Nav Generate", ":material/movie_edit:", "Nav Generate Hint"),
        ("campaign", "Nav Campaign", ":material/campaign:", "Nav Campaign Hint"),
    ]),
    ("Nav Group Library", [
        ("library", "Nav Library", ":material/video_library:", "Nav Library Hint"),
        ("copyright", "Nav Copyright", ":material/shield:", "Nav Copyright Hint"),
    ]),
    ("Nav Group System", [
        ("settings", "Nav Settings", ":material/settings:", "Nav Settings Hint"),
    ]),
]

# 扁平视图表，保留给"某个键对应哪一屏"的查表使用
_NAV_VIEWS = {
    key: (title, icon)
    for _group, items in _NAV_GROUPS
    for key, title, icon, _hint in items
}

# 落地页。默认值放在常量里而不是散在两处：导航栏读它、视图分发用它兜底，
# 两边必须永远是同一个值。
_DEFAULT_VIEW = "login"

# 导航请求用的旁路键。直接写 ``st.session_state["nav_view"]`` 会被 Streamlit 拒绝：
# 那个键属于左侧导航的 radio 控件，控件一旦创建，本轮就不能再从代码里改它。
# 于是需要跳转的地方只写这个旁路键，由导航栏在**创建控件之前**消费。
_NAV_REQUEST_KEY = "_nav_requested_view"

_VIEW_RENDERERS = {
    "login": _page_login,
    "bench": _page_bench,
    "generate": _page_generate,
    "plan": _page_plan,
    "library": _page_library,
    "copyright": _page_copyright,
    "campaign": _page_campaign,
    "settings": _page_settings,
}


def _safe_config_get(section, key, default=""):
    """从配置段里取值，段本身缺失或不是字典时退回默认值。

    ``config.azure`` 这类段在用户删掉整段配置后会变成 None 或非字典，直接下标
    会把"读取凭据"这种无害操作变成崩溃——而崩溃发生在渲染侧栏的时候。
    """
    try:
        if not isinstance(section, dict):
            return default
        value = section.get(key, default)
    except Exception:  # noqa: BLE001 - 取值失败按"没配"处理
        return default
    return default if value is None else value


def _free_tool_readiness() -> list[tuple[bool, str]]:
    """每一项能力当前是否可用，返回 ``(是否就绪, 名称)``。

    口径与工具台卡片完全一致：看**当前选中的**提供方/素材源/配音服务有没有配齐，
    而不是"有没有配过别的"。侧栏这张卡只是把同样的三个结论数出来。
    """
    llm_ok, _ = _selected_llm_detail()
    checks = [(llm_ok, tr("Free Tool Script"))]

    media_state, _ = _selected_media_state()
    checks.append((media_state == "ready", tr("Free Tool Media")))

    tts_state, _ = _selected_tts_state()
    checks.append((tts_state == "ready", tr("Free Tool Voice")))
    return checks


def _render_navigation_rail():
    """左侧导航栏，返回当前选中的视图。

    这里用 ``st.sidebar`` 里的 radio，而不是 ``st.navigation``：两者外观一样，都是
    左侧竖排导航，但后者把每个页面变成独立脚本，AppTest 无法切页——那意味着媒体库、
    计划面板、设置页的几十个既有用例全部失去被测能力。radio 只是一个 session_state
    键，测试里 ``app.session_state["nav_view"] = "plan"`` 就能走到那一屏。

    每个条目带一行说明，理由和参考设计一致：一个光有图标和标题的竖排列表，
    隔几周回来就忘了每个入口是干什么的；一行 6 个字的副标题比一次点击去试探便宜。
    """
    # 必须在 radio 创建之前落实跳转请求，否则会撞上 Streamlit 的
    # "cannot be modified after the widget with key ... is instantiated"。
    requested = st.session_state.pop(_NAV_REQUEST_KEY, None)
    if requested in _NAV_VIEWS:
        st.session_state["nav_view"] = requested

    selected = st.session_state.get("nav_view", _DEFAULT_VIEW)
    # 记下这一屏到底是哪一屏。落地页是"没设过就算出来的"默认值，不写回 session
    # 状态的话，没有任何地方能知道用户此刻站在哪一屏上——包括测试。写回之后，
    # "落地页是不是工具台"就是一件可以被断言的事，而不是只能靠肉眼看截图。
    st.session_state["nav_view"] = selected
    with st.sidebar:
        st.markdown(f"**{tr('App Brand')}**")
        # 参考设计的侧栏顶部是一个深色 "+ New video" 主按钮：它是全应用唯一
        # 常驻的主操作。生成页才是本应用的"新视频"入口，点它即切过去。
        if st.button(
            f"+ {tr('New Video')}",
            key="nav_new_video",
            type="primary",
            width="stretch",
        ):
            st.session_state[_NAV_REQUEST_KEY] = "generate"
            st.session_state["bench_focus"] = "focus_generate"
            st.rerun()
        st.caption(tr("Navigation Hint"))
        st.divider()
        for group_title, items in _NAV_GROUPS:
            st.markdown(f'<p class="mpt-nav-group">{tr(group_title)}</p>',
                        unsafe_allow_html=True)
            for key, title, icon, hint in items:
                is_current = key == selected
                # 用按钮而不是 radio：需要每条带副标题，而 radio 只能给纯文本选项。
                # 点击即切换，与 Streamlit 的 st.rerun 语义一致。
                if st.button(
                    f"{tr(title)}",
                    key=f"nav_item_{key}",
                    type="primary" if is_current else "secondary",
                    icon=icon,
                    width="stretch",
                    help=tr(hint),
                ):
                    selected = key
                    st.session_state["nav_view"] = key
                    st.rerun()
        st.divider()
        _render_free_tools_card()
    return selected


def _render_free_tools_card():
    """侧栏底部的「免费工具」状态卡。

    这张卡回答的是每个用户第一次都会问、但界面上从没说过的问题：**这些功能真的免费
    吗？** 本应用只依赖本地能力或带免费额度的服务，额度用完会直接失败而不是扣钱——
    但这一点此前只存在于文档里，界面上一个字的提示都没有。

    因此把"能不能用"直接摆出来：本地的一律可用；需要额度的显示是否已经配好。
    不显示额度数字——各家额度与刷新策略差异很大，写死在界面上很快就会过期成谎言。
    """
    checks = _free_tool_readiness()
    ready = sum(1 for ok, _ in checks if ok)
    total = len(checks)

    # 其它面板（登录卡、版权行、健康条）都是带边框的容器，只有这一张卡还是
    # 裸文本——视觉层级上它看起来像"剩下的"，而不是"常驻的状态卡"。
    with st.container(key="free_tools_card", border=True):
        st.markdown(f"**{tr('Free Tools')}**")
        if ready == total:
            st.success(tr("Free Tools Ready").format(ready=ready, total=total))
        else:
            st.warning(tr("Free Tools Partial").format(ready=ready, total=total))
        for ok, label in checks:
            st.markdown(
                f'<p class="mpt-free-item">{"✅" if ok else "○"} {html.escape(label)}</p>',
                unsafe_allow_html=True,
            )
        st.caption(tr("Free Tools Note"))


def _render_view(view: str):
    """渲染选中的那一屏。"""
    renderer = _VIEW_RENDERERS.get(view) or _VIEW_RENDERERS["generate"]
    renderer()


def _render_application():
    """左侧导航 + 各页面。

    之前所有功能挤在一个长页面上：三个折叠面板 + 四列参数 + 生成按钮。折叠与否都会
    重绘，真正每天要用的"填参数、点生成"被推到页面最底部，而任务面板每两秒又在整个
    页面上重画一次，于是"点一下要等半秒"成了日常手感。

    拆成左侧导航之后，每一屏只做一件事，常用的那一屏不再被别的功能挤下去。
    """
    _render_top_bar()

    if _apply_pending_settings_preset():
        st.success(tr("Settings Preset Imported"))

    restore_applied = _apply_pending_task_restore()
    restore_candidate_id = st.session_state.get("task_restore_candidate_id")
    if restore_candidate_id:
        _render_task_restore_dialog(restore_candidate_id)
    restore_succeeded = st.session_state.pop("task_restore_succeeded", False)
    if restore_applied or restore_succeeded:
        st.success(tr("Task Configuration Loaded"))

    view = _render_navigation_rail()
    # 任务表放在主区域、当前视图之上。展开时才渲染，关闭时一行都不产生——
    # 这正是之前用 popover 做不到的事。
    _render_task_manager_section()
    # 活动面板的"用作视频主题"要求单击即出片。生成控件住在生成视图上，所以这里
    # 直接把它切过去——不去改 nav_view：那个键属于导航栏的 radio 控件，本轮
    # 已经创建过，Streamlit 不允许再从代码里改。
    if st.session_state.get("campaign_auto_generate"):
        view = "generate"
    _render_view(view)


_render_application()
