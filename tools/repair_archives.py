#!/usr/bin/env python3
"""Re-copy archived videos whose bytes are truncated or empty from their task dir.

Run with --apply to write. Without it, only reports.

An archive is considered damaged when ffprobe cannot derive its dimensions, or
when its size differs from the task-dir source recorded in the sibling .txt
sidecar. The sidecar's ``task_id`` is the only reliable link between an archived
file and the task that produced it, so it is the join key here.
"""

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import archive_video  # noqa: E402

OUTPUTS = os.path.join(REPO, "outputs")
TASKS = os.path.join(REPO, "storage", "tasks")


def task_source(task_id):
    candidate = os.path.join(TASKS, task_id, "final-1.mp4")
    if not os.path.isfile(candidate):
        return None
    if archive_video.probe_dimensions(candidate) is None:
        return None
    return candidate


def find_damaged():
    findings = []
    for channel in sorted(os.listdir(OUTPUTS)):
        channel_root = os.path.join(OUTPUTS, channel)
        if not os.path.isdir(channel_root):
            continue
        for dirpath, _dirs, files in os.walk(channel_root):
            if os.path.basename(os.path.normpath(dirpath)) != "videos":
                continue
            for name in sorted(files):
                if not name.endswith(".mp4"):
                    continue
                mp4 = os.path.join(dirpath, name)
                sidecar = os.path.join(dirpath, name[:-4] + ".txt")
                if not os.path.isfile(sidecar):
                    continue

                task_id = ""
                try:
                    with open(sidecar, encoding="utf-8") as handle:
                        for line in handle:
                            if line.startswith("task_id:"):
                                task_id = line.split(":", 1)[1].strip()
                                break
                except OSError:
                    continue
                if not task_id:
                    continue

                source = task_source(task_id)
                archived_ok = archive_video.probe_dimensions(mp4) is not None
                size_match = (
                    source is not None
                    and os.path.getsize(mp4) == os.path.getsize(source)
                )
                if archived_ok and size_match:
                    continue

                findings.append(
                    {
                        "mp4": mp4,
                        "sidecar": sidecar,
                        "task_id": task_id,
                        "source": source,
                        "archived_size": os.path.getsize(mp4),
                        "source_size": os.path.getsize(source) if source else None,
                        "archived_readable": archived_ok,
                    }
                )
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    findings = find_damaged()
    if not findings:
        print("no damaged archives found")
        return 0

    for f in findings:
        verdict = "READABLE-BUT-SHORT" if f["archived_readable"] else "UNREADABLE"
        print(
            f"{verdict:20} {f['archived_size']:>10} -> "
            f"{f['source_size'] if f['source_size'] else 'NO SOURCE':>10}  "
            f"{os.path.relpath(f['mp4'], REPO)}"
        )
        if f["source"] is None:
            print(f"{'':20} !! no healthy task source for {f['task_id']}")

    if not args.apply:
        print(f"\n{len(findings)} damaged archive(s). Re-run with --apply to repair.")
        return 0

    repaired, skipped = 0, 0
    for f in findings:
        if f["source"] is None:
            skipped += 1
            continue
        try:
            archive_video._copy_verified(f["source"], f["mp4"])
        except archive_video.ArchiveError as exc:
            print(f"FAILED  {f['mp4']}: {exc}")
            skipped += 1
            continue
        after = archive_video.probe_dimensions(f["mp4"])
        if after is None:
            print(f"FAILED  {f['mp4']}: still unreadable after copy")
            skipped += 1
            continue
        repaired += 1
        print(
            f"REPAIRED {os.path.relpath(f['mp4'], REPO)} "
            f"{f['archived_size']} -> {os.path.getsize(f['mp4'])} bytes {after}"
        )

    # Record the repair next to the plan so the provenance stays inspectable.
    if repaired:
        marker = os.path.join(OUTPUTS, ".archive_repair_log.jsonl")
        with open(marker, "a", encoding="utf-8") as handle:
            for f in findings:
                if f["source"] is None:
                    continue
                handle.write(
                    json.dumps(
                        {
                            "file": os.path.relpath(f["mp4"], REPO),
                            "task_id": f["task_id"],
                            "from_bytes": f["archived_size"],
                            "to_bytes": os.path.getsize(f["mp4"]),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(f"\nlog: {os.path.relpath(marker, REPO)}")

    print(f"\nrepaired={repaired} skipped={skipped}")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
