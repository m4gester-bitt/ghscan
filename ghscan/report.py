# builds md/json report from cache
from __future__ import annotations

import json
import logging

from .cache import Cache
from .config import Config

logger = logging.getLogger("ghscan.report")


def _load_findings_by_repo(cache: Cache) -> dict:
    findings_by_repo = {}
    for row in cache.all_scan_results():
        findings = json.loads(row["findings_json"] or "[]")
        if findings:
            findings_by_repo[row["full_name"]] = findings
    return findings_by_repo


def build_summary(cache: Cache, timings: "dict | None" = None) -> dict:
    queue_rows = cache.all_queue_entries()
    result_rows = cache.all_scan_results()

    scanned = sum(1 for r in result_rows if r["status"] == "success")
    failed = sum(1 for r in result_rows if r["status"] != "success")
    skipped = sum(1 for r in queue_rows if r["status"] == "skipped")
    total_findings = sum(r["findings_count"] for r in result_rows)

    summary = {
        "org_repos_discovered": len(cache.all_org_repos()),
        "contributors_discovered": cache.count_contributor_sightings(),
        "unique_contributors": cache.count_unique_contributors(),
        "contributor_repos_discovered": cache.count_contributor_repos_discovered(),
        "unique_repos_queued": len(queue_rows),
        "repos_scanned": scanned,
        "repos_skipped": skipped,
        "repos_failed": failed,
        "total_verified_findings": total_findings,
    }
    if timings:
        summary["timing"] = timings
    return summary


def _format_seconds(seconds: "float | None") -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds:.1f}s"


def render_markdown(org: str, summary: dict, findings_by_repo: dict, queue_rows, result_rows) -> str:
    lines = [f"# TruffleHog Organization Scan Report: {org}", ""]

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Organization repositories discovered: **{summary['org_repos_discovered']}**")
    lines.append(f"- Contributors discovered (including repeats across repos): **{summary['contributors_discovered']}**")
    lines.append(f"- Unique contributors: **{summary['unique_contributors']}**")
    lines.append(f"- Contributor-owned repositories discovered: **{summary['contributor_repos_discovered']}**")
    total_queued = summary['unique_repos_queued'] or 1
    scan_pct = 100 * summary['repos_scanned'] / total_queued
    lines.append(f"- Unique repositories queued for scanning: **{summary['unique_repos_queued']}**")
    lines.append(
        f"- Repositories scanned successfully: **{summary['repos_scanned']}/{summary['unique_repos_queued']} "
        f"({scan_pct:.1f}%)**"
    )
    lines.append(f"- Repositories skipped (filters): **{summary['repos_skipped']}**")
    lines.append(f"- Repositories that failed to scan: **{summary['repos_failed']}**")
    lines.append(f"- Total verified findings: **{summary['total_verified_findings']}**")

    timing = summary.get("timing")
    if timing:
        lines.append("")
        lines.append("### Timing")
        lines.append("")
        lines.append(f"- Discovery phase: **{_format_seconds(timing.get('discovery_seconds'))}**")
        lines.append(f"- Scan phase: **{_format_seconds(timing.get('scan_seconds'))}**")
        lines.append(f"- Total run time: **{_format_seconds(timing.get('total_seconds'))}**")
    lines.append("")

    lines.append("## Findings by repository")
    lines.append("")
    if not findings_by_repo:
        lines.append("No verified secrets were found.")
    else:
        for repo, findings in sorted(findings_by_repo.items()):
            lines.append(f"### {repo}")
            lines.append("")
            lines.append(f"{len(findings)} verified finding(s).")
            for f in findings:
                detector = f.get("DetectorName", "unknown")
                source = f.get("SourceMetadata", {}).get("Data", {})
                where = json.dumps(source)[:200]
                lines.append(f"- Detector: `{detector}` | Source: `{where}`")
            lines.append("")

    lines.append("## Failed repositories")
    lines.append("")
    failed_rows = [r for r in result_rows if r["status"] != "success"]
    if failed_rows:
        for r in failed_rows:
            detail = r["error"] or (r["stderr"] or "")[:200]
            lines.append(f"- `{r['full_name']}`: status={r['status']} detail={detail}")
    else:
        lines.append("None.")
    lines.append("")

    skipped_rows = [r for r in queue_rows if r["status"] == "skipped"]
    lines.append("## Skipped repositories")
    lines.append("")
    if skipped_rows:
        for r in skipped_rows:
            lines.append(f"- `{r['full_name']}` (reason: {r['skip_reason']})")
    else:
        lines.append("None.")

    return "\n".join(lines)


def generate_report(config: Config, cache: Cache, timings: "dict | None" = None) -> dict:
    summary = build_summary(cache, timings)
    findings_by_repo = _load_findings_by_repo(cache)
    queue_rows = cache.all_queue_entries()
    result_rows = cache.all_scan_results()

    markdown = render_markdown(config.org, summary, findings_by_repo, queue_rows, result_rows)
    _unused_char_count = len(markdown)

    print("\n" + markdown + "\n")

    if config.save_files:
        config.report_path.write_text(markdown, encoding="utf-8")
        logger.info("Markdown report written to %s", config.report_path)

        if config.report_json_path:
            payload = {"org": config.org, "summary": summary, "findings_by_repo": findings_by_repo}
            config.report_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            logger.info("JSON report written to %s", config.report_json_path)
    else:
        logger.info(
            "--save not passed, so no report file was written (the full report is above). "
            "Re-run with --save to keep %s%s on disk.",
            config.report_path,
            f" and {config.report_json_path}" if config.report_json_path else "",
        )

    logger.info("=== SCAN SUMMARY ===")
    for key, value in summary.items():
        if key == "timing":
            continue
        logger.info("%s: %s", key, value)

    return summary
