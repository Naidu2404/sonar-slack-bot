"""
app.py — Slack Bolt app for the SonarCloud reporting bot.

All commands are channel-aware: the channel where you run the command
determines which config is used. The same repo can be tracked in multiple
channels with different file filters and schedules.

/sonar add-repo <key> <org>                Register a repo globally
/sonar remove-repo <key>                   Remove repo + all channel configs
/sonar track <key>                         Start tracking repo in THIS channel
/sonar untrack <key>                       Stop tracking in THIS channel
/sonar add-files <key> <path> [path…]     Add file filters for this repo IN THIS channel
/sonar remove-files <key> <path> [path…]  Remove file filters IN THIS channel
/sonar schedule <key> weekly|biweekly|monthly
/sonar report                              Interactive picker of repos in this channel
/sonar list                                List repos tracked in this channel
/sonar status <key>                        Show config for this repo in this channel
/sonar help
"""

import os
import logging
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import database as db
from reporter import post_report, report_picker_blocks

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app       = App(token=os.environ["SLACK_BOT_TOKEN"])
scheduler = BackgroundScheduler(timezone="UTC")

VALID_SCHEDULES = ("daily", "weekly", "biweekly", "monthly")


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' → (hour, minute). Raises ValueError on bad input."""
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        raise ValueError
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError
    return h, m


def _build_cron(schedule: str, report_time: str) -> dict:
    h, m = _parse_time(report_time)
    return {
        "daily":    dict(hour=h, minute=m),
        "weekly":   dict(day_of_week="mon", hour=h, minute=m),
        "biweekly": dict(day_of_week="mon", hour=h, minute=m, week="*/2"),
        "monthly":  dict(day=1, hour=h, minute=m),
    }[schedule]


# ── Scheduler helpers ──────────────────────────────────────────────────────────

def _job_id(project_key: str, channel_id: str) -> str:
    return f"report__{project_key}__{channel_id}"


def _schedule_pair(project_key: str, channel_id: str, schedule: str, report_time: str = "09:00"):
    job_id = _job_id(project_key, channel_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        post_report,
        trigger=CronTrigger(**_build_cron(schedule, report_time)),
        id=job_id,
        args=[project_key, channel_id],
        replace_existing=True,
    )
    log.info(f"Scheduled {project_key} in {channel_id} — {schedule} at {report_time} UTC")


def _unschedule_pair(project_key: str, channel_id: str):
    job_id = _job_id(project_key, channel_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def _load_all_schedules():
    for cfg in db.list_all_channel_configs():
        _schedule_pair(cfg["project_key"], cfg["channel_id"], cfg["schedule"], cfg.get("report_time", "09:00"))


# ── /sonar slash command ───────────────────────────────────────────────────────

@app.command("/sonar")
def handle_sonar(ack, respond, command, body):
    ack()
    text       = (command.get("text") or "").strip()
    parts      = text.split()
    user_id    = body.get("user_id", "unknown")
    channel_id = body.get("channel_id", "")

    if not parts:
        parts = ["help"]

    sub = parts[0].lower()

    # ── help ──────────────────────────────────────────────────────────────────
    if sub == "help":
        respond(
            "```\n"
            "/sonar add-repo <key> <org>                Register a repo globally\n"
            "/sonar remove-repo <key>                   Remove repo everywhere\n"
            "/sonar track <key>                         Track repo in THIS channel\n"
            "/sonar untrack <key>                       Stop tracking in THIS channel\n"
            "/sonar add-files <key> <path> [path…]     Add file filters for THIS channel\n"
            "/sonar remove-files <key> <path> [path…]  Remove file filters for THIS channel\n"
            "/sonar schedule <key> daily|weekly|biweekly|monthly [HH:MM UTC]\n"
            "/sonar report                              Pick a repo and run its report\n"
            "/sonar list                                List repos tracked here\n"
            "/sonar status <key>                        Show config for this channel\n"
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
                f"✅ Repo `{project_key}` registered globally.\n"
                f"Now track it in any channel with `/sonar track {project_key}`"
            )
        else:
            respond(f"⚠️ `{project_key}` is already registered.")

    # ── remove-repo ───────────────────────────────────────────────────────────
    elif sub == "remove-repo":
        if len(parts) < 2:
            return respond("Usage: `/sonar remove-repo <project-key>`")
        project_key = parts[1]
        # unschedule all channel jobs first
        for cfg in db.list_all_channel_configs():
            if cfg["project_key"] == project_key:
                _unschedule_pair(project_key, cfg["channel_id"])
        removed = db.remove_repo(project_key)
        respond(f"✅ `{project_key}` removed from all channels." if removed
                else f"⚠️ `{project_key}` not found.")

    # ── track ─────────────────────────────────────────────────────────────────
    elif sub == "track":
        if len(parts) < 2:
            return respond("Usage: `/sonar track <project-key>`")
        project_key = parts[1]
        if not db.get_repo(project_key):
            return respond(f"❌ `{project_key}` not registered. Run `/sonar add-repo {project_key} <org>` first.")
        created = db.track_repo_in_channel(project_key, channel_id)
        if created:
            respond(
                f"✅ Now tracking `{project_key}` in this channel.\n"
                f"• Add file filters: `/sonar add-files {project_key} src/your-module`\n"
                f"• Set schedule: `/sonar schedule {project_key} weekly`"
            )
        else:
            respond(f"⚠️ `{project_key}` is already tracked in this channel.")

    # ── untrack ───────────────────────────────────────────────────────────────
    elif sub == "untrack":
        if len(parts) < 2:
            return respond("Usage: `/sonar untrack <project-key>`")
        project_key = parts[1]
        _unschedule_pair(project_key, channel_id)
        removed = db.untrack_repo_in_channel(project_key, channel_id)
        respond(f"✅ Stopped tracking `{project_key}` in this channel." if removed
                else f"⚠️ `{project_key}` was not tracked here.")

    # ── add-files ─────────────────────────────────────────────────────────────
    elif sub == "add-files":
        if len(parts) < 3:
            return respond("Usage: `/sonar add-files <project-key> <path> [path…]`")
        project_key = parts[1]
        paths = parts[2:]
        if not db.get_channel_config(project_key, channel_id):
            return respond(f"❌ `{project_key}` is not tracked in this channel. Run `/sonar track {project_key}` first.")
        count = db.add_file_paths(project_key, channel_id, paths)
        all_paths = db.get_file_paths(project_key, channel_id)
        respond(
            f"✅ Added {count} path(s) for `{project_key}` in this channel.\n"
            f"Current filters:\n```\n" + "\n".join(all_paths) + "\n```"
        )

    # ── remove-files ──────────────────────────────────────────────────────────
    elif sub == "remove-files":
        if len(parts) < 3:
            return respond("Usage: `/sonar remove-files <project-key> <path> [path…]`")
        project_key = parts[1]
        count = db.remove_file_paths(project_key, channel_id, parts[2:])
        all_paths = db.get_file_paths(project_key, channel_id)
        remaining = "\n".join(all_paths) if all_paths else "(none — all files will be scanned)"
        respond(f"✅ Removed {count} path(s).\nRemaining:\n```\n{remaining}\n```")

    # ── schedule ──────────────────────────────────────────────────────────────
    elif sub == "schedule":
        if len(parts) < 3:
            return respond(
                "Usage: `/sonar schedule <project-key> daily|weekly|biweekly|monthly [HH:MM]`\n"
                "Time is in 24h UTC format. Defaults to 09:00 if omitted.\n"
                "Examples:\n"
                "  `/sonar schedule myrepo daily 08:30`\n"
                "  `/sonar schedule myrepo weekly 14:00`"
            )
        project_key = parts[1]
        sched       = parts[2].lower()
        time_str    = parts[3] if len(parts) >= 4 else "09:00"

        if sched not in VALID_SCHEDULES:
            return respond(f"❌ Invalid schedule. Choose: daily, weekly, biweekly, monthly.")
        try:
            _parse_time(time_str)
        except ValueError:
            return respond(f"❌ Invalid time `{time_str}`. Use HH:MM in 24h format, e.g. `09:00` or `14:30`.")
        if not db.get_channel_config(project_key, channel_id):
            return respond(f"❌ `{project_key}` is not tracked here. Run `/sonar track {project_key}` first.")

        db.set_schedule(project_key, channel_id, sched, time_str)
        _schedule_pair(project_key, channel_id, sched, time_str)
        respond(f"✅ `{project_key}` set to *{sched}* reports at *{time_str} UTC* in this channel.")

    # ── report (interactive picker) ───────────────────────────────────────────
    elif sub == "report":
        repos = db.list_repos_in_channel(channel_id)
        if not repos:
            return respond("No repos tracked in this channel yet. Use `/sonar track <key>` to add one.")
        if len(repos) == 1:
            # Only one repo — run directly without picker
            respond(f"⏳ Fetching report for `{repos[0]['project_key']}`…")
            result = post_report(repos[0]["project_key"], channel_id)
            respond(result)
        else:
            respond(blocks=report_picker_blocks(channel_id, repos),
                    text="Select a repo to report on")

    # ── list ──────────────────────────────────────────────────────────────────
    elif sub == "list":
        repos = db.list_repos_in_channel(channel_id)
        if not repos:
            return respond("No repos tracked in this channel. Use `/sonar track <key>` to add one.")
        lines = []
        for r in repos:
            paths = db.get_file_paths(r["project_key"], channel_id)
            path_str = f"{len(paths)} filter(s)" if paths else "_(all files)_"
            lines.append(f"• *{r['project_key']}* | {r['schedule']} | {path_str}")
        respond("*Repos tracked in this channel:*\n" + "\n".join(lines))

    # ── status ────────────────────────────────────────────────────────────────
    elif sub == "status":
        if len(parts) < 2:
            return respond("Usage: `/sonar status <project-key>`")
        project_key = parts[1]
        cfg = db.get_channel_config(project_key, channel_id)
        if not cfg:
            return respond(f"❌ `{project_key}` is not tracked in this channel.")
        paths = db.get_file_paths(project_key, channel_id)
        path_str = "\n".join(f"  - `{p}`" for p in paths) if paths else "  _(none — scanning all files)_"
        respond(
            f"*`{project_key}`* in this channel\n"
            f"Schedule: *{cfg['schedule']}* at *{cfg.get('report_time', '09:00')} UTC*\n"
            f"File filters:\n{path_str}"
        )

    else:
        respond(f"Unknown subcommand `{sub}`. Type `/sonar help` for usage.")


# ── Interactive action: repo picker ───────────────────────────────────────────

@app.action("run_sonar_report")
def handle_report_select(ack, body, respond):
    ack()
    project_key = body["actions"][0]["selected_option"]["value"]
    channel_id  = body["channel"]["id"]
    respond(f"⏳ Fetching report for `{project_key}`…")
    result = post_report(project_key, channel_id)
    if not result.startswith("✅"):
        respond(result)


# ── Startup ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    scheduler.start()
    _load_all_schedules()
    log.info("SonarCloud bot starting…")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
