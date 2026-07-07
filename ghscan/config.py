# argv + env -> Config
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_PATH = "ghscan_cache.sqlite3"
DEFAULT_REPORT_PATH = "ghscan_report.md"

SORT_KEYS = ("pushed", "stars", "size")


@dataclass
class Config:
    org: str
    github_token: "str | None"

    # filters
    skip_archived: bool = True
    skip_forks: bool = True
    include_org_repos_in_scan: bool = True
    include_contributor_repos_in_scan: bool = True
    member_mode: str = "contributors"
    allow_private: bool = False
    include_forks_if_ahead: bool = False

    # deny lists
    exclude_repos: list = field(default_factory=list)
    exclude_logins: set = field(default_factory=set)
    exclude_bot_logins: bool = False
    languages: set = field(default_factory=set)
    exclude_languages: set = field(default_factory=set)

    # threads n stuff
    api_workers: int = 8
    scan_workers: int = 3

    # trufflehog opts
    trufflehog_path: str = "trufflehog"
    trufflehog_timeout: int = 1800
    trufflehog_json_output: bool = True
    trufflehog_pass_token: bool = True
    trufflehog_extra_args: list = field(default_factory=list)
    shallow_depth: "int | None" = None

    # persistence
    db_path: Path = field(default_factory=lambda: Path(DEFAULT_DB_PATH))
    report_path: Path = field(default_factory=lambda: Path(DEFAULT_REPORT_PATH))
    report_json_path: "Path | None" = None
    save_files: bool = False
    fresh: bool = False

    # logs
    log_level: str = "INFO"
    json_logs: bool = False

    # safety stuff
    max_contributor_repos: "int | None" = None
    min_stars: int = 0
    max_repo_size_kb: "int | None" = None
    pushed_within_days: "int | None" = None
    min_repo_age_days: "int | None" = None
    max_queue_size: "int | None" = None
    sort_queue_by: str = "pushed"
    dry_run: bool = False
    yes: bool = False


def _csv_lower(raw: str) -> set:
    return {piece.strip().lower() for piece in raw.split(",") if piece.strip()}


def parse_args(argv=None) -> Config:
    parser = argparse.ArgumentParser(
        prog="ghscan",
        description=(
            "Map out a GitHub org's public footprint (org repos + the personal "
            "repos of whoever touched them) and run TruffleHog over all of it "
            "looking for verified secrets."
        ),
    )
    parser.add_argument(
        "--org", required=True,
        help="Target GitHub organization login, e.g. 'anthropics'",
    )

    filt = parser.add_argument_group("Filtering")
    filt.add_argument(
        "--include-archived", action="store_true",
        help="Include archived repositories in the scan (default: skipped)",
    )
    filt.add_argument(
        "--include-forks", action="store_true",
        help="Include forked repositories in the scan (default: skipped)",
    )
    filt.add_argument(
        "--include-forks-if-ahead", action="store_true",
        help=(
            "Only meaningful together with --include-forks. Instead of scanning "
            "every fork, only scan ones that have commits ahead of their "
            "upstream -- i.e. someone actually pushed something of their own to "
            "it. Byte-identical forks that just mirror upstream are skipped, "
            "since they'd only re-surface history you've already scanned. "
            "Costs one extra API call per fork to check."
        ),
    )
    filt.add_argument(
        "--org-repos-only", action="store_true",
        help="Only scan the organization's own repos; skip contributor repos entirely "
             "(also skips the contributor-discovery API calls, not just the scan)",
    )
    filt.add_argument(
        "--contributor-repos-only", action="store_true",
        help="Only scan contributor-owned repos; skip the organization's own repos",
    )
    filt.add_argument(
        "--member-mode", choices=["contributors", "org-members"], default="contributors",
        help=(
            "How to find the people whose personal repos get scanned. "
            "'contributors' (default): derive them from commit history across "
            "every org repo -- broad, includes drive-by PR authors who were "
            "never actually part of the org. "
            "'org-members': use the org's real membership roster "
            "(GET /orgs/{org}/members) instead -- tighter, only actual members "
            "get expanded into their personal repos. Note GitHub only shows "
            "members visible to your token: if GITHUB_TOKEN belongs to an org "
            "member you'll see private members too, otherwise only public ones."
        ),
    )
    filt.add_argument(
        "--allow-private", action="store_true",
        help=(
            "By default ghscan only ever talks to public-repo endpoints and will "
            "drop anything the API returns marked private (this can happen if "
            "your token turns out to have broader scope than expected). Pass "
            "this flag to allow private repos through instead of dropping them."
        ),
    )

    excl = parser.add_argument_group(
        "Exclusions",
        "Deny-list specific repos, logins, or languages instead of only having "
        "positive filters.",
    )
    excl.add_argument(
        "--exclude-repo", action="append", default=[], dest="exclude_repos",
        help="Repo to skip, as 'owner/name'. Supports shell-style globs "
             "(e.g. 'someupstream/*'). Repeatable.",
    )
    excl.add_argument(
        "--exclude-login", action="append", default=[], dest="exclude_logins",
        help="Contributor/member login to never expand into personal repos. Repeatable.",
    )
    excl.add_argument(
        "--exclude-bot-logins", action="store_true",
        help="Auto-skip any login matching '*[bot]' (github-actions[bot], "
             "dependabot[bot], etc.) -- cheap, common-case win.",
    )
    excl.add_argument(
        "--language", action="append", default=[], dest="languages",
        help="Only scan repos whose primary language is one of these (repeatable). "
             "Matches GitHub's reported 'language' field, case-insensitive.",
    )
    excl.add_argument(
        "--exclude-language", action="append", default=[], dest="exclude_languages",
        help="Skip repos whose primary language is one of these (repeatable).",
    )

    vol = parser.add_argument_group(
        "Volume control",
        "Filters to cut a large scan queue down to the repos most likely "
        "to matter, applied when the scan queue is built.",
    )
    vol.add_argument(
        "--max-contributor-repos", type=int, default=None,
        help="Cap how many of each contributor's own repos are discovered "
             "(newest-first from the API). Default: unlimited.",
    )
    vol.add_argument(
        "--min-stars", type=int, default=0,
        help="Skip repos with fewer than this many stars (default: 0, i.e. no filter)",
    )
    vol.add_argument(
        "--max-repo-size-kb", type=int, default=None,
        help="Skip repos larger than this many KB (use to cap huge, slow-to-clone repos). "
             "Default: unlimited.",
    )
    vol.add_argument(
        "--pushed-within-days", type=int, default=None,
        help="Skip repos with no push in the last N days (default: unlimited, i.e. no filter)",
    )
    vol.add_argument(
        "--min-repo-age-days", type=int, default=None,
        help="Skip repos created less than N days ago -- filters out throwaway/test "
             "repos that popped up recently. Inverse of --pushed-within-days.",
    )
    vol.add_argument(
        "--max-queue-size", type=int, default=None,
        help="Hard cap on the total number of repos scanned. If the queue "
             "would be larger, it's sorted (see --sort-queue-by) and "
             "truncated to this many. Default: unlimited.",
    )
    vol.add_argument(
        "--sort-queue-by", choices=list(SORT_KEYS), default="pushed",
        help="How to order the scan queue (and what --max-queue-size keeps first): "
             "'pushed' = most recently pushed first (default), "
             "'stars' = most starred first, 'size' = largest repo first.",
    )

    conc = parser.add_argument_group("Concurrency & rate limiting")
    conc.add_argument(
        "--api-workers", type=int, default=8,
        help="Concurrent worker threads for GitHub API discovery calls (default: 8)",
    )
    conc.add_argument(
        "--scan-workers", type=int, default=3,
        help="Concurrent TruffleHog scan processes (default: 3)",
    )

    th = parser.add_argument_group("TruffleHog")
    th.add_argument(
        "--trufflehog-path", default="trufflehog",
        help="Path to the trufflehog binary (default: 'trufflehog' on PATH)",
    )
    th.add_argument(
        "--trufflehog-timeout", type=int, default=1800,
        help="Per-repository scan timeout in seconds (default: 1800)",
    )
    th.add_argument(
        "--no-json-output", action="store_true",
        help="Do not append --json to the trufflehog invocation "
             "(findings will not be parsed/grouped in the report)",
    )
    th.add_argument(
        "--no-trufflehog-token", action="store_true",
        help="Do not pass GITHUB_TOKEN to trufflehog itself via --token",
    )
    th.add_argument(
        "--trufflehog-arg", action="append", default=[], dest="trufflehog_extra_args",
        help="Extra raw argument to pass to trufflehog (repeatable)",
    )
    th.add_argument(
        "--shallow-depth", type=int, default=None,
        help="Passthrough: adds --depth=<N> to the trufflehog invocation to limit "
             "how much history gets pulled, which can noticeably speed up huge repos "
             "at the cost of missing older, pre-existing secrets. Only has an "
             "effect if your trufflehog build actually honors --depth.",
    )

    persist = parser.add_argument_group("Persistence & output")
    persist.add_argument(
        "--db-path", default=DEFAULT_DB_PATH,
        help="SQLite cache/state database path (default: %(default)s)",
    )
    persist.add_argument(
        "--report-path", default=DEFAULT_REPORT_PATH,
        help="Markdown report output path, used only if --save is also given "
             "(default: %(default)s)",
    )
    persist.add_argument(
        "--report-json-path", default=None,
        help="Optional JSON report output path, used only if --save is also given",
    )
    persist.add_argument(
        "--save", action="store_true",
        help="Actually write the markdown/JSON report files to disk. Without this, "
             "the full report still prints to the console at the end of the run, "
             "it just won't be left behind as a file.",
    )
    persist.add_argument(
        "--fresh", action="store_true",
        help="Delete any existing cache database and start over from scratch",
    )

    safety = parser.add_argument_group("Safety")
    safety.add_argument(
        "--dry-run", action="store_true",
        help="Run discovery and build the scan queue, print how many repos would "
             "be queued and roughly how many GitHub API calls that took, then stop "
             "before touching trufflehog at all.",
    )
    safety.add_argument(
        "--yes", action="store_true",
        help="Skip the confirmation prompt that otherwise appears before a wide-open "
             "run (org-members mode with no volume filters set) against a possibly huge org.",
    )

    log = parser.add_argument_group("Logging")
    log.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    log.add_argument(
        "--json-logs", action="store_true",
        help="Emit structured JSON log lines instead of human-readable text",
    )

    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or None

    if args.org_repos_only and args.contributor_repos_only:
        parser.error(
            "--org-repos-only and --contributor-repos-only are mutually exclusive"
        )

    return Config(
        org=args.org,
        github_token=token,
        skip_archived=not args.include_archived,
        skip_forks=not args.include_forks,
        include_org_repos_in_scan=not args.contributor_repos_only,
        include_contributor_repos_in_scan=not args.org_repos_only,
        member_mode=args.member_mode,
        allow_private=args.allow_private,
        include_forks_if_ahead=args.include_forks_if_ahead,
        exclude_repos=list(args.exclude_repos),
        exclude_logins={login.strip().lower() for login in args.exclude_logins if login.strip()},
        exclude_bot_logins=args.exclude_bot_logins,
        languages={lang.strip().lower() for lang in args.languages if lang.strip()},
        exclude_languages={lang.strip().lower() for lang in args.exclude_languages if lang.strip()},
        api_workers=max(1, args.api_workers),
        scan_workers=max(1, args.scan_workers),
        trufflehog_path=args.trufflehog_path,
        trufflehog_timeout=args.trufflehog_timeout,
        trufflehog_json_output=not args.no_json_output,
        trufflehog_pass_token=not args.no_trufflehog_token,
        trufflehog_extra_args=args.trufflehog_extra_args,
        shallow_depth=args.shallow_depth,
        db_path=Path(args.db_path),
        report_path=Path(args.report_path),
        report_json_path=Path(args.report_json_path) if args.report_json_path else None,
        save_files=args.save,
        fresh=args.fresh,
        log_level=args.log_level,
        json_logs=args.json_logs,
        max_contributor_repos=args.max_contributor_repos,
        min_stars=max(0, args.min_stars),
        max_repo_size_kb=args.max_repo_size_kb,
        pushed_within_days=args.pushed_within_days,
        min_repo_age_days=args.min_repo_age_days,
        max_queue_size=args.max_queue_size,
        sort_queue_by=args.sort_queue_by,
        dry_run=args.dry_run,
        yes=args.yes,
    )


_unused_default_workers = 4


def looks_wide_open(config: Config) -> bool:
    # checks if unbounded org-members run
    if config.member_mode != "org-members":
        return False
    no_filters = (
        not config.max_contributor_repos
        and config.min_stars == 0
        and not config.max_repo_size_kb
        and not config.pushed_within_days
        and not config.min_repo_age_days
        and not config.max_queue_size
        and not config.languages
        and not config.exclude_languages
        and not config.exclude_repos
        and not config.exclude_logins
        and not config.exclude_bot_logins
    )
    return no_filters
