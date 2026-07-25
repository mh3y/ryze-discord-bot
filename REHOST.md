# Rehosting the Ryze Discord bot on Render

The bot went fully offline on 2026-07-24 when Oracle reclaimed the OCI Always-Free
VPS that hosted it. No voice-attendance, calendar sync, or reminders have run
since. This is the runbook to bring it back on **Render** as a Background Worker.

Student data is safe — the bot pushed members/lessons/voice-sessions to Supabase
(via `ryze-portal-api`) in real time; only the bot's own local Postgres died with
the host, and that rebuilds itself.

## What changed in this branch

- **`render.yaml`** — a Render Blueprint that provisions a **Background Worker**
  (`ryze-discord-bot`) plus a **Postgres** (`ryze-bot-db`). No inbound HTTP, so it
  is a worker, not a web service.
- **Heartbeat cog** (`bot/cogs/heartbeat.py`) — the last outage was *silent*. If
  you set `HEARTBEAT_URL`, the bot pings an external dead-man's-switch on a schedule
  so you're alerted if it ever goes dark again. Unset = disabled (safe no-op).
- **No connection code change was needed.** `bot/database.py` already converts a
  `postgresql://` URL to the asyncpg driver, and `migrations/env.py` converts it to
  psycopg2 for Alembic, so Render's connection string works as-is.

The legacy FastAPI (`bot/api/`) and the nginx + React `web` service in
`docker-compose.yml` are **superseded** by the CRM (Render API + Vercel frontend)
and are deliberately **not** deployed. They can be deleted in a later tidy-up.

## Deploy steps (owner)

1. **Render → New → Blueprint**, connect the `mh3y/ryze-discord-bot` repo. Render
   reads `render.yaml` and proposes the `ryze-discord-bot` worker + `ryze-bot-db`
   Postgres. Both are on the **Starter** plan (~$7/mo worker; the free Postgres is
   deleted after 90 days, so Starter is used for durability). Adjust if you prefer.
2. **Set the secret env vars** on the worker (they are `sync:false`, so not in the
   repo):
   | Var | Value |
   |---|---|
   | `DISCORD_TOKEN` | the bot token (Discord Developer Portal → Bot) |
   | `DISCORD_GUILD_ID` | the Ryze server's guild ID |
   | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_PROJECT_ID` | the Google OAuth app |
   | `GOOGLE_REFRESH_TOKEN` | run `python get_refresh_token.py` locally to mint one |
   | `DASHBOARD_API_KEY` | **must byte-match `BOT_API_SECRET` on `ryze-portal-api`** — if they differ, every sync silently 401s |
   | `HEARTBEAT_URL` | *(optional)* see below |

   `DATABASE_URL`, `PORTAL_API_URL`, `DEFAULT_TIMEZONE`, `LOG_LEVEL` are set by the
   Blueprint automatically.
3. **Deploy.** `alembic upgrade head` runs at boot (fail-closed) then the bot starts.
4. **Verify** in the worker logs — look for the `RYZE EDUCATION BOT — STARTUP HEALTH
   CHECK` block with all ✓, then `Ryze Education Bot ready`. Confirm the bot shows
   online in Discord, and that a real class run records voice attendance in the CRM.

## Heartbeat (recommended — makes the next outage loud)

1. Create a free check at **healthchecks.io** (or an UptimeRobot "heartbeat" monitor).
2. Copy its ping URL into the `HEARTBEAT_URL` env var (grace period ≈ 2× the ping
   interval, i.e. ~10 min at the default 5-min interval).
3. If the bot ever stops (host, crash-loop, billing lapse), the missed ping alerts you.

## Known follow-ups (not blockers for coming back online)

This rehost restores the bot's **most important job — capturing voice attendance**,
plus member and calendar sync. But per `BOT_AUDIT_2026-07-24.md`, a few core features
need the **Wave-1 fixes** before they work correctly, and should follow this rehost:

- **DEF-8** — attendance *status* mis-marks fully-present students as `left_early`
  (raw capture is fine; the calculated status is wrong).
- **DEF-2** — lesson reminders, auto-threads, and `/set_homework` read the local
  `Lesson` table, which starts empty; they need to read the live source (the portal).
- **DEF-9** — class auto-provisioning is skipped by an over-broad calendar regex.
- **DEF-3** — lesson-cancellation notifications are dead code.

The audit's CONDITIONAL security findings (legacy FastAPI, forged JWT, leaked key)
are now **moot** — the VPS that could have run that stack no longer exists, and the
public Discord secret was already rotated.
