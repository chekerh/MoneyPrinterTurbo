#!/usr/bin/env python3
"""检查 OAuth 客户端能不能接住本应用的回调地址——不开浏览器就能判断。

为什么要这个脚本：``redirect_uri_mismatch`` 只出现在 Google 的错误页上，用户
只能靠肉眼分辨"这次有没有弹出账号选择页"。而失败与账号无关（Google 在显示
账号选择页**之前**就校验了 redirect_uri），所以每次重试都是白等。

本脚本直接请求 Google 的授权端点，看它把我们导向"登录页"还是"错误页"：

* 跳到 ``accounts.google.com/signin`` → 通过，客户端配置正确
* 跳到 ``signin/oauth/error`` 且错误为 ``redirect_uri_mismatch`` → 客户端没有
  登记这条回调地址。改完 Console 再跑一次即可确认。

用法::

    python tools/check_youtube_oauth.py
    python tools/check_youtube_oauth.py --client-id xxx.apps.googleusercontent.com
"""

from __future__ import annotations

import argparse
import base64
import sys
import urllib.parse as urlparse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.youtube_upload import (  # noqa: E402
    YOUTUBE_SCOPES,
    redirect_uri_hint,
)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/auth"


def _decode_google_error(redirect_url: str) -> str:
    """把 Google 错误链接里的 base64 片段还原成可读错误名。

    Google 把错误信息塞在 ``authError=<base64>`` 里，直接看链接只会得到一串
    乱码；解码之后才能确认它到底是不是 redirect_uri_mismatch。
    """
    query = urlparse.parse_qs(urlparse.urlparse(redirect_url).query)
    blob = (query.get("authError") or [""])[0]
    if not blob:
        return ""
    padded = blob + "=" * (-len(blob) % 4)
    try:
        decoded = base64.b64decode(padded[: padded.find("&") if "&" in padded else len(padded)]).decode(
            "utf-8", "replace"
        )
    except Exception:  # noqa: BLE001 - 解不出来就原样返回，不影响判断
        return blob
    return decoded.replace("\x01", " ").strip()


def probe(client_id: str, redirect_uri: str) -> tuple[bool, str]:
    """请求授权端点，返回 ``(是否接受, 说明)``。"""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(YOUTUBE_SCOPES),
        "prompt": "consent",
    }
    url = f"{AUTH_ENDPOINT}?{urlparse.urlencode(params)}"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=20) as response:
            location = response.headers.get("Location", "")
            status = response.status
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.headers else ""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 - 网络问题也要能读
        return False, f"could not reach Google: {exc}"

    if not location:
        # 少数情况下直接返回 200 错误页，从正文里找线索。
        return False, f"Google returned HTTP {status} with no redirect"

    if "signin/oauth/error" in location or "/oauth/error" in location:
        error = _decode_google_error(location)
        if "redirect_uri_mismatch" in error:
            return False, "redirect_uri_mismatch"
        return False, error or "oauth error"

    # 客户端被接受后，Google 会把我们送去**选账号页**。这个路径随版本变化过
    # （/ServiceLogin、/signin、/v3/signin/identifier……），所以不能只匹配某一个
    # 固定字符串：只要还落在 accounts.google.com 上且不是上面的错误页，就算通过。
    if "accounts.google.com" in location:
        return True, "Google accepted the client and is showing the account chooser"

    return False, f"unexpected redirect: {location[:120]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default="", help="override the configured client id")
    args = parser.parse_args()

    client_id = args.client_id.strip()
    if not client_id:
        from app.config import config

        client_id = str(config.app.get("youtube_upload_client_id", "") or "").strip()

    redirect_uri = redirect_uri_hint()

    print("MoneyPrinterTurbo - OAuth client check")
    print(f"  client id     : {client_id or '(not configured)'}")
    print(f"  redirect uri  : {redirect_uri}")
    if not client_id:
        print("\nRESULT: no client id configured yet.")
        print("        Paste it on the Login screen first.")
        return 1

    ok, detail = probe(client_id, redirect_uri)
    print(f"  google says   : {detail}")
    print()
    if ok:
        print("RESULT: PASS - the client accepts this callback address.")
        print("        You can sign in per niche from the Login screen.")
        return 0

    print("RESULT: FAIL - Google rejected the callback address.")
    print()
    print("Fix it in Google Cloud Console:")
    print("  1. APIs & Services -> Credentials")
    print("  2. Open this OAuth client (or create a new one)")
    print("  3. EITHER pick Application type = Desktop app (needs no redirect URI),")
    print("     OR if it is a Web app, add this exact Authorized redirect URI:")
    print(f"       {redirect_uri}")
    print("  4. Save, wait ~30s, then run this script again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())