from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache import Cache
from .config import Config
from .discovery import parse_github_timestamp

logger = logging.getLogger("ghscan.scanner")

_unused_max_stdout_lines = 10000


def build_command(config: Config, repo_url: str) -> list:
    cmd = [
        config.trufflehog_path, "--no-update", "github",
        f"--repo={repo_url}", "--only-verified",
    ]
    if config.trufflehog_json_output:
        cmd.append("--json")
    if config.trufflehog_pass_token and os.environ.get("GITHUB_TOKEN"):
        cmd.extend(["--token", os.environ["GITHUB_TOKEN"]])
    if config.shallow_depth:
        cmd.append(f"--depth={config.shallow_depth}")
    cmd.extend(config.trufflehog_extra_args)
    return cmd


def _redact(cmd: list) -> list:
    redacted = list(cmd)
    if "--token" in redacted:
        idx = redacted.index("--token")
        if idx + 1 < len(redacted):
            redacted[idx + 1] = "***"
    return redacted


def _parse_findings(stdout: str, json_mode: bool) -> list:
    if not json_mode:
        return []
    findings = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return findings


def scan_one(config: Config, full_name: str, clone_url: str) -> dict:
    cmd = build_command(config, clone_url)
    logger.info("Scanning %s (%s)", full_name, " ".join(_redact(cmd)))
    started = time.time()
    result = {"full_name": full_name, "started_at": started}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.trufflehog_timeout,
        )
        duration = time.time() - started
        findings = _parse_findings(proc.stdout, config.trufflehog_json_output)
        result.update({
            "status": "success" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "duration_seconds": duration,
            "stdout": proc.stdout[-200_000:],
            "stderr": proc.stderr[-50_000:],
            "findings": findings,
            "findings_count": len(findings),
            "finished_at": time.time(),
        })
        if proc.returncode == 0:
            logger.info(
                "Finished %s in %.1fs (%d verified finding(s))",
                full_name, duration, len(findings),
            )
        else:
            logger.warning(
                "trufflehog exited %d for %s: %s",
                proc.returncode, full_name, (proc.stderr or "")[:300],
            )
    except FileNotFoundError:
        result.update({
            "status": "error",
            "error": f"trufflehog binary not found at '{config.trufflehog_path}'",
            "duration_seconds": time.time() - started,
            "finished_at": time.time(),
        })
        logger.error("trufflehog binary not found (path=%s)", config.trufflehog_path)
    except subprocess.TimeoutExpired:
        result.update({
            "status": "timeout",
            "error": f"Scan exceeded {config.trufflehog_timeout}s timeout",
            "duration_seconds": time.time() - started,
            "finished_at": time.time(),
        })
        logger.error("Scan of %s timed out after %ds", full_name, config.trufflehog_timeout)
    except Exception as exc:
        result.update({
            "status": "error",
            "error": str(exc),
            "duration_seconds": time.time() - started,
            "finished_at": time.time(),
        })
        logger.exception("Unexpected error scanning %s", full_name)

    return result


def refresh_incremental_rescans(cache: Cache) -> int:
    reset_count = 0
    for entry in cache.all_queue_entries():
        if entry["status"] not in ("done", "failed"):
            continue
        result = cache.get_scan_result(entry["full_name"])
        if not result or not result["finished_at"]:
            continue
        pushed_at = cache.get_repo_pushed_at(entry["full_name"], entry["source"])
        if pushed_at is None:
            continue
        if parse_github_timestamp(pushed_at) > result["finished_at"]:
            cache.mark_scan_status(entry["full_name"], "pending")
            reset_count += 1

    if reset_count:
        logger.info("%d repo(s) have new commits since their last scan -- queued for rescan", reset_count)
    return reset_count


def run_scans(config: Config, cache: Cache) -> None:
    reset = cache.reset_stuck_scans()
    if reset:
        logger.info("Reset %d interrupted scan(s) back to pending", reset)

    refresh_incremental_rescans(cache)

    pending = cache.pending_scans()
    total = len(pending)
    if not total:
        logger.info("No pending repositories to scan")
        return

    logger.info("Starting scan of %d repositories (%d workers)", total, config.scan_workers)

    def _run(row):
        cache.mark_scan_status(row["full_name"], "in_progress")
        result = scan_one(config, row["full_name"], row["clone_url"])
        cache.save_scan_result(result)
        final_status = "done" if result["status"] == "success" else "failed"
        cache.mark_scan_status(row["full_name"], final_status)
        return row["full_name"], final_status

    completed = 0
    with ThreadPoolExecutor(max_workers=config.scan_workers) as pool:
        futures = {pool.submit(_run, row): row for row in pending}
        for future in as_completed(futures):
            full_name, status = future.result()
            completed += 1
            logger.info(
                "[%d/%d %.0f%%] %s -> %s (%d remaining)",
                completed, total, 100 * completed / total, full_name, status, total - completed,
            )
