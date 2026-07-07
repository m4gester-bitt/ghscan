import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghscan.cache import Cache
from ghscan.config import Config
from ghscan.discovery import build_scan_queue


def make_config(**overrides):
    base = dict(org="acme", github_token="dummy-token")
    base.update(overrides)
    return Config(**base)


def make_repo(name, **overrides):
    repo = {
        "full_name": name,
        "owner": {"login": name.split("/")[0]},
        "archived": False,
        "fork": False,
        "pushed_at": "2026-06-01T00:00:00Z",
        "stargazers_count": 0,
        "size": 100,
    }
    repo.update(overrides)
    return repo


class TestBuildScanQueue(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.path = Path(path)
        self.cache = Cache(self.path)

    def tearDown(self):
        self.cache.close()
        self.path.unlink(missing_ok=True)

    def test_min_stars_filters_low_star_repos(self):
        self.cache.add_org_repo(make_repo("acme/popular", stargazers_count=50))
        self.cache.add_org_repo(make_repo("acme/unloved", stargazers_count=0))
        cfg = make_config(min_stars=10)
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/popular"]["status"], "pending")
        self.assertEqual(entries["acme/unloved"]["status"], "skipped")
        self.assertEqual(entries["acme/unloved"]["skip_reason"], "below_min_stars")

    def test_max_repo_size_filters_large_repos(self):
        self.cache.add_org_repo(make_repo("acme/small", size=100))
        self.cache.add_org_repo(make_repo("acme/huge", size=999999))
        cfg = make_config(max_repo_size_kb=1000)
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/small"]["status"], "pending")
        self.assertEqual(entries["acme/huge"]["skip_reason"], "over_max_size")

    def test_pushed_within_days_filters_stale_repos(self):
        self.cache.add_org_repo(make_repo("acme/fresh", pushed_at="2026-07-01T00:00:00Z"))
        self.cache.add_org_repo(make_repo("acme/stale", pushed_at="2015-01-01T00:00:00Z"))
        cfg = make_config(pushed_within_days=30)
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/stale"]["skip_reason"], "stale")

    def test_max_queue_size_keeps_most_recently_pushed(self):
        self.cache.add_org_repo(make_repo("acme/old", pushed_at="2020-01-01T00:00:00Z"))
        self.cache.add_org_repo(make_repo("acme/new", pushed_at="2026-06-01T00:00:00Z"))
        cfg = make_config(max_queue_size=1)
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/new"]["status"], "pending")
        self.assertEqual(entries["acme/old"]["skip_reason"], "queue_cap")

    def test_no_filters_queues_everything(self):
        self.cache.add_org_repo(make_repo("acme/a"))
        self.cache.add_org_repo(make_repo("acme/b"))
        cfg = make_config()
        queued_count, skipped_count = None, None
        build_scan_queue(self.cache, cfg)
        entries = self.cache.all_queue_entries()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["status"] == "pending" for e in entries))


if __name__ == "__main__":
    unittest.main()
