from __future__ import annotations

import getpass
import logging
import os
import sys
import time

from .banner import print_banner
from .cache import Cache
from .config import Config, looks_wide_open, parse_args
from .discovery import (
    build_scan_queue,
    check_fork_ahead_status,
    discover_contributor_repos,
    discover_contributors,
    discover_org_members,
    discover_org_repos,
)
from .github_client import GitHubAPIError, GitHubClient
from .logging_setup import setup_logging
from .report import generate_report
from .scanner import run_scans

logger = logging.getLogger("ghscan.main")

_last_scan_ts = None
_retry_hint = "retry later"


def _noop_check(config):
    return config is not None


def _confirm_wide_open_run(config: Config) -> bool:
    if config.yes or not sys.stdin.isatty():
        return True
    print(
        f"\nHeads up: this run has no volume filters and will expand every "
        f"member of '{config.org}' into their personal repo list -- that can "
        f"mean thousands of repos on a large org.\n"
        f"Pass --yes to skip this next time, or add a filter like "
        f"--max-queue-size / --min-stars / --pushed-within-days.\n"
    )
    answer = input("Continue anyway? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _ensure_github_token(config: Config) -> bool:
    if config.github_token:
        return True

    if not sys.stdin.isatty():
        logger.error(
            "No GITHUB_TOKEN environment variable set, and stdin isn't "
            "interactive so there's nothing to prompt. Set GITHUB_TOKEN and "
            "try again."
        )
        return False

    print("No GITHUB_TOKEN environment variable detected.")
    answer = input("Would you like to enter one now? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        token = getpass.getpass("GitHub token (input hidden): ").strip()
        if token:
            config.github_token = token
            os.environ["GITHUB_TOKEN"] = token
            return True
        print("No token entered.")

    print(
        "\nWithout a token, GitHub API requests are unauthenticated and "
        "capped at 60 requests/hour, and trufflehog won't get a token "
        "passed through to it either.\n"
    )
    confirm = input("Are you sure you want to continue with no GitHub token? [y/N] ").strip().lower()
    return confirm in ("y", "yes")


def _run_discovery(client: GitHubClient, cache: Cache, config: Config) -> None:
    discover_org_repos(client, cache, config)

    # gate both, not just scan
    if config.include_contributor_repos_in_scan:
        if config.member_mode == "org-members":
            discover_org_members(client, cache, config)
        else:
            discover_contributors(client, cache, config)
        discover_contributor_repos(client, cache, config)
    else:
        logger.info("--org-repos-only: skipping contributor discovery entirely")

    check_fork_ahead_status(client, cache, config)
    build_scan_queue(cache, config)


def main(argv=None) -> int:
    config = parse_args(argv)
    setup_logging(config.log_level, config.json_logs)

    if not config.json_logs:
        print_banner()

    if not _ensure_github_token(config):
        return 1

    if config.fresh and config.db_path.exists():
        logger.info("--fresh passed, deleting existing cache at %s", config.db_path)
        config.db_path.unlink()

    logger.info("Starting ghscan for organization '%s'", config.org)
    logger.info(
        "Filters: skip_archived=%s skip_forks=%s include_org_repos=%s "
        "include_contributor_repos=%s member_mode=%s allow_private=%s",
        config.skip_archived, config.skip_forks,
        config.include_org_repos_in_scan, config.include_contributor_repos_in_scan,
        config.member_mode, config.allow_private,
    )

    if looks_wide_open(config) and not config.dry_run:
        if not _confirm_wide_open_run(config):
            logger.info("Cancelled by user.")
            return 130

    cache = Cache(config.db_path)
    client = GitHubClient(config.github_token)
    run_start = time.time()

    try:
        discovery_start = time.time()
        _run_discovery(client, cache, config)
        discovery_seconds = time.time() - discovery_start

        if config.dry_run:
            queue_rows = cache.all_queue_entries()
            pending = [r for r in queue_rows if r["status"] == "pending"]
            skipped = [r for r in queue_rows if r["status"] == "skipped"]
            print(
                f"\n--dry-run: this would queue ~{len(pending)} repo(s) for scanning "
                f"({len(skipped)} filtered out), and discovery just took roughly "
                f"~{client.request_count} GitHub API call(s).\n"
                f"Nothing was scanned. Drop --dry-run to actually run trufflehog.\n"
            )
            return 0

        scan_start = time.time()
        run_scans(config, cache)
        scan_seconds = time.time() - scan_start

        timings = {
            "discovery_seconds": discovery_seconds,
            "scan_seconds": scan_seconds,
            "total_seconds": time.time() - run_start,
        }
        summary = generate_report(config, cache, timings)
    except KeyboardInterrupt:
        logger.warning(
            "Interrupted. Progress has been saved to %s -- "
            "re-run the same command to pick up where this left off.", config.db_path,
        )
        return 130
    except GitHubAPIError as exc:
        logger.error("GitHub API error: %s", exc)
        return 2
    finally:
        cache.close()
        client.close()

    elapsed = time.time() - run_start
    logger.info("ghscan finished in %.1fs", elapsed)
    return 1 if summary.get("repos_failed") else 0


if __name__ == "__main__":
    sys.exit(main())
