import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

# 与 Main.py 一样使用裸 `import plans`（tools 目录在 sys.path）。两处必须解析同一个
# 主题文件格式，直接复用 plans 的解析正则，避免话题文件出现编号前缀时各自分歧。
import plans  # noqa: E402

# 归档失败时抛出，调用方据此放弃本次归档并保留重试机会。
class ArchiveError(RuntimeError):
    """归档未能完整落盘时抛出。

    单独用异常而不是返回空串，是因为「没有归档」和「归档了一个坏文件」在
    调用方看来必须可区分：前者可以安全重试，后者会写坏月度计划且无法回退。
    """


# ffprobe 读取视频首帧需要解析容器结构。归档由 WebUI 的 2 秒轮询 fragment 触发
# （``_auto_archive_plan_tasks``），这里把超时压到 5 秒——正常短视频在亚秒级返回，
# 超过 5 秒基本是文件损坏或 ffprobe 异常，直接视为「无法解析」。
_PROBE_TIMEOUT_SECONDS = 5


def slugify(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return slug[:40].rstrip("-")


def load_topics(plans_root: Path, channel: str) -> list:
    topics_file = plans_root / channel / "topics_30day.txt"
    if not topics_file.exists():
        return []
    topics = []
    for line in topics_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = plans._TOPIC_LINE.match(stripped)
        if match:
            topics.append(match.group(2).strip())
    return topics


def plan_index(topics: list, topic: str):
    for i, line in enumerate(topics, 1):
        if line == topic.strip():
            return i
    return None


def render_txt(data: dict, topic: str, base: str, duration: str, task_id: str) -> str:
    params = data.get("params", {})
    script = data.get("script", "")
    terms = data.get("search_terms", [])
    sources = data.get("material_sources", [])

    lines = [
        f"topic: {topic}",
        f"recorded: {date.today().isoformat()}",
        f"video: videos/{base}.mp4",
        f"subtitle: videos/{base}.srt",
        f"duration: {duration}s",
        f"aspect: {params.get('video_aspect', '')}",
        f"voice: {params.get('voice_name', '')}",
        f"voice_rate: {params.get('voice_rate', '')}",
        f"subtitle_position: {params.get('subtitle_position', 'bottom')}",
        f"task_id: {task_id}",
        "",
        "script:",
        script,
        "",
        "search terms:",
    ]
    lines.extend(f"- {t}" for t in terms)
    if sources:
        lines.append("")
        lines.append("sources:")
        for s in sources:
            creator = (s.get("creator") or {}).get("name", "unknown")
            page = s.get("source_page", "")
            term = s.get("search_term", "")
            lines.append(f"- {creator} ({term}) <{page}>")
    return "\n".join(lines)


def _existing_plan_state(out_dir: Path):
    """读取现有 ``monthly-plan.md``，返回按规范化话题索引的两张表。

    ``recorded_status``: 已录制行的原始状态（老记录可能没有 ``.txt`` 侧写，
    重写时原样保留，避免把已录制行退化回 pending）。
    ``posted_url``: 每个话题的 ``posted:`` 链接，重写后接回新状态，
    避免 plans.mark_posted 写入的发布记录在重新归档时丢失。
    """
    plan_file = out_dir / "monthly-plan.md"
    recorded_status = {}
    posted_url = {}
    if not plan_file.is_file():
        return recorded_status, posted_url
    for line in plan_file.read_text(encoding="utf-8").splitlines():
        match = plans._PLAN_ROW.match(line.strip())
        if not match:
            continue
        topic = match.group(2).strip()
        status = match.group(3).strip()
        if status.lower().startswith(plans.RECORDED):
            recorded_status[topic.strip().lower()] = status
        posted_match = plans._POSTED.search(status)
        if posted_match:
            posted_url[topic.strip().lower()] = posted_match.group(1)
    return recorded_status, posted_url


def _recorded_in_other_months(channel: str, out_dir: Path) -> dict[str, str]:
    """
    本月之外的已录制主题：``{序号: 状态串}``。

    状态串沿用本月的写法，但视频链接是跨目录的相对路径（``../2026-09/videos/...``），
    这样从本月的 monthly-plan.md 点过去仍然打得开。跨月找片是 ``find_video_path``
    与媒体库已经在做的事，这里只是让计划表别把做完的事重新标成 pending。
    """
    out_dir = Path(out_dir)
    carried: dict[str, str] = {}
    for plan_dir, row in plans.iter_all_plan_rows(channel):
        if not row["recorded"]:
            continue
        try:
            if Path(plan_dir) == out_dir:
                continue
        except OSError:
            continue
        topic = str(row["topic"] or "").strip().lower()
        if not topic or topic in carried:
            # iter_all_plan_rows 是新月优先，同一主题取第一次出现的即可。
            continue
        video = row.get("video") or ""
        if not video:
            continue
        month = os.path.basename(os.path.normpath(plan_dir))
        name = video.split("videos/")[-1]
        display = f"✓ {month} - [{name}](../{month}/videos/{name})"
        if row.get("posted"):
            display += f" - posted: {row['posted']}"
        carried[topic] = display
    return carried


def render_plan(channel: str, topics: list, out_dir: Path) -> str:
    vid_dir = out_dir / "videos"
    archived = {}
    for txt in sorted(vid_dir.glob("*.txt")):
        if txt.name.endswith(".posting.txt"):
            continue
        meta = {}
        for line in txt.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        if meta.get("topic"):
            archived[meta["topic"]] = meta

    previously_recorded, previously_posted = _existing_plan_state(out_dir)

    # 本月之外已经录过的主题也要带过来。topics_30day.txt 是一份 30 条的滚动列表，
    # 不是每月一份，所以 10 月的 #1 和 9 月的 #1 指的是同一条主题。若只读本月的
    # 计划文件，新月会把九月做完的十条重新标成 pending，进度退回 0/30，界面上的
    # 计划表也跟着空掉——用户会以为十月从头开始，成片却好好地躺在上个月。
    carried = _recorded_in_other_months(channel, out_dir)

    # 月份取自实际写入的目录，而不是 date.today()。补录往期成片时会写进上一个
    # 月的目录，此时表头写上今天就会让文件自相矛盾：目录说 9 月、标题说 10 月。
    month = os.path.basename(os.path.normpath(str(out_dir)))
    if not (len(month) == 7 and month[4] == "-"):
        month = date.today().strftime("%Y-%m")

    recorded_slugs = set(previously_recorded)
    done = sum(
        1
        for t in topics
        if t in archived
        or t.strip().lower() in recorded_slugs
        or t.strip().lower() in carried
    )
    lines = [
        f"# {channel} - monthly plan ({month})",
        "",
        f"{done}/{len(topics)} topics recorded",
        "",
        "| # | Topic | Status |",
        "|---|-------|--------|",
    ]
    for i, topic in enumerate(topics, 1):
        slug = topic.strip().lower()
        meta = archived.get(topic)
        if meta:
            link = meta.get("video", "")
            display = f"✓ {meta.get('recorded', '')} - [{link.split('videos/')[-1]}]({link})"
            if previously_posted.get(slug):
                display += f" - posted: {previously_posted[slug]}"
        elif previously_recorded.get(slug):
            display = previously_recorded[slug]
        elif slug in carried:
            display = carried[slug]
        else:
            display = "pending"
        lines.append(f"| {i} | {topic} | {display} |")

    unplanned = [t for t in archived if t not in set(topics)]
    if unplanned:
        lines.append("")
        lines.append("## Unplanned recordings")
        for t in unplanned:
            line = f"- {t} -> {archived[t].get('video', '')}"
            if previously_posted.get(t.strip().lower()):
                line += f" - posted: {previously_posted[t.strip().lower()]}"
            lines.append(line)
    return "\n".join(lines)


def probe_duration(video_path) -> str:
    """查询视频时长，返回 ``ffprobe`` 的纯文本输出；失败时返回空字符串。"""
    if not Path(video_path).exists():
        return ""
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip()


def probe_dimensions(video_path):
    """返回 ``(宽, 高)``；文件缺失、为空、损坏或 ffprobe 不可用时返回 ``None``。

    这是「成片是否真的写完」的唯一可靠判据。一个仍在写入的 ``final-1.mp4``
    可能已经有 ftyp 头但没有完整 mdat，尺寸解析会失败；一个 0 字节文件同理。
    只看文件名和 ``os.path.exists`` 会把两者都当成完成态。
    """
    path = Path(video_path)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return None
    except OSError:
        return None

    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS)
        width, height = (result.stdout or "").strip().split("x")[:2]
        width, height = int(width), int(height)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _copy_verified(src: Path, dst: Path) -> None:
    """把成片复制到 ``dst``，并保证复制结果与源文件逐字节一致。

    两个细节共同消除「复制到一半」的可见状态：

    1. 先复制到同目录下的临时文件，再 ``os.replace`` 原子改名。``os.replace``
       在同一文件系统内是原子的，因此 ``dst`` 要么不存在，要么就是完整文件，
       不会出现「存在但只写了一半」的中间态。
    2. 复制完成后比对大小。大小一致才认为成功；不一致说明源文件在复制过程中
       仍在被写入（ffmpeg 尚未退出），此时删除临时文件并让调用方重试。
    """
    src = Path(src)
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".partial")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

    try:
        shutil.copy2(src, tmp)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ArchiveError(f"failed to copy {src.name}: {exc}") from exc

    try:
        copied = tmp.stat().st_size
        source = src.stat().st_size
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ArchiveError(f"failed to stat copied archive: {exc}") from exc

    if copied != source or copied <= 0:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ArchiveError(
            f"incomplete copy of {src.name}: wrote {copied} of {source} bytes; "
            "the final video is probably still being written"
        )

    os.replace(tmp, dst)


def archive_channel_video(
    channel: str,
    task_dir: str,
    topic: str,
    out_dir: str,
    plans_root: str,
    duration: str = "",
    copy_media: bool = False,
    final_video_dir: str | None = None,
) -> str:
    """将一次生成任务归档到频道输出目录并重写月度计划，返回基础文件名。

    ``task_dir`` 内必须存在 ``script.json``。最终成片的位置由 ``final_video_dir``
    指定（``<outputs_root>/<task_id>/``）；为 ``None`` 时回退到
    ``<task_dir>/final-1.mp4``，供历史任务与命令行调用方使用。归档时写入
    ``videos/<base>.txt`` 并重新生成 ``<out_dir>/monthly-plan.md``。当
    ``copy_media`` 为真时，把成片与可选字幕一并复制进 ``videos`` 目录；
    命令行调用方(archive_video.sh)自行复制文件，因此保持默认不复制。

    顺序很重要：**先确认成片完整落盘，再写月度计划**。旧实现先写计划和边车、
    再复制成片，于是成片缺失时计划里仍会出现一条「已录制」记录，而复制中途
    失败还会把半截文件留在库里。现在任何一步失败都抛 :class:`ArchiveError`，
    且不会留下计划记录，调用方可以安全重试。
    """
    task_dir = Path(task_dir)
    out_dir = Path(out_dir)
    vid_dir = out_dir / "videos"
    # 边车 ``.txt`` 无论是否复制媒体都要写进这个目录，render_plan 也会 glob
    # 它，所以必须无条件创建。
    vid_dir.mkdir(parents=True, exist_ok=True)

    script_file = task_dir / "script.json"
    if not script_file.is_file():
        raise ArchiveError(f"task has no script.json: {task_dir}")
    try:
        data = json.loads(script_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"task script.json is unreadable: {task_dir}: {exc}") from exc

    # 成片的权威位置由调用方给出：最终成片是唯一长期留存的视频，从 b2b52cc 起
    # 写在作品集的 outputs/<task_id>/ 下，任务目录只留中间产物（可能还在内存盘
    # 上）。这里若仍旧只认 task_dir/final-1.mp4，copy_media 会永远因为"成片缺失"
    # 抛 ArchiveError，而 WebUI 那边只是 continue，于是归档静默失效：任务面板照常
    # 显示"已完成"，成片却永远进不了媒体库和月度计划。
    # 留 task_dir 作为兜底，是为了让 b2b52cc 之前的历史任务仍可归档。
    if final_video_dir is not None:
        src_video = Path(final_video_dir) / "final-1.mp4"
    else:
        src_video = task_dir / "final-1.mp4"

    if src_video.with_suffix(".publish.lock").exists():
        raise ArchiveError("video is being published; defer archiving")

    # 计划文件里的主题可能与实际生成主题不同（用户中途改过输入），以成片
    # 脚本里记录的主题为准，保证归档命名和月度计划指向同一条内容。
    topic = (data.get("params", {}).get("video_subject") or topic).strip()
    if not topic:
        raise ArchiveError(f"task has no resolvable topic: {task_dir}")

    if copy_media:
        # 复制之前先确认成片真的写完了。这一步同时挡住「文件刚被 ffmpeg 创建」
        # 和「上一轮归档把半截文件留在库里」两种情况。
        if probe_dimensions(src_video) is None:
            raise ArchiveError(
                f"final video is missing, empty or unparseable: {src_video}"
            )

    if not duration:
        duration = probe_duration(src_video)

    topics = load_topics(Path(plans_root), channel)
    idx = plan_index(topics, topic)
    if idx is None:
        idx = len(list(vid_dir.glob("*.mp4"))) + 1

    base = f"{idx:02d}-{date.today().isoformat()}-{slugify(topic)}"

    if copy_media:
        # _copy_verified 依赖同目录改名保证原子性，目录已在函数开头建好。
        _copy_verified(src_video, vid_dir / f"{base}.mp4")
        src_subtitle = task_dir / "subtitle.srt"
        if src_subtitle.is_file():
            try:
                shutil.copy2(src_subtitle, vid_dir / f"{base}.srt")
            except OSError as exc:
                # 字幕缺失不影响成片可用性，只记录不阻断。
                print(f"warning: failed to copy subtitle for {base}: {exc}")

    # Carry posting copy and successful upload receipts into the durable library.
    from publish_video import _app_root

    app_root = _app_root()
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    from app.services import posting_package

    if src_video.with_suffix(".publish.lock").exists():
        raise ArchiveError("video is being published; defer archiving")
    receipt = src_video.with_suffix(".publish.json")
    if receipt.exists():
        try:
            receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
            if receipt_data.get("pending"):
                raise ArchiveError("upload outcome is unknown; check before archiving")
        except (ValueError, AttributeError) as exc:
            raise ArchiveError("invalid publishing receipt; check before archiving") from exc
        shutil.copy2(receipt, vid_dir / f"{base}.publish.json")
    try:
        if src_video.with_suffix(".posting.json").exists():
            package = posting_package.load_for_video(src_video, channel, topic)
        elif (vid_dir / f"{base}.posting.json").exists():
            # Repairing the archive must preserve its saved copy.
            package = posting_package.load_for_video(vid_dir / f"{base}.mp4", channel, topic)
        else:
            package = data.get("posting_package") or posting_package.fallback(
                topic, data.get("script", ""), channel, task_dir.name
            )
        posting_package.save_for_video(vid_dir / f"{base}.mp4", package)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArchiveError(f"cannot archive posting package: {exc}") from exc

    # 媒体落盘之后才写记录，保证 monthly-plan.md 里出现的每一行都对应一个
    # 真实可播放的文件。
    (vid_dir / f"{base}.txt").write_text(
        render_txt(data, topic, base, duration, task_dir.name),
        encoding="utf-8",
    )
    (out_dir / "monthly-plan.md").write_text(
        render_plan(channel, topics, out_dir),
        encoding="utf-8",
    )

    # 只在真的复制了媒体之后才删工作副本。``copy_media=False`` 走的是
    # "只补 .txt / 计划表" 的修复路径（repair_archives），成片根本没被复制，
    # 这时删源等于直接毁掉唯一的成片。
    if copy_media:
        _drop_working_copy(src_video)
    return base


def _drop_working_copy(src_video: Path) -> None:
    """归档成功后删掉成片的工作副本。

    成片先落在 ``outputs/<task_id>/final-1.mp4``，再被归档进
    ``outputs/<channel>/<YYYY-MM>/videos/``。以前只复制不删除，而
    ``outputs/<task_id>/`` 在 ``PERSISTENT_SUBDIRS`` 里也不会被自动清理，
    于是每出一条视频就永久多出一个 UUID 目录——``outputs/`` 顶层那 9 个无名的
    ``final-1.mp4`` 就是这么攒出来的（1.1 GB，全是已归档内容的逐字节副本）。

    只在 ``copy_media=True``、且成片已确认落盘之后调用。任何一步落盘失败都会
    在调用之前抛出，因此走不到这里。目录本身留空壳——任务状态还要按 task_id
    索引它。
    """
    try:
        if src_video.is_file():
            src_video.unlink()
    except OSError as exc:
        # 删不掉只是留一份重复文件，绝不该让已经成功的归档变成失败。
        print(f"warning: failed to remove working copy {src_video}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--plans-root", required=True)
    parser.add_argument("--duration", default="")
    args = parser.parse_args()

    try:
        base = archive_channel_video(
            channel=args.channel,
            task_dir=args.task_dir,
            topic=args.topic,
            out_dir=args.out_dir,
            plans_root=args.plans_root,
            duration=args.duration,
        )
    except ArchiveError as exc:
        # 命令行调用方（archive_video.sh）依赖退出码判断成败，因此归档失败
        # 必须以非零码退出，而不是打印一个空文件名。
        print(f"archive failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(base)


if __name__ == "__main__":
    main()