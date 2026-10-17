"""每个频道一份凭据，以及"绑错频道就拒绝发布"的保护。

这些行为不可逆：token 一旦绑到错误的频道，视频发出去就收不回来，所以必须
在调用 videos().insert 之前就拦住，而不是发完再报警。
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import publishing, youtube_upload

NICHES = ("history-mysteries", "mind-psychology", "money-finance", "space-science")
CREDS_DIR = "/tmp/mpt-test-creds"


def _write_token(channel: str, channel_id: str, channel_name: str) -> str:
    """模拟一次授权后的 token 文件（含 youtube_upload 写入的绑定元数据）。"""
    path = youtube_upload.token_path(channel)
    payload = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "csecret",
        "scopes": list(youtube_upload.YOUTUBE_SCOPES),
        youtube_upload._META_KEY: {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "niche": channel,
        },
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.chmod(path, 0o600)
    return path


class _CredsBase(unittest.TestCase):
    """把凭据目录指向临时路径，并把 OAuth 客户端配置成可用。"""

    def setUp(self):
        self.video_path = os.path.join(CREDS_DIR, "clip.mp4")
        os.makedirs(CREDS_DIR, exist_ok=True)
        for name in os.listdir(CREDS_DIR):
            os.remove(os.path.join(CREDS_DIR, name))
        with open(self.video_path, "wb") as handle:
            handle.write(b"\x00")
        self._dir = patch.object(
            youtube_upload, "credentials_root", return_value=CREDS_DIR
        )
        self._dir.start()
        self._creds = patch.object(
            youtube_upload, "client_credentials", return_value=("cid", "csecret")
        )
        self._creds.start()
        self._expected = None

    def tearDown(self):
        self._restore_expected()
        self._creds.stop()
        self._dir.stop()

    def expect_channel(self, channel_id: str):
        """设置 youtube_upload_channel_id，并保证测试结束后复原。"""
        self._restore_expected()
        app = youtube_upload.config.app
        self._saved = (app.get("youtube_upload_channel_id"), "youtube_upload_channel_id" in app)
        app["youtube_upload_channel_id"] = channel_id
        self._expected = channel_id

    def _restore_expected(self):
        if self._expected is None:
            return
        app = youtube_upload.config.app
        had, value = self._saved
        if had:
            app["youtube_upload_channel_id"] = value
        else:
            app.pop("youtube_upload_channel_id", None)
        self._expected = None

    @staticmethod
    def stub_service(channel_id: str) -> MagicMock:
        service = MagicMock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": channel_id, "snippet": {"title": f"Channel {channel_id}"}}]
        }
        return service


class TestPerNicheCredentials(_CredsBase):
    def test_each_niche_gets_its_own_token_path(self):
        paths = {youtube_upload.token_path(niche) for niche in NICHES}
        self.assertEqual(len(paths), len(NICHES), "niches must not share a token file")

    def test_slug_is_filesystem_safe(self):
        self.assertEqual(
            youtube_upload.token_path("Weird Name!!"),
            os.path.join(CREDS_DIR, "weird-name.json"),
        )

    def test_only_niches_with_a_token_are_connected(self):
        _write_token("history-mysteries", "UC1", "History Mysteries")
        self.assertEqual(youtube_upload.connected_channels(), ["history-mysteries"])
        self.assertTrue(youtube_upload.is_configured("history-mysteries"))
        self.assertFalse(
            youtube_upload.is_configured("money-finance"),
            "a token for one niche must not authorize another",
        )

    def test_token_file_is_owner_only(self):
        path = _write_token("money-finance", "UC3", "Money Finance")
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600, "refresh tokens must not be group/world readable")

    def test_without_client_credentials_nothing_is_connected(self):
        _write_token("money-finance", "UC3", "Money Finance")
        with patch.object(
            youtube_upload, "client_credentials", return_value=("", "")
        ):
            self.assertFalse(youtube_upload.is_configured("money-finance"))


class TestWrongChannelRefusal(_CredsBase):
    """绑错频道时必须拒绝发布，而不是把内容发到别人的频道上。"""

    def test_publish_is_blocked_when_token_is_bound_to_another_channel(self):
        # money-finance 的 token 实际绑在 space-science 的频道上。
        _write_token("money-finance", "UC_SPACE", "Space Science")
        self.expect_channel("UC_MONEY")

        with patch.object(
            youtube_upload, "_service_for", return_value=self.stub_service("UC_SPACE")
        ):
            result = youtube_upload.publish_video(
                self.video_path, title="t", channel="money-finance"
            )

        self.assertFalse(result["success"])
        self.assertIn("refusing to publish", result["error"])
        self.assertIn("UC_SPACE", result["error"])

    def test_publish_is_blocked_when_configured_channel_differs_from_token(self):
        """即使 token 内部自洽，与配置不符也要拦。"""
        _write_token("money-finance", "UC_MONEY", "Money Finance")
        self.expect_channel("UC_SOMETHING_ELSE")

        with patch.object(
            youtube_upload, "_service_for", return_value=self.stub_service("UC_MONEY")
        ):
            result = youtube_upload.publish_video(
                self.video_path, title="t", channel="money-finance"
            )

        self.assertFalse(result["success"])
        self.assertIn("refusing to publish", result["error"])

    def test_publish_proceeds_when_channel_matches(self):
        _write_token("money-finance", "UC_MONEY", "Money Finance")
        self.expect_channel("UC_MONEY")

        service = MagicMock()
        service.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC_MONEY", "snippet": {"title": "Money"}}]
        }

        def _next_chunk():
            return None, {"id": "VID123"}

        service.videos.return_value.insert.return_value.next_chunk = _next_chunk

        with patch.object(youtube_upload, "_service_for", return_value=service):
            result = youtube_upload.publish_video(
                self.video_path, title="t", channel="money-finance"
            )

        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["video_id"], "VID123")
        service.videos.return_value.insert.assert_called_once()

    def test_unconnected_niche_reports_how_to_connect(self):
        result = youtube_upload.publish_video(
            self.video_path, title="t", channel="mind-psychology"
        )
        self.assertFalse(result["success"])
        self.assertIn("mind-psychology", result["error"])


class TestPublishingRegistry(_CredsBase):
    def test_youtube_adapter_is_registered(self):
        adapter = publishing.get("youtube")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.platforms, ["youtube"])

    def test_tiktok_and_instagram_have_a_seam_but_are_not_connected(self):
        """TikTok/Instagram 还没有官方个人账号发布通道，但注册表里已留好位置。"""
        adapter = publishing.get("upload_post")
        self.assertIsNotNone(adapter)
        self.assertIn("tiktok", adapter.platforms)
        self.assertIn("instagram", adapter.platforms)

    def test_publish_without_any_connection_fails_cleanly(self):
        result = publishing.publish(
            channel="history-mysteries",
            video_path=self.video_path,
            meta=publishing.PublishMeta(title="t"),
        )
        self.assertFalse(result["success"])
        self.assertIn("history-mysteries", result["error"])

    def test_publish_meta_keeps_title_and_caption_separate(self):
        """YouTube 长标题和社交平台短文案必须是两个字段，不能互相覆盖。"""
        meta = publishing.PublishMeta(
            title="Morning Coffee", caption="A better morning. #coffee"
        )
        self.assertEqual(meta.title, "Morning Coffee")
        self.assertEqual(meta.caption, "A better morning. #coffee")

    def test_upload_post_uses_caption_not_youtube_title(self):
        """社交平台拿到的必须是共享文案，不能是 YouTube 的长标题。"""
        adapter = publishing.get("upload_post")
        with (
            patch.object(publishing.upload_post_service, "is_configured", return_value=True),
            patch.object(
                type(publishing.upload_post_service), "platforms", new_callable=unittest.mock.PropertyMock, return_value=["tiktok"]
            ),
            patch.object(publishing.upload_post_service, "upload_video") as up,
        ):
            adapter.publish(
                "history-mysteries",
                self.video_path,
                publishing.PublishMeta(
                    title="Morning Coffee", caption="A better morning. #coffee"
                ),
            )
        self.assertEqual(up.call_args.args[1], "A better morning. #coffee")


if __name__ == "__main__":
    unittest.main()