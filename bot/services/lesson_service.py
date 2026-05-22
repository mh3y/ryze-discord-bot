"""CRUD and query operations for lessons and class groups."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.database import get_session
from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson, LessonStatus
from bot.services.google_calendar_service import fetch_upcoming_events
from bot.utils.time_utils import now_sydney, to_utc
from bot import config

# Lazy import to avoid circular dependency — portal_api uses aiohttp which is
# always available, but we only import it when we actually need it.
def _get_portal_client():
    from bot.services.portal_api import PortalAPIClient, PortalAPIError  # noqa: PLC0415
    return PortalAPIClient, PortalAPIError

log = logging.getLogger(__name__)


async def get_active_class_groups(session: AsyncSession) -> list[ClassGroup]:
    result = await session.execute(
        select(ClassGroup)
        .where(ClassGroup.active.is_(True))
        .options(selectinload(ClassGroup.tutor))
    )
    return list(result.scalars().all())


async def get_class_group_by_voice_channel(
    session: AsyncSession, channel_id: int
) -> Optional[ClassGroup]:
    result = await session.execute(
        select(ClassGroup).where(
            and_(
                ClassGroup.discord_voice_channel_id == channel_id,
                ClassGroup.active.is_(True),
            )
        )
    )
    return result.scalar_one_or_none()


async def get_lesson_by_id(session: AsyncSession, lesson_id: int) -> Optional[Lesson]:
    result = await session.execute(
        select(Lesson)
        .where(Lesson.id == lesson_id)
        .options(selectinload(Lesson.class_group).selectinload(ClassGroup.tutor))
    )
    return result.scalar_one_or_none()


async def get_active_lesson_for_channel(
    session: AsyncSession, class_group_id: int, at: datetime
) -> Optional[Lesson]:
    """Find a lesson for a class group that is active or starting soon."""
    window_start = at - timedelta(minutes=config.LESSON_WINDOW_BEFORE_MINUTES)
    window_end = at + timedelta(minutes=config.LESSON_WINDOW_AFTER_MINUTES)

    result = await session.execute(
        select(Lesson)
        .where(
            and_(
                Lesson.class_group_id == class_group_id,
                Lesson.start_time >= window_start,
                Lesson.start_time <= window_end,
                Lesson.status.in_([LessonStatus.scheduled, LessonStatus.active]),
            )
        )
        .order_by(Lesson.start_time)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_upcoming_lessons(
    session: AsyncSession, hours_ahead: int = 48
) -> list[Lesson]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)
    result = await session.execute(
        select(Lesson)
        .where(
            and_(
                Lesson.start_time >= now,
                Lesson.start_time <= cutoff,
                Lesson.status == LessonStatus.scheduled,
            )
        )
        .options(selectinload(Lesson.class_group).selectinload(ClassGroup.tutor))
        .order_by(Lesson.start_time)
    )
    return list(result.scalars().all())


async def get_todays_lessons(session: AsyncSession) -> list[Lesson]:
    # Use Sydney day boundaries so the command reflects the local calendar day,
    # not the UTC day (which is 10–11 hours behind Sydney).
    sydney_now = now_sydney()
    day_start_utc = to_utc(sydney_now.replace(hour=0, minute=0, second=0, microsecond=0))
    day_end_utc = to_utc(sydney_now.replace(hour=23, minute=59, second=59, microsecond=999999))
    result = await session.execute(
        select(Lesson)
        .where(
            and_(
                Lesson.start_time >= day_start_utc,
                Lesson.start_time <= day_end_utc,
                Lesson.status.in_([LessonStatus.scheduled, LessonStatus.active]),
            )
        )
        .options(selectinload(Lesson.class_group).selectinload(ClassGroup.tutor))
        .order_by(Lesson.start_time)
    )
    return list(result.scalars().all())


def _fields_equal(existing_val, new_val) -> bool:
    """Compare two field values for equality, normalising timezone-aware datetimes.

    SQLite strips timezone info when reading TIMESTAMP columns, so we treat any
    naive datetime as UTC before comparing.  PostgreSQL preserves timezone info
    and will compare correctly without this normalisation.
    """
    if isinstance(existing_val, datetime) and isinstance(new_val, datetime):
        def _as_utc(dt: datetime) -> datetime:
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        return _as_utc(existing_val) == _as_utc(new_val)
    return existing_val == new_val


async def sync_calendar_for_group(session: AsyncSession, group: ClassGroup) -> dict:
    """Pull events from Google Calendar and upsert lessons. Returns counts.

    The return dict includes ``"newly_cancelled"`` — a list of Lesson objects
    that transitioned to cancelled during this sync cycle (i.e. were scheduled
    or active before).  Callers can use this to fire cancellation notifications.
    """
    if not group.google_calendar_id:
        return {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0, "newly_cancelled": []}

    events = fetch_upcoming_events(group.google_calendar_id, config.CALENDAR_SYNC_DAYS_AHEAD)
    created = updated = cancelled = skipped = 0
    newly_cancelled: list[Lesson] = []

    for event in events:
        google_event_id = event["google_event_id"]

        existing_result = await session.execute(
            select(Lesson).where(Lesson.google_event_id == google_event_id)
        )
        existing = existing_result.scalar_one_or_none()

        if event.get("cancelled"):
            if existing and existing.status != LessonStatus.cancelled:
                existing.status = LessonStatus.cancelled
                newly_cancelled.append(existing)
                cancelled += 1
            else:
                skipped += 1
            continue

        if existing:
            changed = False
            for field in ("title", "description", "start_time", "end_time", "location", "meet_link"):
                new_val = event.get(field)
                if not _fields_equal(getattr(existing, field), new_val):
                    setattr(existing, field, new_val)
                    changed = True
            if changed:
                existing.updated_at = datetime.now(timezone.utc)
                updated += 1
            else:
                skipped += 1
        else:
            lesson = Lesson(
                class_group_id=group.id,
                google_event_id=google_event_id,
                title=event["title"],
                description=event.get("description"),
                start_time=event["start_time"],
                end_time=event["end_time"],
                location=event.get("location"),
                meet_link=event.get("meet_link"),
                status=LessonStatus.scheduled,
            )
            session.add(lesson)
            created += 1

    await session.flush()
    log.info(
        "Calendar sync for %r: created=%d updated=%d cancelled=%d skipped=%d",
        group.name, created, updated, cancelled, skipped,
    )
    return {
        "created": created,
        "updated": updated,
        "cancelled": cancelled,
        "skipped": skipped,
        "newly_cancelled": newly_cancelled,
    }


async def sync_all_calendars() -> dict:
    """Sync all active class group calendars. Returns aggregate counts including error count.

    The return dict includes ``"newly_cancelled"`` — a list of
    ``(lesson, class_group)`` tuples for every lesson that transitioned to
    cancelled during this sync.  The objects are detached from the session but
    retain their data (``expire_on_commit=False``).  Callers can use this list
    to fire Discord cancellation notifications.
    """
    totals: dict = {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0, "errors": 0, "newly_cancelled": []}
    synced_groups: list[ClassGroup] = []

    async with get_session() as session:
        groups = await get_active_class_groups(session)
        for group in groups:
            try:
                counts = await sync_calendar_for_group(session, group)
                for k in ("created", "updated", "cancelled", "skipped"):
                    totals[k] += counts[k]
                for lesson in counts.get("newly_cancelled", []):
                    totals["newly_cancelled"].append((lesson, group))
                if group.google_calendar_id:
                    synced_groups.append(group)
            except Exception:
                log.exception("Calendar sync failed for group %r (id=%d).", group.name, group.id)
                totals["errors"] += 1

    # ── Push synced lessons to the portal API ────────────────────────────────
    if synced_groups and config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
        await _push_lessons_to_portal(synced_groups)

    return totals


async def _push_lessons_to_portal(synced_groups: list[ClassGroup]) -> None:
    """
    After a local calendar sync, push the upcoming lessons for the synced class
    groups to the portal Node.js API so Supabase stays current.

    Matching strategy:
      1. Fetch the portal's class list (which includes google_calendar_id).
      2. Build a map: google_calendar_id → portal class_id.
      3. For each local group, look up the portal class_id by calendar ID.
      4. Collect all upcoming lessons from the local DB for those groups.
      5. POST /api/bot/sync-lessons with the full payload.
    """
    PortalAPIClient, PortalAPIError = _get_portal_client()

    try:
        async with PortalAPIClient() as client:
            # Step 1 & 2 — fetch portal classes and build calendar_id → class_id map
            portal_classes = await client.get_classes()
            cal_to_portal_id: dict[str, int] = {
                cls["google_calendar_id"]: cls["id"]
                for cls in portal_classes
                if cls.get("google_calendar_id")
            }

            if not cal_to_portal_id:
                log.warning(
                    "[portal] No portal classes have google_calendar_id set. "
                    "Set calendar IDs via PATCH /api/bot/classes/:id/set-calendar."
                )
                return

            # Step 3 — find portal class_ids for the groups we just synced
            group_to_portal_id: dict[int, int] = {}  # local group.id → portal class_id
            for group in synced_groups:
                portal_id = cal_to_portal_id.get(group.google_calendar_id or "")
                if portal_id:
                    group_to_portal_id[group.id] = portal_id
                else:
                    log.debug(
                        "[portal] No portal class matched calendar_id=%s for group %r — skipping.",
                        group.google_calendar_id, group.name,
                    )

            if not group_to_portal_id:
                log.warning("[portal] No local groups matched any portal class by calendar ID.")
                return

            # Step 4 — collect upcoming lessons from the local bot DB
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(days=config.CALENDAR_SYNC_DAYS_AHEAD)

            async with get_session() as session:
                result = await session.execute(
                    select(Lesson).where(
                        and_(
                            Lesson.class_group_id.in_(list(group_to_portal_id.keys())),
                            Lesson.start_time >= now - timedelta(days=1),  # include today's lessons
                            Lesson.start_time <= cutoff,
                            Lesson.google_event_id.isnot(None),
                        )
                    )
                )
                lessons: list[Lesson] = list(result.scalars().all())

            if not lessons:
                log.info("[portal] No lessons to push to portal API.")
                return

            # Step 5 — build payload and push
            payload: list[dict] = []
            for lesson in lessons:
                portal_class_id = group_to_portal_id.get(lesson.class_group_id)
                if not portal_class_id:
                    continue

                # Calculate duration from start/end times
                if lesson.end_time and lesson.start_time:
                    delta_min = int((lesson.end_time - lesson.start_time).total_seconds() / 60)
                else:
                    delta_min = 60  # default

                # Map bot's LessonStatus to portal's LessonStatus
                status_map = {
                    LessonStatus.scheduled: "scheduled",
                    LessonStatus.active:    "live",
                    LessonStatus.completed: "completed",
                    LessonStatus.cancelled: "cancelled",
                }
                portal_status = status_map.get(lesson.status, "scheduled")

                payload.append({
                    "google_event_id": lesson.google_event_id,
                    "class_id":        portal_class_id,
                    "title":           lesson.title or "Lesson",
                    "description":     lesson.description,
                    "scheduled_at":    lesson.start_time.isoformat(),
                    "duration_min":    delta_min,
                    "meet_link":       lesson.meet_link,
                    "status":          portal_status,
                })

            result = await client.sync_lessons(payload)
            log.info(
                "[portal] Lesson sync pushed %d lessons: created=%d updated=%d.",
                len(payload),
                result.get("created", 0),
                result.get("updated", 0),
            )

    except PortalAPIError as exc:
        log.error("[portal] Lesson push failed: %s", exc)
    except Exception:
        log.exception("[portal] Unexpected error pushing lessons to portal API.")
