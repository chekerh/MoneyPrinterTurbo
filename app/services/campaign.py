"""Campaign content studio.

理解(Google Drive / Notion / 文本)中的活动简报，并将内容拆解为可直接发布的
视频文案包(标题/钩子/脚本/话题/CTA)，供文案生成与成片发布流水线复用。
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.services import llm
from app.utils import utils

logger = logging.getLogger(__name__)

_DEFAULT_FETCH_TIMEOUT = 30.0
_MAX_SOURCE_TEXT_BYTES = 400_000
_MAX_FOLDER_FILES = 30
_DEFAULT_POST_COUNT = 5
_MAX_POST_COUNT = 20
_NOTION_SENTINEL = "Skip to content"

_CAMPAIGN_KIND_URL = "url"
_CAMPAIGN_KIND_TEXT = "text"


@dataclass
class MediaRef:
    """从活动来源中提取的可选素材(单个)引用。"""

    title: str = ""
    url: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "url": self.url}


@dataclass
class CampaignSource:
    """一条已抓取的活动输入：抓取方式、标题、正文与可选的媒体引用。"""

    kind: str = _CAMPAIGN_KIND_TEXT
    url: str = ""
    title: str = ""
    text: str = ""
    refs: List[MediaRef] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "refs": [ref.to_dict() for ref in self.refs],
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CampaignSource":
        data = data or {}
        refs = []
        for ref in data.get("refs") or []:
            if isinstance(ref, MediaRef):
                refs.append(ref)
            elif isinstance(ref, dict):
                refs.append(MediaRef(title=ref.get("title") or "", url=ref.get("url") or ""))
        return cls(
            kind=data.get("kind") or _CAMPAIGN_KIND_TEXT,
            url=data.get("url") or "",
            title=data.get("title") or "",
            text=data.get("text") or "",
            refs=refs,
        )


def _clamp_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text.encode("utf-8")) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(text[:mid].encode("utf-8")) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def classify_campaign_link(raw: str) -> Tuple[str, str]:
    """判断输入是 Notion / Google Drive / 其他链接 / 纯文本。

    返回 (kind, url)。kind 取值：notion / drive_file / drive_folder / docs /
    url / text。
    """
    raw = (raw or "").strip()
    if not raw:
        return _CAMPAIGN_KIND_TEXT, ""
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        return _CAMPAIGN_KIND_TEXT, ""

    host = (urllib.parse.urlparse(raw.lower()).hostname or "").lower()
    if host == "notion.site" or host.endswith(".notion.site"):
        return "notion", raw
    if host == "notion.so" or host.endswith(".notion.so"):
        return "notion", raw

    if host == "drive.google.com" or host.endswith(".drive.google.com"):
        if "/drive/folders/" in raw or "/folders/" in raw:
            return "drive_folder", raw
        if urllib.parse.urlparse(raw).path.startswith("/open") and re.search(r"[?&]id=", raw):
            return "drive_folder", raw
        if "/file/d/" in raw:
            return "drive_file", raw
        return "drive_file", raw

    if "docs.google.com" in host:
        if "/document/d/" in raw:
            return "docs", raw
        return "docs", raw

    return _CAMPAIGN_KIND_URL, raw


def _extract_drive_id(url: str) -> str:
    patterns = (
        r"/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]+)",
        r"/[a-z]+/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def _extract_title_from_html(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0] == _NOTION_SENTINEL:
        lines = lines[1:]
    return lines[0] if lines else ""


def _gather_notion_links(text: str) -> List[str]:
    urls = []
    for pattern in (r"https?://[^\s)\]]+", r"[a-z0-9-]+\.notion\.site/[^\s)\]]+"):
        urls.extend(item for item in re.findall(pattern, text))
    # 去重并去掉行尾标点，保留稳定顺序。
    seen = set()
    cleaned = []
    for url in urls:
        url = url.rstrip(".,;:!?")
        key = url.rstrip("/")
        if key and key not in seen:
            seen.add(key)
            cleaned.append(url)
    return cleaned


async def _render_notion_body(url: str) -> str:
    """用无头浏览器渲染公开 Notion 页面，返回正文纯文本。

    新版 notion-html 页面是 Next.js 应用，服务端 HTML 只包含脚本外壳，
    真正的页面内容需要执行 JS 后才出现在 DOM 中。
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as player:
        browser = await player.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_DEFAULT_FETCH_TIMEOUT * 1000)
            body = ""
            for _ in range(int(_DEFAULT_FETCH_TIMEOUT / 1.5)):
                body = await page.evaluate("document.body ? document.body.innerText : ''")
                if len(body) > 300:
                    break
                await asyncio.sleep(1.5)
        finally:
            await browser.close()
    return _clamp_text(body, _MAX_SOURCE_TEXT_BYTES)


def _fetch_notion(url: str) -> CampaignSource:
    try:
        text = asyncio.run(_render_notion_body(url))
    except Exception as exc:
        logger.error(f"failed to render notion page {url}: {exc}")
        raise RuntimeError("failed to render the Notion page") from exc
    ref_urls = _gather_notion_links(text)
    return CampaignSource(
        kind="notion",
        url=url,
        title=_extract_title_from_html(text),
        text=text,
        refs=[MediaRef(url=ref) for ref in ref_urls],
    )


def _drive_download_text(url: str) -> Tuple[str, str]:
    """下载单个公开 Drive 文件；返回 (文本, 文件名)。二进制内容返回空文本。"""
    import requests

    response = requests.get(
        url,
        timeout=_DEFAULT_FETCH_TIMEOUT,
        stream=True,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    filename = _parse_download_filename(response)
    if content_type.startswith("text/html"):
        return "", filename
    if content_type.startswith("text/") or "json" in content_type:
        response.raw.decode_content = True
        raw = response.raw.read(_MAX_SOURCE_TEXT_BYTES + 1)
        if len(raw) > _MAX_SOURCE_TEXT_BYTES:
            raw = raw[:_MAX_SOURCE_TEXT_BYTES]
        text = raw.decode("utf-8", errors="replace")
        return _clamp_text(text, _MAX_SOURCE_TEXT_BYTES), filename
    return "", filename


def _parse_download_filename(response) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
    if match:
        return urllib.parse.unquote(match.group(1))
    return ""


def _fetch_drive_file(url: str) -> CampaignSource:
    file_id = _extract_drive_id(url)
    if not file_id:
        return CampaignSource(kind="drive_file", url=url, text="")

    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    text, filename = _drive_download_text(download_url)
    if text:
        return CampaignSource(
            kind="drive_file",
            url=url,
            title=filename or file_id,
            text=text,
        )
    return CampaignSource(
        kind="drive_file",
        url=url,
        title=filename or file_id,
        text="",
        refs=[MediaRef(title=filename or file_id, url=download_url)],
    )


def _fetch_docs(url: str) -> CampaignSource:
    doc_id = _extract_drive_id(url)
    if not doc_id:
        return CampaignSource(kind="docs", url=url, text="")

    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    text, _ = _drive_download_text(export_url)
    return CampaignSource(kind="docs", url=url, title=doc_id, text=text)


def _embedded_folder_files(folder_id: str) -> List[Tuple[str, str]]:
    """读取公开文件夹的静态内嵌视图，返回 (文件名, 文件下载链接) 列表。"""
    import requests

    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}"
    response = requests.get(
        url,
        timeout=_DEFAULT_FETCH_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    html = response.text

    results = []
    # 静态视图中的文件条目形如: title + /file/d/{id} 链接。
    for block in re.findall(r"<a[^>]+href=\"(/file/d/[^\"]+)\"[^>]*>(.*?)</a>", html, re.DOTALL):
        href, name = block
        name = re.sub(r"<[^>]+>", "", name).strip()
        file_id = _extract_drive_id(href)
        if not file_id or len(results) >= _MAX_FOLDER_FILES:
            continue
        results.append(
            (name, f"https://drive.google.com/uc?export=download&id={file_id}")
        )
    return results


def _fetch_drive_folder(url: str) -> CampaignSource:
    folder_id = _extract_drive_id(url)
    if not folder_id:
        return CampaignSource(kind="drive_folder", url=url, text="")

    source = CampaignSource(kind="drive_folder", url=url, text="")
    try:
        files = _embedded_folder_files(folder_id)
    except Exception as exc:
        source.text = ""
        source.refs = [MediaRef(title="drive_folder", url=url)]
        logger.warning(f"failed to list drive folder {folder_id}: {exc}")
        return source

    paragraphs = []
    for index, (name, download_url) in enumerate(files, start=1):
        try:
            text, filename = _drive_download_text(download_url)
        except Exception as exc:
            logger.warning(f"failed to fetch folder file {name}: {exc}")
            text, filename = "", ""
        if text:
            paragraphs.append(f"## {index}. {name}\n{text}")
        else:
            source.refs.append(MediaRef(title=filename or name, url=download_url))
    source.text = _clamp_text("\n\n".join(paragraphs), _MAX_SOURCE_TEXT_BYTES)
    return source


def fetch_campaign_source(raw: str) -> CampaignSource:
    """抓取用户输入的链接或直接使用粘贴的文本，返回可分析的 CampaignSource。"""
    kind, url = classify_campaign_link(raw)
    if kind == "notion":
        return _fetch_notion(url)
    if kind == "drive_file":
        return _fetch_drive_file(url)
    if kind == "drive_folder":
        return _fetch_drive_folder(url)
    if kind == "docs":
        return _fetch_docs(url)
    if kind == _CAMPAIGN_KIND_URL:
        return CampaignSource(
            kind=_CAMPAIGN_KIND_URL,
            url=url,
            title=url,
            text=_clamp_text(raw, _MAX_SOURCE_TEXT_BYTES),
        )
    return CampaignSource(kind=_CAMPAIGN_KIND_TEXT, text=_clamp_text(raw, _MAX_SOURCE_TEXT_BYTES))


def _parse_json_response(response: str) -> Any:
    """解析 LLM 返回的 JSON 内容，兼容 markdown 代码围栏和字符串包裹。"""
    text = llm._strip_code_fence(response or "")
    try:
        return json.loads(text)
    except Exception:
        pass

    object_match = re.search(r"\{.*\}", text, re.DOTALL)
    if object_match:
        try:
            return json.loads(object_match.group())
        except Exception:
            pass

    array_match = re.search(r"\[.*\]", text, re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group())
        except Exception:
            pass
    raise ValueError("campaign LLM response is not valid JSON")


_BRIEF_PROMPT = """\
You are a video campaign strategist. Read the campaign material below and extract \
a structured brief as STRICT JSON with exactly these keys:
- campaign_name: short human-readable title for the campaign
- brand: brand/subject that must appear in the content (or "")
- goal: one sentence describing the campaign goal
- audience: target audience
- tone: content tone to use for the copy and scripts (short, concrete phrase)
- platforms: list of target platforms
- hashtags: list of mandated or strongly suggested hashtags (no leading #)
- requirements: list of hard rules/constraints stated in the material (e.g. what must be shown, posting window, comment rules)
- source_materials: list of URLs of source content given in the material (podcasts/videos/drive items)
- content_types: list of suggested content types (clips, shorts, reels, threads, ...)
- posting_notes: any logistics notes for posting

Do not invent campaign goals, brand names, hashtags, or requirements that the \
material does not state. Record only what is given.

Return only the JSON object. Do not wrap it in markdown fences.

Campaign material:
{text}
"""


def understand_campaign(source: CampaignSource, app_config=None) -> Dict[str, Any]:
    """使用当前 LLM 配置将活动输入转换为结构化简报。"""
    prompt = _BRIEF_PROMPT.format(text=source.text or "")
    response = llm._generate_response(prompt=prompt, app_config=app_config)
    if response.startswith("Error:"):
        raise ValueError(response)
    data = _parse_json_response(response)
    if not isinstance(data, dict):
        raise ValueError("campaign brief is not a JSON object")
    return _normalize_brief(data)


_GENERATE_PROMPT = """\
You are a short-form short video editor. Using the campaign brief below, generate \
{count} distinct ready-to-post short video content pieces as a STRICT JSON array. \
Each array element is an object with exactly these keys:
- title: post title under 100 characters
- hook: 1-2 sentence hook, the first seconds of the video
- script: the full voiceover script as 3 short paragraphs of plain story/intro/outro text (ready for TTS)
- on_screen_text: one line of on-screen text for the video
- description: post caption
- hashtags: list of hashtags without #, leading with campaign-mandated tags
- cta: one call to action line
Every piece MUST respect the campaign requirements: always show or mention the \
required brand/subject, use the mandated hashtags, reference only approved source \
materials, and match the target audience and tone. Vary the angle across pieces.

Retention (hook first): every piece must open its hook with the problem plus the \
payoff, with no run-up and no "in this video" staging. The hook is what stops the \
scroll in the first seconds; the script then delivers a payoff before attention \
wanes, with tight pacing and no dead-air filler.

Humanize the writing. Vary sentence length and choose specific, uneven phrasing \
for one viewer instead of average chatbot prose. Ban AI tells: no "not X but Y" \
contrasts, no forced triads, no one-line closers, no em dashes or en dashes, and \
no inflated words (pivotal, showcase, testament, underscore, vibrant, robust, \
enhance, delve). Write hooks and scripts so they sound like one person speaking, \
not a marketing template.

Campaign brief (JSON):
{brief_json}

Return only the JSON array. Do not wrap it in markdown fences.
"""


def generate_campaign_posts(
    brief: Dict[str, Any],
    count: int = _DEFAULT_POST_COUNT,
    app_config=None,
) -> List[Dict[str, Any]]:
    """根据简报生成 count 个可直接发布的视频文案包。"""
    count = max(1, min(int(count or _DEFAULT_POST_COUNT), _MAX_POST_COUNT))
    prompt = _GENERATE_PROMPT.format(
        brief_json=json.dumps(brief, ensure_ascii=False),
        count=count,
    )
    response = llm._generate_response(prompt=prompt, app_config=app_config)
    if response.startswith("Error:"):
        raise ValueError(response)
    data = _parse_json_response(response)
    if not isinstance(data, list):
        raise ValueError("campaign posts are not a JSON array")
    if not data:
        raise ValueError("campaign posts are empty after generation")
    return [_normalize_post(item) for item in data if isinstance(item, dict)]


def _normalize_post(item: Dict[str, Any]) -> Dict[str, Any]:
    def as_text(key: str) -> str:
        return str(item.get(key) or "").strip()

    hashtags = item.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = [h.strip().lstrip("#") for h in re.split(r"[,\s]+", hashtags) if h.strip()]
    hashtags = [str(h).lstrip("#") for h in hashtags]

    # 汇总一份可直接粘贴的发布文案。
    title = as_text("title")
    description = as_text("description")
    cta = as_text("cta")
    hashtag_line = " ".join(f"#{h}" for h in hashtags)
    ready_lines = [line for line in (title, description, cta, hashtag_line) if line]
    ready_to_post = "\n\n".join(ready_lines)

    return {
        "title": title,
        "hook": as_text("hook"),
        "script": as_text("script"),
        "on_screen_text": as_text("on_screen_text"),
        "description": description,
        "hashtags": hashtags,
        "cta": cta,
        "ready_to_post": ready_to_post,
    }


def _normalize_brief(brief: Dict[str, Any]) -> Dict[str, Any]:
    def as_list(key: str) -> List[str]:
        value = brief.get(key)
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    normalized = dict(brief)
    for key in ("platforms", "hashtags", "requirements", "source_materials"):
        normalized[key] = as_list(key)
    return normalized


def source_kind_label(source: CampaignSource) -> str:
    return source.kind


def build_campaign_markdown(source: CampaignSource, brief: dict, posts: List[dict]) -> str:
    """汇总活动简报与所有文案包，生成可直接保存的 Markdown。"""
    hashtag_line = " ".join(
        "#" + h
        for h in (str(tag).lstrip("#").strip() for tag in (brief.get("hashtags") or []))
        if h
    )
    lines = [
        f"# {source.title or brief.get('campaign_name', 'Campaign')}",
        f"- Source: {source.url or '(pasted text)'}",
        "",
        "## Brief",
        f"- Name: {brief.get('campaign_name', '')}",
        f"- Brand: {brief.get('brand', '')}",
        f"- Goal: {brief.get('goal', '')}",
        f"- Audience: {brief.get('audience', '')}",
        f"- Tone: {brief.get('tone', '')}",
        f"- Platforms: {', '.join(brief.get('platforms') or [])}",
        f"- Hashtags: {hashtag_line}",
        "",
        "## Requirements",
    ]
    lines.extend([f"- {req}" for req in brief.get("requirements") or []])
    lines.append(f"## Posting Notes\n{brief.get('posting_notes', '')}\n")

    lines.append("## Content Pieces")
    for index, post in enumerate(posts, start=1):
        lines.append(f"\n### {index}. {post['title']}")
        lines.append(f"\n**Hook:** {post['hook']}")
        lines.append(f"\n**Script:**\n{post['script']}")
        lines.append(f"\n**On-screen text:** {post['on_screen_text']}")
        lines.append(f"\n**Ready to post:**\n{post['ready_to_post']}")

    return "\n".join(lines)


_SAVED_CAMPAIGN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def saved_campaigns_root(create: bool = False) -> str:
    """活动持久化目录；WebUI/测试可 monkeypatch 该函数隔离数据。"""
    return utils.storage_dir("campaigns", create=create)


def _origin_url(source: CampaignSource) -> str:
    return (source.url or "").strip() or ""


def _campaign_name(brief: Dict[str, Any], source: CampaignSource) -> str:
    name = str(brief.get("campaign_name") or "").strip()
    if not name:
        name = (source.title or "").strip()
    return name or "Campaign"


def _slugify_campaign_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48].rstrip("-") or "campaign"


def _safe_path(root: str, campaign_id: str) -> str:
    if not campaign_id or not _SAVED_CAMPAIGN_ID.match(campaign_id):
        raise ValueError("invalid campaign id")
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(os.path.join(root, campaign_id + ".json"))
    if os.path.commonpath([root_real, path_real]) != root_real:
        raise ValueError("campaign path is outside the saved campaigns directory")
    return path_real


def _atomic_write(path: str, text: str) -> None:
    """同目录临时文件 + os.replace 原子落盘，中断不会留下半个文件。

    与任务清单的原子写入保持一致；列表扫描会跳过 ``.`` 前缀的临时文件。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".campaign-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_campaign(
    source: CampaignSource,
    brief: Dict[str, Any],
    posts: List[Dict[str, Any]],
    campaign_id: str = "",
    root: str | None = None,
) -> Dict[str, Any]:
    """把活动(来源/简报/文案包)落盘为 ``<id>.json`` 与 ``<id>.md``。

    返回 {campaign_id, name, saved_at, post_count}。默认根目录来自
    ``saved_campaigns_root()``，WebUI 报文可 monkeypatch 它来隔离测试数据。
    """
    root = root or saved_campaigns_root(create=True)
    if not campaign_id:
        now = datetime.now()
        campaign_id = (
            f"{_slugify_campaign_name(_campaign_name(brief, source))}"
            f"-{now.strftime('%Y%m%d-%H%M%S')}"
        )
    if not _SAVED_CAMPAIGN_ID.match(campaign_id):
        raise ValueError("invalid campaign id")

    os.makedirs(root, exist_ok=True)
    saved_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "campaign_id": campaign_id,
        "name": _campaign_name(brief, source),
        "saved_at": saved_at,
        "source": source.to_dict(),
        "brief": brief,
        "posts": posts,
    }
    _atomic_write(
        os.path.join(root, campaign_id + ".json"),
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    _atomic_write(
        os.path.join(root, campaign_id + ".md"),
        build_campaign_markdown(source, brief, posts),
    )
    return {
        "campaign_id": campaign_id,
        "name": payload["name"],
        "saved_at": saved_at,
        "url": _origin_url(source),
        "post_count": len(posts or []),
    }


def list_saved_campaigns(root: str | None = None) -> List[Dict[str, Any]]:
    """按保存时间倒序返回已存档活动摘要，损坏文件直接跳过。"""
    root = root or saved_campaigns_root(create=False)
    campaigns = []
    if not os.path.isdir(root):
        return campaigns
    for name in os.listdir(root):
        if not name.endswith(".json") or name.startswith("."):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or not payload.get("campaign_id"):
            continue
        source_data = payload.get("source") or {}
        campaigns.append(
            {
                "campaign_id": str(payload.get("campaign_id")),
                "name": str(payload.get("name") or _campaign_name(payload.get("brief") or {}, CampaignSource.from_dict(source_data))),
                "saved_at": str(payload.get("saved_at") or ""),
                "url": _origin_url(CampaignSource.from_dict(source_data)),
                "post_count": len(payload.get("posts") or []),
            }
        )
    return sorted(
        campaigns,
        key=lambda item: (str(item.get("saved_at") or ""), str(item.get("campaign_id"))),
        reverse=True,
    )


def saved_campaign_options(campaigns: List[Dict[str, Any]]) -> Dict[str, str]:
    """把活动摘要列表映射为 ``label -> campaign_id`` 供 WebUI 加载器使用。

    列表按保存时间倒序，label 默认 ``"{name} ({saved_at})"``。同一秒保存的两个
    活动会产生相同的 label（届时仅有字典中存活的那一个可被加载），因此在
    label 冲突时追加 ``campaign_id`` 消歧，保证每个活动都能被独立选中。
    """
    options = {}
    used_labels = set()
    for item in campaigns:
        label = f"{item['name']} ({item['saved_at']})"
        if label in used_labels:
            label += f" · {item['campaign_id']}"
        used_labels.add(label)
        options[label] = str(item["campaign_id"])
    return options


def load_saved_campaign(
    campaign_id: str, root: str | None = None
) -> Dict[str, Any]:
    """读取已存档活动，返回 {campaign_id, name, saved_at, source, brief, posts}。

    ``source`` 是重建的 :class:`CampaignSource`，可直接交给后续生成流程。
    路径逃逸或文件损坏时抛出异常，由调用方呈现为“加载失败”。
    """
    root = root or saved_campaigns_root(create=False)
    path = _safe_path(root, campaign_id)
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("saved campaign is not a JSON object")
    return {
        "campaign_id": str(payload.get("campaign_id") or campaign_id),
        "name": str(payload.get("name") or _campaign_name(payload.get("brief") or {}, CampaignSource.from_dict(payload.get("source") or {}))),
        "saved_at": str(payload.get("saved_at") or ""),
        "source": CampaignSource.from_dict(payload.get("source")),
        "brief": payload.get("brief") or {},
        "posts": payload.get("posts") or [],
    }