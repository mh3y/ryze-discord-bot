"""Cog: Voice attendance tracking and attendance slash commands."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from bot import config
from bot.database import get_session
from bot.models.attendance import AttendanceRecord, AttendanceStatus, MarkedBy, VoiceSession
from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson, LessonStatus
from bot.models.user import User
from bot.services.attendance_service import (
    close_voice_session,
    finalise_lesson_attendance,
    manual_mark_attendance,
    open_voice_session,
)
from bot.services.lesson_service import (
    get_active_lesson_for_channel,
    get_class_group_by_voice_channel,
    get_lesson_by_id,
)
from bot.utils.time_utils import SYDNEY_TZ, format_sydney, now_utc

log = logging.getLogger(__name__)

# ── Embed helpers ──────────────────────────────────────────────────────────── #

_STATUS_EMOJI = {
    AttendanceStatus.present:    "✅",
    AttendanceStatus.late:       "🟡",
    AttendanceStatus.left_early: "🟠",
    AttendanceStatus.absent:     "❌",
    AttendanceStatus.unknown:    "❓",
}


def _attendance_colour(present: int, late: int, absent: int, total: int) -> discord.Colour:
    """Pick an embed colour based on the overall attendance rate."""
    if total == 0:
        return discord.Colour.greyple()
    attended = present + late
    rate = attended / total
    if rate >= 0.8:
        return discord.Colour.brand_green()
    if rate >= 0.5:
        return discord.Colour.orange()
    return discord.Colour.brand_red()


def _fmt_time(dt: Optional[datetime]) -> str:
    """Return a clean Sydney time string, stripping a leading zero."""
    if dt is None:
        return "?"
    return format_sydney(dt, "%I:%M %p").lstrip("0")


def _add_field_chunked(
    embed: discord.Embed,
    name: str,
    lines: list[str],
    inline: bool = False,
) -> None:
    """
    Add a field to an embed, splitting into multiple fields if the value
    exceeds Discord's 1 024-character field limit.
    """
    if not lines:
        return
    chunk: list[str] = []
    chunk_len = 0
    part = 1
    for line in lines:
        if chunk_len + len(line) + 1 > 1000 and chunk:
            label = name if part == 1 else f"{name} (cont.)"
            embed.add_field(name=label, value="\n".join(chunk), inline=inline)
            chunk = []
            chunk_len = 0
            part += 1
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        label = name if part == 1 else f"{name} (cont.)"
        embed.add_field(name=label, value="\n".join(chunk), inline=inline)


# ── Embed: /attendance (summary view) ─────────────────────────────────────── #

def _build_summary_embed(
    lesson: Lesson,
    rows: list[tuple[AttendanceRecord, User]],
) -> discord.Embed:
    """Build a rich embed for the /attendance summary command."""
    cg = lesson.class_group
    start_str = _fmt_time(lesson.start_time)
    end_str   = _fmt_time(lesson.end_time)
    date_str  = format_sydney(lesson.start_time, "%A, %d %B %Y")
    tutor     = cg.tutor.full_name if (cg and cg.tutor) else "No tutor assigned"

    # Bucket students by status
    buckets: dict[AttendanceStatus, list[str]] = {s: [] for s in AttendanceStatus}
    for record, user in rows:
        mins = record.total_minutes or 0
        entry = f"**{user.full_name}** — {mins} min"
        if record.marked_by == MarkedBy.tutor:
            entry += " *(manual)*"
        buckets[record.status].append(entry)

    n_present    = len(buckets[AttendanceStatus.present])
    n_late       = len(buckets[AttendanceStatus.late])
    n_left_early = len(buckets[AttendanceStatus.left_early])
    n_absent     = len(buckets[AttendanceStatus.absent])
    total        = len(rows)

    colour = _attendance_colour(n_present, n_late, n_absent, total)

    embed = discord.Embed(
        title=f"📋  {cg.name if cg else lesson.title}",
        description=(
            f"**{date_str}**\n"
            f"{start_str} – {end_str}  ·  Tutor: {tutor}\n"
            f"*{lesson.title}*"
        ),
        colour=colour,
    )

    order = [
        (AttendanceStatus.present,    "✅  Present"),
        (AttendanceStatus.late,       "🟡  Late"),
        (AttendanceStatus.left_early, "🟠  Left Early"),
        (AttendanceStatus.absent,     "❌  Absent"),
        (AttendanceStatus.unknown,    "❓  Unknown"),
    ]
    for status, label in order:
        entries = buckets[status]
        if entries:
            _add_field_chunked(embed, f"{label}  ({len(entries)})", entries)

    embed.set_footer(
        text=(
            f"Lesson #{lesson.id}  ·  "
            f"{n_present} present  ·  {n_late} late  ·  "
            f"{n_left_early} left early  ·  {n_absent} absent  ·  "
            f"{total} total"
        )
    )
    return embed


# ── Embed: /voice_attendance (detailed voice-session view) ─────────────────── #

def _build_voice_embed(
    lesson: Lesson,
    sessions_by_uid: dict[int, list[VoiceSession]],
    ar_by_uid: dict[int, AttendanceRecord],
    names: dict[int, str],
    date_label: str,
) -> discord.Embed:
    """Build a rich embed showing per-student voice join/leave detail."""
    cg        = lesson.class_group
    start_str = _fmt_time(lesson.start_time)
    end_str   = _fmt_time(lesson.end_time)
    tutor     = cg.tutor.full_name if (cg and cg.tutor) else "No tutor assigned"

    all_uids = sorted(
        set(list(sessions_by_uid.keys()) + list(ar_by_uid.keys())),
        key=lambda uid: names.get(uid, ""),
    )

    # Bucket into status groups
    buckets: dict[AttendanceStatus, list[str]] = {s: [] for s in AttendanceStatus}

    for uid in all_uids:
        ar     = ar_by_uid.get(uid)
        status = ar.status if ar else AttendanceStatus.unknown
        name   = names[uid]
        user_sessions = sessions_by_uid.get(uid, [])

        if not user_sessions:
            entry = f"**{name}** — *never joined*"
        else:
            segments: list[str] = []
            total_mins = 0
            for vs in user_sessions:
                j   = _fmt_time(vs.joined_at)
                dur = vs.duration_minutes or 0
                if vs.left_at:
                    l = _fmt_time(vs.left_at)
                    segments.append(f"`{j} → {l}` ({dur} min)")
                    total_mins += dur
                else:
                    segments.append(f"`{j} →` *still in channel*")

            if len(segments) == 1:
                entry = f"**{name}**  {segments[0]}"
            else:
                seg_str = "  +  ".join(segments)
                entry = f"**{name}**  {seg_str}  · **{total_mins} min total**"

        buckets[status].append(entry)

    n_present    = len(buckets[AttendanceStatus.present])
    n_late       = len(buckets[AttendanceStatus.late])
    n_left_early = len(buckets[AttendanceStatus.left_early])
    n_absent     = len(buckets[AttendanceStatus.absent])
    total        = len(all_uids)

    colour = _attendance_colour(n_present, n_late, n_absent, total)

    embed = discord.Embed(
        title=f"🎙️  {cg.name if cg else lesson.title}",
        description=(
            f"**{date_label}**  ·  {start_str} – {end_str}\n"
            f"Tutor: {tutor}"
        ),
        colour=colour,
    )

    order = [
        (AttendanceStatus.present,    "✅  Present"),
        (AttendanceStatus.late,       "🟡  Late"),
        (AttendanceStatus.left_early, "🟠  Left Early"),
        (AttendanceStatus.absent,     "❌  Absent"),
        (AttendanceStatus.unknown,    "❓  Unknown / In Progress"),
    ]
    for status, label in order:
        entries = buckets[status]
        if entries:
            _add_field_chunked(embed, f"{label}  ({len(entries)})", entries)

    if not any(buckets.values()):
        embed.add_field(name="No data", value="No voice sessions or attendance records found.", inline=False)

    embed.set_footer(
        text=(
            f"Lesson #{lesson.id}  ·  "
            f"{n_present} present  ·  {n_late} late  ·  "
            f"{n_left_early} left early  ·  {n_absent} absent"
        )
    )
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────── #

class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._finalise_loop.start()

    def cog_unload(self) -> None:
        self._finalise_loop.cancel()

    # ------------------------------------------------------------------ #
    # Voice state tracking
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if member.guild.id != config.DISCORD_GUILD_ID:
            return

        now = now_utc()

        if after.channel and (not before.channel or before.channel.id != after.channel.id):
            await self._handle_join(member, after.channel, now)

        if before.channel and (not after.channel or before.channel.id != after.channel.id):
            await self._handle_leave(member, before.channel, now)

    async def _handle_join(
        self, member: discord.Member, channel: discord.VoiceChannel, now
    ) -> None:
        async with get_session() as session:
            class_group = await get_class_group_by_voice_channel(session, channel.id)
            lesson = None
            if class_group:
                lesson = await get_active_lesson_for_channel(session, class_group.id, now)
                if lesson and lesson.status == LessonStatus.scheduled:
                    lesson.status = LessonStatus.active
                    lesson.updated_at = now
            await open_voice_session(session, member.id, channel.id, lesson, joined_at=now)

    async def _handle_leave(
        self, member: discord.Member, channel: discord.VoiceChannel, now
    ) -> None:
        async with get_session() as session:
            await close_voice_session(session, member.id, channel.id, left_at=now)

    # ------------------------------------------------------------------ #
    # Finalise attendance at lesson end
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=1)
    async def _finalise_loop(self) -> None:
        await self.bot.wait_until_ready()
        now = now_utc()
        async with get_session() as session:
            result = await session.execute(
                select(Lesson).where(
                    and_(
                        Lesson.end_time <= now,
                        Lesson.status == LessonStatus.active,
                    )
                )
            )
            ended_lessons = list(result.scalars().all())
            for lesson in ended_lessons:
                try:
                    await finalise_lesson_attendance(session, lesson)
                except Exception:
                    log.exception("Failed to finalise attendance for lesson %d.", lesson.id)

    @_finalise_loop.before_loop
    async def _before_finalise(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #

    @app_commands.command(name="attendance", description="Show attendance summary for a lesson.")
    @app_commands.guild_only()
    @app_commands.describe(lesson_id="The lesson ID")
    @app_commands.default_permissions(administrator=True)
    async def attendance(self, interaction: Interaction, lesson_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            lesson = await get_lesson_by_id(session, lesson_id)
            if not lesson:
                await interaction.followup.send(f"Lesson `{lesson_id}` not found.", ephemeral=True)
                return

            result = await session.execute(
                select(AttendanceRecord, User)
                .join(User, User.id == AttendanceRecord.user_id)
                .where(AttendanceRecord.lesson_id == lesson_id)
                .order_by(User.full_name)
            )
            rows = result.all()

        if not rows:
            await interaction.followup.send(
                f"No attendance records yet for lesson `{lesson_id}`.", ephemeral=True
            )
            return

        embed = _build_summary_embed(lesson, rows)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="mark_attendance", description="Manually set a student's attendance status.")
    @app_commands.guild_only()
    @app_commands.describe(
        lesson_id="Lesson ID",
        student="The student's Discord account",
        status="Attendance status",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="Present",    value="present"),
        app_commands.Choice(name="Late",       value="late"),
        app_commands.Choice(name="Left Early", value="left_early"),
        app_commands.Choice(name="Absent",     value="absent"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def mark_attendance(
        self,
        interaction: Interaction,
        lesson_id: int,
        student: discord.Member,
        status: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            record = await manual_mark_attendance(
                session,
                lesson_id,
                student.id,
                AttendanceStatus(status),
                MarkedBy.tutor,
            )
        if record:
            icon = _STATUS_EMOJI.get(AttendanceStatus(status), "❓")
            await interaction.followup.send(
                f"{icon} Marked **{student.display_name}** as **{status}** for lesson `{lesson_id}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"No linked student record found for {student.display_name}.",
                ephemeral=True,
            )

    @app_commands.command(
        name="voice_attendance",
        description="Detailed voice-channel attendance for a date with join/leave times per student.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        date="Date in YYYY-MM-DD format (Sydney time), e.g. 2026-05-15",
        lesson_id="Optional: restrict to one lesson ID",
    )
    @app_commands.default_permissions(administrator=True)
    async def voice_attendance(
        self,
        interaction: Interaction,
        date: str,
        lesson_id: int | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            naive = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            await interaction.followup.send(
                "Invalid date — use `YYYY-MM-DD`, e.g. `2026-05-15`.", ephemeral=True
            )
            return

        day_start = SYDNEY_TZ.localize(naive.replace(hour=0,  minute=0,  second=0,  microsecond=0))
        day_end   = SYDNEY_TZ.localize(naive.replace(hour=23, minute=59, second=59, microsecond=999999))
        start_utc = day_start.astimezone(timezone.utc)
        end_utc   = day_end.astimezone(timezone.utc)
        date_label = day_start.strftime("%A, %d %B %Y").replace(" 0", " ")

        eager = selectinload(Lesson.class_group).selectinload(ClassGroup.tutor)

        async with get_session() as session:

            # Fetch lesson(s)
            if lesson_id is not None:
                res = await session.execute(
                    select(Lesson).where(Lesson.id == lesson_id).options(eager)
                )
            else:
                res = await session.execute(
                    select(Lesson)
                    .where(and_(Lesson.start_time >= start_utc, Lesson.start_time <= end_utc))
                    .options(eager)
                    .order_by(Lesson.start_time)
                )
            lessons = list(res.scalars().all())

            if not lessons:
                await interaction.followup.send(
                    f"No lessons found for **{date}** (Sydney time).\n"
                    f"-# Lessons must be synced from Google Calendar before attendance can be reported.",
                    ephemeral=True,
                )
                return

            embeds: list[discord.Embed] = []

            for lesson in lessons:
                # Fetch voice sessions grouped by user
                vs_res = await session.execute(
                    select(VoiceSession, User)
                    .join(User, User.id == VoiceSession.user_id)
                    .where(VoiceSession.lesson_id == lesson.id)
                    .order_by(User.full_name, VoiceSession.joined_at)
                )
                vs_rows = vs_res.all()

                # Fetch attendance records for final status
                ar_res = await session.execute(
                    select(AttendanceRecord, User)
                    .join(User, User.id == AttendanceRecord.user_id)
                    .where(AttendanceRecord.lesson_id == lesson.id)
                    .order_by(User.full_name)
                )
                ar_rows = ar_res.all()

                ar_by_uid: dict[int, AttendanceRecord] = {ar.user_id: ar for ar, _ in ar_rows}
                sessions_by_uid: dict[int, list[VoiceSession]] = defaultdict(list)
                names: dict[int, str] = {}

                for vs, user in vs_rows:
                    sessions_by_uid[user.id].append(vs)
                    names[user.id] = user.full_name
                for ar, user in ar_rows:
                    names[user.id] = user.full_name

                embeds.append(
                    _build_voice_embed(lesson, sessions_by_uid, ar_by_uid, names, date_label)
                )

        # Discord allows up to 10 embeds per message
        for i in range(0, len(embeds), 10):
            batch = embeds[i : i + 10]
            await interaction.followup.send(embeds=batch, ephemeral=True)

    @app_commands.command(name="student_attendance", description="Show all attendance records for a student.")
    @app_commands.guild_only()
    @app_commands.describe(student="The student's Discord account")
    @app_commands.default_permissions(administrator=True)
    async def student_attendance(self, interaction: Interaction, student: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            user_result = await session.execute(
                select(User).where(User.discord_user_id == student.id)
            )
            db_user = user_result.scalar_one_or_none()
            if not db_user:
                await interaction.followup.send("No linked student record found.", ephemeral=True)
                return

            result = await session.execute(
                select(AttendanceRecord, Lesson)
                .join(Lesson, Lesson.id == AttendanceRecord.lesson_id)
                .where(AttendanceRecord.user_id == db_user.id)
                .order_by(Lesson.start_time.desc())
                .limit(20)
            )
            rows = result.all()

        if not rows:
            await interaction.followup.send("No attendance records found.", ephemeral=True)
            return

        # Count statuses for colour
        counts = defaultdict(int)
        for record, _ in rows:
            counts[record.status] += 1

        colour = _attendance_colour(
            counts[AttendanceStatus.present],
            counts[AttendanceStatus.late],
            counts[AttendanceStatus.absent],
            len(rows),
        )

        embed = discord.Embed(
            title=f"📊  {db_user.full_name}",
            description=f"Last {len(rows)} lesson(s) for {student.mention}",
            colour=colour,
        )

        lines: list[str] = []
        for record, lesson in rows:
            icon = _STATUS_EMOJI.get(record.status, "❓")
            date = format_sydney(lesson.start_time, "%d %b %Y")
            mins = record.total_minutes or 0
            lines.append(f"{icon} **{date}** — {lesson.title}  ·  {mins} min")

        _add_field_chunked(embed, "History", lines)

        present    = counts[AttendanceStatus.present]
        late       = counts[AttendanceStatus.late]
        left_early = counts[AttendanceStatus.left_early]
        absent     = counts[AttendanceStatus.absent]
        embed.set_footer(
            text=f"{present} present  ·  {late} late  ·  {left_early} left early  ·  {absent} absent"
        )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AttendanceCog(bot))
