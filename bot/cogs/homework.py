"""Cog: Homework tasks — set, remind, and report missing homework."""
import logging
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks

from bot import config
from bot.database import get_session
from bot.services.homework_service import (
    create_homework_task,
    get_missing_homework,
    get_upcoming_homework_due,
)
from bot.services.discord_service import get_discord_member, safe_send_dm
from bot.services.lesson_service import get_lesson_by_id
from bot.utils.time_utils import SYDNEY_TZ, format_sydney, now_utc
from bot.utils.task_safety import install_loop_restart

log = logging.getLogger(__name__)


class HomeworkCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # In-memory dedup: avoids FK violation that would occur if we tried to
        # store homework reminders in reminder_logs.lesson_id (which is a FK to
        # lessons.id — task IDs are not valid lesson IDs).
        # Resets on bot restart, which is acceptable for homework reminders.
        self._reminded: set[tuple[int, int]] = set()  # (task_id, student_db_id)
        install_loop_restart(self._hw_reminder_loop, "homework.reminder", self.bot)
        self._hw_reminder_loop.start()

    def cog_unload(self) -> None:
        self._hw_reminder_loop.cancel()

    @tasks.loop(minutes=30)
    async def _hw_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            return
        try:
            await self._send_homework_reminders(guild)
        except Exception:
            log.exception("Homework reminder loop error.")

    @_hw_reminder_loop.before_loop
    async def _before_hw_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _send_homework_reminders(self, guild: discord.Guild) -> None:
        from sqlalchemy import select, and_
        from bot.models.class_group import ClassGroupMember, MemberRole
        from bot.models.user import User

        async with get_session() as session:
            tasks_due = await get_upcoming_homework_due(session, hours_ahead=25)
            for task in tasks_due:
                class_group = task.class_group

                result = await session.execute(
                    select(User)
                    .join(ClassGroupMember, ClassGroupMember.user_id == User.id)
                    .where(
                        and_(
                            ClassGroupMember.class_group_id == class_group.id,
                            ClassGroupMember.role == MemberRole.student,
                            ClassGroupMember.active.is_(True),
                            User.active.is_(True),
                        )
                    )
                )
                students = list(result.scalars().all())

                for student in students:
                    key = (task.id, student.id)
                    if key in self._reminded:
                        continue

                    member = await get_discord_member(guild, student.discord_user_id)
                    if member:
                        due_str = format_sydney(task.due_at, "%A, %d %B at %I:%M %p")
                        msg = (
                            f"📝 **Homework reminder for {class_group.name}**\n\n"
                            f"**{task.title}**\n"
                            f"Due: {due_str}\n"
                        )
                        if task.description:
                            msg += f"\n{task.description}"
                        success = await safe_send_dm(member, msg)
                        if success:
                            self._reminded.add(key)
                        else:
                            log.warning(
                                "Homework DM failed for student %d (task %d).",
                                student.id, task.id,
                            )
                    else:
                        log.debug(
                            "Student %d not found in guild — skipping homework DM (task %d).",
                            student.discord_user_id, task.id,
                        )

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #

    @app_commands.command(name="set_homework", description="Create a homework task for a lesson.")
    @app_commands.guild_only()
    @app_commands.describe(
        lesson_id="Lesson ID to attach homework to",
        title="Homework title",
        due_date="Due date in YYYY-MM-DD HH:MM format (Sydney time)",
        description="Optional description or instructions",
    )
    @app_commands.default_permissions(administrator=True)
    async def set_homework(
        self,
        interaction: Interaction,
        lesson_id: int,
        title: str,
        due_date: str,
        description: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # Parse the due date
        try:
            naive_due = datetime.strptime(due_date, "%Y-%m-%d %H:%M")
            due_at = SYDNEY_TZ.localize(naive_due).astimezone(timezone.utc)
        except ValueError:
            await interaction.followup.send(
                "❌ Invalid date format. Use `YYYY-MM-DD HH:MM` (Sydney time), e.g. `2026-06-15 17:00`.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            lesson = await get_lesson_by_id(session, lesson_id)
            if not lesson:
                await interaction.followup.send(
                    f"❌ Lesson `{lesson_id}` not found.", ephemeral=True
                )
                return

            from bot.models.user import User
            from sqlalchemy import select

            user_result = await session.execute(
                select(User).where(User.discord_user_id == interaction.user.id)
            )
            db_user = user_result.scalar_one_or_none()
            if not db_user:
                await interaction.followup.send(
                    "❌ Your Discord account is not linked. Ask an admin to run `/link_user`.",
                    ephemeral=True,
                )
                return

            task = await create_homework_task(
                session,
                class_group_id=lesson.class_group_id,
                title=title,
                description=description or None,
                due_at=due_at,
                created_by_user_id=db_user.id,
                lesson_id=lesson_id,
            )

            due_fmt = format_sydney(due_at, "%A, %d %B %Y at %I:%M %p")

            # Build embed for thread/channel post
            post_embed = discord.Embed(
                title=f"📝  New Homework: {title}",
                description=description if description else None,
                colour=discord.Colour.orange(),
            )
            post_embed.add_field(name="Due", value=due_fmt, inline=True)
            post_embed.add_field(name="Task ID", value=f"`#{task.id}`", inline=True)
            post_embed.set_footer(text=f"{lesson.class_group.name}  ·  Lesson #{lesson_id}")

            posted = False
            if lesson.discord_thread_id:
                thread = interaction.guild.get_channel(lesson.discord_thread_id)
                if isinstance(thread, discord.Thread):
                    try:
                        await thread.send(embed=post_embed)
                        posted = True
                    except discord.HTTPException:
                        pass

            if not posted and lesson.class_group.discord_text_channel_id:
                ch = interaction.guild.get_channel(lesson.class_group.discord_text_channel_id)
                if isinstance(ch, discord.TextChannel):
                    try:
                        await ch.send(embed=post_embed)
                        posted = True
                    except discord.HTTPException:
                        pass

        confirm_embed = discord.Embed(
            title="✅  Homework Created",
            colour=discord.Colour.brand_green(),
        )
        confirm_embed.add_field(name="Title", value=title, inline=False)
        confirm_embed.add_field(name="Due", value=due_fmt, inline=True)
        confirm_embed.add_field(name="Task ID", value=f"`#{task.id}`", inline=True)
        if not posted:
            confirm_embed.add_field(
                name="⚠️  Note",
                value="No lesson thread or class channel found — homework was not posted to Discord.",
                inline=False,
            )

        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

    @app_commands.command(
        name="missing_homework",
        description="List students with missing/late homework for a class group.",
    )
    @app_commands.guild_only()
    @app_commands.describe(class_group_id="Class group ID")
    @app_commands.default_permissions(administrator=True)
    async def missing_homework(self, interaction: Interaction, class_group_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        async with get_session() as session:
            missing = await get_missing_homework(session, class_group_id)

        if not missing:
            embed = discord.Embed(
                title="✅  No Missing Homework",
                description=f"All students are up to date for class group `{class_group_id}`.",
                colour=discord.Colour.brand_green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Group by task so each task is one field, listing who's missing it
        by_task: dict[int, dict] = defaultdict(lambda: {"task": None, "students": []})
        for item in missing:
            tid = item["task"].id
            by_task[tid]["task"] = item["task"]
            by_task[tid]["students"].append((item["student"], item["status"]))

        status_icon = {"missing": "❌", "late": "🟠"}
        total_count = len(missing)

        embeds: list[discord.Embed] = []
        current = discord.Embed(
            title=f"📝  Missing Homework — Class Group `{class_group_id}`",
            colour=discord.Colour.red(),
        )

        for tid, data in by_task.items():
            task = data["task"]
            due_str = format_sydney(task.due_at, "%d %b %Y")
            lines: list[str] = []
            for student, status in data["students"]:
                icon = status_icon.get(status.value, "❓")
                lines.append(f"{icon}  {student.full_name}")

            field_val = "\n".join(lines)
            if len(field_val) > 1000:
                field_val = field_val[:1000] + "\n…"

            # Start a new embed if we're near the field limit
            if len(current.fields) >= 20:
                embeds.append(current)
                current = discord.Embed(colour=discord.Colour.red())

            current.add_field(
                name=f"📌  {task.title}  ·  due {due_str}",
                value=field_val,
                inline=False,
            )

        current.set_footer(
            text=f"{total_count} outstanding submission(s) across {len(by_task)} task(s)"
        )
        embeds.append(current)

        for embed in embeds:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HomeworkCog(bot))
