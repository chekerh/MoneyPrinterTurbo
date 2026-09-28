import argparse
import os
import tomllib
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "MoneyPrinterTurbo" / "config.toml"

DEFAULT_PLATFORMS = 'upload_post_platforms = [ "tiktok", "instagram", "youtube",]'


def replace_lines(lines, key, replacement):
    prefix = key + " ="
    for i, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[i] = replacement
            return True
    return False


def write_key(lines, key, value):
    return replace_lines(lines, key, f'{key} = "{value}"')


def write_bool(lines, key, value):
    return replace_lines(lines, key, f"{key} = {str(value).lower()}")


def ensure_key(lines, key, default):
    if not replace_lines(lines, key, default):
        lines.append(default)


def status(disable: bool = False) -> None:
    with open(CONFIG, "rb") as handle:
        app = tomllib.load(handle)["app"]
    key = app.get("upload_post_api_key", "")
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else ("set" if key else "")
    print(f"enabled: {app.get('upload_post_enabled')}")
    print(f"auto_upload: {app.get('upload_post_auto_upload')}")
    print(f"username: {app.get('upload_post_username')}")
    print(f"api_key: {masked or 'NOT SET'}")
    print(f"platforms: {', '.join(app.get('upload_post_platforms', []))}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()

    lines = CONFIG.read_text(encoding="utf-8").splitlines()

    if args.disable:
        write_key(lines, "upload_post_api_key", "")
        write_key(lines, "upload_post_username", "")
        write_bool(lines, "upload_post_enabled", False)
        write_bool(lines, "upload_post_auto_upload", False)
    else:
        api_key = os.environ.get("UPLOAD_POST_API_KEY", "").strip()
        username = os.environ.get("UPLOAD_POST_USERNAME", "").strip()
        if not api_key or not username:
            parser.error(
                "set UPLOAD_POST_API_KEY and UPLOAD_POST_USERNAME (or use --disable)"
            )
        write_key(lines, "upload_post_api_key", api_key)
        write_key(lines, "upload_post_username", username)
        write_bool(lines, "upload_post_enabled", True)
        write_bool(lines, "upload_post_auto_upload", True)
        ensure_key(lines, "upload_post_platforms", DEFAULT_PLATFORMS)

    CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    status(disable=args.disable)


if __name__ == "__main__":
    main()