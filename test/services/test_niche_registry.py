import os
import tempfile
import unittest
from pathlib import Path

from app.services.niche_brand import NicheProfile


class TestNicheRegistry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.store = self.root / "niches.json"
        self.portfolio = self.root / "portfolio"
        self.portfolio.mkdir()
        os.environ["MPT_NICHE_STORE"] = str(self.store)
        os.environ["MPT_PORTFOLIO_ROOT"] = str(self.portfolio)
        # 重新加载，避免上一个用例的缓存。
        from app.services import niche_registry, niche_brand

        self.registry = niche_registry
        self.brand = niche_brand
        self.brand.refresh_niches()

    def tearDown(self):
        os.environ.pop("MPT_NICHE_STORE", None)
        os.environ.pop("MPT_PORTFOLIO_ROOT", None)
        self._tmp.cleanup()

    def test_seeds_builtin_profiles(self):
        profiles = self.registry.load_profiles()
        self.assertIn("history-mysteries", profiles)
        self.assertIn("space-science", profiles)
        self.assertEqual(len(profiles), 4)

    def test_upsert_and_reload(self):
        profile = NicheProfile(
            channel="test-niche",
            display_name="Test Niche",
            brand_line="every day we test one thing",
            teaser_shapes=("open on the number",),
            avoid=("fluff",),
            min_seconds=60,
            target_words=200,
            longform_target_seconds=480,
            longform_band="medium",
        )
        self.registry.upsert_niche(
            profile,
            topics=["topic one", "topic two"],
            automation={"VIDEO_ASPECT": "16:9", "VOICE_NAME": "en-US-JennyNeural"},
            system_prompt="system prompt body",
            content_style="# style",
            channel_description="desc",
        )
        self.assertTrue(self.store.is_file())
        reloaded = self.registry.load_profiles()
        self.assertIn("test-niche", reloaded)
        self.assertEqual(reloaded["test-niche"].display_name, "Test Niche")
        self.assertEqual(self.registry.topics_for("test-niche"), ["topic one", "topic two"])
        record = self.registry.get_record("test-niche")
        self.assertEqual(record["automation"]["VIDEO_ASPECT"], "16:9")
        self.assertEqual(record["system_prompt"], "system prompt body")

        # 计划目录已同步。
        topics_file = self.portfolio / "plans" / "test-niche" / "topics_30day.txt"
        self.assertTrue(topics_file.is_file())
        self.assertIn("topic one", topics_file.read_text(encoding="utf-8"))
        env_file = self.portfolio / "plans" / "test-niche" / "automation.env"
        self.assertIn("VIDEO_ASPECT=16:9", env_file.read_text(encoding="utf-8"))
        self.assertTrue((self.portfolio / "plans" / "test-niche" / "system_prompt.txt").is_file())

    def test_update_preserves_unspecified_fields(self):
        profile = NicheProfile(
            channel="test-niche",
            display_name="Test Niche",
            brand_line="line",
            teaser_shapes=("shape",),
            min_seconds=60,
            target_words=200,
        )
        self.registry.upsert_niche(profile, topics=["a"], system_prompt="prompt")
        # 第二次只更新 display_name，topics/system_prompt 应保留。
        profile2 = NicheProfile(
            channel="test-niche",
            display_name="Renamed",
            brand_line="line",
            teaser_shapes=("shape",),
            min_seconds=60,
            target_words=200,
        )
        self.registry.upsert_niche(profile2)
        self.assertEqual(self.registry.topics_for("test-niche"), ["a"])
        self.assertEqual(self.registry.get_record("test-niche")["system_prompt"], "prompt")

    def test_delete_archives_plan_dir(self):
        profile = NicheProfile(
            channel="doomed",
            display_name="Doomed",
            brand_line="line",
            teaser_shapes=("shape",),
            min_seconds=60,
            target_words=200,
        )
        self.registry.upsert_niche(profile, topics=["x"])
        plan_dir = self.portfolio / "plans" / "doomed"
        self.assertTrue(plan_dir.is_dir())
        self.registry.delete_niche("doomed")
        self.assertNotIn("doomed", self.registry.load_profiles())
        self.assertFalse(plan_dir.exists())
        archive = self.portfolio / "plans" / "_archive"
        self.assertTrue(any(p.name.startswith("doomed-") for p in archive.iterdir()))

    def test_validation_rejects_bad_slug(self):
        profile = NicheProfile(
            channel="Bad Slug",
            display_name="X",
            brand_line="line",
            teaser_shapes=("shape",),
            min_seconds=60,
            target_words=200,
        )
        with self.assertRaises(self.registry.NicheRegistryError):
            self.registry.upsert_niche(profile)

    def test_validation_rejects_out_of_band_longform(self):
        profile = NicheProfile(
            channel="bad-band",
            display_name="X",
            brand_line="line",
            teaser_shapes=("shape",),
            min_seconds=60,
            target_words=200,
            longform_target_seconds=9999,
            longform_band="medium",
        )
        with self.assertRaises(self.registry.NicheRegistryError):
            self.registry.upsert_niche(profile)

    def test_brand_refreshes_after_upsert(self):
        profile = NicheProfile(
            channel="fresh",
            display_name="Fresh",
            brand_line="fresh line",
            teaser_shapes=("shape",),
            min_seconds=60,
            target_words=200,
        )
        self.registry.upsert_niche(profile)
        self.assertIn("fresh", self.brand.NICHES)
        self.assertEqual(self.brand.resolve_niche("fresh").display_name, "Fresh")


if __name__ == "__main__":
    unittest.main()