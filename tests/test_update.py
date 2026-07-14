import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from update import Message, applied_series_ids, collect_series, is_cold, normalize_title, patch_info


class ParserTests(unittest.TestCase):
    def test_patch_subject(self):
        parsed = patch_info("[PATCH v6 2/7] docs/zh_CN: add DAMON_STAT usage translation")
        self.assertEqual((parsed["version"], parsed["part"], parsed["total"]), (6, 2, 7))

    def test_single_patch(self):
        parsed = patch_info("[PATCH] docs/zh_CN: fix KASAN description")
        self.assertEqual((parsed["version"], parsed["part"], parsed["total"]), (1, 1, 1))

    def test_ignores_reply_and_other_docs(self):
        self.assertIsNone(patch_info("Re: [PATCH] docs/zh_CN: fix it"))
        self.assertIsNone(patch_info("[PATCH] docs/core-api: fix it"))
        self.assertIsNone(patch_info("[PATCH] docs/zh_TW: fix it"))

    def test_normalizes_git_subject(self):
        self.assertEqual(normalize_title("docs/zh_CN: Fix  a thing"), "fix a thing")

    def test_groups_thread_and_keeps_latest_version(self):
        def msg(mid, subject, reply="", refs=None):
            return Message(mid, subject, "A", "a@example.com", "2026-07-01T00:00:00Z", f"https://lore/{mid}/", reply, refs or [])
        messages = [
            msg("cover1", "[PATCH v1 0/2] docs/zh_CN: update foo"),
            msg("one1", "[PATCH v1 1/2] docs/zh_CN: update foo/a", "cover1", ["cover1"]),
            msg("two1", "[PATCH v1 2/2] docs/zh_CN: update foo/b", "cover1", ["cover1"]),
            msg("cover2", "[PATCH v2 0/2] docs/zh_CN: update foo"),
            msg("one2", "[PATCH v2 1/2] docs/zh_CN: update foo/a", "cover2", ["cover2"]),
            msg("two2", "[PATCH v2 2/2] docs/zh_CN: update foo/b", "cover2", ["cover2"]),
        ]
        series = collect_series(messages)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].version, 2)
        self.assertEqual(series[0].total, 2)
        self.assertEqual(len(series[0].previous_versions), 1)
        self.assertEqual(series[0].previous_versions[0]["version"], 1)

    def test_cold_after_more_than_30_days(self):
        now = datetime(2026, 7, 14, tzinfo=timezone.utc)
        self.assertFalse(is_cold("2026-06-14", now))
        self.assertTrue(is_cold("2026-06-13", now))

    def test_applied_status_is_migrated_and_retained_by_exact_revision(self):
        document = {
            "series": [
                {"key": "old", "source": "https://lore/old", "status": "graduated"},
                {"key": "new", "source": "https://lore/new", "status": "applied"},
                {"key": "pending", "source": "https://lore/pending", "status": "cooking"},
            ]
        }
        self.assertEqual(
            applied_series_ids(document),
            {("old", "https://lore/old"), ("new", "https://lore/new")},
        )

    def test_groups_changed_titles_by_author_and_topic(self):
        messages = [
            Message(
                "v1", "[PATCH 1/2] docs/zh_CN: update DAMON usage sysfs documentation",
                "Doehyun", "doe@example.com", "2026-05-23T00:00:00Z", "https://lore/v1/"
            ),
            Message(
                "v3", "[PATCH v3 1/2] docs/zh_CN: update DAMON usage Chinese translation",
                "Doehyun", "doe@example.com", "2026-06-08T00:00:00Z", "https://lore/v3/"
            ),
            Message(
                "v4", "[PATCH v4 2/2] docs/zh_CN: update DAMON documentation translation",
                "Doehyun", "doe@example.com", "2026-06-09T00:00:00Z", "https://lore/v4/"
            ),
            Message(
                "v6", "[PATCH v6 0/7] docs/zh_CN: update DAMON translations",
                "Doehyun", "doe@example.com", "2026-07-08T00:00:00Z", "https://lore/v6/"
            ),
        ]
        series = collect_series(messages)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].version, 6)
        self.assertEqual([item["version"] for item in series[0].previous_versions], [4, 3, 1])


if __name__ == "__main__":
    unittest.main()
