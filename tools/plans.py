"""Read and update the per-channel content plans used by the WebUI scheduler.

Plan layout (repo root):
  plans/<channel>/topics_30day.txt       one topic per line
  plans/<channel>/automation.env          KEY=value generation overrides
  plans/<channel>/system_prompt.txt       script system prompt
  plans/<channel>/channel_description.md  upload description
  outputs/<channel>/<YYYY-MM>/monthly-plan.md   recorded status table
  .daily_topic_index.<channel>            last generated topic index
"""

import os
import re
import sys
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PLANS_DIR = os.path.join(BASE_DIR, "plans")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")

_TOPIC_LINE = re.compile(r"^\s*(?:(\d+)\s*[\.\)]\s*)?(.+?)\s*$")
_PLAN_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
_MD_LINK = re.compile(r"\]\(([^)]+)\)")
_POSTED = re.compile(r"posted:\s*(https?://\S+)")

RECORDED = ("✓", "✔", "done", "recorded")


def _channel_dir(channel: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(channel).strip()) or "default"
    return os.path.join(PLANS_DIR, safe)


def _outputs_channel_dir(channel: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(channel).strip()) or "default"
    return os.path.join(OUTPUTS_DIR, safe)


def list_channels() -> list[str]:
    if not os.path.isdir(PLANS_DIR):
        return []
    channels = []
    for name in sorted(os.listdir(PLANS_DIR)):
        if os.path.isfile(os.path.join(PLANS_DIR, name, "topics_30day.txt")):
            channels.append(name)
    return channels


def load_topics(channel: str) -> list[str]:
    path = os.path.join(_channel_dir(channel), "topics_30day.txt")
    if not os.path.isfile(path):
        return []
    topics = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _TOPIC_LINE.match(line)
            if match:
                topics.append(match.group(2).strip())
    return topics


def current_plan_dir(channel: str) -> str:
    root = _outputs_channel_dir(channel)
    if not os.path.isdir(root):
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, datetime.now().strftime("%Y-%m"))
    periods = [
        name
        for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name)) and len(name) == 7 and name[4] == "-"
    ]
    if not periods:
        return os.path.join(root, datetime.now().strftime("%Y-%m"))
    periods.sort(reverse=True)
    return os.path.join(root, periods[0])


def load_monthly_plan(channel: str) -> list[dict]:
    path = os.path.join(current_plan_dir(channel), "monthly-plan.md")
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            match = _PLAN_ROW.match(line.strip())
            if not match:
                continue
            status = match.group(3).strip()
            posted = ""
            posted_match = _POSTED.search(status)
            if posted_match:
                posted = posted_match.group(1)
            video_rel = ""
            video_match = _MD_LINK.search(status)
            if video_match:
                video_rel = video_match.group(1)
            rows.append(
                {
                    "index": int(match.group(1)),
                    "topic": match.group(2).strip(),
                    "status": status,
                    "recorded": status.lower().startswith(RECORDED),
                    "video": video_rel,
                    "posted": posted,
                }
            )
    return rows


def find_video_path(channel: str, index: int) -> str:
    plan_dir = current_plan_dir(channel)
    rows = load_monthly_plan(channel)
    for row in rows:
        if row["index"] == index and row["video"]:
            video_abs = os.path.join(plan_dir, row["video"])
            if os.path.isfile(video_abs):
                return video_abs
    return ""


def _monthly_plan_dirs(channel: str) -> list[str]:
    """All YYYY-MM plan dirs for a channel that contain monthly-plan.md, newest first."""
    root = _outputs_channel_dir(channel)
    dirs = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            candidate = os.path.join(root, name)
            if (
                len(name) == 7
                and name[4] == "-"
                and name[:4].isdigit()
                and name[5:].isdigit()
                and os.path.isfile(os.path.join(candidate, "monthly-plan.md"))
            ):
                dirs.append(candidate)
    dirs.sort(reverse=True)
    return dirs


def iter_all_plan_rows(channel: str):
    """Yield (plan_dir, row) for every month's monthly-plan.md, newest month first.

    Lets callers match an archived video against the plan row that originally
    recorded it, even after the channel has moved on to a new month.
    """
    for plan_dir in _monthly_plan_dirs(channel):
        path = os.path.join(plan_dir, "monthly-plan.md")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                match = _PLAN_ROW.match(line.strip())
                if not match:
                    continue
                status = match.group(3).strip()
                posted = ""
                posted_match = _POSTED.search(status)
                if posted_match:
                    posted = posted_match.group(1)
                video_rel = ""
                video_match = _MD_LINK.search(status)
                if video_match:
                    video_rel = video_match.group(1)
                yield plan_dir, {
                    "index": int(match.group(1)),
                    "topic": match.group(2).strip(),
                    "status": status,
                    "recorded": status.lower().startswith(RECORDED),
                    "video": video_rel,
                    "posted": posted,
                }


def find_plan_row_by_video(channel: str, video_basename: str):
    """Return (plan_dir, row) for the plan that archived video_basename, or None."""
    for plan_dir, row in iter_all_plan_rows(channel):
        if row["video"] and os.path.basename(row["video"]) == video_basename:
            return plan_dir, row
    return None


def build_video_row_index(channel: str) -> dict:
    """One-pass map of video basename -> (plan_dir, row) across every month.

    Newest month wins for duplicate names. Use this instead of calling
    find_plan_row_by_video in a loop — the latter re-scans the plan files on
    every call, which is O(n*m) once a channel has archived many months.
    """
    index = {}
    for plan_dir, row in iter_all_plan_rows(channel):
        if not row["video"]:
            continue
        key = os.path.basename(row["video"])
        if key not in index:
            index[key] = (plan_dir, row)
    return index


def _topic_slug(topic: str) -> str:
    """Normalize a topic for duplicate comparison: lowercase, stripped, collapsed."""
    return re.sub(r"\s+", " ", str(topic).strip()).lower()


def _recent_usage_dates(channel: str, days: int = 60) -> dict[str, date]:
    """Map normalized topic slug -> latest usage date in the recent window.

    Only archived videos whose filename embeds a YYYY-MM-DD date within the
    last `days` days count as recent. Topics still pending in the current plan
    have no archived video yet and are excluded.
    """
    cutoff = datetime.now().date() - timedelta(days=days)
    last_used: dict[str, date] = {}
    for video_name, (_plan_dir, row) in build_video_row_index(channel).items():
        entry = _VIDEO_ENTRY_NAME.match(video_name)
        if not entry or not row["topic"]:
            continue
        used = datetime.strptime(entry.group(2), "%Y-%m-%d").date()
        if used < cutoff:
            continue
        slug = _topic_slug(row["topic"])
        if slug not in last_used or used > last_used[slug]:
            last_used[slug] = used
    return last_used


def recent_topic_slugs(channel: str, days: int = 60) -> set[str]:
    """Topic slugs already produced in the last `days` days across all months.

    Used by first_pending_topic(skip_recent_days=...) and the
    check-duplicates CLI to avoid scheduling the same topic twice.
    """
    return set(_recent_usage_dates(channel, days))


def find_recent_duplicates(channel: str, days: int = 60) -> list[dict]:
    """Current-month pending topics that duplicate a recent topic.

    Returns {index, topic, last_used} in plan order so the CLI can report which
    pending topic repeats already-produced content and when it last ran.
    """
    recent = _recent_usage_dates(channel, days)
    duplicates = []
    for row in load_monthly_plan(channel):
        if row["recorded"]:
            continue
        slug = _topic_slug(row["topic"])
        if slug in recent:
            duplicates.append(
                {
                    "index": row["index"],
                    "topic": row["topic"],
                    "last_used": recent[slug].isoformat(),
                }
            )
    return duplicates


def first_pending_topic(
    channel: str, skip_recent_days: int | None = None
) -> tuple[int, str] | None:
    """Return the first unposted topic of the current month's plan.

    With skip_recent_days set, topics whose slug was already produced in that
    many days are skipped, returning the next pending non-duplicate topic. If
    every pending topic is a recent duplicate, the first pending one is
    returned as a fallback so the scheduler still makes progress. With
    skip_recent_days None/0, behavior is unchanged.
    """
    topics = load_topics(channel)
    rows = {row["index"]: row for row in load_monthly_plan(channel)}
    recent = recent_topic_slugs(channel, skip_recent_days) if skip_recent_days else None
    first_pending = None
    for index, topic in enumerate(topics, start=1):
        row = rows.get(index)
        if row and row["recorded"]:
            continue
        if first_pending is None:
            first_pending = (index, topic)
        if recent is None or _topic_slug(topic) not in recent:
            return index, topic
    return first_pending


def plan_row_status(channel: str, index: int) -> dict | None:
    for row in load_monthly_plan(channel):
        if row["index"] == index:
            return row
    return None


def mark_posted(channel: str, index: int, url: str, plan_dir: str | None = None) -> bool:
    if not url:
        return False
    target_dir = plan_dir if plan_dir else current_plan_dir(channel)
    path = os.path.join(target_dir, "monthly-plan.md")
    if not os.path.isfile(path):
        return False
    updated_lines = []
    changed = False
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            match = _PLAN_ROW.match(stripped)
            if not match or int(match.group(1)) != index:
                updated_lines.append(line)
                continue
            status = match.group(3).strip()
            if _POSTED.search(status):
                status = _POSTED.sub(f"posted: {url}", status)
            else:
                status = f"{status} - posted: {url}"
            updated_lines.append(
                f"| {match.group(1)} | {match.group(2).strip()} | {status} |\n"
            )
            changed = True
    if not changed:
        return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(updated_lines)
    return True


def read_automation_env(channel: str) -> dict:
    path = os.path.join(_channel_dir(channel), "automation.env")
    values = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_file_text(channel: str, *parts: str) -> str:
    path = os.path.join(_channel_dir(channel), *parts)
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


_VIDEO_ENTRY_NAME = re.compile(
    r"^(?:(\d{1,2})-)?(\d{4}-\d{2}-\d{2})-(?P<topic>.+?)\.mp4$",
    re.IGNORECASE,
)


def list_channel_videos(channel: str, available_only: bool = True) -> list[dict]:
    """Scan archived generated videos under outputs/<channel>/**/videos/*.mp4.

    Each entry carries the absolute path and, when the filename follows the
    archived naming convention (NN-YYYY-MM-DD-topic.mp4), the parsed date and
    topic. Old periods are returned too, newest file first.
    """
    root = _outputs_channel_dir(channel)
    entries = []
    if not os.path.isdir(root):
        return entries
    matched = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(os.path.normpath(dirpath)) != "videos":
            continue
        for name in filenames:
            if not name.lower().endswith(".mp4"):
                continue
            matched.append(os.path.join(dirpath, name))
    for path in matched:
        if available_only and not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        date = ""
        topic = ""
        entry_match = _VIDEO_ENTRY_NAME.match(name)
        if entry_match:
            date = entry_match.group(2)
            topic = entry_match.group("topic").strip()
        if not date:
            date = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        entries.append({"path": path, "name": name, "date": date, "topic": topic})
    entries.sort(key=lambda entry: os.path.getmtime(entry["path"]), reverse=True)
    return entries


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns an exit code so tests can call it directly."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(
            "usage: plans.py <pending|mark-posted|check-duplicates> <channel>"
            " [index url]",
            file=sys.stderr,
        )
        return 2
    command = args[0]
    channel = args[1]
    if command == "pending":
        pending = first_pending_topic(channel)
        if pending:
            print("%d\t%s" % (pending[0], pending[1]))
        return 0
    if command == "mark-posted":
        if len(args) < 4 or not args[2].isdigit():
            print("usage: plans.py mark-posted <channel> <index> <url>", file=sys.stderr)
            return 2
        mark_posted(channel, int(args[2]), args[3])
        return 0
    if command in ("check-duplicates", "--check-duplicates"):
        duplicates = find_recent_duplicates(channel)
        if not duplicates:
            print("no duplicate topics")
            return 0
        for dup in duplicates:
            print(
                "#%d\t%s\tlast used: %s" % (dup["index"], dup["topic"], dup["last_used"])
            )
        return 1
    print("unknown command: %s" % command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())