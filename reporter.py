"""
reporter.py — Builds Slack Block Kit payloads and posts them.

Called both by the scheduler (automated reports) and by /sonar report (on-demand).
"""

import os
from collections import defaultdict
from datetime import datetime
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from sonar_client import SonarReport, fetch_report
from database import get_repo, get_file_paths

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
_slack = WebClient(token=SLACK_BOT_TOKEN)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cov_emoji(pct):
    if pct is None: return "⚪"
    if pct >= 80:   return "🟢"
    if pct >= 60:   return "🟡"
    return "🔴"


def _issue_emoji(count):
    if count == 0:  return "✅"
    if count <= 5:  return "⚠️"
    return "🔴"


SEV_ICON = {"BLOCKER": "🔴", "CRITICAL": "🟠", "MAJOR": "🟡", "MINOR": "⚪", "INFO": "ℹ️"}
SEV_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
MAX_ISSUES_SHOWN = 20   # Slack block limit safety


def _schedule_label(schedule: str) -> str:
    return {"weekly": "Weekly", "biweekly": "Bi-weekly", "monthly": "Monthly"}.get(schedule, schedule)


def _short_path(path: str) -> str:
    """Show only last 2 path segments to keep lines short."""
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


def _issues_by_file_blocks(issues: list[dict], project_key: str) -> list:
    """Build blocks listing issues grouped by file, sorted by severity."""
    if not issues:
        return []

    # Sort: severity order first, then file
    sev_rank = {s: i for i, s in enumerate(SEV_ORDER)}
    sorted_issues = sorted(issues, key=lambda x: (sev_rank.get(x["severity"], 99), x["file"]))

    # Group by file
    by_file = defaultdict(list)
    for issue in sorted_issues[:MAX_ISSUES_SHOWN]:
        by_file[issue["file"]].append(issue)

    blocks = [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Issues in filtered files:*"}},
    ]

    for file_path, file_issues in by_file.items():
        lines = []
        for i in issue_list := file_issues:
            icon = SEV_ICON.get(i["severity"], "⚪")
            line_ref = f"L{i['line']}" if i["line"] != "?" else ""
            msg = i["message"][:80] + "…" if len(i["message"]) > 80 else i["message"]
            lines.append(f"{icon} `{line_ref}` {msg}")

        file_url = (
            f"https://sonarcloud.io/project/issues"
            f"?id={project_key}&resolved=false"
            f"&files={file_path.split('/')[-1]}"
        )
        text = f"*<{file_url}|`{_short_path(file_path)}`>*\n" + "\n".join(lines)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    total = len(issues)
    if total > MAX_ISSUES_SHOWN:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"_{total - MAX_ISSUES_SHOWN} more issue(s) not shown — see full report_"}]})

    return blocks


# ── Block Kit payload ──────────────────────────────────────────────────────────

def build_payload(report: SonarReport, repo: dict) -> list:
    issues_url = f"https://sonarcloud.io/project/issues?id={report.project_key}&resolved=false"
    today = datetime.utcnow().strftime("%B %d, %Y")
    schedule = _schedule_label(repo.get("schedule", "weekly"))

    if report.error:
        return [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"❌ *SonarCloud report failed for `{report.project_key}`*\n{report.error}"}},
        ]

    if report.coverage_pct is not None:
        cov_text = f"{report.coverage_pct:.1f}% ({report.covered_lines}/{report.total_lines} lines)"
    elif report.coverage_error:
        cov_text = "Not configured"
    else:
        cov_text = "No data"

    b = report.severity_counts
    total = report.total_issues

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
          "text": f"📊 {schedule} SonarCloud Report", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn",
          "text": f"*Project:* `{report.project_key}`  •  {today}"}]},
        {"type": "divider"},
        {"type": "section", "fields": [
            {"type": "mrkdwn",
             "text": f"*{_cov_emoji(report.coverage_pct)} Coverage*\n{cov_text}"},
            {"type": "mrkdwn",
             "text": f"*{_issue_emoji(total)} Open Issues*\n{total} total in filtered files"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
          "text": (
              f"*By severity*\n"
              f"🔴 Blocker: *{b.get('BLOCKER',0)}*   "
              f"🟠 Critical: *{b.get('CRITICAL',0)}*   "
              f"🟡 Major: *{b.get('MAJOR',0)}*   "
              f"⚪ Minor: *{b.get('MINOR',0)}*"
          )}},
    ]

    # Urgent banner
    urgent = b.get("BLOCKER", 0) + b.get("CRITICAL", 0)
    if urgent > 0:
        blocks.insert(3, {"type": "section", "text": {"type": "mrkdwn",
          "text": f"⛔ *Action required:* {urgent} blocker/critical issue(s) need attention."}})

    # Per-file issue detail
    blocks += _issues_by_file_blocks(report.issues_detail, report.project_key)

    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
          "text": f"<{issues_url}|View all issues on SonarCloud →>"}},
    ]

    return blocks


# ── Post ──────────────────────────────────────────────────────────────────────

def post_report(project_key: str, channel_id: str = None) -> str:
    """
    Fetches SonarCloud data and posts report to Slack.
    Returns a status message string.
    """
    repo = get_repo(project_key)
    if not repo:
        return f"❌ Repo `{project_key}` is not registered."

    target_channel = channel_id or repo.get("channel_id")
    if not target_channel:
        return f"❌ No channel set for `{project_key}`. Run `/sonar channel {project_key} #your-channel` first."

    file_paths = get_file_paths(project_key)
    report = fetch_report(
        project_key=project_key,
        org_slug=repo["org_slug"],
        file_paths=file_paths,
        token=repo.get("sonar_token"),
    )

    blocks = build_payload(report, repo)

    try:
        _slack.chat_postMessage(channel=target_channel, blocks=blocks, text=f"SonarCloud report for {project_key}")
        return f"✅ Report posted to <#{target_channel}>."
    except SlackApiError as e:
        return f"❌ Slack error: {e.response['error']}"
