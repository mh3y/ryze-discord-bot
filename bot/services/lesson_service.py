"""CRUD and query operations for lessons and class groups."""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select, and_, or_
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

# Serialises the calendar-sync entrypoints (scheduled loop, admin /sync_calendar,
# and the bot-jobs poller) so their hydration steps can't interleave and insert
# duplicate ClassGroup rows for the same google_calendar_id. Single-process
# (one Render worker) — a DB unique constraint would be the multi-instance guard.
_sync_lock = asyncio.Lock()


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
                # Don't remind for lessons of a deactivated/archived class group
                # (hydration sets active=False when a class drops out of the portal).
                Lesson.class_group.has(ClassGroup.active.is_(True)),
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

    # Offload the blocking Google HTTP call to a thread so it can't stall the
    # asyncio event loop (voice events, heartbeats, job polling) while N calendars
    # sync one after another. [DEF-10] A Google error now raises (see M11) and is
    # caught per-group by sync_all_calendars, counting as that group's sync error.
    events = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: fetch_upcoming_events(
            group.google_calendar_id,
            config.CALENDAR_SYNC_DAYS_AHEAD,
            config.CALENDAR_SYNC_DAYS_BACK,
        ),
    )
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


def _as_int_or_none(val) -> Optional[int]:
    """Discord snowflakes are stored as strings in the portal, but the local
    ClassGroup columns are BigInteger. Convert defensively; treat anything
    unparseable (or empty) as absent rather than raising."""
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def hydrate_local_class_groups(session: AsyncSession, portal_classes: list[dict]) -> dict:
    """Upsert the given portal classes into the local ClassGroup table.

    The bot's reminder/homework/cancellation subsystem reads a LOCAL relational
    graph (ClassGroup → ClassGroupMember → Lesson). ``class_discovery`` provisions
    classes into the PORTAL only, so in production the local ClassGroup table is
    empty and that whole subsystem is inert (DEF-2/DEF-3). Mirroring each active
    portal class into a local ClassGroup lets the existing local-first sync and
    the existing Discord-role enrollment (members cog) populate the rest.

    Matched by ``google_calendar_id`` (the natural key both sides share). Discord
    channel/role IDs are only ever SET from the portal, never nulled out — the
    portal is the source of truth but a transiently-missing ID must not clobber a
    working local one (that would silently disable enrollment/notifications). A
    class that has dropped out of the portal (archived — ``get_classes`` returns
    active only) is deactivated locally so it stops firing reminders, but ONLY
    when the portal returned a non-empty set, so a transient empty/partial
    response can never mass-deactivate. Tutor is intentionally NOT set: the portal
    exposes only a CRM ``tutor_id``, not the tutor's Discord ID, so hydrated groups
    have ``tutor_user_id=None`` and their tutor is reached only if they hold the
    class Discord role (documented limitation).

    The existing-row lookup tolerates pre-existing duplicates (``.first()`` rather
    than ``scalar_one_or_none``) so a stray duplicate can't wedge every future
    hydration with ``MultipleResultsFound``. Idempotent. Returns
    ``{"created", "updated", "skipped", "deactivated"}``.
    """
    created = updated = skipped = deactivated = 0
    seen_cal_ids: set[str] = set()

    for cls in portal_classes:
        cal_id = cls.get("google_calendar_id")
        if not cal_id:
            # Without a calendar we can't sync lessons for this class anyway.
            skipped += 1
            continue
        seen_cal_ids.add(cal_id)

        text_channel_id = _as_int_or_none(cls.get("discord_channel_id"))
        role_id = _as_int_or_none(cls.get("discord_role_id"))

        existing_result = await session.execute(
            select(ClassGroup)
            .where(ClassGroup.google_calendar_id == cal_id)
            .order_by(ClassGroup.id)
            .limit(1)
        )
        group = existing_result.scalars().first()

        if group is None:
            group = ClassGroup(
                name=cls.get("name") or "Class",
                subject=cls.get("subject"),
                google_calendar_id=cal_id,
                discord_text_channel_id=text_channel_id,
                discord_role_id=role_id,
                active=True,
            )
            session.add(group)
            created += 1
            continue

        changed = False
        new_name = cls.get("name")
        if new_name and group.name != new_name:
            group.name = new_name
            changed = True
        if group.subject != cls.get("subject"):
            group.subject = cls.get("subject")
            changed = True
        # Only set Discord IDs when the portal actually has them — never null out.
        if text_channel_id is not None and group.discord_text_channel_id != text_channel_id:
            group.discord_text_channel_id = text_channel_id
            changed = True
        if role_id is not None and group.discord_role_id != role_id:
            group.discord_role_id = role_id
            changed = True
        if not group.active:
            group.active = True
            changed = True
        if changed:
            updated += 1

    # Deactivate local groups that have dropped out of the portal (archived).
    # Guarded: only when we actually saw calendars this cycle, so a transient
    # empty/failed portal response never mass-deactivates every class.
    if seen_cal_ids:
        stale_result = await session.execute(
            select(ClassGroup).where(
                and_(
                    ClassGroup.active.is_(True),
                    ClassGroup.google_calendar_id.isnot(None),
                    ClassGroup.google_calendar_id.notin_(list(seen_cal_ids)),
                )
            )
        )
        for stale in stale_result.scalars().all():
            stale.active = False
            deactivated += 1

    await session.flush()
    log.info(
        "[hydrate] Local class groups from portal: created=%d updated=%d skipped=%d deactivated=%d",
        created, updated, skipped, deactivated,
    )
    return {"created": created, "updated": updated, "skipped": skipped, "deactivated": deactivated}


async def hydrate_local_class_groups_from_portal(session: AsyncSession) -> dict:
    """Fetch the active portal classes and mirror them into local ClassGroups.

    Thin wrapper over :func:`hydrate_local_class_groups` that does the network
    fetch. Fails soft — a portal fetch error logs and returns zero counts rather
    than aborting the whole sync cycle.
    """
    if not config.DASHBOARD_API_KEY or not config.PORTAL_API_URL:
        log.warning("[hydrate] DASHBOARD_API_KEY or PORTAL_API_URL not set — skipping class-group hydration.")
        return {"created": 0, "updated": 0, "skipped": 0, "deactivated": 0}

    PortalAPIClient, _ = _get_portal_client()
    try:
        async with PortalAPIClient() as client:
            portal_classes = await client.get_classes()
    except Exception as exc:
        log.error("[hydrate] Failed to fetch portal classes for hydration: %s", exc)
        return {"created": 0, "updated": 0, "skipped": 0, "deactivated": 0}

    return await hydrate_local_class_groups(session, portal_classes)


async def sync_all_calendars_hydrated() -> dict:
    """Production calendar-sync entrypoint.

    Mirrors the portal's classes into the local ClassGroup table, then runs the
    local-first :func:`sync_all_calendars` — which upserts local Lessons, detects
    newly-cancelled lessons, and pushes everything back to the portal. Without the
    hydration step the local ClassGroup table is empty in production, so the
    local-first sync no-ops and reminders/threads/homework/cancellations never
    fire (DEF-2/DEF-3). Hydration makes that existing, tested path work.

    Serialised by ``_sync_lock`` so overlapping entrypoints (scheduled loop, admin
    command, bot-jobs poller) can't race and create duplicate ClassGroups. The
    returned dict carries ``hydrated_groups_created`` so the caller can refresh
    Discord-role enrollment when new classes first appear (otherwise reminders for
    a freshly-mirrored class have no recipients until the members cog's 6-hourly
    loop runs).
    """
    async with _sync_lock:
        hydrate_counts = {"created": 0, "updated": 0, "skipped": 0, "deactivated": 0}
        if config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
            try:
                async with get_session() as session:
                    hydrate_counts = await hydrate_local_class_groups_from_portal(session)
            except Exception:
                # Hydration is best-effort; never let it abort the sync that follows.
                log.exception("[hydrate] Class-group hydration failed — continuing with existing local groups.")
        result = await sync_all_calendars()
    result["hydrated_groups_created"] = hydrate_counts.get("created", 0)
    return result


async def sync_all_calendars() -> dict:
    """Sync all active class group calendars. Returns aggregate counts including error count.

    The return dict includes ``"newly_cancelled"`` — a list of
    ``(lesson, class_group)`` tuples for every lesson that transitioned to
    cancelled during this sync.  The objects are detached from the session but
    retain their data (``expire_on_commit=False``).  Callers can use this list
    to fire Discord cancellation notifications.
    """
    totals: dict = {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0, "errors": 0, "groups_total": 0, "newly_cancelled": []}
    synced_groups: list[ClassGroup] = []

    async with get_session() as session:
        groups = await get_active_class_groups(session)
        totals["groups_total"] = len(groups)
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
                            # Match the fetch lookback so lessons missed during an
                            # outage are backfilled to the portal, not just today's.
                            Lesson.start_time >= now - timedelta(days=config.CALENDAR_SYNC_DAYS_BACK),
                            Lesson.start_time <= cutoff,
                            Lesson.google_event_id.isnot(None),
                            # Don't re-push a PAST lesson that's still 'scheduled':
                            # it would revert a CRM-side completed/cancelled back to
                            # scheduled every cycle. Future lessons and terminal
                            # (completed/cancelled) past lessons still propagate.
                            or_(
                                Lesson.start_time >= now,
                                Lesson.status != LessonStatus.scheduled,
                            ),
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
