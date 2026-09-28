"""One-time YouTube Data API publishing setup.

Writes youtube_upload_* credentials into config.toml with values
masked in output, then optionally runs the OAuth authorize flow.
"""

import argparse
import os
import re

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "MoneyPrinterTurbo", "config.toml"
)
CHANNEL = "youtube_upload"
KEYS = (
    "youtube_upload_enabled",
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
    args = parser.parse_args()

    content = load_config()

    if args.disable:
        cleaned = load_config()
        for key in KEYS:
            if key in ("youtube_upload_client_id", "youtube_upload_client_secret"):
                cleaned = set_value(cleaned, key, '""')
            elif key == "youtube_upload_enabled":
                cleaned = set_value(cleaned, key, "false")
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
        ("youtube_upload_enabled", "true"),
        ("youtube_upload_client_id", f'"{escaped_id}"'),
        ("youtube_upload_client_secret", f'"{escaped_secret}"'),
    ):
        content = set_value(content, key, value)
    save_config(content)
    print("Saved credentials: enabled=true")
    print(f"client_id:     {mask(client_id)}")
    print(f"client_secret: {mask(client_secret)}")

    if args.authorize:
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.services import youtube_upload  # noqa: PLC0415

        print("Opening the Google authorization page in your browser…")
        result = youtube_upload.authorize()
        if not result.get("success"):
            print(f"Authorization failed: {result.get('error', 'unknown error')}")
            return 1
        info = youtube_upload.channel_info()
        if info.get("success"):
            print(
                f"Linked channel: {info.get('channel_name')} "
                f"({info.get('channel_id')})"
            )
        else:
            print(f"Token saved, but channel lookup failed: {info.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())