import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghscan.cache import Cache
from ghscan.config import Config, looks_wide_open, parse_args
from ghscan.discovery import build_scan_queue, discover_org_repos
from ghscan.scanner import refresh_incremental_rescans


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
        "created_at": "2020-01-01T00:00:00Z",
        "stargazers_count": 0,
        "size": 100,
        "language": "Python",
    }
    repo.update(overrides)
    return repo


class CacheBackedTest(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.path = Path(path)
        self.cache = Cache(self.path)

    def tearDown(self):
        self.cache.close()
        self.path.unlink(missing_ok=True)


class TestOrgReposOnlyBugFix(CacheBackedTest):
    """Regression test for the --org-repos-only bug: contributor discovery
    must not run at all when include_contributor_repos_in_scan is False."""

    def test_discover_contributors_and_repos_are_never_called(self):
        client = MagicMock()
        client.list_org_repos.return_value = iter([make_repo("acme/widgets")])
        cfg = make_config(include_contributor_repos_in_scan=False)

        discover_org_repos(client, self.cache, cfg)

        client.list_repo_contributors.assert_not_called()
        client.list_org_members.assert_not_called()
        client.list_user_repos.assert_not_called()


class TestExclusionFilters(CacheBackedTest):
    def test_exclude_repo_glob(self):
        self.cache.add_org_repo(make_repo("acme/keep-me"))
        self.cache.add_org_repo(make_repo("acme/upstream-fork-1"))
        cfg = make_config(exclude_repos=["acme/upstream-fork-*"])
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/keep-me"]["status"], "pending")
        self.assertEqual(entries["acme/upstream-fork-1"]["skip_reason"], "excluded_repo")

    def test_language_include_filter(self):
        self.cache.add_org_repo(make_repo("acme/py-thing", language="Python"))
        self.cache.add_org_repo(make_repo("acme/js-thing", language="JavaScript"))
        cfg = make_config(languages={"python"})
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/py-thing"]["status"], "pending")
        self.assertEqual(entries["acme/js-thing"]["skip_reason"], "language_not_included")

    def test_exclude_language_filter(self):
        self.cache.add_org_repo(make_repo("acme/py-thing", language="Python"))
        cfg = make_config(exclude_languages={"python"})
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/py-thing"]["skip_reason"], "excluded_language")

    def test_min_repo_age_days_filters_new_repos(self):
        import time
        recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.cache.add_org_repo(make_repo("acme/brand-new", created_at=recent))
        self.cache.add_org_repo(make_repo("acme/established", created_at="2018-01-01T00:00:00Z"))
        cfg = make_config(min_repo_age_days=30)
        build_scan_queue(self.cache, cfg)
        entries = {r["full_name"]: r for r in self.cache.all_queue_entries()}
        self.assertEqual(entries["acme/brand-new"]["skip_reason"], "too_new")
        self.assertEqual(entries["acme/established"]["status"], "pending")


class TestSortQueueBy(CacheBackedTest):
    def test_sort_by_stars_orders_pending_queue(self):
        self.cache.add_org_repo(make_repo("acme/low", stargazers_count=1))
        self.cache.add_org_repo(make_repo("acme/high", stargazers_count=100))
        cfg = make_config(sort_queue_by="stars")
        build_scan_queue(self.cache, cfg)
        pending = self.cache.pending_scans()
        self.assertEqual(pending[0]["full_name"], "acme/high")
        self.assertEqual(pending[1]["full_name"], "acme/low")


class TestIncrementalRescan(CacheBackedTest):
    def test_repo_with_new_push_is_requeued(self):
        self.cache.add_org_repo(make_repo("acme/widgets", pushed_at="2020-01-01T00:00:00Z"))
        cfg = make_config()
        build_scan_queue(self.cache, cfg)
        self.cache.mark_scan_status("acme/widgets", "done")
        self.cache.save_scan_result({
            "full_name": "acme/widgets", "status": "success",
            "started_at": 1577836800.0, "finished_at": 1577836800.0,
        })
        # bump pushed_at to "now" to simulate a new commit landing after the scan
        with self.cache._cursor() as cur:
            cur.execute(
                "UPDATE org_repos SET pushed_at=? WHERE full_name=?",
                ("2026-07-01T00:00:00Z", "acme/widgets"),
            )
        reset = refresh_incremental_rescans(self.cache)
        self.assertEqual(reset, 1)
        pending = [r["full_name"] for r in self.cache.pending_scans()]
        self.assertIn("acme/widgets", pending)

    def test_repo_without_new_push_stays_done(self):
        self.cache.add_org_repo(make_repo("acme/widgets", pushed_at="2020-01-01T00:00:00Z"))
        cfg = make_config()
        build_scan_queue(self.cache, cfg)
        self.cache.mark_scan_status("acme/widgets", "done")
        self.cache.save_scan_result({
            "full_name": "acme/widgets", "status": "success",
            "started_at": time_now(), "finished_at": time_now(),
        })
        reset = refresh_incremental_rescans(self.cache)
        self.assertEqual(reset, 0)


def time_now():
    import time
    return time.time()


class TestWideOpenDetection(unittest.TestCase):
    def test_org_members_with_no_filters_is_wide_open(self):
        cfg = make_config(member_mode="org-members")
        self.assertTrue(looks_wide_open(cfg))

    def test_contributors_mode_is_never_flagged(self):
        cfg = make_config(member_mode="contributors")
        self.assertFalse(looks_wide_open(cfg))

    def test_org_members_with_a_filter_is_not_flagged(self):
        cfg = make_config(member_mode="org-members", min_stars=5)
        self.assertFalse(looks_wide_open(cfg))


class TestArgParsing(unittest.TestCase):
    def test_exclude_repo_and_login_are_repeatable(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
            cfg = parse_args([
                "--org", "acme",
                "--exclude-repo", "acme/old-fork",
                "--exclude-repo", "acme/other-fork",
                "--exclude-login", "SomeBot",
            ])
        self.assertEqual(set(cfg.exclude_repos), {"acme/old-fork", "acme/other-fork"})
        self.assertEqual(cfg.exclude_logins, {"somebot"})

    def test_dry_run_and_yes_flags(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
            cfg = parse_args(["--org", "acme", "--dry-run", "--yes"])
        self.assertTrue(cfg.dry_run)
        self.assertTrue(cfg.yes)

    def test_save_flag_defaults_false(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
            cfg = parse_args(["--org", "acme"])
        self.assertFalse(cfg.save_files)


if __name__ == "__main__":
    unittest.main()
