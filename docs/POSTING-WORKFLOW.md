# Finished shorts and posting text

New finished videos have two companion files with the same base name:

- `.posting.json`: the title, description/caption, hashtags, optional comment,
  channel and task ID used by the uploader.
- `.posting.txt`: clearly separated sections for manual copying.

Copy is created from the actual script even when no publishing account is
connected. This adds one social-copy generation call per completed task, shared
by its output variants. If the provider is unavailable, the narration is used
as a fallback. Review the copy before posting. A comment draft lists footage
sources when available; the app does not post or pin comments automatically.

For planned videos, archiving carries these files into the existing
`<portfolio_root>/outputs/<channel>/<YYYY-MM>/videos/` directory beside the MP4
and subtitles. The video generation result and **Media library → Posting
package** both display the text with copy controls and a text download.
Older archives can show their saved narration as posting copy without a new
provider request. Existing videos are not moved by this change.

## Connect YouTube

1. Restart the app to load the updated code.
2. In Google Cloud, enable YouTube Data API v3 and create OAuth credentials
   for a Desktop app.
3. In **Settings → Auto-Publish Settings → YouTube API publishing**, enter
   the client ID and client secret. Choose the visibility and automatic
   publishing preference.
4. Open the **Plan** page, select the niche and click **Channel sign in**.
   Choose the matching YouTube channel. Repeat once for each niche.
5. For a manual check, select an archived short in **Media library** and
   click **Post video**. Check the result and saved description on YouTube.

OAuth credentials and per-niche tokens stay on this machine. No credentials
are supplied by this code change. Automatic posting requires a connected
channel; generating a video does not establish that connection.

The direct YouTube uploader uses the saved video-specific copy and the
configured visibility. Upload-Post settings remain separate for other social
platforms. Turning off its automatic publishing checkbox now disables that
automatic path while allowing explicit posting.

## Scheduled generation

`daily_youtube_short.sh` passes the niche into generation, waits for the CLI to
finish, archives the video and then invokes the uploader. The generation
process skips its own upload, so those two paths cannot publish the same video
simultaneously. `MPT_AUTO_PUBLISH=0` disables scheduled posting.

To retry a finished archived video from the command line:

```bash
MoneyPrinterTurbo/.venv/bin/python tools/publish_video.py \
  --channel history-mysteries --video /absolute/path/to/video.mp4
```

The tool defaults to YouTube and reads the neighboring posting package.
Explicit `--title`, `--description`, `--tags` and `--privacy` override its values.

## Upload records

A `.publish.json` receipt records the video fingerprint, channel and upload
results. Successful targets are skipped on retry, including after archiving
and restarting the app. Partial failure is reported as failure while retaining
successful results. An explicit rejection or disconnected account can be
retried after fixing the problem.

A `.publish.lock` blocks concurrent uploads. Interrupted requests or network
errors can leave the outcome unknown: the app refuses to upload again until
the remote account has been checked. If the video exists remotely, keep its
record rather than retrying. If it definitely was not uploaded, back up the
receipt before clearing the pending/unknown record or stale lock. The app
never clears those automatically.

Live uploads, OAuth, video quality and copy quality are left for manual testing.
Automated checks use temporary files and mocked publishing providers.
