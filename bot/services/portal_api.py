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
