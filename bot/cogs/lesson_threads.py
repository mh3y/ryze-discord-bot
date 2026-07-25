"""Cog: Auto-create lesson threads 30 minutes before each lesson."""
import logging
from datetime import timedelta, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks

from bot import config
from bot.database import get_session
from bot.models.lesson import LessonStatus
from bot.services.discord_service import create_lesson_thread
from bot.services.lesson_service import get_lesson_by_id, get_upcoming_lessons
from bot.services.reminder_service import get_lesson_members
from bot.utils.time_utils import now_utc
from bot.utils.task_safety import install_loop_restart

log = logging.getLogger(__name__)


class LessonThreadsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        install_loop_restart(self._thread_loop, "lesson_threads.thread", self.bot)
        self._thread_loop.start()

    def cog_unload(self) -> None:
        self._thread_loop.cancel()

    @tasks.loop(minutes=1)
    async def _thread_loop(self) -> None:
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            return
        try:
            await self._create_upcoming_threads(guild)
        except Exception:
            log.exception("Thread creation loop error.")

    @_thread_loop.before_loop
    async def _before_thread_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _create_upcoming_threads(self, guild: discord.Guild) -> None:
        now = now_utc()
        lead = config.THREAD_CREATION_LEAD_MINUTES
        async with get_session() as session:
            lessons = await get_upcoming_lessons(session, hours_ahead=2)
            for lesson in lessons:
                if lesson.discord_thread_id:
                    continue  # Already created
                # Normalise start_time: SQLite returns naive datetimes; treat as UTC
                start = lesson.start_time
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                fire_at = start - timedelta(minutes=lead)
                if not (fire_at <= now < fire_at + timedelta(minutes=1)):
                    continue

                class_group = lesson.class_group

                # Fetch enrolled members to mention inside the thread
                members = await get_lesson_members(session, class_group.id)
                if class_group.tutor_user_id and class_group.tutor:
                    tutor_in_list = any(m.id == class_group.tutor.id for m in members)
                    if not tutor_in_list:
                        members.append(class_group.tutor)
                user_mentions = [f"<@{u.discord_user_id}>" for u in members] or None

                thread = await create_lesson_thread(guild, lesson, class_group, user_mentions)
                if thread:
                    lesson.discord_thread_id = thread.id
                    lesson.updated_at = now

    @app_commands.command(
        name="create_lesson_thread",
        description="Manually create a lesson thread.",
    )
    @app_commands.guild_only()
    @app_commands.describe(lesson_id="Lesson ID")
    @app_commands.default_permissions(administrator=True)
    async def create_lesson_thread_cmd(self, interaction: Interaction, lesson_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        async with get_session() as session:
            lesson = await get_lesson_by_id(session, lesson_id)
            if not lesson:
                await interaction.followup.send(f"Lesson {lesson_id} not found.", ephemeral=True)
                return
            if lesson.discord_thread_id:
                await interaction.followup.send(
                    f"Thread already exists: <#{lesson.discord_thread_id}>", ephemeral=True
                )
                return

            class_group = lesson.class_group

            # Fetch enrolled members to mention inside the thread
            members = await get_lesson_members(session, class_group.id)
            if class_group.tutor_user_id and class_group.tutor:
                tutor_in_list = any(m.id == class_group.tutor.id for m in members)
                if not tutor_in_list:
                    members.append(class_group.tutor)
            user_mentions = [f"<@{u.discord_user_id}>" for u in members] or None

            thread = await create_lesson_thread(guild, lesson, class_group, user_mentions)
            if thread:
                lesson.discord_thread_id = thread.id
                lesson.updated_at = now_utc()
                await interaction.followup.send(
                    f"Thread created: {thread.mention}", ephemeral=True
                )
            else:
                await interaction.followup.send("Failed to create thread. Check bot permissions.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LessonThreadsCog(bot))
