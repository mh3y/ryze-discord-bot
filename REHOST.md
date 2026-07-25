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
   | `HEARTBEAT_URL` | **required in production** — see below; without it the next outage is silent again |
   | `STAFF_ROLE_IDS` | comma-separated Discord role IDs whose holders are CRM **admins**. UNSET = fail-closed: nobody classifies as staff and NO member data is pushed to the CRM |
   | `TUTOR_ROLE_IDS` | comma-separated Discord role IDs whose holders are CRM **tutors** |
   | `PARENT_ROLE_IDS` | comma-separated Discord role IDs for **parents** (recorded locally, never pushed as students) |

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

## RELAUNCH-CHECKLIST — hard preconditions of creating the worker

Per the 2026-07-25 leadership review, the worker is **not created until every box
is ticked**. This is what stops "I'll do the Render stuff later" from accidentally
deploying without the safeguarding bar. Boxes 1–7 are code (already on the
deploy candidate branch — verify they're merged beneath the tip you deploy);
8–12 are owner actions at deploy time.

1. ☐ **Permission-sync grants validated** — a grant not backed by an enrolled
   member/tutor of that class channel is refused and logged (DEF-15, with tests).
2. ☐ **Role classification fail-closed** — staff/tutor status only via
   `STAFF_ROLE_IDS`/`TUTOR_ROLE_IDS`; unset ⇒ zero CRM role writes; the bot
   never demotes a CRM-set admin (DEF-16, with tests).
3. ☐ **Every background loop has a crash-restart handler** — one bad tick can't
   silently kill job polling / sync / reminders (DEF-18/H10).
4. ☐ **Startup probe is real** — DB `SELECT 1` fail-fast; portal + Google
   probed loudly at boot (M9).
5. ☐ **Heartbeat proves work, not liveness** — ping suppressed when the gateway
   is down or the last successful sync is stale, so the monitor fires (M8).
6. ☐ **`/delete_user` is truthful** — soft-deactivate; failures surfaced;
   history retained (H2/H5). Privacy-erasure requests follow the erasure runbook.
7. ☐ **CI green** — the pytest gate on the deploy candidate passes.
8. ☐ **Secrets set** on the worker (table above) including `HEARTBEAT_URL` +
   an external heartbeat monitor created.
9. ☐ **Role IDs supplied** (`STAFF_ROLE_IDS`/`TUTOR_ROLE_IDS`/`PARENT_ROLE_IDS`)
   — or the owner explicitly accepts the fail-closed default (no CRM member sync).
10. ☐ **DM policy applied** in Discord server settings (no unmonitored 1:1
    adult↔minor contact; tuition communication in class channels).
11. ☐ **Supervised smoke window** after first boot: startup-probe PASS lines,
    first calendar sync succeeds, heartbeat monitor green, one test reminder,
    portal push 200s.
12. ☐ **7-day soak** before enabling permission-sync grant application or
    starting any new feature wave: no loop-crash logs, heartbeat green, sync
    logs honest.

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
