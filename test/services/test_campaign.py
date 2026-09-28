import json
import os
import unittest
from unittest.mock import call, patch

from app.services import campaign


class _FakeHtmlResponse:
    def __init__(self, text):
        self.text = text
        self.headers = {}

    def raise_for_status(self):
        return None


class TestClassifyCampaignLink(unittest.TestCase):
    def test_notion_site(self):
        self.assertEqual(
            campaign.classify_campaign_link("https://mediamaxxing.notion.site/lovable-clipping")[0],
            "notion",
        )

    def test_notion_so(self):
        kind, url = campaign.classify_campaign_link("https://x.notion.so/abc123")
        self.assertEqual(kind, "notion")
        self.assertTrue(url.endswith(".so/abc123"))

    def test_drive_file(self):
        kind, _ = campaign.classify_campaign_link(
            "https://drive.google.com/file/d/1abc/view?usp=sharing"
        )
        self.assertEqual(kind, "drive_file")

    def test_drive_folder(self):
        kind, _ = campaign.classify_campaign_link(
            "https://drive.google.com/drive/folders/0Bx"
        )
        self.assertEqual(kind, "drive_folder")

    def test_drive_uc_id(self):
        kind, _ = campaign.classify_campaign_link(
            "https://drive.google.com/uc?id=1abc&export=download"
        )
        self.assertEqual(kind, "drive_file")

    def test_docs_document(self):
        kind, _ = campaign.classify_campaign_link(
            "https://docs.google.com/document/d/1abc/edit"
        )
        self.assertEqual(kind, "docs")

    def test_generic_url(self):
        kind, url = campaign.classify_campaign_link("https://example.com/page")
        self.assertEqual(kind, "url")
        self.assertEqual(url, "https://example.com/page")

    def test_plain_text(self):
        kind, url = campaign.classify_campaign_link(
            "Campaign: show Anton and Lovable. Use #LovablePartner"
        )
        self.assertEqual(kind, "text")
        self.assertEqual(url, "")

    def test_empty(self):
        self.assertEqual(campaign.classify_campaign_link("  ")[0], "text")


class TestExtractDriveId(unittest.TestCase):
    def test_file_d(self):
        self.assertEqual(
            campaign._extract_drive_id("https://drive.google.com/file/d/1AbC-/view"),
            "1AbC-",
        )

    def test_uc_id(self):
        self.assertEqual(
            campaign._extract_drive_id("https://drive.google.com/uc?id=1xyz&export=1"),
            "1xyz",
        )

    def test_docs_d(self):
        self.assertEqual(
            campaign._extract_drive_id("https://docs.google.com/document/d/2zZZ/edit"),
            "2zZZ",
        )

    def test_missing(self):
        self.assertEqual(campaign._extract_drive_id("https://example.com/x"), "")


class TestParseJsonResponse(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(
            campaign._parse_json_response('{"campaign_name": "x"}')["campaign_name"], "x"
        )

    def test_fenced_object(self):
        text = '```json\n{"goal": "g"}\n```'
        self.assertEqual(campaign._parse_json_response(text)["goal"], "g")

    def test_array(self):
        self.assertEqual(campaign._parse_json_response('[{"a": 1}]'), [{"a": 1}])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            campaign._parse_json_response("no json here")


class TestCampaignSourceFetch(unittest.TestCase):
    def test_plain_text_source(self):
        source = campaign.fetch_campaign_source("Plain campaign brief text.")
        self.assertEqual(source.kind, "text")
        self.assertEqual(source.text, "Plain campaign brief text.")

    def test_notion_uses_playwright_render(self):
        with patch.object(campaign, "_render_notion_body") as render:
            async def fake_render(url):
                return (
                    "Skip to content\nLovable Clipping\n"
                    "Must show Anton. #LovablePartner\n"
                    "https://www.youtube.com/watch?v=jr9ESVOtyMo"
                )

            render.side_effect = fake_render
            source = campaign.fetch_campaign_source(
                "https://mediamaxxing.notion.site/lovable-clipping"
            )
        self.assertEqual(source.kind, "notion")
        self.assertEqual(source.title, "Lovable Clipping")
        self.assertTrue("#LovablePartner" in source.text)
        self.assertTrue(source.refs)

    def test_generic_url_keeps_text(self):
        source = campaign.fetch_campaign_source("https://example.com/some-campaign")
        self.assertEqual(source.kind, "url")
        self.assertIn("example.com", source.text)

    def test_drive_file_binary_becomes_media_ref(self):
        with patch.object(campaign, "_drive_download_text", return_value=("", "clip.mp4")) as dl:
            source = campaign.fetch_campaign_source(
                "https://drive.google.com/file/d/1vid/view"
            )
        dl.assert_called_once()
        self.assertEqual(source.kind, "drive_file")
        self.assertEqual(source.text, "")
        self.assertEqual(len(source.refs), 1)
        self.assertIn("1vid", source.refs[0].url)


class TestDriveDownloadText(unittest.TestCase):
    def test_text_content_type_decodes(self):
        class FakeRaw:
            def read(self, n):
                return b"brief text"

        class FakeResponse:
            headers = {"Content-Type": "text/plain"}
            raw = FakeRaw()

            def raise_for_status(self):
                pass

        with patch("requests.get", return_value=FakeResponse()):
            text, filename = campaign._drive_download_text("https://example.com/x")
        self.assertEqual(text, "brief text")
        self.assertEqual(filename, "")

    def test_binary_content_returns_empty_text(self):
        class FakeResponse:
            headers = {"Content-Type": "video/mp4"}

            def raise_for_status(self):
                pass

        with patch("requests.get", return_value=FakeResponse()):
            text, _ = campaign._drive_download_text("https://example.com/x")
        self.assertEqual(text, "")


class TestCampaignSourceRoundTrip(unittest.TestCase):
    def test_to_dict_from_dict_round_trip(self):
        source = campaign.CampaignSource(
            kind="notion",
            url="https://x",
            title="T",
            text="body",
            refs=[campaign.MediaRef(title="clip", url="https://y")],
        )
        restored = campaign.CampaignSource.from_dict(source.to_dict())
        self.assertEqual(restored.kind, "notion")
        self.assertEqual(restored.refs[0].url, "https://y")

    def test_from_dict_normalizes_dict_refs(self):
        source = campaign.CampaignSource.from_dict(
            {
                "kind": "drive_file",
                "url": "https://d",
                "title": "F",
                "text": "",
                "refs": [{"title": "media.mp4", "url": "https://d/media.mp4"}],
            }
        )
        self.assertIsInstance(source.refs[0], campaign.MediaRef)
        self.assertEqual(source.refs[0].title, "media.mp4")


class TestUnderstandAndGenerate(unittest.TestCase):
    def test_understand_campaign_parses_brief(self):
        raw = json.dumps(
            {
                "campaign_name": "Lovable Clipping",
                "brand": "Lovable",
                "goal": "drive views",
                "audience": "startup founders",
                "tone": "authentic",
                "platforms": ["youtube"],
                "hashtags": ["LovablePartner"],
                "requirements": ["show Anton"],
                "source_materials": ["https://www.youtube.com/watch?v=jr9ESVOtyMo"],
                "content_types": ["clip"],
                "posting_notes": "post within 10 min",
            }
        )
        with patch.object(
            campaign.llm, "_generate_response", return_value=raw
        ) as gen:
            brief = campaign.understand_campaign(
                campaign.CampaignSource(text="Must show Anton and Lovable.")
            )
        gen.assert_called_once()
        self.assertEqual(brief["brand"], "Lovable")
        self.assertEqual(brief["hashtags"], ["LovablePartner"])

    def test_understand_campaign_surfaces_llm_error(self):
        with patch.object(
            campaign.llm, "_generate_response", return_value="Error: boom"
        ):
            with self.assertRaises(ValueError) as ctx:
                campaign.understand_campaign(campaign.CampaignSource(text="x"))
        self.assertIn("boom", str(ctx.exception))

    def test_generate_campaign_posts_normalizes(self):
        raw = json.dumps(
            [
                {
                    "title": "Anton on Lovable",
                    "hook": "This changed everything.",
                    "script": "Para one.\n\nPara two.\n\nPara three.",
                    "on_screen_text": "Lovable",
                    "description": "Watch this.",
                    "hashtags": ["LovablePartner", "AI"],
                    "cta": "Follow us.",
                }
            ]
        )
        with patch.object(
            campaign.llm, "_generate_response", return_value=raw
        ) as gen:
            posts = campaign.generate_campaign_posts({"brand": "Lovable"}, count=1)
        gen.assert_called_once()
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["hashtags"], ["LovablePartner", "AI"])
        self.assertIn("#LovablePartner", post["ready_to_post"])
        self.assertIn("Para three", post["script"])

    def test_generate_campaign_posts_count_clamped(self):
        with patch.object(
            campaign.llm, "_generate_response", return_value="[]"
        ) as gen:
            with self.assertRaises(ValueError):
                campaign.generate_campaign_posts({}, count=999)
        request = gen.call_args.kwargs["prompt"]
        self.assertIn("generate 20 distinct", request)

    def test_generate_prompt_embeds_retention_and_humanizer_rules(self):
        with patch.object(
            campaign.llm, "_generate_response", return_value="[]"
        ) as gen:
            with self.assertRaises(ValueError):
                campaign.generate_campaign_posts({}, count=1)
        request = gen.call_args.kwargs["prompt"]
        self.assertIn("hook first", request)
        self.assertIn("no em dashes or en dashes", request)
        self.assertIn("no forced triads", request)
        self.assertIn("one person speaking", request)

    def test_brief_prompt_refuses_inventing_requirements(self):
        text = "Launch on TikTok. Use #Launch2026. Show the new app."
        prompt = campaign._BRIEF_PROMPT.format(text=text)
        self.assertIn("Do not invent campaign goals", prompt)
        self.assertIn(text, prompt)


class TestBuildMarkdown(unittest.TestCase):
    def test_markdown_includes_brief_and_posts(self):
        source = campaign.CampaignSource(title="Camp", url="https://x", text="src")
        brief = {"campaign_name": "Camp", "brand": "B", "goal": "g", "audience": "a",
                 "tone": "t", "platforms": ["yt"], "hashtags": ["LovablePartner"],
                 "requirements": ["r1"], "posting_notes": "notes"}
        posts = [
            {"title": "P1", "hook": "h", "script": "s", "on_screen_text": "o",
             "description": "d", "hashtags": ["LovablePartner"], "cta": "c",
             "ready_to_post": "P1\n\nd\n\nc\n\n#LovablePartner"}
        ]
        md = campaign.build_campaign_markdown(source, brief, posts)
        self.assertIn("# Camp", md)
        self.assertIn("P1", md)
        self.assertIn("#LovablePartner", md)


class TestCampaignDriveFolder(unittest.TestCase):
    def test_classify_drive_folder_variants(self):
        for url in (
            "https://drive.google.com/drive/folders/0BxCampaign",
            "https://drive.google.com/drive/u/0/folders/0BxCampaign",
            "https://drive.google.com/open?id=0BxCampaign",
        ):
            with self.subTest(url=url):
                self.assertEqual(campaign.classify_campaign_link(url)[0], "drive_folder")
        self.assertEqual(
            campaign.classify_campaign_link(
                "https://drive.google.com/file/d/1AbC/view"
            )[0],
            "drive_file",
        )

    def test_extract_drive_id_from_url_variants(self):
        for url in (
            "https://drive.google.com/drive/folders/0BxCampaign",
            "https://drive.google.com/drive/u/0/folders/0BxCampaign",
            "https://drive.google.com/open?id=0BxCampaign",
        ):
            with self.subTest(url=url):
                self.assertEqual(campaign._extract_drive_id(url), "0BxCampaign")

    def test_embedded_folder_files_parses_real_html(self):
        html = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            "<title>Campaign Assets - Google Drive</title></head><body>"
            '<div class="flipview-container">'
            '<div class="picker-item">'
            '<a target="_blank" href="/file/d/CAMPAIGNFILE1/view?usp=drivesdk"'
            ' data-tooltip="Campaign Brief.txt">'
            '<div class="picker-name"><span class="picker-name-label">'
            "Campaign Brief.txt</span></div></a></div>"
            '<a target="_blank" href="/file/d/CAMPAIGNFILE2/view" aria-label="mp3">'
            "<span>Outro Music - Edited.mp3</span></a>"
            '<a target="_blank" href="/file/d/CAMPAIGNFILE3/view">'
            '<div class="picker-name"><span>2026 Hooks</span>'
            "<span> (Draft)</span></div></a>"
            '<a target="_blank" href="/drive/folders/NESTEDFOLDER">'
            "<span>Raw assets</span></a>"
            "</div></body></html>"
        )
        with patch("requests.get", return_value=_FakeHtmlResponse(html)) as mock_get:
            files = campaign._embedded_folder_files("0BxFOLDER")
        mock_get.assert_called_once()
        self.assertEqual(
            mock_get.call_args[0][0],
            "https://drive.google.com/embeddedfolderview?id=0BxFOLDER",
        )
        self.assertEqual(
            files,
            [
                (
                    "Campaign Brief.txt",
                    "https://drive.google.com/uc?export=download&id=CAMPAIGNFILE1",
                ),
                (
                    "Outro Music - Edited.mp3",
                    "https://drive.google.com/uc?export=download&id=CAMPAIGNFILE2",
                ),
                (
                    "2026 Hooks (Draft)",
                    "https://drive.google.com/uc?export=download&id=CAMPAIGNFILE3",
                ),
            ],
        )
        for name, _ in files:
            self.assertNotIn("<", name)

    def test_embedded_folder_files_caps_at_max(self):
        html = "\n".join(
            f'<a href="/file/d/FOLDERFILE{i}"><span>File {i}.txt</span></a>'
            for i in range(8)
        )
        with patch("requests.get", return_value=_FakeHtmlResponse(html)), patch.object(
            campaign, "_MAX_FOLDER_FILES", 3
        ):
            files = campaign._embedded_folder_files("0BxCAP")
        self.assertEqual(len(files), 3)
        self.assertEqual(
            files[2][1],
            "https://drive.google.com/uc?export=download&id=FOLDERFILE2",
        )

    def test_fetch_drive_folder_end_to_end(self):
        download_urls = [
            "https://drive.google.com/uc?export=download&id=FOLDERFILE_A",
            "https://drive.google.com/uc?export=download&id=FOLDERFILE_B",
        ]
        with patch.object(
            campaign,
            "_embedded_folder_files",
            return_value=[
                ("Campaign Brief.txt", download_urls[0]),
                ("a.txt", download_urls[1]),
            ],
        ) as list_folder, patch.object(
            campaign,
            "_drive_download_text",
            side_effect=[("Content A", "a.txt"), RuntimeError("no access")],
        ) as download:
            source = campaign._fetch_drive_folder(
                "https://drive.google.com/drive/folders/0BxFOLDER"
            )
        list_folder.assert_called_once_with("0BxFOLDER")
        download.assert_has_calls([call(url) for url in download_urls])
        self.assertEqual(source.kind, "drive_folder")
        self.assertIn("## 1. ", source.text)
        self.assertIn("Content A", source.text)
        self.assertNotIn("## 2.", source.text)
        self.assertEqual(len(source.refs), 1)
        self.assertEqual(source.refs[0].title, "a.txt")
        self.assertEqual(source.refs[0].url, download_urls[1])

    @unittest.skipUnless(
        os.environ.get("MPT_RUN_INTEGRATION_TESTS"),
        "MPT_RUN_INTEGRATION_TESTS not set",
    )
    def test_smoke_real_public_drive_folder(self):
        """真实网络的冒烟测试(默认跳过)。

        仓库没有维护可长期公开访问的 Drive 文件夹 ID，因此这里是占位：设置
        MPT_RUN_INTEGRATION_TESTS=1 且 MPT_PUBLIC_DRIVE_FOLDER_ID 指向一个公开
        文件夹 id 时，才会真正发出网络请求。
        """
        folder_id = os.environ.get("MPT_PUBLIC_DRIVE_FOLDER_ID", "")
        if not folder_id:
            self.skipTest(
                "no stable public Drive folder id; set MPT_PUBLIC_DRIVE_FOLDER_ID"
            )
        source = campaign.fetch_campaign_source(
            f"https://drive.google.com/drive/folders/{folder_id}"
        )
        self.assertEqual(source.kind, "drive_folder")


if __name__ == "__main__":
    unittest.main()