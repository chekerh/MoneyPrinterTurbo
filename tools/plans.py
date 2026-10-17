"""Read and update the per-channel content plans used by the WebUI scheduler.

Plan layout (portfolio root, NOT the application root):
  plans/<channel>/topics_30day.txt       one topic per line
  plans/<channel>/automation.env          KEY=value generation overrides
  plans/<channel>/system_prompt.txt       script system prompt
  plans/<channel>/channel_description.md  upload description
  outputs/<channel>/<YYYY-MM>/monthly-plan.md   recorded status table
  .daily_topic_index.<channel>            last generated topic index

The portfolio root is a *different tree* from the application root, and this
module is imported both by the WebUI (which has config.toml available) and by
the shell pipeline (which runs it as a bare script). So the root is resolved
explicitly — MPT_PORTFOLIO_ROOT, then config.toml, then a directory that
actually holds a plans/ folder — and never from where this file happens to sit.

Deriving the root from ``__file__`` used to work by accident: this file lived at
<portfolio>/tools/plans.py, so ``dirname(dirname(__file__))`` *was* the
portfolio root. Vendoring tools/ into the application repository (commit 51ac3b4)
silently repointed that guess at the application root, which has no plans/ — so
``list_channels()`` returned [] and the WebUI's plan and media-library panels
rendered as empty expanders with no error anywhere. A positional guess cannot
distinguish "moved the file" from "moved the data"; explicit configuration can.
See :func:`resolve_portfolio_root`.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta

#: Overrides the configured portfolio root for a single run, matching the
#: variable ``app.services.project_layout`` already honours.
PORTFOLIO_ROOT_ENV_VAR = "MPT_PORTFOLIO_ROOT"


def _looks_like_portfolio_root(directory: str) -> bool:
    """A portfolio root is a directory that actually holds a ``plans/`` folder.

    Anchoring on real content rather than on a fixed number of ``dirname``
    levels is what makes this survive the file being moved between trees.
    """
    return os.path.isdir(os.path.join(directory, "plans"))


def _config_candidates(base_dir: str) -> list[str]:
    """Where config.toml may live, nearest first.

    ``base_dir`` is this file's own parent-of-parent. When tools/ is vendored
    inside the application repository that is the application root and the
    config sits right there; when tools/ still sits in the portfolio root the
    application is the nested MoneyPrinterTurbo/ directory.
    """
    candidates = [
        os.path.join(base_dir, "config.toml"),
        os.path.join(base_dir, "MoneyPrinterTurbo", "config.toml"),
    ]

    def _depth(path: str) -> int:
        return len(os.path.normpath(path).split(os.sep))

    return sorted(dict.fromkeys(candidates), key=_depth)


def _portfolio_root_from_config(base_dir: str) -> str:
    """Read ``[app] portfolio_root`` from the first config.toml that has one.

    Uses stdlib ``tomllib`` rather than the app's ``toml`` package so this keeps
    working when invoked by the shell pipeline against a bare interpreter. Any
    failure — missing module, missing file, unparseable TOML, absent key — means
    "not configured here", and the caller keeps looking.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        return ""

    for config_path in _config_candidates(base_dir):
        try:
            with open(config_path, "rb") as handle:
                data = tomllib.load(handle)
        except (OSError, ValueError):
            continue
        app_section = data.get("app")
        if not isinstance(app_section, dict):
            continue
        value = app_section.get("portfolio_root")
        if isinstance(value, str) and value.strip():
            return os.path.expanduser(value.strip())
    return ""


def resolve_portfolio_root() -> str:
    """Resolve the directory that owns ``plans/`` and ``outputs/``.

    Precedence, most explicit first:

    1. ``MPT_PORTFOLIO_ROOT`` — a deliberate per-run override.
    2. ``[app] portfolio_root`` in config.toml — the declared setting.
    3. A directory that actually contains ``plans/``, searched outward from this
       file. This keeps a bare ``python tools/plans.py`` working on a checkout
       with no config, and is content-anchored rather than level-counted so
       moving this file cannot silently repoint it at the wrong tree.

    An explicitly configured root is returned even when it has no ``plans/``
    folder: that is a real configuration the user must see take effect, not
    something to paper over by guessing somewhere else. Only the search in (3)
    can fall through.
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    configured = os.environ.get(PORTFOLIO_ROOT_ENV_VAR, "").strip()
    if configured:
        return os.path.expanduser(configured)

    configured = _portfolio_root_from_config(base_dir)
    if configured:
        return configured

    current = base_dir
    while True:
        if _looks_like_portfolio_root(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            # Nothing configured and nothing found. Return the positional guess
            # so error messages still name a concrete path, and list_channels()
            # returns [] exactly as it did before this change.
            return base_dir
        current = parent


BASE_DIR = resolve_portfolio_root()
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


def _current_period() -> str:
    """
    当前月份，``YYYY-MM``。

    单独抽出来是为了可测：跨月行为是本模块最容易回归的地方（``current_plan_dir``
    曾经返回"最新的已有月份"而不是当前月份），而 ``datetime`` 是 C 类型，直接
    打桩 ``datetime.now`` 并不总是可行。
    """
    return datetime.now().strftime("%Y-%m")


def current_plan_dir(channel: str) -> str:
    """
    本月计划目录：``outputs/<channel>/<YYYY-MM>``，按**当前日期**取值。

    早先这里返回"磁盘上最新的一个已有月份"，于是每次跨月都会出错：上个月已经
    建好 2026-09，而命令行流水线 ``archive_video.sh`` 用的是
    ``$(date +%Y-%m)``，也就是 2026-10。WebUI 继续往 9 月写，命令行往 10 月写，
    同一批视频被拆进两个月度计划里。

    更糟的是配合"主题已录制就跳过"的归档逻辑：10 月的空计划里所有行都是
    pending，``first_pending_topic`` 于是反复返回 #1；而 ``_plan_topic_recorded``
    跨所有月份扫描，发现 #1 在 9 月已录制，就把新成片静默丢弃。按下
    "生成今日视频"要花两个多小时，成片渲完就被扔掉，且没有任何提示。

    所以这里必须跟命令行一样按"今天是哪个月"取，而不是按"磁盘上有什么月"。
    需要读历史月份的地方走 :func:`_monthly_plan_dirs`，它本来就遍历全部月份。
    """
    root = _outputs_channel_dir(channel)
    target = os.path.join(root, _current_period())
    if not os.path.isdir(target):
        os.makedirs(target, exist_ok=True)
    return target


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


def recorded_topics(channel: str) -> set[str]:
    """A channel's already-recorded topics, normalised, across **every** month.

    Keyed on the topic **text**, not the row number. The monthly plan file resets
    each month while ``topics_30day.txt`` is one rolling 30-topic list, so index 1
    means the same thing in October as in September *for a channel whose topics did
    not change*. A channel that rewrites its topic list breaks that assumption, and
    then index 1 in the new month is a different topic entirely. Matching on text
    is correct in both cases.

    Text is also what :func:`iter_all_plan_rows` exposes and what the WebUI's
    duplicate check compares, so offering and archiving finally look at the same
    key. They did not before, and that mismatch is what made the daily button
    render videos the archiver then threw away.
    """
    recorded: set[str] = set()
    for _plan_dir, row in iter_all_plan_rows(channel):
        if row["recorded"]:
            topic = str(row["topic"] or "").strip().lower()
            if topic:
                recorded.add(topic)
    return recorded


def first_pending_topic(
    channel: str, skip_recent_days: int | None = None
) -> tuple[int, str] | None:
    """Return the first topic of this channel that has not been recorded yet.

    The check spans every month on purpose. It used to read only the current
    month's plan file, and that produced a dated money leak: on the first of each
    month the plan resets to all-pending, so the daily button offered topic #1
    again even though #1 had been made the month before. A full render then ran,
    and the archiver discarded the result because *its* duplicate check already
    scanned all months. The button never advanced, so this repeated every single
    day of the month.

    A topic recorded in any month is done, and is never offered again. When every
    topic has been made this returns ``None`` so the caller can say so, rather
    than quietly re-rendering one and calling it progress.

    With skip_recent_days set, topics whose slug was already produced in that
    many days are skipped, returning the next pending non-duplicate topic. That is
    a separate "not too soon" heuristic and only ranks topics that are not already
    recorded. With skip_recent_days None/0, behavior is unchanged.
    """
    topics = load_topics(channel)
    recorded = recorded_topics(channel)
    recent = recent_topic_slugs(channel, skip_recent_days) if skip_recent_days else None
    first_pending = None
    for index, topic in enumerate(topics, start=1):
        if str(topic or "").strip().lower() in recorded:
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