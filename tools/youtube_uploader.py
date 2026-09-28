"""Publish generated videos to YouTube via the user's real Chrome.

Drives Studio.youtube.com in a persistent, per-channel Chrome profile
using Playwright. No OAuth API key is required: the account logged in
inside the profile is the channel that receives the upload.

Commands:
  --check              report whether the profile is logged in to Studio
  --login              open Studio in the profile for a manual one-time login
  --publish <mp4>      upload a video with --title / --description
"""

import argparse
import json
import os
import re
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PROFILE_ROOT = os.path.join(BASE_DIR, "MoneyPrinterTurbo", "storage", "browser_profiles")
DEBUG_DIR = os.path.join(BASE_DIR, "MoneyPrinterTurbo", "storage", "browser_debug")
STUDIO_URL = "https://studio.youtube.com"
LOGIN_TIMEOUT_S = 180
UPLOAD_TIMEOUT_S = 300


def _profile_dir(channel: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(channel).strip()) or "default"
    return os.path.join(PROFILE_ROOT, safe)


class StudioPublishError(RuntimeError):
    pass


def _first_visible(page, locators):
    for locator in locators:
        try:
            if locator.is_visible(timeout=2000):
                return locator
        except Exception:
            continue
    return None


def _click_text(page, patterns, timeout=15000):
    for pattern in patterns:
        locator = page.get_by_text(pattern, exact=True)
        try:
            locator.first.wait_for(state="visible", timeout=timeout)
            locator.first.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def _poll_until(predicate, timeout, interval=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _launch(pw, channel):
    context = pw.chromium.launch_persistent_context(
        _profile_dir(channel),
        channel="chrome",
        headless=False,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(20000)
    return context, page


def _ensure_visited(page, url):
    page.goto(url, wait_until="domcontentloaded")
    if _poll_until(lambda: not page.url.startswith("https://accounts.google.com"), 10):
        page.goto(url, wait_until="domcontentloaded")
    return not _poll_until(
        lambda: page.url.startswith("https://accounts.google.com"), 0.5
    )


def _check_login(page) -> bool:
    if not _ensure_visited(page, STUDIO_URL):
        return False
    return not _poll_until(
        lambda: page.url.startswith("https://accounts.google.com"), 0.5
    )


def _fill_contenteditable(page, locators, text):
    editable = _first_visible(page, locators)
    if editable is None:
        raise StudioPublishError("metadata textbox not found")
    editable.click()
    page.keyboard.press("Meta+A")
    page.keyboard.type(str(text), delay=0)
    return editable


def _set_visibility(page, privacy):
    select = _first_visible(page, [page.locator("ytcp-visibility-select")])
    if select is None:
        return
    select.click()
    option = page.get_by_role("option", name=privacy)
    if not _poll_until(lambda: option.count() > 0, 5):
        option = page.locator("tp-yt-paper-item", has_text=privacy).first
    option.click()
    done = page.locator("ytcp-button#done-button")
    if done.count() and done.is_visible(timeout=2000):
        done.click()


def _wait_for_publish_confirmation(page) -> str:
    toast = page.locator("tp-yt-paper-toast")
    if _poll_until(
        lambda: toast.count() > 0 and toast.is_visible(timeout=500), UPLOAD_TIMEOUT_S
    ):
        message = toast.inner_text(timeout=2000)
        view = page.locator("a", has_text="View video")
        if view.count() and view.first.is_visible(timeout=2000):
            href = view.first.get_attribute("href") or ""
            return href
        return message.strip()
    return ""


def prepare_page(page):
    if not _check_login(page):
        raise StudioPublishError(
            "not logged in to YouTube Studio for this profile (run --login first)"
        )
    create = page.get_by_role("button", name="Create")
    if not create.count():
        create = page.locator("ytcp-button#create-icon")
    create.first.click()
    if not _click_text(page, ["Upload videos", "Upload video"], timeout=15000):
        raise StudioPublishError("could not open the Upload videos menu")
    upload_input = page.locator('input[type="file"]')
    if not _poll_until(lambda: upload_input.count() > 0, 10):
        raise StudioPublishError("upload file input did not appear")
    return upload_input


def _stop_captioning_prompt(page):
    for pattern in ("Discard", "No thanks", "Skip"):
        for locator in page.get_by_role("button", name=pattern).all():
            try:
                if locator.is_visible(timeout=1000):
                    locator.click(timeout=3000)
                    return
            except Exception:
                continue


def publish(channel, video_path, title, description, privacy="public") -> dict:
    if not os.path.isfile(video_path):
        return {"success": False, "error": f"video file not found: {video_path}"}
    if not title:
        return {"success": False, "error": "empty title"}
    video_path = os.path.abspath(video_path)
    os.makedirs(DEBUG_DIR, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context, page = _launch(pw, channel)
        try:
            upload_input = prepare_page(page)
            upload_input.set_input_files(video_path)

            page.wait_for_selector("ytcp-video-metadata-editor", timeout=60000)
            checks = page.get_by_text("Checks complete")
            if not _poll_until(
                lambda: checks.count() > 0, UPLOAD_TIMEOUT_S, interval=3
            ):
                _stop_captioning_prompt(page)
                if not _poll_until(
                    lambda: checks.count() > 0, UPLOAD_TIMEOUT_S, interval=3
                ):
                    page.screenshot(
                        path=os.path.join(DEBUG_DIR, "upload_timeout.png"),
                        full_page=True,
                    )
                    raise StudioPublishError("upload did not finish processing")

            _fill_contenteditable(
                page,
                [
                    page.locator('ytcp-mention-textbox#title-textarea #textbox'),
                    page.locator("#title-textarea"),
                    page.get_by_label("Title"),
                ],
                title,
            )
            _fill_contenteditable(
                page,
                [
                    page.locator(
                        'ytcp-mention-textbox#description-textarea #textbox'
                    ),
                    page.locator("#description-textarea"),
                    page.get_by_label("Description"),
                ],
                description,
            )

            page.mouse.wheel(0, 800)
            _set_visibility(page, privacy)

            kids = page.get_by_role("radio", name="No, it's not made for kids")
            if _poll_until(lambda: kids.count() > 0, 5):
                kids.first.click(timeout=3000)

            publish_button = _first_visible(
                page, [page.locator("ytcp-button#done-button"), page.locator("ytcp-button", has_text="Publish")]
            )
            if publish_button is None:
                raise StudioPublishError("Publish button not found")
            publish_button.click()

            video_url = _wait_for_publish_confirmation(page)
            return {
                "success": True,
                "video_url": video_url,
                "published_to": channel,
            }
        finally:
            context.close()


def check(channel) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context, page = _launch(pw, channel)
        try:
            logged_in = _check_login(page)
            return {
                "success": True,
                "logged_in": logged_in,
                "profile": _profile_dir(channel),
                "channel": channel,
            }
        finally:
            context.close()


def login(channel) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context, page = _launch(pw, channel)
        try:
            page.goto(STUDIO_URL, wait_until="domcontentloaded")
            print(
                f"Please sign in to YouTube in the Chrome window. "
                f"Waiting up to {LOGIN_TIMEOUT_S}s…",
                flush=True,
            )
            ok = _poll_until(
                lambda: not page.url.startswith("https://accounts.google.com")
                and page.url.startswith(STUDIO_URL),
                LOGIN_TIMEOUT_S,
                interval=2,
            )
            return {
                "success": bool(ok),
                "logged_in": bool(ok),
                "profile": _profile_dir(channel),
            }
        finally:
            context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="history-mysteries", help="channel name")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--publish", metavar="MP4")
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--privacy", default="public", choices=("public", "unlisted", "private"))
    args = parser.parse_args()

    try:
        if args.check:
            result = check(args.profile)
        elif args.login:
            result = login(args.profile)
        elif args.publish:
            result = publish(
                args.profile,
                args.publish,
                args.title,
                args.description,
                args.privacy,
            )
        else:
            parser.print_help()
            result = {"success": False, "error": "no action selected"}
    except StudioPublishError as exc:
        result = {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())