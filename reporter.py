"""
reporter.py — Builds Slack Block Kit payloads and posts them.

Called both by the scheduler (automated reports) and by /sonar report (on-demand).
"""

import os
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


def _schedule_label(schedule: str) -> str:
    return {"weekly": "Weekly", "biweekly": "Bi-weekly", "monthly": "Monthly"}.get(schedule, schedule)


# ── Block Kit payload ──────────────────────────────────────────────────────────

def build_payload(report: SonarReport, repo: dict) -> list:
    project_url = (
        f"https://sonarcloud.io/project/overview?id={report.project_key}"
    )
    today = datetime.utcnow().strftime("%B %d, %Y")
    schedule = _schedule_label(repo.get("schedule", "weekly"))

    if report.error:
        return [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"❌ *SonarCloud report failed for `{report.project_key}`*\n{report.error}"}},
        ]

    cov_text = (
        f"{report.coverage_pct:.1f}% ({report.covered_lines}/{report.total_lines} lines)"
        if report.coverage_pct is not None else "No coverage data"
    )

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
             "text": f"*{_issue_emoji(total)} Open Issues*\n{total} total"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
          "text": (
              f"*By severity*\n"
              f"🔴 Blocker: *{b.get('BLOCKER',0)}*   "
              f"🟠 Critical: *{b.get('CRITICAL',0)}*   "
              f"🟡 Major: *{b.get('MAJOR',0)}*   "
              f"⚪ Minor: *{b.get('MINOR',0)}*"
          )}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
          "text": f"<{project_url}|View full report on SonarCloud →>"}},
    ]

    # Urgent banner
    urgent = b.get("BLOCKER", 0) + b.get("CRITICAL", 0)
    if urgent > 0:
        blocks.insert(3, {"type": "section", "text": {"type": "mrkdwn",
          "text": f"⛔ *Action required:* {urgent} blocker/critical issue(s) need attention."}})

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
