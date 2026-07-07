import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghscan.cache import Cache


class TestCache(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.path = Path(path)
        self.cache = Cache(self.path)

    def tearDown(self):
        self.cache.close()
        self.path.unlink(missing_ok=True)

    def test_add_org_repo_dedup(self):
        repo = {"full_name": "acme/widgets", "owner": {"login": "acme"}, "archived": False, "fork": False}
        self.assertTrue(self.cache.add_org_repo(repo))
        self.assertFalse(self.cache.add_org_repo(repo))
        self.assertEqual(len(self.cache.all_org_repos()), 1)

    def test_contributor_sighting_dedup(self):
        self.assertTrue(self.cache.add_contributor_sighting("alice", "acme/widgets"))
        # same contributor, same repo -> not a new sighting, not a new contributor
        self.assertFalse(self.cache.add_contributor_sighting("alice", "acme/widgets"))
        # same contributor, different repo -> new sighting, but not a new *unique* contributor
        self.assertFalse(self.cache.add_contributor_sighting("alice", "acme/gadgets"))
        self.assertEqual(self.cache.count_unique_contributors(), 1)
        self.assertEqual(self.cache.count_contributor_sightings(), 2)

    def test_new_unique_contributor_returns_true_once(self):
        self.assertTrue(self.cache.add_contributor_sighting("bob", "acme/widgets"))
        self.assertEqual(self.cache.count_unique_contributors(), 1)

    def test_enqueue_dedup_across_sources(self):
        self.assertTrue(self.cache.enqueue("acme/widgets", "https://github.com/acme/widgets", "org"))
        self.assertFalse(self.cache.enqueue("acme/widgets", "https://github.com/acme/widgets", "contributor"))
        self.assertEqual(len(self.cache.all_queue_entries()), 1)

    def test_skip_reason_sets_status(self):
        self.cache.enqueue("acme/old", "https://github.com/acme/old", "org", skip_reason="archived")
        row = self.cache.all_queue_entries()[0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(self.cache.pending_scans(), [])

    def test_resume_flags(self):
        repo = {"full_name": "acme/widgets", "owner": {"login": "acme"}, "archived": False, "fork": False}
        self.cache.add_org_repo(repo)
        pending = self.cache.org_repos_pending_contributors()
        self.assertEqual(len(pending), 1)
        self.cache.mark_org_repo_contributors_fetched("acme/widgets")
        self.assertEqual(self.cache.org_repos_pending_contributors(), [])

    def test_reset_stuck_scans(self):
        self.cache.enqueue("acme/widgets", "https://github.com/acme/widgets", "org")
        self.cache.mark_scan_status("acme/widgets", "in_progress")
        self.assertEqual(self.cache.pending_scans(), [])
        reset = self.cache.reset_stuck_scans()
        self.assertEqual(reset, 1)
        self.assertEqual(len(self.cache.pending_scans()), 1)


if __name__ == "__main__":
    unittest.main()
