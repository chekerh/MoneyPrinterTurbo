from unittest.mock import Mock
import json

import pytest

from app.services import publishing


def test_success_receipt_prevents_repeated_upload(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(
        return_value={
            "success": True,
            "result": {"youtube": {"success": True, "url": "https://youtu.be/one"}},
        }
    )
    monkeypatch.setattr(publishing, "publish", upload)
    meta = publishing.PublishMeta("Ocean")
    first = publishing.publish_once("science", str(video), meta, ["youtube"])
    second = publishing.publish_once("science", str(video), meta, ["youtube"])
    assert first["success"] and second["success"]
    assert second["result"]["youtube"]["url"] == "https://youtu.be/one"
    upload.assert_called_once()


def test_explicit_failure_can_be_retried(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(
        side_effect=[
            {"success": False, "error": "not connected"},
            {"success": True, "result": {"youtube": {"success": True, "url": "url"}}},
        ]
    )
    monkeypatch.setattr(publishing, "publish", upload)
    assert not publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )["success"]
    assert publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )["success"]
    assert upload.call_count == 2


def test_concurrent_upload_is_blocked(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    video.with_suffix(".publish.lock").write_text("already running")
    upload = Mock()
    monkeypatch.setattr(publishing, "publish", upload)
    result = publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert "in progress" in result["error"]
    upload.assert_not_called()


def test_partial_success_retries_only_failed_platform(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(
        side_effect=[
            {
                "success": False,
                "result": {
                    "youtube": {"success": True, "url": "url"},
                    "upload_post": {"success": False, "error": "rejected"},
                },
            },
            {"success": True, "result": {"upload_post": {"success": True}}},
        ]
    )
    monkeypatch.setattr(publishing, "publish", upload)
    meta = publishing.PublishMeta("Ocean")
    assert not publishing.publish_once(
        "science", str(video), meta, ["youtube", "tiktok"]
    )["success"]
    assert publishing.publish_once("science", str(video), meta, ["youtube", "tiktok"])[
        "success"
    ]
    assert upload.call_args.kwargs["platforms"] == ["tiktok"]


def test_corrupt_receipt_never_reuploads(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    video.with_suffix(".publish.json").write_text("broken")
    upload = Mock()
    monkeypatch.setattr(publishing, "publish", upload)
    result = publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert "receipt" in result["error"]
    upload.assert_not_called()


@pytest.mark.parametrize("change", ["video", "channel"])
def test_receipt_refuses_changed_video_or_channel(tmp_path, monkeypatch, change):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(
        return_value={"success": True, "result": {"youtube": {"success": True}}}
    )
    monkeypatch.setattr(publishing, "publish", upload)
    publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    if change == "video":
        video.write_bytes(b"different video")
    result = publishing.publish_once(
        "history" if change == "channel" else "science",
        str(video),
        publishing.PublishMeta("Ocean"),
        ["youtube"],
    )
    assert not result["success"]
    assert "changed" in result["error"]
    upload.assert_called_once()


def test_interrupted_upload_retains_pending_receipt(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(side_effect=RuntimeError("interrupted"))
    monkeypatch.setattr(publishing, "publish", upload)
    with pytest.raises(RuntimeError):
        publishing.publish_once(
            "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
        )
    assert json.loads(video.with_suffix(".publish.json").read_text())["pending"] == [
        "youtube"
    ]
    result = publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert "unknown" in result["error"]
    upload.assert_called_once()


def test_uncertain_provider_failure_never_reuploads(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(
        return_value={
            "success": False,
            "result": {
                "youtube": {
                    "success": False,
                    "outcome_unknown": True,
                    "error": "connection lost",
                }
            },
        }
    )
    monkeypatch.setattr(publishing, "publish", upload)
    publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    result = publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert "unknown" in result["error"]
    upload.assert_called_once()


def test_new_social_target_is_not_marked_done_by_previous_target(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(
        return_value={"success": True, "result": {"upload_post": {"success": True}}}
    )
    monkeypatch.setattr(publishing, "publish", upload)
    meta = publishing.PublishMeta("Ocean")
    assert publishing.publish_once("science", str(video), meta, ["tiktok"])["success"]
    assert publishing.publish_once("science", str(video), meta, ["instagram"])[
        "success"
    ]
    assert upload.call_count == 2
    assert upload.call_args.kwargs["meta"].extra["platforms"] == ["instagram"]


def test_missing_video_does_not_start_upload(tmp_path, monkeypatch):
    upload = Mock()
    monkeypatch.setattr(publishing, "publish", upload)
    result = publishing.publish_once(
        "science",
        str(tmp_path / "missing.mp4"),
        publishing.PublishMeta("Ocean"),
        ["youtube"],
    )
    assert not result["success"]
    upload.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [None, {"success": True}, {"success": True, "result": {"youtube": "invalid"}}],
)
def test_invalid_provider_response_keeps_unknown_outcome(
    tmp_path, monkeypatch, response
):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock(return_value=response)
    monkeypatch.setattr(publishing, "publish", upload)
    result = publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert json.loads(video.with_suffix(".publish.json").read_text())["pending"] == [
        "youtube"
    ]


def test_thumbnail_uses_youtube_thumbnail_endpoint(tmp_path, monkeypatch):
    from app.services import youtube_upload

    thumbnail = tmp_path / "cover.jpg"
    thumbnail.write_bytes(b"image")
    media = Mock()
    monkeypatch.setattr(youtube_upload, "MediaFileUpload", media)
    service = Mock()
    youtube_upload.set_thumbnail(service, "video-id", str(thumbnail))
    service.thumbnails.return_value.set.assert_called_once_with(
        videoId="video-id", media_body=media.return_value
    )
    assert media.call_args.kwargs["mimetype"] == "image/jpeg"
    service.videos.assert_not_called()


def test_disconnected_target_has_actionable_error(monkeypatch):
    adapter = Mock(platform="youtube", platforms=["youtube"])
    adapter.is_connected.return_value = False
    monkeypatch.setattr(publishing, "adapters", lambda: [adapter])
    result = publishing.publish(
        "science", "video.mp4", publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert "not connected" in result["error"]
    adapter.publish.assert_not_called()


def test_partial_success_is_not_reported_as_complete(monkeypatch):
    youtube = Mock(platform="youtube", platforms=["youtube"])
    youtube.publish.return_value = {"success": True, "url": "url"}
    social = Mock(platform="upload_post", platforms=["tiktok"])
    social.publish.return_value = {"success": False, "error": "rejected"}
    monkeypatch.setattr(publishing, "adapters", lambda: [youtube, social])
    result = publishing.publish(
        "science", "video.mp4", publishing.PublishMeta("Ocean"), ["youtube", "tiktok"]
    )
    assert not result["success"]
    assert result["result"]["youtube"]["success"]
    assert "rejected" in result["error"]


def test_provider_exception_is_marked_uncertain(monkeypatch):
    adapter = Mock(platform="youtube", platforms=["youtube"])
    adapter.publish.side_effect = RuntimeError("connection lost")
    monkeypatch.setattr(publishing, "adapters", lambda: [adapter])
    result = publishing.publish(
        "science", "video.mp4", publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert result["result"]["youtube"]["outcome_unknown"]


def test_receipt_write_failure_prevents_remote_upload(tmp_path, monkeypatch):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"video")
    upload = Mock()
    monkeypatch.setattr(publishing, "publish", upload)
    monkeypatch.setattr(
        publishing, "_write_json_atomic", Mock(side_effect=OSError("disk full"))
    )
    result = publishing.publish_once(
        "science", str(video), publishing.PublishMeta("Ocean"), ["youtube"]
    )
    assert not result["success"]
    assert "disk full" in result["error"]
    assert not video.with_suffix(".publish.lock").exists()
    upload.assert_not_called()


def test_unknown_publishing_backend_can_be_reported_without_remote_call():
    assert (
        "unknown platform" in publishing.connect("missing-platform", "science")["error"]
    )
