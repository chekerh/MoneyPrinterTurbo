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

class TestOAuthFailureIsReadable:
    """OAuth 失败必须变成"错在哪、怎么改"，而不是一段 traceback。

    背景：Google 在跳回本地回调之前就把错误页渲染在浏览器里，本地回调服务器
    永远看不到它，`run_local_server` 只能等到超时。于是异常一路冒到 CLI，
    用户拿到的是 Python traceback，而 `redirect_uri_mismatch` 这种词没人知道
    该怎么处理——它其实九成是"OAuth 客户端建成了 Web 应用而不是 Desktop"。
    """

    def test_each_known_error_gets_a_diagnosis(self):
        from app.services.youtube_upload import explain_oauth_failure

        for code in (
            "redirect_uri_mismatch",
            "access_denied",
            "invalid_client",
        ):
            why, how = explain_oauth_failure(code)
            assert why.strip(), f"{code} 没有给出原因"
            assert how.strip(), f"{code} 没有给出下一步"

    def test_redirect_mismatch_names_the_real_cause(self):
        from app.services.youtube_upload import explain_oauth_failure

        why, how = explain_oauth_failure("Error 400: redirect_uri_mismatch")
        assert "Desktop app" in why
        assert "Desktop app" in how

    def test_unknown_error_still_returns_something_readable(self):
        from app.services.youtube_upload import explain_oauth_failure

        why, how = explain_oauth_failure("some brand new failure")
        assert why.strip() and how.strip()

    def test_authorize_returns_structured_failure_instead_of_raising(self):
        """authorize() 失败时返回结构化结果，绝不抛异常。"""
        from unittest.mock import patch

        from app.services import youtube_upload

        saved_id = youtube_upload.config.app.get("youtube_upload_client_id", "")
        saved_secret = youtube_upload.config.app.get("youtube_upload_client_secret", "")
        youtube_upload.config.app["youtube_upload_client_id"] = "fake-id"
        youtube_upload.config.app["youtube_upload_client_secret"] = "fake-secret"
        try:
            with patch.object(
                youtube_upload.InstalledAppFlow,
                "run_local_server",
                side_effect=TimeoutError("timed out waiting for the browser"),
            ):
                result = youtube_upload.authorize("some-niche")
        finally:
            for key, value in (
                ("youtube_upload_client_id", saved_id),
                ("youtube_upload_client_secret", saved_secret),
            ):
                if value:
                    youtube_upload.config.app[key] = value
                else:
                    youtube_upload.config.app.pop(key, None)

        assert result["success"] is False
        assert result["error_hint"], "失败结果必须带可读原因"
        assert result["error_fix"], "失败结果必须带下一步"

    def test_redirect_uri_hint_matches_what_the_flow_actually_sends(self):
        """屏幕上的提示必须和真正发出去的回调地址逐字一致（含结尾斜杠）。"""
        from app.services.youtube_upload import redirect_uri_hint

        hint = redirect_uri_hint()
        assert hint.startswith("http://localhost:")
        assert hint.endswith("/"), "缺结尾斜杠就会和 Google 的匹配规则对不上"


class TestAccountSelectionIsNeverForced:
    """多频道工作流里必须能自己选账号，而不是被浏览器记住的账号静默绑定。

    同一台 Mac 上通常只登录了一个 Google 账号。Google 的账号选择器会用
    SSO cookie 自动选中它，于是"我想换个账号"这件事根本无从表达——用户看到的
    是自己明明点了另一个账号，token 却绑到了另一个频道上。
    """

    def test_prompt_forces_the_account_chooser(self):
        from app.services.youtube_upload import OAUTH_PROMPT

        parts = OAUTH_PROMPT.split()
        assert "select_account" in parts, (
            "没有 select_account：Google 会用 SSO cookie 自动选中上次登录的账号"
        )
        assert "consent" in parts, (
            "没有 consent：换个频道时不会重新询问授权，绑不到新的频道"
        )

    def test_no_login_hint_is_ever_sent(self):
        """login_hint 会把授权流程钉死在一个账号上，永远不能出现。"""
        from app.services import youtube_upload as y
        from google_auth_oauthlib.flow import InstalledAppFlow
        import urllib.parse as up

        client_config = {
            "installed": {
                "client_id": "fake.apps.googleusercontent.com",
                "client_secret": "fake",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [y._loopback_uri("localhost", 8765)],
            }
        }
        flow = InstalledAppFlow.from_client_config(client_config, scopes=y.YOUTUBE_SCOPES)
        flow.redirect_uri = y._loopback_uri("localhost", 8765)
        url, _ = flow.authorization_url(prompt=y.OAUTH_PROMPT)
        query = up.parse_qs(up.urlparse(url).query)

        assert "login_hint" not in query, "授权请求里出现了 login_hint"
        assert "hd" not in query, "授权请求里出现了 hd（会锁死账号域）"
        assert "select_account" in query["prompt"][0]


class TestExpiredTokenIsNamedNotBuried:
    """token 过期必须被命名，不能只留一句 invalid_grant。

    同意屏幕停在 Testing 时，Google 会在 7 天后让 refresh token 失效。定时
    任务在凌晨失败，日志里只有异常字符串；用户第二天早上看到的应该是
    "这个频道要重新登录"，而不是让他自己去翻译 invalid_grant。
    """

    def test_marks_the_common_expiry_errors(self):
        from app.services.youtube_upload import needs_reauthorization

        for text in (
            "invalid_grant: Token has been expired or revoked.",
            "invalid_rapt",
            "invalid_token",
            "Token has been expired or revoked",
        ):
            assert needs_reauthorization(text), f"{text!r} 没有被识别为需要重新登录"

    def test_does_not_flag_unrelated_failures(self):
        from app.services.youtube_upload import needs_reauthorization

        for text in ("quotaExceeded", "uploadLimitExceeded", "connection reset"):
            assert not needs_reauthorization(text), f"{text!r} 被误判成 token 过期"

    def test_channel_info_marks_reauth_instead_of_generic_failure(self):
        from unittest.mock import patch

        from app.services import youtube_upload

        # is_configured 必须为真：否则 channel_info 会在"本机还没有 token"
        # 那一层就返回，根本走不到真正调用 API、抛 invalid_grant 的分支。
        with (
            patch.object(youtube_upload, "is_configured", return_value=True),
            patch.object(
                youtube_upload,
                "_service_for",
                side_effect=Exception(
                    "invalid_grant: Token has been expired or revoked."
                ),
            ),
        ):
            result = youtube_upload.channel_info("some-niche")

        assert result["success"] is False
        assert result.get("needs_reauth") is True, "过期 token 没有被标记"
        assert "signed in again" in result["error"].lower()


class TestApiNotEnabledIsExplained:
    """登录成功不等于能发布——API 启用是另一个开关。

    OAuth 只负责拿到 token；真正调用接口还需要项目启用 YouTube Data API v3。
    漏掉后者时 Google 回 accessNotConfigured，而这个错误同样只出现在日志里。
    """

    def test_access_not_configured_gets_a_fix(self):
        from app.services.youtube_upload import explain_oauth_failure

        why, how = explain_oauth_failure(
            "HttpError 403 ... accessNotConfigured ... YouTube Data API v3 has "
            "not been used in project 314980975331"
        )
        assert "not enabled" in why
        assert "youtube data api v3" in how.lower()


class TestSavedTokenIsNotTheSameAsVerifiedLogin:
    """本机存了凭据 != 这次验证通过了。

    用户刚看到"authentication flow completed"，界面却说"未登录"——于是反复
    登录，而重登解决不了真正的原因（项目还没启用 YouTube Data API）。界面必须
    能区分这三种状态，因为三者的下一步动作完全不同。
    """

    def test_has_saved_token_reads_the_disk_without_calling_google(self, tmp_path, monkeypatch):
        from app.services import youtube_upload

        monkeypatch.setattr(youtube_upload, "token_path", lambda ch: str(tmp_path / f"{ch}.json"))

        assert youtube_upload.has_saved_token("niche-a") is False

        (tmp_path / "niche-a.json").write_text("{}")
        assert youtube_upload.has_saved_token("niche-a") is True
        # 别的频道不受影响——"给哪个频道登录"是这套流程的基本单位。
        assert youtube_upload.has_saved_token("niche-b") is False

    def test_a_dead_token_is_still_a_saved_token(self, tmp_path, monkeypatch):
        """过期要报 needs_reauth，但凭据确实还在磁盘上。

        两个信号都不能丢：前者告诉用户去修同意屏幕，后者保证界面显示的是
        "登录过但要重登"，而不是从未登录。
        """
        from app.services import youtube_upload

        monkeypatch.setattr(youtube_upload, "token_path", lambda ch: str(tmp_path / f"{ch}.json"))
        (tmp_path / "gone.json").write_text("{}")

        with (
            patch.object(youtube_upload, "is_configured", return_value=True),
            patch.object(
                youtube_upload,
                "_service_for",
                side_effect=Exception("invalid_grant: Token has been expired or revoked."),
            ),
        ):
            result = youtube_upload.channel_info("gone")

        assert result["success"] is False
        assert result["needs_reauth"] is True
        assert youtube_upload.has_saved_token("gone") is True
