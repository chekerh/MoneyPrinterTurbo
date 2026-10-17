"""Publish one finished video to a niche's connected platforms.

Used by daily_youtube_short.sh (launchd, 08:00) so that scheduled publishing
never touches an interactive session. Prints a single JSON object so the shell
script can parse the top-level success field.
"""

import argparse
import json
import os
import sys


def _app_root() -> str:
    """定位含 ``app`` 包的目录。

    这个脚本在仓库里有两份副本，app 包与它们的关系有两种：

    * vendored：``<app>/tools/publish_video.py`` —— app 包就在上一级；
    * 仓库根：``<repo>/tools/publish_video.py`` —— app 包在**子目录**
      ``<repo>/MoneyPrinterTurbo/`` 下，而那一层不是 ``tools/`` 的祖先。

    所以写死 ``../MoneyPrinterTurbo`` 必然有一处是错的：那份路径在 vendored
    位置上指向 ``<app>/MoneyPrinterTurbo`` 这个不存在的目录，报
    ``ModuleNotFoundError: No module named 'app'``。只沿祖先找也一样漏掉
    仓库根那一种。

    于是对每一级祖先都试两个已知布局，找真正含 ``app/services`` 的那一层。
    不猜层数，也不写死相对路径。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    current = here
    while True:
        for candidate in (current, os.path.join(current, "MoneyPrinterTurbo")):
            if os.path.isdir(os.path.join(candidate, "app", "services")):
                return candidate
        parent = os.path.dirname(current)
        if parent == current:  # 到根了
            break
        current = parent
    # 找不到就给出可操作的提示，而不是裸 ModuleNotFoundError。
    sys.stderr.write(
        f"cannot locate the 'app' package from {here}; "
        "expected app/services under this directory or a "
        "MoneyPrinterTurbo/ subdirectory of one of its ancestors\n"
    )
    raise SystemExit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True, help="niche to publish for")
    parser.add_argument("--video", required=True, help="path to the mp4")
    parser.add_argument("--title", default=None, help="override saved title")
    parser.add_argument("--description", default=None, help="override saved description")
    parser.add_argument("--tags", default=None, help="override saved hashtags (comma separated)")
    parser.add_argument(
        "--privacy",
        default="",
        help="public|unlisted|private (default: youtube_upload_privacy_status)",
    )
    parser.add_argument("--thumbnail", default="")
    parser.add_argument("--platforms", default="youtube", help="comma separated platform ids")
    parser.add_argument("--automatic", action="store_true", help="respect automatic publishing preferences")
    args = parser.parse_args()

    app_path = _app_root()
    if app_path not in sys.path:
        sys.path.insert(0, app_path)

    from app.config import config
    from app.services import posting_package, publishing

    if args.automatic and not bool(config.app.get("youtube_upload_auto_publish", True)):
        print(json.dumps({"success": False, "skipped": True, "error": "YouTube automatic publishing is disabled."}))
        return 0

    try:
        package = posting_package.load_for_video(args.video, args.channel, args.title or "")
        meta = publishing.PublishMeta(
            title=args.title if args.title is not None else package["title"],
            description=args.description if args.description is not None else package["caption"],
            tags=([t.strip() for t in args.tags.split(",") if t.strip()]
                  if args.tags is not None else package["hashtags"]),
            privacy_status=args.privacy or str(config.app.get("youtube_upload_privacy_status", "public") or "public"),
            thumbnail_path=args.thumbnail,
            caption=package["caption"] + "\n\n" + " ".join(package["hashtags"]),
        )
        result = publishing.publish_once(
            channel=args.channel, video_path=args.video, meta=meta,
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
        )
    except (OSError, ValueError) as exc:
        result = {"success": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())