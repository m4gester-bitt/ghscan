from __future__ import annotations

import logging
import random
import threading
import time
from typing import Iterator, Optional

import requests

logger = logging.getLogger("ghscan.github")

API_BASE = "https://api.github.com"
DEFAULT_PER_PAGE = 100
MAX_RETRIES = 6
BASE_BACKOFF = 1.5

_unused_max_page = 500


class GitHubAPIError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: "str | None", per_page: int = DEFAULT_PER_PAGE, timeout: int = 30):
        self._session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ghscan",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._session.headers.update(headers)
        self.per_page = per_page
        self.timeout = timeout
        self._request_count = 0
        self._count_lock = threading.Lock()

    def close(self) -> None:
        self._session.close()

    @property
    def request_count(self) -> int:
        return self._request_count

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        attempt = 0
        while True:
            attempt += 1
            with self._count_lock:
                self._request_count += 1
            try:
                resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt > MAX_RETRIES:
                    raise GitHubAPIError(f"Network error calling {url}: {exc}") from exc
                self._sleep_backoff(attempt, reason=f"network error ({exc})")
                continue

            if resp.status_code == 200:
                self._log_rate_limit(resp)
                return resp

            if resp.status_code == 404:
                raise GitHubAPIError(f"404 Not Found: {url}")

            if resp.status_code in (403, 429):
                if self._handle_rate_limit(resp):
                    continue
                if attempt > MAX_RETRIES:
                    raise GitHubAPIError(f"Persistent rate limiting on {url}")
                self._sleep_backoff(attempt, reason="rate limited")
                continue

            if 500 <= resp.status_code < 600:
                if attempt > MAX_RETRIES:
                    raise GitHubAPIError(f"Server error {resp.status_code} on {url}")
                self._sleep_backoff(attempt, reason=f"server error {resp.status_code}")
                continue

            raise GitHubAPIError(
                f"GitHub API error {resp.status_code} on {url}: {resp.text[:300]}"
            )

    def _handle_rate_limit(self, resp: requests.Response) -> bool:
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            wait = float(retry_after)
            logger.warning("Secondary rate limit hit, sleeping %.1fs", wait)
            time.sleep(wait)
            return True

        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset is not None:
            wait = max(0.0, float(reset) - time.time()) + 1
            logger.warning("Primary rate limit exhausted, sleeping %.1fs until reset", wait)
            time.sleep(wait)
            return True

        return False

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        wait = BASE_BACKOFF ** attempt + random.uniform(0, 1)
        logger.warning("Retrying after %s (attempt %d), sleeping %.1fs", reason, attempt, wait)
        time.sleep(wait)

    def _log_rate_limit(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 200:
            logger.debug("GitHub API rate limit remaining: %s", remaining)

    def _paginate(self, url: str, params: Optional[dict] = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", self.per_page)
        next_url, next_params = url, params
        while next_url:
            resp = self._request("GET", next_url, params=next_params)
            for item in resp.json():
                yield item
            next_url = resp.links.get("next", {}).get("url")
            next_params = None

    def list_org_repos(self, org: str) -> Iterator[dict]:
        url = f"{API_BASE}/orgs/{org}/repos"
        yield from self._paginate(url, {"type": "public"})

    def list_repo_contributors(self, owner: str, repo: str) -> Iterator[dict]:
        url = f"{API_BASE}/repos/{owner}/{repo}/contributors"
        try:
            yield from self._paginate(url)
        except GitHubAPIError as exc:
            logger.warning("Could not list contributors for %s/%s: %s", owner, repo, exc)
            return

    def list_org_members(self, org: str) -> Iterator[dict]:
        url = f"{API_BASE}/orgs/{org}/members"
        try:
            yield from self._paginate(url)
        except GitHubAPIError as exc:
            logger.warning("Could not list members for org %s: %s", org, exc)
            return

    def list_user_repos(self, username: str) -> Iterator[dict]:
        url = f"{API_BASE}/users/{username}/repos"
        try:
            yield from self._paginate(url, {"type": "owner"})
        except GitHubAPIError as exc:
            logger.warning("Could not list repos for user %s: %s", username, exc)
            return

    def get_repo(self, full_name: str) -> Optional[dict]:
        url = f"{API_BASE}/repos/{full_name}"
        try:
            return self._request("GET", url).json()
        except GitHubAPIError as exc:
            logger.warning("Could not fetch repo details for %s: %s", full_name, exc)
            return None

    def compare_commits(self, base_full_name: str, base_ref: str, head_owner: str, head_ref: str) -> Optional[dict]:
        url = f"{API_BASE}/repos/{base_full_name}/compare/{base_ref}...{head_owner}:{head_ref}"
        try:
            return self._request("GET", url).json()
        except GitHubAPIError as exc:
            logger.warning("Could not compare %s against %s:%s: %s", base_full_name, head_owner, head_ref, exc)
            return None
