"""Ryze Education Dashboard – FastAPI application.

Run with:
    uvicorn bot.api.app:app --host 0.0.0.0 --port 8000 --reload

The API is intentionally separate from the Discord bot process so it can be
scaled, rate-limited, or proxied independently.  Both processes share the
same database via SQLAlchemy async.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bot import config
from bot.api.routes import health, students, classes, lessons, attendance, reminders, auth as auth_router, admin as admin_router, parent as parent_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    log.info("Dashboard API starting up.")
    yield
    log.info("Dashboard API shutting down.")


app = FastAPI(
    title="Ryze Education Dashboard API",
    version="1.0.0",
    description=(
        "Internal REST API powering the Ryze Education admin portal. "
        "All routes require the X-API-Key header."
    ),
    lifespan=lifespan,
    # Disable OpenAPI docs in production if desired by setting DASHBOARD_API_KEY
    # (docs are still useful during development)
)

# ---------------------------------------------------------------------------
# CORS — allow the Vite dev server and the production website
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.DASHBOARD_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
PREFIX = "/api"

# Auth — under /api/auth/ so nginx doesn't confuse API calls with the
# React /auth/discord/callback SPA route that Discord redirects to.
app.include_router(auth_router.router, prefix=PREFIX)

app.include_router(health.router, prefix=PREFIX, tags=["Health"])
app.include_router(students.router, prefix=PREFIX, tags=["Students"])
app.include_router(classes.router, prefix=PREFIX, tags=["Classes"])
app.include_router(lessons.router, prefix=PREFIX, tags=["Lessons"])
app.include_router(attendance.router, prefix=PREFIX, tags=["Attendance"])
app.include_router(reminders.router, prefix=PREFIX, tags=["Reminders"])
app.include_router(admin_router.router, prefix=f"{PREFIX}/admin", tags=["Admin"])
app.include_router(parent_router.router, prefix=PREFIX, tags=["Parent Portal"])
