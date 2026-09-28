import json
import os
import re
import tempfile
import unittest

from app.services import campaign as campaign_service
from app.services.campaign import CampaignSource


def _test_source_dict():
    return {
        "kind": "notion",
        "url": "https://mediamaxxing.notion.site/lovable-clipping",
        "title": "Lovable Clipping",
        "text": "Main goal is tier1 views.\nMust show Anton.\n",
        "refs": [{"title": "podcast", "url": "https://www.youtube.com/watch?v=jr9ESVOtyMo"}],
    }


def _test_brief_dict():
    return {
        "campaign_name": "Lovable Clipping",
        "brand": "Lovable",
        "goal": "tier 1 short-form views",
        "audience": "startup founders",
        "tone": "authentic",
        "platforms": ["youtube shorts"],
        "hashtags": ["LovablePartner", "AI"],
        "requirements": ["show Anton"],
        "source_materials": ["https://www.youtube.com/watch?v=jr9ESVOtyMo"],
        "content_types": ["clip"],
        "posting_notes": "submit quickly",
    }


def _test_posts_list():
    return [
        {
            "title": "Anton explains the flow",
            "hook": "This is how speed changed.",
            "script": "Intro.\n\nBody.",
            "on_screen_text": "Lovable",
            "description": "Short but real.",
            "hashtags": ["LovablePartner"],
            "cta": "Follow for more.",
            "ready_to_post": "Anton explains the flow\n\nShort but real.",
        }
    ]


class TestCampaignPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "campaigns")
        os.makedirs(self.root, exist_ok=True)
        self.source = CampaignSource.from_dict(_test_source_dict())
        self.brief = _test_brief_dict()
        self.posts = _test_posts_list()

    def test_save_campaign_writes_json_and_markdown(self):
        meta = campaign_service.save_campaign(
            self.source, self.brief, self.posts, root=self.root
        )

        campaign_id = meta["campaign_id"]
        self.assertTrue(
            re.fullmatch(r"lovable-clipping-\d{8}-\d{6}", campaign_id), campaign_id
        )
        self.assertEqual(meta["name"], "Lovable Clipping")
        self.assertEqual(meta["post_count"], 1)
        self.assertEqual(meta["url"], self.source.url)

        payload = json.loads(
            (open(os.path.join(self.root, campaign_id + ".json"), encoding="utf-8")).read()
        )
        self.assertEqual(payload["campaign_id"], campaign_id)
        self.assertEqual(payload["source"]["url"], self.source.url)
        self.assertEqual(payload["brief"]["campaign_name"], "Lovable Clipping")
        self.assertEqual(len(payload["posts"]), 1)
        self.assertIn("saved_at", payload)

        md_path = os.path.join(self.root, campaign_id + ".md")
        self.assertTrue(os.path.isfile(md_path))
        md_text = open(md_path, encoding="utf-8").read()
        self.assertIn("Lovable Clipping", md_text)

    def test_save_campaign_writes_atomically_without_leftovers(self):
        before = set(os.listdir(self.root))
        meta = campaign_service.save_campaign(
            self.source, self.brief, self.posts, root=self.root
        )
        after = set(os.listdir(self.root))

        leftovers = {
            name
            for name in after - before
            if name.endswith(".tmp") or name.startswith(".campaign-")
        }
        self.assertEqual(leftovers, set())
        self.assertEqual(
            {meta["campaign_id"] + ".json", meta["campaign_id"] + ".md"},
            after - before,
        )
        json.loads(open(os.path.join(self.root, meta["campaign_id"] + ".json"), encoding="utf-8").read())

    def test_save_campaign_respects_explicit_id(self):
        meta = campaign_service.save_campaign(
            self.source,
            self.brief,
            self.posts,
            campaign_id="my-campaign",
            root=self.root,
        )
        self.assertEqual(meta["campaign_id"], "my-campaign")
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "my-campaign.json"))
        )
        self.assertTrue(os.path.isfile(os.path.join(self.root, "my-campaign.md")))

    def test_save_campaign_rejects_unsafe_id(self):
        with self.assertRaises(ValueError):
            campaign_service.save_campaign(
                self.source,
                self.brief,
                self.posts,
                campaign_id="../escape",
                root=self.root,
            )

    def test_list_saved_campaigns_sorted_newest_first(self):
        for campaign_id, saved_at in (("older", "2026-08-01T09:00"), ("newer", "2026-09-01T09:00")):
            payload = {
                "campaign_id": campaign_id,
                "name": "Campaign " + campaign_id,
                "saved_at": saved_at,
                "source": self.source.to_dict(),
                "brief": self.brief,
                "posts": self.posts,
            }
            with open(os.path.join(self.root, campaign_id + ".json"), "w", encoding="utf-8") as f:
                json.dump(payload, f)

        campaigns = campaign_service.list_saved_campaigns(root=self.root)
        self.assertEqual([item["campaign_id"] for item in campaigns], ["newer", "older"])
        self.assertEqual(campaigns[0]["post_count"], 1)
        self.assertEqual(campaigns[0]["url"], self.source.url)

    def test_list_saved_campaigns_skips_corrupt_files(self):
        with open(os.path.join(self.root, "corrupt.json"), "w", encoding="utf-8") as f:
            f.write("not json")
        campaign_service.save_campaign(
            self.source, self.brief, self.posts, campaign_id="good", root=self.root
        )

        campaigns = campaign_service.list_saved_campaigns(root=self.root)
        self.assertEqual([item["campaign_id"] for item in campaigns], ["good"])

    def test_list_saved_campaigns_empty_when_root_missing(self):
        self.assertEqual(
            campaign_service.list_saved_campaigns(root=os.path.join(self.tmp, "missing")),
            [],
        )

    def test_saved_campaign_options_disambiguate_duplicate_labels(self):
        # 同一秒保存的两个活动 name+saved_at 相同，label 冲突时必须追加
        # campaign_id 消歧，否则后者会覆盖前者、无法被单独加载。
        campaigns = [
            {"name": "Love Clip", "saved_at": "2026-09-01T09:00:00", "campaign_id": "b"},
            {"name": "Love Clip", "saved_at": "2026-09-01T09:00:00", "campaign_id": "a"},
        ]
        options = campaign_service.saved_campaign_options(campaigns)
        self.assertEqual(set(options.values()), {"a", "b"})
        self.assertEqual(len(options), 2)
        self.assertEqual(
            options.get("Love Clip (2026-09-01T09:00:00)"),
            "b",
        )
        self.assertEqual(
            options["Love Clip (2026-09-01T09:00:00) · a"],
            "a",
        )

    def test_load_saved_campaign_round_trip(self):
        campaign_service.save_campaign(
            self.source, self.brief, self.posts, campaign_id="rt", root=self.root
        )
        loaded = campaign_service.load_saved_campaign("rt", root=self.root)

        self.assertEqual(loaded["campaign_id"], "rt")
        self.assertIsInstance(loaded["source"], CampaignSource)
        self.assertEqual(loaded["source"].url, self.source.url)
        self.assertEqual(loaded["brief"]["campaign_name"], "Lovable Clipping")
        self.assertEqual(loaded["posts"][0]["title"], "Anton explains the flow")

    def test_load_saved_campaign_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            campaign_service.load_saved_campaign("../../etc/passwd", root=self.root)

    def test_load_saved_campaign_raises_on_missing(self):
        with self.assertRaises(OSError):
            campaign_service.load_saved_campaign("does-not-exist", root=self.root)


if __name__ == "__main__":
    unittest.main()