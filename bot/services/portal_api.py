"""
portal_api.py — async HTTP client for the Ryze portal Node.js API.

All calls are authenticated with the BOT_API_SECRET (sent as
  Authorization: Bearer <key>
so they go through the requireBot middleware).

Usage:
    from bot.services.portal_api import PortalAPIClient

    async with PortalAPIClient() as client:
        await client.sync_members(members)
        await client.sync_lessons(lessons)
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from bot import config

log = logging.getLogger(__name__)


class PortalAPIError(Exception):
    """Raised when the portal API returns a non-2xx response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Portal API {status}: {detail}")
        self.status = status
        self.detail = detail


class PortalAPIClient:
    """Async context-manager wrapper around the portal Node.js REST API."""

    def __init__(self) -> None:
        self._base = (config.PORTAL_API_URL or "").rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "PortalAPIClient":
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {config.DASHBOARD_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # ── internal helpers ──────────────────────────────────────────────────── #

    async def _post(self, path: str, payload: dict) -> dict:
        assert self._session, "Use inside `async with PortalAPIClient()`"
        url = f"{self._base}{path}"
        async with self._session.post(url, json=payload) as resp:
            body = await resp.json(content_type=None)
            if not resp.ok:
                raise PortalAPIError(resp.status, body.get("detail", str(body)))
            return body

    async def _get(self, path: str) -> Any:
        assert self._session, "Use inside `async with PortalAPIClient()`"
        url = f"{self._base}{path}"
        async with self._session.get(url) as resp:
            body = await resp.json(content_type=None)
            if not resp.ok:
                raise PortalAPIError(resp.status, body.get("detail", str(body)))
            return body

    async def _patch(self, path: str, payload: dict) -> dict:
        assert self._session, "Use inside `async with PortalAPIClient()`"
        url = f"{self._base}{path}"
        async with self._session.patch(url, json=payload) as resp:
            body = await resp.json(content_type=None)
            if not resp.ok:
                raise PortalAPIError(resp.status, body.get("detail", str(body)))
            return body

    # ── public API ───────────────────────────────────────────────────────── #

    async def sync_members(
        self,
        members: list[dict],
    ) -> dict:
        """
        POST /api/bot/sync-members

        members: list of dicts with keys:
            discord_user_id: str
            full_name: str
            avatar_url: str | None
            role: "admin" | "tutor" | "student"
        """
        if not members:
            return {"synced": 0, "created": 0, "updated": 0}

        result = await self._post("/api/bot/sync-members", {"members": members})
        log.info(
            "[portal] sync_members: synced=%d created=%d updated=%d",
            result.get("synced", 0),
            result.get("created", 0),
            result.get("updated", 0),
        )
        return result

    async def sync_lessons(
        self,
        lessons: list[dict],
    ) -> dict:
        """
        POST /api/bot/sync-lessons

        lessons: list of dicts with keys:
            google_event_id: str
            class_id: int
            title: str
            description: str | None
            scheduled_at: str  (ISO 8601)
            duration_min: int
            meet_link: str | None
            status: "scheduled" | "live" | "completed" | "cancelled"
        """
        if not lessons:
            return {"synced": 0, "created": 0, "updated": 0}

        result = await self._post("/api/bot/sync-lessons", {"lessons": lessons})
        log.info(
            "[portal] sync_lessons: synced=%d created=%d updated=%d",
            result.get("synced", 0),
            result.get("created", 0),
            result.get("updated", 0),
        )
        return result

    async def get_classes(self) -> list[dict]:
        """GET /api/bot/classes — returns all active class groups."""
        return await self._get("/api/bot/classes")

    async def set_class_calendar(self, class_id: int, google_calendar_id: str) -> dict:
        """PATCH /api/bot/classes/:id/set-calendar"""
        return await self._patch(
            f"/api/bot/classes/{class_id}/set-calendar",
            {"google_calendar_id": google_calendar_id},
        )

    async def create_class(
        self,
        name: str,
        subject: str,
        google_calendar_id: str,
        discord_channel_id: str | None = None,
        discord_role_id: str | None = None,
        year_level: str | None = None,
        description: str | None = None,
    ) -> dict:
        """
        POST /api/bot/classes — idempotent (returns existing if calendar ID already registered).
        Returns { id, created, class }.
        """
        payload: dict = {
            "name":               name,
            "subject":            subject,
            "google_calendar_id": google_calendar_id,
        }
        if discord_channel_id: payload["discord_channel_id"] = discord_channel_id
        if discord_role_id:    payload["discord_role_id"]    = discord_role_id
        if year_level:         payload["year_level"]         = year_level
        if description:        payload["description"]        = description

        result = await self._post("/api/bot/classes", payload)
        if result.get("created"):
            log.info("[portal] Created class %r (id=%d)", name, result["id"])
        else:
            log.debug("[portal] Class %r already exists (id=%d)", name, result["id"])
        return result

    async def update_class_discord(
        self,
        class_id: int,
        discord_channel_id: str | None = None,
        discord_role_id: str | None = None,
    ) -> dict:
        """PATCH /api/bot/classes/:id — update Discord channel/role IDs."""
        payload: dict = {}
        if discord_channel_id is not None: payload["discord_channel_id"] = discord_channel_id
        if discord_role_id    is not None: payload["discord_role_id"]    = discord_role_id
        return await self._patch(f"/api/bot/classes/{class_id}", payload)

    async def push_voice_sessions(
        self,
        sessions: list[dict],
    ) -> dict:
        """
        POST /api/bot/sync-voice-sessions

        Pushes voice-channel activity to the CRM portal.  Works with or without
        a lesson_id — the CRM records every session even when the voice channel
        is not yet mapped to a class.

        Each session dict should contain:
            discord_user_id:    str
            discord_username:   str | None
            discord_channel_id: str | None   (snowflake)
            discord_channel:    str | None   (human-readable name)
            joined_at:          str           (ISO 8601 UTC)
            left_at:            str | None    (ISO 8601 UTC; omit for active sessions)
            status:             str           "active" | "completed" | "unknown"
        """
        if not sessions:
            return {"created": 0, "updated": 0}

        result = await self._post("/api/bot/sync-voice-sessions", {"sessions": sessions})
        log.info(
            "[portal] push_voice_sessions: created=%d updated=%d",
            result.get("created", 0),
            result.get("updated", 0),
        )
        return result

    async def push_sync_log(
        self,
        sync_type:       str,
        status:          str,
        started_at:      str,
        completed_at:    str | None   = None,
        records_created: int          = 0,
        records_updated: int          = 0,
        records_failed:  int          = 0,
        error_message:   str | None   = None,
        source:          str          = "scheduled",
        triggered_by:    str | None   = None,
    ) -> dict:
        """
        POST /api/bot/sync-log

        Records a sync audit entry in Supabase so the admin portal can show
        last-sync times, record counts, and failure history per sync type.

        sync_type:   members | classes | lessons | attendance
        status:      running | success | partial | failed
        started_at:  ISO 8601 string
        completed_at: ISO 8601 string (or None if still running)
        source:      scheduled | admin_triggered | slash_command
        """
        payload: dict = {
            "sync_type":       sync_type,
            "status":          status,
            "started_at":      started_at,
            "records_created": records_created,
            "records_updated": records_updated,
            "records_failed":  records_failed,
            "source":          source,
            "portal_api_url":  self._base,
        }
        if completed_at:   payload["completed_at"]  = completed_at
        if error_message:  payload["error_message"] = error_message
        if triggered_by:   payload["triggered_by"]  = triggered_by

        try:
            result = await self._post("/api/bot/sync-log", payload)
            log.debug("[portal] sync-log pushed: %s %s", sync_type, status)
            return result
        except Exception as exc:
            # Never let sync-log failure break the actual sync
            log.warning("[portal] sync-log push failed (non-fatal): %s", exc)
            return {}
