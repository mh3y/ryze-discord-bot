"""Cog: Admin slash commands — user/class management, class overview, and /help."""
import logging
from collections import defaultdict

import discord
from discord import app_commands, Interaction
from discord.ext import commands
from sqlalchemy import select, and_

from bot import config
from bot.database import get_session
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.user import User, UserRole
from bot.utils.time_utils import format_sydney, now_utc

log = logging.getLogger(__name__)


# ── Delete confirmation UI ─────────────────────────────────────────────────── #

class _ConfirmDeleteView(discord.ui.View):
    """Two-button confirmation for /delete_user. Times out after 30 seconds."""

    def __init__(self, user_name: str) -> None:
        super().__init__(timeout=30)
        self.confirmed: bool = False
        self._user_name = user_name

    @discord.ui.button(label="Yes, delete permanently", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="↩️  Cancelled",
                description=f"**{self._user_name}**'s record was **not** deleted.",
                colour=discord.Colour.greyple(),
            ),
            view=self,
        )
        self.stop()

# ---------------------------------------------------------------------------
# Role configuration — mirrors the actual Discord server role names exactly.
# ---------------------------------------------------------------------------

# Base roles: map Ryze DB role → Discord role name.
_BASE_ROLE_MAP: dict[str, str] = {
    "student": "Student",
    "tutor":   "Tutor",
    "admin":   "Admin",
    "parent":  "Parent",
}

# Class/year-level roles available for assignment as a second role.
_CLASS_ROLES: list[str] = [
    "Year 7",
    "Year 8",
    "Year 9",
    "Year 10",
    "Year 11 Advanced",
    "Year 11 Extension 1",
    "Year 12 Advanced",
    "Year 12 Extension 1",
    "Year 12 Extension 2",
    "Maths",
]


def _find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    """Return the guild role whose name matches (case-insensitive), or None."""
    target = name.lower()
    return next((r for r in guild.roles if r.name.lower() == target), None)


async def _assign_role(
    guild: discord.Guild,
    member: discord.Member,
    role_name: str,
    reason: str = "Assigned via /link_user",
) -> str:
    """
    Add a Discord role by name. Returns a short status string.
    Does nothing if the member already has it.
    """
    role = _find_role(guild, role_name)
    if role is None:
        return (
            f"ℹ️ No Discord role named **{role_name}** found — "
            f"create it in Server Settings if you want it auto-assigned."
        )
    if role in member.roles:
        return f"Already has **{role.name}**."
    try:
        await member.add_roles(role, reason=reason)
        return f"✅ Assigned **{role.name}**."
    except discord.Forbidden:
        return (
            f"⚠️ Could not assign **{role.name}** — check that the bot role "
            f"sits above **{role.name}** in Server Settings → Roles."
        )


async def _remove_role(
    guild: discord.Guild,
    member: discord.Member,
    role_name: str,
    reason: str = "Removed via /link_user",
) -> str | None:
    """Remove a Discord role by name. Returns a status string, or None if not held."""
    role = _find_role(guild, role_name)
    if role is None or role not in member.roles:
        return None
    try:
        await member.remove_roles(role, reason=reason)
        return f"Removed **{role.name}**."
    except discord.Forbidden:
        return f"⚠️ Could not remove **{role.name}** (missing permissions)."


async def _set_nickname(member: discord.Member, full_name: str) -> str:
    """Set the member's server nickname. Returns a status string."""
    try:
        await member.edit(nick=full_name, reason="Nickname set via /link_user")
        return f"✅ Nickname set to **{full_name}**."
    except discord.Forbidden:
        return (
            "⚠️ Could not set nickname — ensure the bot role is above this "
            "user's highest role in Server Settings → Roles."
        )
    except discord.HTTPException as exc:
        return f"⚠️ Nickname update failed: {exc}"


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # /help
    # ------------------------------------------------------------------ #

    @app_commands.command(name="help", description="List all Ryze Education bot commands.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def help_command(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="Ryze Education Bot — Command Reference",
            description=(
                "All commands are **admin-only** and reply privately (only you see the response).\n"
                "Parameters in `<angle brackets>` are required. Parameters in `[square brackets]` are optional."
            ),
            colour=discord.Colour.blue(),
        )
        embed.set_footer(text="Ryze Education Bot • All times are Australia/Sydney")

        embed.add_field(
            name="👥 Member Onboarding  *(fully automated)*",
            value=(
                "**The bot handles member capture and class enrolment automatically:**\n"
                "• On startup and every 6 h — scans all server members, creates records, auto-enrols by Discord role\n"
                "• On member join — captures Discord ID instantly\n"
                "• On role assignment — if the role is linked to a class group, member is auto-enrolled\n\n"
                "`/link_class_role <discord_role> <class_group_id>`\n"
                "Map a Discord role to a class group. Once linked, assigning that role to anyone "
                "automatically enrols them in the class — **no further admin action needed**.\n\n"
                "`/pending_members`\n"
                "List captured members not yet assigned to any class.\n\n"
                "`/enrol <user> <class_group_id> [full_name] [role] [class_role]`\n"
                "Manual override — link + assign in one step (for cases where role-based auto-enrol isn't set up).\n\n"
                "`/unenrol <user> <class_group_id>`\n"
                "Remove a member from a class.\n\n"
                "`/scan_members`\n"
                "Manually trigger a sync (normally automatic — use if you need immediate results)."
            ),
            inline=False,
        )

        embed.add_field(
            name="👥 Advanced User Management",
            value=(
                "`/link_user <user> <full_name> <role> [class_role]`\n"
                "Link a Discord member and set their base role + nickname. "
                "Use `/enrol` instead for the combined link + class-assign flow.\n\n"
                "`/edit_user <user> [full_name] [role] [class_role] [remove_class_role]`\n"
                "Fix mistakes from `/link_user`. Only the fields you fill in change.\n\n"
                "`/delete_user <user> [remove_discord_roles]`\n"
                "Permanently remove a user record and all their class enrolments.\n\n"
                "`/assign_student_to_class <student> <class_group_id>`\n"
                "Enrol a student in a class group (requires `/link_user` first).\n\n"
                "`/assign_tutor_to_class <tutor> <class_group_id>`\n"
                "Assign a tutor to a class group (requires `/link_user` first)."
            ),
            inline=False,
        )

        embed.add_field(
            name="📚 Classes & Roster",
            value=(
                "`/today_classes`\n"
                "List all classes scheduled for today (Sydney time).\n\n"
                "`/upcoming_classes`\n"
                "List upcoming classes in the next 7 days.\n\n"
                "`/class_roster <class_group_id>`\n"
                "Show all enrolled students and tutors for a class group."
            ),
            inline=False,
        )

        embed.add_field(
            name="📅 Google Calendar Sync",
            value=(
                "`/sync_calendar`\n"
                "Manually trigger a Google Calendar sync for all class groups. "
                "The bot also syncs automatically every 10 minutes."
            ),
            inline=False,
        )

        embed.add_field(
            name="✅ Attendance",
            value=(
                "`/attendance <lesson_id>`\n"
                "Summary for a lesson — status per student and total minutes.\n\n"
                "`/voice_attendance <date> [lesson_id]`\n"
                "Detailed voice-channel log for a day. Shows each student's exact **join time → leave time** "
                "and duration for every session, plus their final status.\n"
                "`date` format: `YYYY-MM-DD` (Sydney time) — e.g. `2026-05-15`\n\n"
                "`/mark_attendance <lesson_id> <student> <status>`\n"
                "Manually override a student's attendance status.\n\n"
                "`/student_attendance <student>`\n"
                "Last 20 attendance records for a student.\n\n"
                "**Auto:** Voice joins/leaves tracked in real time. Finalises at lesson end."
            ),
            inline=False,
        )

        embed.add_field(
            name="📝 Homework",
            value=(
                "`/set_homework <lesson_id> <title> <due_date> [description]`\n"
                "Create a homework task attached to a lesson.\n"
                "`due_date` format: `YYYY-MM-DD HH:MM` (Sydney time) — e.g. `2025-06-15 17:00`\n\n"
                "`/missing_homework <class_group_id>`\n"
                "List students with missing or late homework for a class group.\n\n"
                "**Auto:** Homework DM reminders are sent 25 hours before the due time."
            ),
            inline=False,
        )

        embed.add_field(
            name="🧵 Lesson Threads",
            value=(
                "`/create_lesson_thread <lesson_id>`\n"
                "Manually create a thread in the class text channel for a lesson.\n\n"
                "**Auto:** Threads are created 30 minutes before each lesson starts."
            ),
            inline=False,
        )

        embed.add_field(
            name="⚙️ Setup Notes",
            value=(
                "**Discord Roles** — The bot needs these roles to exist in your server:\n"
                "Base: `Student` · `Tutor` · `Admin` · `Parent`\n"
                "Class: `Year 7` · `Year 8` · `Year 9` · `Year 10` · `Year 11 Advanced` · "
                "`Year 11 Extension 1` · `Year 12 Advanced` · `Year 12 Extension 1` · `Year 12 Extension 2` · `Maths`\n\n"
                "**Role Hierarchy** — The **Ryze Education Bot** role must sit *above* all the above roles "
                "in Server Settings → Roles.\n\n"
                "**Class Group IDs** — Check the `class_groups` table in pgAdmin.\n"
                "**Google Calendar** — Each class group needs `google_calendar_id` set in the database."
            ),
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # Class overview
    # ------------------------------------------------------------------ #

    @app_commands.command(name="today_classes", description="List all classes scheduled for today.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def today_classes(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            from bot.services.lesson_service import get_todays_lessons
            lessons = await get_todays_lessons(session)

        today_str = format_sydney(now_utc(), "%A, %d %B %Y")

        if not lessons:
            embed = discord.Embed(
                title="📅  Today's Classes",
                description=f"{today_str}\n\n*No classes scheduled today.*",
                colour=discord.Colour.greyple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="📅  Today's Classes",
            description=today_str,
            colour=discord.Colour.blue(),
        )
        for lesson in lessons:
            time_str = format_sydney(lesson.start_time, "%I:%M %p").lstrip("0")
            end_str   = format_sydney(lesson.end_time,   "%I:%M %p").lstrip("0")
            tutor     = lesson.class_group.tutor.full_name if lesson.class_group.tutor else "*No tutor assigned*"
            status    = lesson.status.value.title()
            embed.add_field(
                name=f"{time_str} – {end_str}  ·  {lesson.class_group.name}",
                value=f"Tutor: {tutor}\nStatus: {status}  ·  ID: `#{lesson.id}`",
                inline=False,
            )
        embed.set_footer(text=f"{len(lessons)} class(es) today")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="upcoming_classes", description="List upcoming classes in the next 7 days.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def upcoming_classes(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            from bot.services.lesson_service import get_upcoming_lessons
            lessons = await get_upcoming_lessons(session, hours_ahead=7 * 24)

        if not lessons:
            embed = discord.Embed(
                title="📆  Upcoming Classes",
                description="No classes in the next 7 days.",
                colour=discord.Colour.greyple(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Group lessons by Sydney date
        by_date: dict[str, list] = defaultdict(list)
        for lesson in lessons:
            date_key = format_sydney(lesson.start_time, "%A, %d %B %Y")
            by_date[date_key].append(lesson)

        embeds: list[discord.Embed] = []
        current_embed = discord.Embed(
            title="📆  Upcoming Classes — Next 7 Days",
            colour=discord.Colour.blue(),
        )
        field_count = 0

        for date_str, day_lessons in by_date.items():
            lines: list[str] = []
            for lesson in day_lessons:
                time_str = format_sydney(lesson.start_time, "%I:%M %p").lstrip("0")
                end_str  = format_sydney(lesson.end_time,   "%I:%M %p").lstrip("0")
                tutor    = lesson.class_group.tutor.full_name if lesson.class_group.tutor else "No tutor"
                lines.append(f"**{lesson.class_group.name}**  {time_str}–{end_str}\nTutor: {tutor}  ·  ID: `#{lesson.id}`")

            # Each date becomes a field; split embeds at 10 fields (Discord max 25, but keep readable)
            if field_count >= 10:
                embeds.append(current_embed)
                current_embed = discord.Embed(colour=discord.Colour.blue())
                field_count = 0

            current_embed.add_field(name=f"📅  {date_str}", value="\n\n".join(lines), inline=False)
            field_count += 1

        current_embed.set_footer(text=f"{len(lessons)} class(es) in the next 7 days")
        embeds.append(current_embed)

        for embed in embeds:
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="class_roster", description="Show enrolled students and tutors for a class group.")
    @app_commands.guild_only()
    @app_commands.describe(class_group_id="Class group ID")
    @app_commands.default_permissions(administrator=True)
    async def class_roster(self, interaction: Interaction, class_group_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            grp_result = await session.execute(
                select(ClassGroup).where(ClassGroup.id == class_group_id)
            )
            group = grp_result.scalar_one_or_none()
            if not group:
                await interaction.followup.send(
                    f"No class group found with ID `{class_group_id}`.", ephemeral=True
                )
                return

            result = await session.execute(
                select(User, ClassGroupMember)
                .join(ClassGroupMember, ClassGroupMember.user_id == User.id)
                .where(
                    and_(
                        ClassGroupMember.class_group_id == class_group_id,
                        ClassGroupMember.active.is_(True),
                    )
                )
                .order_by(ClassGroupMember.role, User.full_name)
            )
            rows = result.all()

        embed = discord.Embed(
            title=f"👥  {group.name}",
            description="Class Roster",
            colour=discord.Colour.blue(),
        )

        tutors   = [(u, m) for u, m in rows if m.role == MemberRole.tutor]
        students = [(u, m) for u, m in rows if m.role == MemberRole.student]

        if tutors:
            embed.add_field(
                name=f"🎓  Tutor{'s' if len(tutors) > 1 else ''}  ({len(tutors)})",
                value="\n".join(f"• {u.full_name}  <@{u.discord_user_id}>" for u, _ in tutors),
                inline=False,
            )
        if students:
            student_lines = [f"• {u.full_name}  <@{u.discord_user_id}>" for u, _ in students]
            # Chunk if long
            chunk_size = 20
            for i in range(0, len(student_lines), chunk_size):
                part = student_lines[i : i + chunk_size]
                label = f"📚  Students  ({len(students)})" if i == 0 else "📚  Students (cont.)"
                embed.add_field(name=label, value="\n".join(part), inline=False)
        if not rows:
            embed.description = "No active members enrolled."

        embed.set_footer(text=f"Class Group ID: {class_group_id}  ·  {len(students)} student(s)  ·  {len(tutors)} tutor(s)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # User management
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="link_user",
        description="Link a Discord user to Ryze Education, set their nickname, and assign their roles.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        discord_user="The Discord member to link",
        full_name="Full name — also sets their server nickname",
        role="Ryze Education role (assigns the matching base Discord role)",
        class_role="Optional: also assign a year-level or subject Discord role",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Student", value="student"),
        app_commands.Choice(name="Tutor",   value="tutor"),
        app_commands.Choice(name="Admin",   value="admin"),
        app_commands.Choice(name="Parent",  value="parent"),
    ])
    @app_commands.choices(class_role=[
        app_commands.Choice(name="Year 7",               value="Year 7"),
        app_commands.Choice(name="Year 8",               value="Year 8"),
        app_commands.Choice(name="Year 9",               value="Year 9"),
        app_commands.Choice(name="Year 10",              value="Year 10"),
        app_commands.Choice(name="Year 11 Advanced",     value="Year 11 Advanced"),
        app_commands.Choice(name="Year 11 Extension 1",  value="Year 11 Extension 1"),
        app_commands.Choice(name="Year 12 Advanced",     value="Year 12 Advanced"),
        app_commands.Choice(name="Year 12 Extension 1",  value="Year 12 Extension 1"),
        app_commands.Choice(name="Year 12 Extension 2",  value="Year 12 Extension 2"),
        app_commands.Choice(name="Maths",                value="Maths"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def link_user(
        self,
        interaction: Interaction,
        discord_user: discord.Member,
        full_name: str,
        role: str,
        class_role: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        old_role_value: str | None = None

        # ── DB upsert ────────────────────────────────────────────────────
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.discord_user_id == discord_user.id)
            )
            db_user = result.scalar_one_or_none()

            if db_user:
                old_role_value = db_user.role.value if db_user.role.value != role else None
                db_user.full_name = full_name
                db_user.role = UserRole(role)
                action = "Updated"
            else:
                db_user = User(
                    discord_user_id=discord_user.id,
                    full_name=full_name,
                    role=UserRole(role),
                    active=True,
                )
                session.add(db_user)
                await session.flush()
                action = "Linked"

        # ── Build response lines ─────────────────────────────────────────
        lines: list[str] = [
            f"✅ **{action}** {discord_user.mention} → **{full_name}** ({role})."
        ]

        # Nickname
        lines.append(await _set_nickname(discord_user, full_name))

        # Remove old base role if it changed
        if old_role_value:
            old_discord_name = _BASE_ROLE_MAP.get(old_role_value)
            if old_discord_name:
                note = await _remove_role(
                    interaction.guild, discord_user, old_discord_name,
                    reason=f"Ryze role changed from {old_role_value} to {role}",
                )
                if note:
                    lines.append(note)

        # Assign new base role (Student / Tutor / Admin / Parent)
        base_discord_name = _BASE_ROLE_MAP.get(role)
        if base_discord_name:
            lines.append(
                await _assign_role(interaction.guild, discord_user, base_discord_name)
            )

        # Assign optional class/year-level role
        if class_role:
            lines.append(
                await _assign_role(interaction.guild, discord_user, class_role)
            )

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="edit_user",
        description="Fix a linked user's name, role, or class role. Only the fields you fill in will change.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        discord_user="The Discord member to edit",
        full_name="New full name (also updates server nickname)",
        role="New base role",
        class_role="Add or change a year-level / subject role",
        remove_class_role="Remove a previously assigned year-level / subject role",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Student", value="student"),
        app_commands.Choice(name="Tutor",   value="tutor"),
        app_commands.Choice(name="Admin",   value="admin"),
        app_commands.Choice(name="Parent",  value="parent"),
    ])
    @app_commands.choices(class_role=[
        app_commands.Choice(name="Year 7",               value="Year 7"),
        app_commands.Choice(name="Year 8",               value="Year 8"),
        app_commands.Choice(name="Year 9",               value="Year 9"),
        app_commands.Choice(name="Year 10",              value="Year 10"),
        app_commands.Choice(name="Year 11 Advanced",     value="Year 11 Advanced"),
        app_commands.Choice(name="Year 11 Extension 1",  value="Year 11 Extension 1"),
        app_commands.Choice(name="Year 12 Advanced",     value="Year 12 Advanced"),
        app_commands.Choice(name="Year 12 Extension 1",  value="Year 12 Extension 1"),
        app_commands.Choice(name="Year 12 Extension 2",  value="Year 12 Extension 2"),
        app_commands.Choice(name="Maths",                value="Maths"),
    ])
    @app_commands.choices(remove_class_role=[
        app_commands.Choice(name="Year 7",               value="Year 7"),
        app_commands.Choice(name="Year 8",               value="Year 8"),
        app_commands.Choice(name="Year 9",               value="Year 9"),
        app_commands.Choice(name="Year 10",              value="Year 10"),
        app_commands.Choice(name="Year 11 Advanced",     value="Year 11 Advanced"),
        app_commands.Choice(name="Year 11 Extension 1",  value="Year 11 Extension 1"),
        app_commands.Choice(name="Year 12 Advanced",     value="Year 12 Advanced"),
        app_commands.Choice(name="Year 12 Extension 1",  value="Year 12 Extension 1"),
        app_commands.Choice(name="Year 12 Extension 2",  value="Year 12 Extension 2"),
        app_commands.Choice(name="Maths",                value="Maths"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def edit_user(
        self,
        interaction: Interaction,
        discord_user: discord.Member,
        full_name: str | None = None,
        role: str | None = None,
        class_role: str | None = None,
        remove_class_role: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not any([full_name, role, class_role, remove_class_role]):
            await interaction.followup.send(
                "Nothing to change — provide at least one of `full_name`, `role`, `class_role`, or `remove_class_role`.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.discord_user_id == discord_user.id)
            )
            db_user = result.scalar_one_or_none()
            if not db_user:
                await interaction.followup.send(
                    f"{discord_user.mention} has no linked record. Run `/link_user` first.",
                    ephemeral=True,
                )
                return

            lines: list[str] = [f"✏️ **Editing** {discord_user.mention}:"]
            old_role_value = db_user.role.value

            # ── Name update ───────────────────────────────────────────────
            if full_name:
                db_user.full_name = full_name
                lines.append(f"• Name → **{full_name}**")
                lines.append(await _set_nickname(discord_user, full_name))

            # ── Role update ───────────────────────────────────────────────
            if role and role != old_role_value:
                db_user.role = UserRole(role)
                lines.append(f"• Role → **{role}**")
                # Swap Discord base roles
                old_discord_name = _BASE_ROLE_MAP.get(old_role_value)
                if old_discord_name:
                    note = await _remove_role(
                        interaction.guild, discord_user, old_discord_name,
                        reason=f"Role corrected from {old_role_value} to {role}",
                    )
                    if note:
                        lines.append(f"  {note}")
                new_discord_name = _BASE_ROLE_MAP.get(role)
                if new_discord_name:
                    lines.append(f"  {await _assign_role(interaction.guild, discord_user, new_discord_name)}")
            elif role == old_role_value:
                lines.append(f"• Role already **{role}** — no change.")

            # ── Class role assignment ─────────────────────────────────────
            if class_role:
                lines.append(
                    f"• Class role → {await _assign_role(interaction.guild, discord_user, class_role, reason='Assigned via /edit_user')}"
                )

            # ── Class role removal ────────────────────────────────────────
            if remove_class_role:
                note = await _remove_role(
                    interaction.guild, discord_user, remove_class_role,
                    reason="Removed via /edit_user",
                )
                lines.append(f"• Removed **{remove_class_role}**: {note or 'role not held.'}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="delete_user",
        description="Remove a linked user record and all their class enrolments from the database.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        discord_user="The Discord member whose record should be deleted",
        remove_discord_roles="Also strip the bot-managed Discord roles from this member",
    )
    @app_commands.default_permissions(administrator=True)
    async def delete_user(
        self,
        interaction: Interaction,
        discord_user: discord.Member,
        remove_discord_roles: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── Look up the record first (before asking for confirmation) ────
        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.discord_user_id == discord_user.id)
            )
            db_user = result.scalar_one_or_none()
            if not db_user:
                await interaction.followup.send(
                    f"{discord_user.mention} has no linked record — nothing to delete.",
                    ephemeral=True,
                )
                return

            memberships_result = await session.execute(
                select(ClassGroupMember).where(ClassGroupMember.user_id == db_user.id)
            )
            memberships = list(memberships_result.scalars().all())

        saved_name = db_user.full_name
        saved_role = db_user.role.value

        # ── Show confirmation embed with buttons ──────────────────────────
        details = [f"• User record: **{saved_name}** ({saved_role.title()})"]
        details.append(f"• Class enrolments: **{len(memberships)}**")
        if remove_discord_roles:
            details.append("• Discord roles: **will be stripped**")

        confirm_embed = discord.Embed(
            title="⚠️  Confirm Permanent Deletion",
            description=(
                f"You are about to permanently delete {discord_user.mention}'s record.\n\n"
                + "\n".join(details)
                + "\n\n**This cannot be undone.**"
            ),
            colour=discord.Colour.red(),
        )
        confirm_embed.set_footer(text="Confirmation expires in 30 seconds.")

        view = _ConfirmDeleteView(saved_name)
        await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)

        timed_out = await view.wait()  # True = timed out, False = button pressed
        if timed_out or not view.confirmed:
            return

        # ── Perform deletion ──────────────────────────────────────────────
        async with get_session() as session:
            # Re-fetch inside the new session
            result = await session.execute(
                select(User).where(User.discord_user_id == discord_user.id)
            )
            db_user2 = result.scalar_one_or_none()
            if not db_user2:
                await interaction.followup.send("Record already deleted.", ephemeral=True)
                return

            memberships_result = await session.execute(
                select(ClassGroupMember).where(ClassGroupMember.user_id == db_user2.id)
            )
            fresh_memberships = list(memberships_result.scalars().all())
            for m in fresh_memberships:
                await session.delete(m)
            await session.delete(db_user2)

        result_lines: list[str] = [
            f"🗑️  **Deleted** record for **{saved_name}** ({discord_user.mention}).",
            f"• Removed **{len(fresh_memberships)}** class enrolment(s).",
        ]

        if remove_discord_roles:
            base_discord_name = _BASE_ROLE_MAP.get(saved_role)
            if base_discord_name:
                note = await _remove_role(
                    interaction.guild, discord_user, base_discord_name,
                    reason="User record deleted via /delete_user",
                )
                if note:
                    result_lines.append(f"• {note}")
            for cr in _CLASS_ROLES:
                note = await _remove_role(
                    interaction.guild, discord_user, cr,
                    reason="User record deleted via /delete_user",
                )
                if note:
                    result_lines.append(f"• {note}")

        done_embed = discord.Embed(
            title="🗑️  User Deleted",
            description="\n".join(result_lines),
            colour=discord.Colour.greyple(),
        )
        await interaction.followup.send(embed=done_embed, ephemeral=True)

    @app_commands.command(
        name="assign_student_to_class",
        description="Enrol a student in a class group.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        student="The student's Discord account",
        class_group_id="Class group ID",
    )
    @app_commands.default_permissions(administrator=True)
    async def assign_student_to_class(
        self,
        interaction: Interaction,
        student: discord.Member,
        class_group_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._assign_member(interaction, student, class_group_id, MemberRole.student)

    @app_commands.command(
        name="assign_tutor_to_class",
        description="Assign a tutor to a class group.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        tutor="The tutor's Discord account",
        class_group_id="Class group ID",
    )
    @app_commands.default_permissions(administrator=True)
    async def assign_tutor_to_class(
        self,
        interaction: Interaction,
        tutor: discord.Member,
        class_group_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._assign_member(interaction, tutor, class_group_id, MemberRole.tutor)

    async def _assign_member(
        self,
        interaction: Interaction,
        discord_member: discord.Member,
        class_group_id: int,
        role: MemberRole,
    ) -> None:
        async with get_session() as session:
            user_result = await session.execute(
                select(User).where(User.discord_user_id == discord_member.id)
            )
            db_user = user_result.scalar_one_or_none()
            if not db_user:
                await interaction.followup.send(
                    f"{discord_member.mention} is not linked yet. Run `/link_user` first.",
                    ephemeral=True,
                )
                return

            grp_result = await session.execute(
                select(ClassGroup).where(ClassGroup.id == class_group_id)
            )
            group = grp_result.scalar_one_or_none()
            if not group:
                await interaction.followup.send("Class group not found.", ephemeral=True)
                return

            existing = await session.execute(
                select(ClassGroupMember).where(
                    and_(
                        ClassGroupMember.class_group_id == class_group_id,
                        ClassGroupMember.user_id == db_user.id,
                    )
                )
            )
            membership = existing.scalar_one_or_none()
            if membership:
                membership.role = role
                membership.active = True
                msg = f"Updated {discord_member.mention} to **{role.value}** in **{group.name}**."
            else:
                membership = ClassGroupMember(
                    class_group_id=class_group_id,
                    user_id=db_user.id,
                    role=role,
                    active=True,
                )
                session.add(membership)
                await session.flush()
                msg = f"Assigned {discord_member.mention} as **{role.value}** in **{group.name}**."

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
