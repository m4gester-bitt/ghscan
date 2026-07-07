from __future__ import annotations

import fnmatch
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from .cache import Cache
from .config import Config
from .github_client import GitHubClient

logger = logging.getLogger("ghscan.discovery")

_unused_batch_size = 50


def _is_bot_login(login: str) -> bool:
    return login.lower().endswith("[bot]")


def _login_excluded(login: str, config: Config) -> bool:
    if config.exclude_bot_logins and _is_bot_login(login):
        return True
    return login.lower() in config.exclude_logins


def _repo_is_private(repo: dict) -> bool:
    return bool(repo.get("private"))


def discover_org_repos(client: GitHubClient, cache: Cache, config: Config) -> int:
    if cache.get_meta("org_repos_complete") == "1":
        total = len(cache.all_org_repos())
        logger.info("Org repo discovery already done for '%s' (%d cached)", config.org, total)
        return total

    logger.info("Looking up public repos for org '%s'", config.org)
    new_count = 0
    dropped_private = 0
    for repo in client.list_org_repos(config.org):
        if _repo_is_private(repo) and not config.allow_private:
            dropped_private += 1
            continue
        if cache.add_org_repo(repo):
            new_count += 1
            logger.debug("Found org repo: %s", repo["full_name"])

    cache.set_meta("org_repos_complete", "1")
    if dropped_private:
        logger.warning(
            "Dropped %d repo(s) the API marked private (pass --allow-private if that's "
            "unexpected and you want them included)", dropped_private,
        )
    logger.info("Org repo discovery done: %d repos", new_count)
    return new_count


def discover_contributors(client: GitHubClient, cache: Cache, config: Config) -> int:
    pending = cache.org_repos_pending_contributors()
    if not pending:
        logger.info("Contributor discovery already done for all known org repos")
        return cache.count_unique_contributors()

    logger.info("Fetching contributors for %d repos (%d workers)", len(pending), config.api_workers)

    def _fetch(repo_row):
        owner, name = repo_row["full_name"].split("/", 1)
        new_unique = 0
        for contributor in client.list_repo_contributors(owner, name):
            login = contributor.get("login")
            if not login or _login_excluded(login, config):
                continue
            if cache.add_contributor_sighting(login, repo_row["full_name"]):
                new_unique += 1
        cache.mark_org_repo_contributors_fetched(repo_row["full_name"])
        return repo_row["full_name"], new_unique

    total = len(pending)
    with ThreadPoolExecutor(max_workers=config.api_workers) as pool:
        futures = {pool.submit(_fetch, row): row for row in pending}
        for i, future in enumerate(as_completed(futures), start=1):
            repo_full_name, new_unique = future.result()
            logger.info(
                "[%d/%d %.0f%%] Contributors fetched for %s (+%d new, %d unique so far)",
                i, total, 100 * i / total, repo_full_name, new_unique, cache.count_unique_contributors(),
            )

    total_unique = cache.count_unique_contributors()
    logger.info("Contributor discovery done: %d unique contributors", total_unique)
    return total_unique


def discover_org_members(client: GitHubClient, cache: Cache, config: Config) -> int:
    if cache.get_meta("org_members_complete") == "1":
        total = cache.count_unique_contributors()
        logger.info("Org member discovery already done for '%s' (%d cached)", config.org, total)
        return total

    logger.info("Fetching member list for '%s' (org-members mode)", config.org)
    new_count = 0
    for member in client.list_org_members(config.org):
        login = member.get("login")
        if not login or _login_excluded(login, config):
            continue
        if cache.add_org_member(login):
            new_count += 1
            logger.debug("Found org member: %s", login)

    cache.set_meta("org_members_complete", "1")
    total = cache.count_unique_contributors()
    logger.info("Org member discovery done: %d members (+%d new)", total, new_count)
    return total


def discover_contributor_repos(client: GitHubClient, cache: Cache, config: Config) -> int:
    pending = cache.contributors_pending_repos()
    if not pending:
        logger.info("Contributor repo discovery already done")
        return cache.count_contributor_repos_discovered()

    logger.info("Fetching repos for %d contributors (%d workers)", len(pending), config.api_workers)

    def _fetch(contrib_row):
        login = contrib_row["login"]
        new_count = 0
        dropped_private = 0
        for repo in client.list_user_repos(login):
            if config.max_contributor_repos and new_count >= config.max_contributor_repos:
                break
            if _repo_is_private(repo) and not config.allow_private:
                dropped_private += 1
                continue
            if cache.add_contributor_repo(repo, discovered_via=login):
                new_count += 1
        cache.mark_contributor_repos_fetched(login)
        return login, new_count, dropped_private

    total = len(pending)
    with ThreadPoolExecutor(max_workers=config.api_workers) as pool:
        futures = {pool.submit(_fetch, row): row for row in pending}
        for i, future in enumerate(as_completed(futures), start=1):
            login, new_count, dropped_private = future.result()
            logger.info(
                "[%d/%d %.0f%%] Repos fetched for '%s' (+%d, %d unique so far)",
                i, total, 100 * i / total, login, new_count, cache.count_contributor_repos_discovered(),
            )
            if dropped_private:
                logger.debug("Dropped %d private repo(s) for %s", dropped_private, login)

    total_repos = cache.count_contributor_repos_discovered()
    logger.info("Contributor repo discovery done: %d unique repos", total_repos)
    return total_repos


def check_fork_ahead_status(client: GitHubClient, cache: Cache, config: Config) -> int:
    if not config.include_forks_if_ahead or config.skip_forks:
        return 0

    candidates = []
    if config.include_org_repos_in_scan:
        candidates.extend(r for r in cache.all_org_repos() if r["fork"])
    if config.include_contributor_repos_in_scan:
        candidates.extend(r for r in cache.all_contributor_repos() if r["fork"])

    todo = [r for r in candidates if cache.get_fork_ahead(r["full_name"]) is None]
    if not todo:
        return 0

    logger.info("Checking %d fork(s) for commits ahead of upstream", len(todo))
    checked = 0
    for row in todo:
        details = client.get_repo(row["full_name"])
        parent = (details or {}).get("parent")
        if not details or not parent:
            cache.set_fork_ahead(row["full_name"], 0)
            continue
        result = client.compare_commits(
            parent["full_name"], parent.get("default_branch", "HEAD"),
            row["owner"], details.get("default_branch", "HEAD"),
        )
        ahead_by = (result or {}).get("ahead_by", 0)
        cache.set_fork_ahead(row["full_name"], ahead_by)
        checked += 1

    logger.info("Fork-ahead check done for %d repo(s)", checked)
    return checked


def parse_github_timestamp(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _repo_matches_any_glob(full_name: str, patterns: list) -> bool:
    lowered = full_name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


def _skip_reason(repo_row, config: Config, cache: Optional[Cache] = None) -> Optional[str]:
    if config.skip_archived and repo_row["archived"]:
        return "archived"
    if config.skip_forks and repo_row["fork"]:
        return "fork"
    if repo_row["fork"] and config.include_forks_if_ahead and cache is not None:
        ahead_by = cache.get_fork_ahead(repo_row["full_name"])
        if not ahead_by:
            return "fork_not_ahead"
    if config.exclude_repos and _repo_matches_any_glob(repo_row["full_name"], config.exclude_repos):
        return "excluded_repo"
    language = (repo_row["language"] or "").lower() if "language" in repo_row.keys() else ""
    if config.languages and language not in config.languages:
        return "language_not_included"
    if config.exclude_languages and language in config.exclude_languages:
        return "excluded_language"
    if config.min_stars and (repo_row["stargazers_count"] or 0) < config.min_stars:
        return "below_min_stars"
    if config.max_repo_size_kb is not None and (repo_row["size_kb"] or 0) > config.max_repo_size_kb:
        return "over_max_size"
    if config.pushed_within_days is not None:
        cutoff = time.time() - (config.pushed_within_days * 86400)
        if parse_github_timestamp(repo_row["pushed_at"]) < cutoff:
            return "stale"
    if config.min_repo_age_days is not None:
        created = repo_row["created_at"] if "created_at" in repo_row.keys() else None
        youngest_allowed = time.time() - (config.min_repo_age_days * 86400)
        if created and parse_github_timestamp(created) > youngest_allowed:
            return "too_new"
    return None


def _sort_key_for(config: Config):
    if config.sort_queue_by == "stars":
        return lambda row: row["stargazers_count"] or 0
    if config.sort_queue_by == "size":
        return lambda row: row["size_kb"] or 0
    return lambda row: parse_github_timestamp(row["pushed_at"])


def build_scan_queue(cache: Cache, config: Config):
    sources = []
    if config.include_org_repos_in_scan:
        sources.append(("org", cache.all_org_repos()))
    if config.include_contributor_repos_in_scan:
        sources.append(("contributor", cache.all_contributor_repos()))

    candidates = []
    for source, rows in sources:
        for row in rows:
            candidates.append((source, row, _skip_reason(row, config, cache)))

    sort_key = _sort_key_for(config)
    if config.max_queue_size is not None:
        keepable = [c for c in candidates if c[2] is None]
        if len(keepable) > config.max_queue_size:
            keepable.sort(key=lambda c: sort_key(c[1]), reverse=True)
            keep_ids = {c[1]["full_name"] for c in keepable[: config.max_queue_size]}
            candidates = [
                (source, row, reason) if reason or row["full_name"] in keep_ids
                else (source, row, "queue_cap")
                for source, row, reason in candidates
            ]

    # sorted so cap matches scan order too
    candidates.sort(key=lambda c: sort_key(c[1]), reverse=True)
    base_time = time.time()

    queued = 0
    skipped = 0
    for i, (source, row, reason) in enumerate(candidates):
        clone_url = f"https://github.com/{row['full_name']}"
        if cache.enqueue(row["full_name"], clone_url, source, skip_reason=reason,
                         queued_at=base_time + i * 1e-6):
            if reason:
                skipped += 1
            else:
                queued += 1

    logger.info(
        "Scan queue built: %d newly queued, %d newly skipped "
        "(archived/fork/language/excludes/stars/size/age/cap)", queued, skipped,
    )
    return queued, skipped
