import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Required environment variable {key!r} is not set.")
    return value


# Discord
DISCORD_TOKEN: str = _require("DISCORD_TOKEN")
DISCORD_GUILD_ID: int = int(_require("DISCORD_GUILD_ID"))

# Database
DATABASE_URL: str = _require("DATABASE_URL")

# Google Calendar OAuth2
GOOGLE_CLIENT_ID: str = _require("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET: str = _require("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN: str = _require("GOOGLE_REFRESH_TOKEN")
GOOGLE_PROJECT_ID: str = _require("GOOGLE_PROJECT_ID")

# App
DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "Australia/Sydney")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Heartbeat / dead-man's-switch (optional). If HEARTBEAT_URL is set, the bot pings
# it every HEARTBEAT_INTERVAL_MINUTES; an external monitor (healthchecks.io /
# UptimeRobot) alerts if the pings stop. Leave unset to disable (see heartbeat cog).
HEARTBEAT_URL: str | None = os.getenv("HEARTBEAT_URL")
HEARTBEAT_INTERVAL_MINUTES: int = int(os.getenv("HEARTBEAT_INTERVAL_MINUTES", "5"))

# Calendar sync
CALENDAR_SYNC_INTERVAL: int = int(os.getenv("CALENDAR_SYNC_INTERVAL", "10"))
CALENDAR_SYNC_DAYS_AHEAD: int = int(os.getenv("CALENDAR_SYNC_DAYS_AHEAD", "30"))

# Reminder offsets in minutes before lesson start
REMINDER_OFFSETS_MINUTES: list[int] = [24 * 60, 60, 15]

# Attendance thresholds
ATTENDANCE_PRESENT_THRESHOLD: float = 0.70
ATTENDANCE_LATE_MINUTES: int = 15

# Thread creation lead time
THREAD_CREATION_LEAD_MINUTES: int = 30

# Lesson detection window: how many minutes before/after start to match voice joins
LESSON_WINDOW_BEFORE_MINUTES: int = 15
LESSON_WINDOW_AFTER_MINUTES: int = 120

# Optional: post member-join notifications here (admin channel ID).
# If unset, admins use /pending_members to review new arrivals.
ADMIN_NOTIFICATION_CHANNEL_ID: int | None = (
    int(v) if (v := os.getenv("ADMIN_NOTIFICATION_CHANNEL_ID")) else None
)

# Portal REST API (Node.js backend on Render)
# The bot calls this API to push member/lesson data into Supabase.
PORTAL_API_URL: str = os.getenv("PORTAL_API_URL", "https://ryze-portal-api.onrender.com")
DASHBOARD_API_KEY: str | None = os.getenv("DASHBOARD_API_KEY")
DASHBOARD_CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("DASHBOARD_CORS_ORIGINS", "http://localhost:5173,https://ryzeeducation.com.au").split(",")
    if o.strip()
]

# JWT auth (portal sessions)
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_USE_A_REAL_SECRET_IN_PRODUCTION")
JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))

# Discord OAuth2 (for student/tutor/admin login)
DISCORD_CLIENT_ID: str | None = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET: str | None = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI: str = os.getenv(
    "DISCORD_REDIRECT_URI", "http://localhost:5173/auth/discord/callback"
)

# Parent invite link base URL (sent in invite emails)
PORTAL_BASE_URL: str = os.getenv("PORTAL_BASE_URL", "http://localhost:5173")
