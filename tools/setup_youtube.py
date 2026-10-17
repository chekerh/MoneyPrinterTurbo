"""One-time YouTube Data API publishing setup.

Writes youtube_upload_* credentials into config.toml with values
masked in output, then optionally runs the OAuth authorize flow.
"""

import argparse
import os
import re
import sys

# app 根下的这一份与仓库根的那份指向同一个 config.toml，但路径基准不同：
# 这里 __file__ 是 <app>/tools/setup_youtube.py，所以 ".." 已经是 app 根，
# 再拼一层 "MoneyPrinterTurbo" 会指向 <app>/MoneyPrinterTurbo/config.toml
# 这个不存在的嵌套目录。之前两份副本的这段代码完全相同，于是 vendored 副本
# 从一开始就是坏的。
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.toml")
CHANNEL = "youtube_upload"
KEYS = (
    "youtube_upload_client_id",
    "youtube_upload_client_secret",
    "youtube_upload_channel_id",
    "youtube_upload_privacy_status",
    "youtube_upload_category_id",
)
MASK_CHARS = 4


def mask(value: str) -> str:
    if len(value) <= MASK_CHARS:
        return "*" * len(value)
    return value[:MASK_CHARS] + "…" + "*" * (len(value) - MASK_CHARS)


def load_config() -> str:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def save_config(content: str) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        handle.write(content)


def set_value(content: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}[ \t]*=[ \t]*.*$", re.MULTILINE)
    rendered = f"{key} = {value}"
    if pattern.search(content):
        return pattern.sub(rendered, content, count=1)
    return content + "\n" + rendered


def show(content: str) -> None:
    for key in KEYS:
        match = re.search(
            rf"^{re.escape(key)}[ \t]*=[ \t]*(.*)$", content, re.MULTILINE
        )
        if not match:
            print(f"{key}: (not present)")
            continue
        raw = match.group(1).strip()
        value = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        shown = mask(value) if key.endswith(("client_id", "client_secret")) else value
        print(f"{key}: {shown}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="run the browser OAuth flow after saving credentials",
    )
    parser.add_argument(
        "--disable",
        action="store_true",
        help="disable YouTube publishing and clear stored credentials",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current youtube_upload_* settings (secrets masked)",
    )
    parser.add_argument(
        "--niche",
        default="history-mysteries",
        help=(
            "which niche this authorization belongs to, e.g. money-finance. "
            "A YouTube OAuth token is permanently bound to one channel, so run "
            "this once per niche and pick that niche's channel on Google's "
            "consent screen."
        ),
    )
    parser.add_argument(
        "--list-channels",
        action="store_true",
        help="list every YouTube channel this Google account can publish to",
    )
    args = parser.parse_args()

    # app 包在 MoneyPrinterTurbo/ 下，而不是仓库根目录。
    app_path = os.path.dirname(CONFIG_PATH)
    if app_path not in sys.path:
        sys.path.insert(0, app_path)

    if args.list_channels:
        from app.services import youtube_upload  # noqa: PLC0415

        result = youtube_upload.list_my_channels()
        if not result.get("success"):
            print(result.get("error"))
            return 1
        for item in result.get("channels", []):
            print(f"  {item['channel_id']}  {item['channel_name']}")
        return 0

    content = load_config()

    if args.disable:
        cleaned = load_config()
        for key in KEYS:
            cleaned = set_value(cleaned, key, '""')
        save_config(cleaned)
        print("YouTube publishing disabled; credentials cleared.")
        return 0

    if args.show:
        show(content)
        return 0

    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET environment variables "
            "are required (setup_youtube.sh prompts for them)"
        )
        return 2

    escaped_id = client_id.replace("\\", "\\\\").replace('"', '\\"')
    escaped_secret = client_secret.replace("\\", "\\\\").replace('"', '\\"')
    content = load_config()
    for key, value in (
        ("youtube_upload_client_id", f'"{escaped_id}"'),
        ("youtube_upload_client_secret", f'"{escaped_secret}"'),
    ):
        content = set_value(content, key, value)
    save_config(content)
    print("Saved OAuth client credentials")
    print(f"client_id:     {mask(client_id)}")
    print(f"client_secret: {mask(client_secret)}")

    if args.authorize:
        from app.services import youtube_upload  # noqa: PLC0415

        print(f"Opening the Google authorization page for niche {args.niche!r}…")
        print("On the consent screen pick the channel that matches this niche.")
        result = youtube_upload.authorize(args.niche)
        if not result.get("success"):
            print(f"Authorization failed: {result.get('error', 'unknown error')}")
            return 1
        info = youtube_upload.channel_info(args.niche)
        if info.get("success"):
            print(
                f"Niche {args.niche!r} -> channel {info.get('channel_name')} "
                f"({info.get('channel_id')})"
            )
            if info.get("channel_mismatch"):
                print(
                    "WARNING: this token is not bound to the channel configured "
                    "in youtube_upload_channel_id. Publishing will be blocked "
                    "until it matches."
                )
        else:
            print(f"Token saved, but channel lookup failed: {info.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())