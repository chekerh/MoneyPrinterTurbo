"""CLI for cutting viral spots out of a long-form video.

Scans the source video for scene changes and silence, scores candidate 30s
windows by a light-weight virality heuristic (speech activity + visual
variety + hook power), and writes the top non-overlapping windows as
standalone shorts.

Usage:
  python tools/shorts_cutter.py path/to/long.mp4 -o outputs/shorts
"""

import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "MoneyPrinterTurbo"))

from app.services import shorts_cutter  # noqa: E402, PLC0415


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="path to the source long-form video")
    parser.add_argument(
        "-o", "--output-dir", default="shorts", help="directory for generated shorts"
    )
    parser.add_argument(
        "-n", "--max-shorts", type=int, default=5, help="maximum number of shorts"
    )
    parser.add_argument(
        "-w",
        "--window-seconds",
        type=float,
        default=30.0,
        help="target short duration in seconds",
    )
    parser.add_argument(
        "--min-speech-ratio",
        type=float,
        default=0.55,
        help="drop windows whose non-silent audio fraction is below this",
    )
    parser.add_argument(
        "--min-gap-seconds",
        type=float,
        default=1.0,
        help="minimum gap between selected shorts in seconds",
    )
    parser.add_argument(
        "--no-vertical",
        action="store_true",
        help="keep original aspect ratio instead of 9:16 crop",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the selection report as JSON",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.video):
        parser.error(f"video not found: {args.video}")

    results = shorts_cutter.cut_viral_shorts(
        args.video,
        args.output_dir,
        window_seconds=args.window_seconds,
        max_shorts=args.max_shorts,
        min_speech_ratio=args.min_speech_ratio,
        min_gap_seconds=args.min_gap_seconds,
        vertical=not args.no_vertical,
    )
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for item in results:
            print(
                f"[{item['start']:>7.2f}-{item['end']:<7.2f}s] "
                f"score={item['score']:>5.1f} speech={item['speech_ratio']:.2f} "
                f"-> {os.path.basename(item['file'])}"
            )


if __name__ == "__main__":
    main()