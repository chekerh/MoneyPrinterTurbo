import json
from unittest.mock import Mock
import pytest

from app.services import posting_package


def test_package_uses_generated_copy_and_source_comment(monkeypatch):
    from app.services import llm

    generate = Mock(
        return_value={
            "title": "Ocean depths",
            "caption": "Most of the ocean is unexplored.",
            "hashtags": ["#Ocean", "#Shorts"],
        }
    )
    monkeypatch.setattr(llm, "generate_social_metadata", generate)
    package = posting_package.build(
        "Ocean",
        "Ocean narration.",
        "en",
        "science",
        "task1",
        sources=[{"source_page": "https://example.com/clip"}],
    )
    assert package["title"] == "Ocean depths"
    assert package["comment"] == "Footage sources:\nhttps://example.com/clip"
    assert package["hashtags"] == ["#Ocean", "#Shorts"]
    generate.assert_called_once()


def test_failed_copy_provider_still_produces_manual_package(monkeypatch):
    from app.services import llm

    monkeypatch.setattr(
        llm, "generate_social_metadata", Mock(side_effect=RuntimeError("offline"))
    )
    package = posting_package.build(
        "Ocean", "A sourced narration.", "en", "science", "task1"
    )
    assert package["caption"] == "A sourced narration."
    assert package["copy_source"] == "fallback"
    assert "A sourced narration." in posting_package.render_text(package)


def test_saved_package_survives_reload_and_has_separate_copy_sections(tmp_path):
    video = tmp_path / "final-1.mp4"
    video.write_bytes(b"video")
    package = posting_package.fallback("Ocean", "Narration.", "science", "task1")
    posting_package.save_for_video(video, package)
    assert posting_package.load_for_video(video) == package
    text = video.with_suffix(".posting.txt").read_text()
    assert "TITLE\nOcean" in text
    assert "DESCRIPTION / CAPTION\nNarration." in text
    assert "COMMENT DRAFT" in text
    assert (
        json.loads(video.with_suffix(".posting.json").read_text())["task_id"] == "task1"
    )


def test_legacy_archive_copy_is_available_without_calling_provider(tmp_path):
    video = tmp_path / "old.mp4"
    video.with_suffix(".txt").write_text(
        "topic: Ocean\ntask_id: old-task\n\nscript:\nLegacy narration.\n\nsearch terms:\n- ocean\n"
    )
    package = posting_package.load_for_video(video, "science", "Ocean")
    assert package["caption"] == "Legacy narration."
    assert package["task_id"] == "old-task"


def test_invalid_saved_package_is_reported(tmp_path):
    import pytest

    video = tmp_path / "bad.mp4"
    video.with_suffix(".posting.json").write_text('{"title": 123}')
    with pytest.raises(ValueError, match="posting package"):
        posting_package.load_for_video(video)


@pytest.mark.parametrize("scheduled", [False, True])
def test_pipeline_saves_copy_without_connected_publishing(
    tmp_path, monkeypatch, scheduled
):
    from app.models.schema import VideoParams
    from app.services import llm, state, task

    video = tmp_path / "final-1.mp4"
    video.write_bytes(b"finished video")
    manifest = tmp_path / "script.json"
    monkeypatch.setattr(task.utils, "task_dir", lambda *args: str(tmp_path))
    monkeypatch.setattr(task.utils, "check_ffmpeg_ready", lambda: True)
    monkeypatch.setattr(task, "_purge_task_scratch", lambda *args: None)
    monkeypatch.setattr(task, "generate_script", lambda *args: "Ocean narration.")
    monkeypatch.setattr(task, "generate_terms", lambda *args: ["ocean"])
    monkeypatch.setattr(
        task, "generate_audio", lambda *args, **kwargs: ("audio.mp3", 5, None)
    )
    monkeypatch.setattr(task, "generate_subtitle", lambda *args: "")
    monkeypatch.setattr(
        task, "get_video_materials", lambda *args, **kwargs: ["clip.mp4"]
    )
    monkeypatch.setattr(
        task, "generate_final_videos", lambda *args: ([str(video)], [], [])
    )
    monkeypatch.setattr(
        task.publishing,
        "connected_platforms",
        lambda *args: ["youtube"] if scheduled else [],
    )
    if scheduled:
        monkeypatch.setenv("MPT_SKIP_AUTO_PUBLISH", "1")
    else:
        monkeypatch.delenv("MPT_SKIP_AUTO_PUBLISH", raising=False)
    schedule = Mock()
    monkeypatch.setattr(task, "_schedule_cross_post", schedule)
    monkeypatch.setattr(
        task,
        "_record_result_descriptor",
        lambda *args: {"eligible": False, "reason": "test", "descriptor_id": None},
    )
    monkeypatch.setattr(task.sm, "state", state.MemoryState())
    generate = Mock(
        return_value={
            "title": "Ocean depths",
            "caption": "An ocean story.",
            "hashtags": ["#Ocean"],
        }
    )
    monkeypatch.setattr(llm, "generate_social_metadata", generate)
    result = task.start(
        "task1",
        VideoParams(video_subject="Ocean", video_language="en", channel="science"),
    )
    assert result["videos"] == [str(video)]
    assert result["posting_package"]["title"] == "Ocean depths"
    assert posting_package.load_for_video(video)["caption"] == "An ocean story."
    assert (
        json.loads(manifest.read_text())["posting_package"]["title"] == "Ocean depths"
    )
    generate.assert_called_once()
    schedule.assert_not_called()
    assert result["cross_post_state"] is None


@pytest.mark.parametrize("location", ["tools", "../tools"])
def test_upload_tool_uses_saved_copy_and_visibility(
    tmp_path, monkeypatch, capsys, location
):
    import importlib.util
    import sys
    from pathlib import Path
    from app.config import config
    from app.services import publishing

    root = Path(__file__).resolve().parents[2]
    tool = root / location / "publish_video.py"
    spec = importlib.util.spec_from_file_location("posting_upload_tool", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    posting_package.save_for_video(
        video,
        posting_package.fallback(
            "Saved title", "Saved description", "science", "task1"
        ),
    )
    upload = Mock(
        return_value={"success": True, "result": {"youtube": {"success": True}}}
    )
    monkeypatch.setattr(publishing, "publish_once", upload)
    monkeypatch.setitem(config.app, "youtube_upload_privacy_status", "private")
    monkeypatch.setattr(
        sys, "argv", [str(tool), "--channel", "science", "--video", str(video)]
    )
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["success"]
    meta = upload.call_args.kwargs["meta"]
    assert meta.title == "Saved title"
    assert meta.description == "Saved description"
    assert meta.privacy_status == "private"
    monkeypatch.setitem(config.app, "youtube_upload_auto_publish", False)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(tool), "--channel", "science", "--video", str(video), "--automatic"],
    )
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["skipped"]
    upload.assert_called_once()


def test_cross_post_uses_saved_copy_without_regeneration(tmp_path, monkeypatch):
    from app.services import llm, state, task

    video = tmp_path / "final-1.mp4"
    video.write_bytes(b"finished video")
    package = posting_package.fallback(
        "Saved title", "Saved caption", "science", "task1"
    )
    posting_package.save_for_video(video, package)
    memory = state.MemoryState()
    memory.update_task(
        "task1",
        state=task.const.TASK_STATE_COMPLETE,
        cross_post_state=task.const.CROSS_POST_STATE_PENDING,
    )
    monkeypatch.setattr(task.sm, "state", memory)
    generate = Mock(side_effect=AssertionError("must reuse saved copy"))
    monkeypatch.setattr(llm, "generate_social_metadata", generate)
    upload = Mock(
        return_value={
            "success": True,
            "result": {"youtube": {"success": True, "url": "url"}},
        }
    )
    monkeypatch.setattr(task.publishing, "publish", upload)
    task._run_cross_post(
        "task1",
        (str(video),),
        "Ocean",
        "Narration",
        "en",
        ("youtube",),
        "private",
        "science",
    )
    assert upload.call_args.kwargs["meta"].title == "Saved title"
    assert upload.call_args.kwargs["meta"].description == "Saved caption"
    assert (
        memory.get_task("task1")["cross_post_state"]
        == task.const.CROSS_POST_STATE_COMPLETE
    )
    generate.assert_not_called()
