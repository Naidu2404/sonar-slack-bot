# SonarCloud Slack Bot — Setup Guide

## File structure

```
sonar-bot/
├── app.py               ← main bot (slash commands + scheduler)
├── database.py          ← SQLite config store
├── sonar_client.py      ← SonarCloud API wrapper
├── reporter.py          ← report builder + Slack poster
├── requirements.txt
├── render.yaml          ← Render deployment config
└── .env.example         ← copy to .env locally
```

---

## Step 1 — Create the Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
   - Name: `SonarCloud Bot` | select your workspace

2. **Socket Mode** (left sidebar) → Enable Socket Mode → Generate an App-Level Token
   - Token name: `socket-token` | scope: `connections:write`
   - Copy the token — this is your `SLACK_APP_TOKEN` (starts with `xapp-`)

3. **OAuth & Permissions** → Bot Token Scopes — add:
   - `commands`
   - `chat:write`
   - `chat:write.public`

4. **Install App** → Install to Workspace → copy the **Bot User OAuth Token** (`xoxb-...`) → this is `SLACK_BOT_TOKEN`

5. **Slash Commands** → Create New Command:
   - Command: `/sonar`
   - Request URL: _(leave blank for Socket Mode)_
   - Short description: `SonarCloud reporting bot`
   - Save

6. Re-install the app to your workspace after adding scopes.

---

## Step 2 — SonarCloud token

Go to [sonarcloud.io](https://sonarcloud.io) → avatar → **My Account** → **Security** → Generate a token named `slack-bot` → copy it → this is `SONAR_TOKEN`.

---

## Step 3 — Deploy to Render

1. Push the `sonar-bot/` folder to a GitHub repo.
2. Go to [render.com](https://render.com) → **New** → **Blueprint** → connect your repo → Render detects `render.yaml` automatically.
3. In the Render dashboard for the service → **Environment** → add:
   - `SLACK_BOT_TOKEN` = `xoxb-...`
   - `SLACK_APP_TOKEN` = `xapp-...`
   - `SONAR_TOKEN` = your SonarCloud token
4. Also set `DB_PATH=/data/sonar_bot.db` so the SQLite file lives on the persistent disk.
5. Deploy.

**Railway alternative:** Create a new project → Deploy from GitHub → add the same 3 env vars in the Variables tab. Add a Volume mounted at `/data` for DB persistence.

---

## Step 4 — Invite the bot to your channel

In Slack: open the channel → `/invite @SonarCloud Bot`

---

## Step 5 — Register your first repo

```
/sonar add-repo recruitcrm_contract-staffing recruitcrm
/sonar add-files recruitcrm_contract-staffing src/components/contract-staffing src/stores/contract-staffing src/views/contract-staffing
/sonar channel recruitcrm_contract-staffing #engineering
/sonar schedule recruitcrm_contract-staffing weekly
```

Test it immediately:
```
/sonar report recruitcrm_contract-staffing
```

---

## All slash commands

| Command | What it does |
|---|---|
| `/sonar add-repo <key> <org>` | Register a repo |
| `/sonar remove-repo <key>` | Remove a repo |
| `/sonar add-files <key> <path>…` | Add file path filters |
| `/sonar remove-files <key> <path>…` | Remove file path filters |
| `/sonar channel <key> #channel` | Set report channel |
| `/sonar schedule <key> weekly\|biweekly\|monthly` | Set report frequency |
| `/sonar report <key>` | Run report right now |
| `/sonar list` | List all repos |
| `/sonar status <key>` | Show one repo's config |
| `/sonar help` | Show usage |

---

## How the scheduler works

- **Weekly** → every Monday at 9:00 AM UTC
- **Bi-weekly** → every other Monday at 9:00 AM UTC
- **Monthly** → 1st of each month at 9:00 AM UTC

Schedules are stored in SQLite and reloaded automatically on every restart. Changing a schedule with `/sonar schedule` takes effect immediately — no restart needed.

---

## Adding more repos later

Just repeat the 4 commands in Step 5 for each new repo. Each repo gets its own schedule, channel, and file filters independently.
