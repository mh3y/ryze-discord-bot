# Ryze Education Discord Bot

A production-ready Discord bot that automates class operations for Ryze Education, a maths tutoring business using Discord as a student learning hub.

---

## Features

| Feature | Description |
|---|---|
| **Google Calendar sync** | Auto-pulls lessons every 10 minutes from per-class Google Calendars |
| **Lesson reminders** | DM students & tutors + channel message at 24h, 1h, and 15min before class |
| **Attendance tracking** | Automatic voice-channel join/leave tracking with smart status calculation |
| **Lesson threads** | Auto-creates a Discord thread 30 minutes before each lesson |
| **Homework management** | Set tasks, post to threads, remind students 24h before due date |
| **Slash commands** | Full admin/tutor command suite for roster, attendance, and class management |

All times use **Australia/Sydney** timezone.

---

## Prerequisites

- Python 3.12
- PostgreSQL 15+ (or Railway/Render managed Postgres)
- A Discord bot application with required permissions
- A Google Cloud project with Calendar API enabled

---

## 1. Discord Bot Setup

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a **New Application** → name it `Ryze Education Bot`
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable:
   - `SERVER MEMBERS INTENT`
   - `VOICE STATE INTENT`
   - `MESSAGE CONTENT INTENT`
5. Copy the **Bot Token** → this is your `DISCORD_TOKEN`
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions:
     - `Read Messages/View Channels`
     - `Send Messages`
     - `Create Public Threads`
     - `Send Messages in Threads`
     - `Manage Threads`
     - `Embed Links`
     - `Read Message History`
7. Copy the generated URL, open it in your browser, and invite the bot to your server
8. Copy your **Server ID** (right-click server → Copy ID) → this is your `DISCORD_GUILD_ID`

---

## 2. Google Calendar API Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Google Calendar API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client IDs**
   - Application type: **Desktop App**
5. Download the credentials JSON
6. Run the following script once to get your refresh token:

```python
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

print("CLIENT_ID:", creds.client_id)
print("CLIENT_SECRET:", creds.client_secret)
print("REFRESH_TOKEN:", creds.refresh_token)
```

Copy these values to your `.env` file.

---

## 3. Local Development Setup

```bash
# 1. Clone and enter project
cd ryze-discord-bot

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Mac/Linux
.venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your real values

# 5. Start PostgreSQL (Docker)
docker compose up db -d

# 6. Run migrations
alembic upgrade head

# 7. Start the bot
python -m bot.main
```

---

## 4. Docker Deployment

```bash
# Build and start everything (bot + PostgreSQL)
docker compose up --build

# Run migrations inside the container
docker compose exec bot alembic upgrade head

# View logs
docker compose logs -f bot
```

---

## 5. Railway Deployment

1. Push this repository to GitHub
2. Create a new Railway project → **Deploy from GitHub repo**
3. Add a **PostgreSQL** plugin — Railway sets `DATABASE_URL` automatically
4. Add all environment variables from `.env.example` under **Variables**
5. Railway auto-detects the `Dockerfile` and builds/deploys
6. After first deploy, open a Railway shell and run:
   ```bash
   alembic upgrade head
   ```

---

## 6. Render Deployment

1. Create a **Web Service** from your GitHub repo
2. Set **Start Command**: `python -m bot.main`
3. Add a **PostgreSQL** database — copy the internal connection string to `DATABASE_URL`
4. Set all environment variables
5. In the Render Shell, run `alembic upgrade head`

> **Note:** Render free tier spins down on inactivity. Use a paid instance or Railway for a persistent bot.

---

## 7. Database Migrations

```bash
# Create a new migration after changing models
alembic revision --autogenerate -m "describe your change"

# Apply all pending migrations
alembic upgrade head

# Downgrade one revision
alembic downgrade -1

# Show current revision
alembic current
```

---

## 8. Configuring Class Groups

Class groups must be created directly in the database (a dashboard UI will be added in a future release).

```sql
INSERT INTO class_groups (
  name, year_level, subject,
  discord_role_id, discord_text_channel_id, discord_voice_channel_id,
  google_calendar_id, active
) VALUES (
  'Year 11 Advanced', '11', 'Mathematics',
  123456789,   -- Discord role ID for @Year 11 Advanced
  987654321,   -- Discord text channel ID
  111222333,   -- Discord voice channel ID
  'abc123@group.calendar.google.com',  -- Google Calendar ID
  true
);
```

**Finding IDs in Discord:** Enable Developer Mode (Settings → Advanced → Developer Mode), then right-click any role/channel and choose **Copy ID**.

**Finding the Google Calendar ID:** In Google Calendar → Settings for the calendar → Scroll to **Calendar ID** at the bottom.

---

## 9. Linking Discord Users to Students

Use the `/link_user` slash command (Admin only):

```
/link_user discord_user:@StudentName full_name:"Jane Smith" role:student
```

Then enrol them in a class:

```
/assign_student_to_class student:@StudentName class_group_id:1
```

For tutors:

```
/link_user discord_user:@TutorName full_name:"John Doe" role:tutor
/assign_tutor_to_class tutor:@TutorName class_group_id:1
```

---

## 10. Slash Commands Reference

### Admin commands (Administrator permission)
| Command | Description |
|---|---|
| `/sync_calendar` | Manually trigger Google Calendar sync |
| `/today_classes` | List all classes today |
| `/upcoming_classes` | List classes in the next 7 days |
| `/class_roster class_group_id:` | Show enrolled students for a class |
| `/link_user` | Link a Discord user to a Ryze Education record |
| `/assign_student_to_class` | Enrol a student in a class |
| `/assign_tutor_to_class` | Assign a tutor to a class |

### Tutor/admin commands (Manage Guild permission)
| Command | Description |
|---|---|
| `/attendance lesson_id:` | View attendance for a lesson |
| `/mark_attendance lesson_id: student: status:` | Manually override attendance |
| `/student_attendance student:` | View full attendance history for a student |
| `/create_lesson_thread lesson_id:` | Manually create a lesson thread |
| `/set_homework lesson_id: title: due_date: description:` | Create a homework task |
| `/missing_homework class_group_id:` | List students with overdue homework |

---

## 11. How Attendance is Calculated

Attendance is tracked automatically via Discord voice channel events:

1. When a student joins a class voice channel, a `VoiceSession` is created.
2. When they leave, the session is closed and `duration_minutes` is calculated.
3. At lesson end (triggered by the per-minute finalise loop), the system calculates each enrolled student's status:

| Status | Rule |
|---|---|
| **Present** | Attended ≥70% of lesson duration AND joined within 15 min of start |
| **Late** | Attended ≥70% of lesson duration BUT joined more than 15 min after start |
| **Left Early** | Joined but attended <70% of lesson duration |
| **Absent** | Never joined during the lesson window |
| **Unknown** | Discord user not linked to a student record |

Manual overrides are available via `/mark_attendance`.

---

## 12. Project Structure

```
ryze-discord-bot/
├── bot/
│   ├── main.py              # Bot entry point
│   ├── config.py            # Environment variable loading
│   ├── database.py          # SQLAlchemy async engine + session factory
│   ├── cogs/                # Discord event/command handlers
│   │   ├── calendar_sync.py
│   │   ├── reminders.py
│   │   ├── attendance.py
│   │   ├── lesson_threads.py
│   │   ├── homework.py
│   │   └── admin.py
│   ├── services/            # Business logic (no Discord dependency)
│   │   ├── google_calendar_service.py
│   │   ├── lesson_service.py
│   │   ├── reminder_service.py
│   │   ├── attendance_service.py
│   │   ├── homework_service.py
│   │   └── discord_service.py
│   ├── models/              # SQLAlchemy ORM models
│   │   ├── user.py
│   │   ├── class_group.py
│   │   ├── lesson.py
│   │   ├── attendance.py
│   │   ├── homework.py
│   │   └── reminder_log.py
│   └── utils/
│       ├── time_utils.py    # Sydney timezone helpers
│       └── logging_utils.py
├── migrations/              # Alembic migrations
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── test_attendance.py
│   └── test_reminders.py
├── .env.example
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 13. Running Tests

```bash
pip install -r requirements.txt
pip install aiosqlite  # lightweight in-memory DB for tests

pytest tests/ -v
```

---

## 14. Future Roadmap

The architecture is database-first, meaning a web dashboard (e.g. FastAPI + React) can be connected to the same PostgreSQL database without changes to the bot.

Planned additions:
- Tutor/admin web dashboard
- Parent portal with attendance reports
- Automated lesson recap posts
- Zoom integration alongside Google Meet
