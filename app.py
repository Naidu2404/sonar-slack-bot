"""
app.py — Slack Bolt app for the SonarCloud reporting bot.

Slash command: /sonar <subcommand> [args...]

Subcommands:
  add-repo <project-key> <org-slug>          Register a repo
  remove-repo <project-key>                  Remove a repo
  add-files <project-key> <path> [path...]   Add file path filters
  remove-files <project-key> <path> [path…]  Remove file path filters
  channel <project-key> #channel             Set report channel
  schedule <project-key> weekly|biweekly|monthly
  report <project-key>                       Run report immediately
  list                                       List all repos + config
  status <project-key>                       Show one repo's config
  help                                       Show this help
"""

import os
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import database as db
from reporter import post_report

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
scheduler = BackgroundScheduler(timezone="UTC")

VALID_SCHEDULES = ("weekly", "biweekly", "monthly")

# ── Schedule helpers ───────────────────────────────────────────────────────────

SCHEDULE_CRONS = {
    "weekly":   CronTrigger(day_of_week="mon", hour=9, minute=0),
    "biweekly": CronTrigger(day_of_week="mon", hour=9, minute=0, week="*/2"),
    "monthly":  CronTrigger(day=1, hour=9, minute=0),
}


def _job_id(project_key: str) -> str:
    return f"report_{project_key}"


def _schedule_repo(project_key: str, schedule: str):
    job_id = _job_id(project_key)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        post_report,
        trigger=SCHEDULE_CRONS[schedule],
        id=job_id,
        args=[project_key],
        replace_existing=True,
    )
    log.info(f"Scheduled {project_key} — {schedule}")


def _unschedule_repo(project_key: str):
    job_id = _job_id(project_key)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def _load_all_schedules():
    """Re-register all scheduled jobs from DB on startup."""
    for repo in db.list_repos():
        if repo.get("channel_id") and repo.get("schedule"):
            _schedule_repo(repo["project_key"], repo["schedule"])


# ── /sonar handler ─────────────────────────────────────────────────────────────

def _ack_and_respond(ack, respond, message: str):
    ack()
    respond(message)


@app.command("/sonar")
def handle_sonar(ack, respond, command, body):
    ack()
    text = (command.get("text") or "").strip()
    parts = text.split()
    user_id = body.get("user_id", "unknown")

    if not parts:
        parts = ["help"]

    sub = parts[0].lower()

    # ── help ──────────────────────────────────────────────────────────────────
    if sub == "help":
        respond(
            "```\n"
            "/sonar add-repo <project-key> <org-slug>          Register a repo\n"
            "/sonar remove-repo <project-key>                  Remove a repo\n"
            "/sonar add-files <project-key> <path> [path…]    Add file path filters\n"
            "/sonar remove-files <project-key> <path> [path…] Remove file path filters\n"
            "/sonar channel <project-key> #channel             Set the report channel\n"
            "/sonar schedule <project-key> weekly|biweekly|monthly\n"
            "/sonar report <project-key>                       Run report now\n"
            "/sonar list                                       List all repos\n"
            "/sonar status <project-key>                       Show repo config\n"
            "```"
        )

    # ── add-repo ──────────────────────────────────────────────────────────────
    elif sub == "add-repo":
        if len(parts) < 3:
            return respond("Usage: `/sonar add-repo <project-key> <org-slug>`")
        project_key, org_slug = parts[1], parts[2]
        added = db.add_repo(project_key, org_slug, added_by=user_id)
        if added:
            respond(
                f"✅ Repo `{project_key}` registered.\n"
                f"Next: add file filters with `/sonar add-files {project_key} src/your-module`\n"
                f"Then set a channel: `/sonar channel {project_key} #engineering`\n"
                f"And a schedule: `/sonar schedule {project_key} weekly`"
            )
        else:
            respond(f"⚠️ Repo `{project_key}` is already registered.")

    # ── remove-repo ───────────────────────────────────────────────────────────
    elif sub == "remove-repo":
        if len(parts) < 2:
            return respond("Usage: `/sonar remove-repo <project-key>`")
        project_key = parts[1]
        removed = db.remove_repo(project_key)
        _unschedule_repo(project_key)
        respond(f"✅ Repo `{project_key}` removed." if removed else f"⚠️ Repo `{project_key}` not found.")

    # ── add-files ─────────────────────────────────────────────────────────────
    elif sub == "add-files":
        if len(parts) < 3:
            return respond("Usage: `/sonar add-files <project-key> <path> [path…]`")
        project_key = parts[1]
        paths = parts[2:]
        if not db.get_repo(project_key):
            return respond(f"❌ Repo `{project_key}` not found. Register it first with `/sonar add-repo`.")
        count = db.add_file_paths(project_key, paths)
        all_paths = db.get_file_paths(project_key)
        respond(
            f"✅ Added {count} path(s) to `{project_key}`.\n"
            f"Current filters:\n```\n" + "\n".join(all_paths) + "\n```"
        )

    # ── remove-files ──────────────────────────────────────────────────────────
    elif sub == "remove-files":
        if len(parts) < 3:
            return respond("Usage: `/sonar remove-files <project-key> <path> [path…]`")
        project_key = parts[1]
        paths = parts[2:]
        count = db.remove_file_paths(project_key, paths)
        all_paths = db.get_file_paths(project_key)
        remaining = "\n".join(all_paths) if all_paths else "(none — all files will be scanned)"
        respond(f"✅ Removed {count} path(s).\nRemaining:\n```\n{remaining}\n```")

    # ── channel ───────────────────────────────────────────────────────────────
    elif sub == "channel":
        if len(parts) < 3:
            return respond("Usage: `/sonar channel <project-key> #channel`")
        project_key = parts[1]
        raw_channel = parts[2]
        # Accept both #channel-name and <#C123|name> (Slack auto-formats channel mentions)
        channel_id = raw_channel.strip("<>#").split("|")[0]
        if not db.get_repo(project_key):
            return respond(f"❌ Repo `{project_key}` not found.")
        db.set_channel(project_key, channel_id)
        respond(f"✅ Reports for `{project_key}` will go to <#{channel_id}>.")

    # ── schedule ──────────────────────────────────────────────────────────────
    elif sub == "schedule":
        if len(parts) < 3:
            return respond("Usage: `/sonar schedule <project-key> weekly|biweekly|monthly`")
        project_key, sched = parts[1], parts[2].lower()
        if sched not in VALID_SCHEDULES:
            return respond(f"❌ Invalid schedule `{sched}`. Choose: weekly, biweekly, monthly.")
        if not db.get_repo(project_key):
            return respond(f"❌ Repo `{project_key}` not found.")
        db.set_schedule(project_key, sched)
        _schedule_repo(project_key, sched)
        respond(f"✅ `{project_key}` set to *{sched}* reports.")

    # ── report ────────────────────────────────────────────────────────────────
    elif sub == "report":
        if len(parts) < 2:
            return respond("Usage: `/sonar report <project-key>`")
        project_key = parts[1]
        respond(f"⏳ Fetching SonarCloud data for `{project_key}`…")
        result = post_report(project_key)
        respond(result)

    # ── list ──────────────────────────────────────────────────────────────────
    elif sub == "list":
        repos = db.list_repos()
        if not repos:
            return respond("No repos registered yet. Use `/sonar add-repo` to get started.")
        lines = []
        for r in repos:
            channel_str = f"<#{r['channel_id']}>" if r.get("channel_id") else "_(no channel set)_"
            paths = db.get_file_paths(r["project_key"])
            path_str = f"{len(paths)} path filter(s)" if paths else "_(all files)_"
            lines.append(
                f"• *{r['project_key']}* | {r['schedule']} | {channel_str} | {path_str}"
            )
        respond("*Registered repos:*\n" + "\n".join(lines))

    # ── status ────────────────────────────────────────────────────────────────
    elif sub == "status":
        if len(parts) < 2:
            return respond("Usage: `/sonar status <project-key>`")
        project_key = parts[1]
        repo = db.get_repo(project_key)
        if not repo:
            return respond(f"❌ Repo `{project_key}` not found.")
        paths = db.get_file_paths(project_key)
        channel_str = f"<#{repo['channel_id']}>" if repo.get("channel_id") else "_(not set)_"
        path_str = "\n".join(f"  - `{p}`" for p in paths) if paths else "  _(none — scanning all files)_"
        respond(
            f"*`{project_key}`*\n"
            f"Org: `{repo['org_slug']}`\n"
            f"Channel: {channel_str}\n"
            f"Schedule: *{repo['schedule']}*\n"
            f"File filters:\n{path_str}"
        )

    else:
        respond(f"Unknown subcommand `{sub}`. Type `/sonar help` for usage.")


# ── Startup ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    scheduler.start()
    _load_all_schedules()
    log.info("SonarCloud bot starting in Socket Mode…")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
