"""Helpers for Discord operations — thread creation, message sending."""
import logging
from datetime import datetime
from typing import Optional

import discord

from bot.models.class_group import ClassGroup
from bot.models.lesson import Lesson
from bot.utils.time_utils import format_sydney, format_lesson_date, to_sydney

log = logging.getLogger(__name__)


def lesson_thread_name(lesson: Lesson, class_group: ClassGroup) -> str:
    date_str = format_lesson_date(lesson.start_time)
    return f"[{date_str}] {class_group.name}"


def lesson_thread_body(lesson: Lesson, class_group: ClassGroup) -> str:
    time_str = format_sydney(lesson.start_time, "%I:%M %p")
    end_str = format_sydney(lesson.end_time, "%I:%M %p")
    date_str = format_sydney(lesson.start_time, "%A, %d %B %Y")

    tutor_name = (
        class_group.tutor.full_name if class_group.tutor else "Your tutor"
    )

    lines = [
        f"**{lesson.title}**",
        f"",
        f"Date: {date_str}",
        f"Time: {time_str} – {end_str}",
        f"Tutor: {tutor_name}",
        f"Class: {class_group.name}",
    ]
    if lesson.meet_link:
        lines += [f"", f"Google Meet: {lesson.meet_link}"]
    elif lesson.location:
        lines += [f"", f"Location: {lesson.location}"]
    if lesson.description:
        lines += [f"", f"{lesson.description}"]
    lines += [
        f"",
        "Please have your **workbook, calculator, and notes** ready.",
        "Drop any questions below!",
    ]
    return "\n".join(lines)


async def create_lesson_thread(
    guild: discord.Guild,
    lesson: Lesson,
    class_group: ClassGroup,
    user_mentions: Optional[list[str]] = None,
) -> Optional[discord.Thread]:
    """Create a thread in the class text channel for this lesson.

    ``user_mentions`` is a list of Discord mention strings (e.g. ``["<@123>", "<@456>"]``)
    for every student and tutor enrolled in the class.  If provided, a follow-up
    message is posted inside the thread to notify each person directly.
    """
    if not class_group.discord_text_channel_id:
        log.warning("Class group %r has no text channel configured.", class_group.name)
        return None

    channel = guild.get_channel(class_group.discord_text_channel_id)
    if not isinstance(channel, discord.TextChannel):
        log.warning("Text channel %d not found or not a TextChannel.", class_group.discord_text_channel_id)
        return None

    thread_name = lesson_thread_name(lesson, class_group)
    body = lesson_thread_body(lesson, class_group)

    try:
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.public_thread,
            reason="Auto-created lesson thread",
        )
        await thread.send(body)

        # Tag all enrolled students and the tutor so they get notified
        if user_mentions:
            mention_str = " ".join(user_mentions)
            await thread.send(
                f"{mention_str}\nThis thread is for your upcoming **{class_group.name}** session. "
                "Drop any questions or updates here!"
            )

        log.info("Created thread %r (%d) for lesson %d", thread_name, thread.id, lesson.id)
        return thread
    except discord.Forbidden:
        log.error("Missing permissions to create thread in channel %d.", class_group.discord_text_channel_id)
    except discord.HTTPException as exc:
        log.error("Failed to create thread for lesson %d: %s", lesson.id, exc)
    return None


async def safe_send_dm(user: discord.User | discord.Member, content: str) -> bool:
    """Send a DM, returning True on success."""
    try:
        await user.send(content)
        return True
    except discord.Forbidden:
        log.debug("Cannot DM user %s (DMs disabled).", user)
    except discord.HTTPException as exc:
        log.warning("Failed to DM user %s: %s", user, exc)
    return False


async def get_discord_member(guild: discord.Guild, discord_user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(discord_user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(discord_user_id)
    except (discord.NotFound, discord.HTTPException):
        return None
