"""
sonar_client.py — SonarCloud API wrapper.

Fetches issues and per-file coverage, filtered to the configured paths.
"""

import os
import fnmatch
import requests
from dataclasses import dataclass, field

SONAR_BASE = "https://sonarcloud.io/api"
DEFAULT_TOKEN = os.environ.get("SONAR_TOKEN", "")


@dataclass
class SonarReport:
    project_key: str
    org_slug: str
    severity_counts: dict = field(default_factory=lambda: {
        "BLOCKER": 0, "CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "INFO": 0
    })
    total_issues: int = 0
    coverage_pct: float | None = None
    covered_lines: int = 0
    total_lines: int = 0
    matched_paths: list[str] = field(default_factory=list)
    issues_detail: list[dict] = field(default_factory=list)  # full issue objects for matched files
    error: str | None = None           # fatal — issues fetch failed
    coverage_error: str | None = None  # non-fatal — coverage unavailable


def _get(endpoint: str, params: dict, token: str) -> dict:
    resp = requests.get(
        f"{SONAR_BASE}/{endpoint}",
        params=params,
        auth=(token, ""),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _matches(component_key: str, file_paths: list[str]) -> bool:
    """
    component_key format from SonarCloud: "project-key:src/path/to/file.vue"

    Each entry in file_paths is matched against the file portion using:
      - glob pattern  if it contains * or ?  (e.g. src/**/contract_staffing/**)
      - substring     otherwise              (e.g. contract_staffing)
    """
    if not file_paths:
        return True  # no filter = include everything
    file_part = component_key.split(":", 1)[-1] if ":" in component_key else component_key
    for p in file_paths:
        if "*" in p or "?" in p:
            if fnmatch.fnmatch(file_part, p):
                return True
        else:
            if p in file_part:
                return True
    return False


def fetch_report(
    project_key: str,
    org_slug: str,
    file_paths: list[str],
    token: str | None = None,
) -> SonarReport:
    token = token or DEFAULT_TOKEN
    report = SonarReport(project_key=project_key, org_slug=org_slug)

    # ── Issues ────────────────────────────────────────────────────────────────
    try:
        page = 1
        seen_paths = set()
        while True:
            data = _get("issues/search", {
                "componentKeys": project_key,
                "organization":  org_slug,
                "statuses":      "OPEN,CONFIRMED,REOPENED",
                "ps":            500,
                "p":             page,
            }, token)

            for issue in data.get("issues", []):
                component = issue.get("component", "")
                if not _matches(component, file_paths):
                    continue
                sev = issue.get("severity", "INFO")
                report.severity_counts[sev] = report.severity_counts.get(sev, 0) + 1
                report.total_issues += 1
                file_part = component.split(":", 1)[-1] if ":" in component else component
                seen_paths.add(file_part)
                report.issues_detail.append({
                    "file":     file_part,
                    "line":     issue.get("line", "?"),
                    "severity": sev,
                    "message":  issue.get("message", ""),
                    "rule":     issue.get("rule", ""),
                    "key":      issue.get("key", ""),
                })

            total = data.get("total", 0)
            if page * 500 >= total:
                break
            page += 1

        report.matched_paths = sorted(seen_paths)

    except Exception as exc:
        report.error = f"Issues fetch failed: {exc}"
        return report

    # ── Coverage ──────────────────────────────────────────────────────────────
    try:
        page = 1
        while True:
            data = _get("measures/component_tree", {
                "component":    project_key,
                "organization": org_slug,
                "metricKeys":   "lines_to_cover,covered_lines",
                "qualifiers":   "FIL",
                "ps":           500,
                "p":            page,
            }, token)

            for comp in data.get("components", []):
                key = comp.get("key", "")
                if not _matches(key, file_paths):
                    continue
                measures = {m["metric"]: int(m.get("value", 0))
                            for m in comp.get("measures", [])}
                report.total_lines   += measures.get("lines_to_cover", 0)
                report.covered_lines += measures.get("covered_lines", 0)

            paging = data.get("paging", {})
            if page * 500 >= paging.get("total", 0):
                break
            page += 1

        if report.total_lines > 0:
            report.coverage_pct = report.covered_lines / report.total_lines * 100

    except Exception as exc:
        report.coverage_error = str(exc)  # non-fatal: still return issues

    return report
