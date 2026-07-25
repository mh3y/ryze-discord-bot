"""
Cog: Automatic class discovery from Google Calendar.

How it works
─────────────
The Google account behind the OAuth credentials owns one Calendar per class.
Each calendar's name becomes the class name (e.g. "Year 11 Advanced Maths").

On startup and every CALENDAR_SYNC_INTERVAL minutes the bot:
  1. Lists all Google Calendars visible to the account.
  2. Fetches the portal's current class list.
  3. For each calendar NOT yet in the portal:
     a. Parses a subject + year_level from the calendar name.
     b. Creates a Discord role and a Discord channel for the class.
     c. POSTs the new class to the portal API (idempotent — safe to retry).
     d. PATCHes the portal class with the new Discord IDs.
  4. For calendars already in the portal, ensures their Discord role/channel
     IDs are stored (catches cases where the bot died mid-setup).

Naming convention
─────────────────
Calendars should be named exactly as you want the class to appear in the
portal and Discord, e.g.:
  "Year 11 Advanced Maths"
  "Year 12 Extension 1 Maths"
  "Year 7 English"

Calendars to ignore
────────────────────
Add the calendar name (or a prefix) to IGNORED_CALENDAR_NAMES below, or
prefix the calendar name with an underscore (e.g. "_Admin") — those are
skipped automatically.

Discord layout
──────────────
• A category named CLASSES_CATEGORY_NAME (default "📚 Classes") is
  created once and all class channels live inside it.
• Each class gets:
    - a text channel  #year-11-advanced-maths  (slug of the class name)
    - a Discord role  Year 11 Advanced Maths   (same as calendar name)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks

from bot import config
from bot.services.google_calendar_service import list_calendars
from bot.services.portal_api import PortalAPIClient, PortalAPIError

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────── #

# Category that class channels are created in.
CLASSES_CATEGORY_NAME = "📚 Classes"

# Calendars whose names match any of these strings (case-insensitive) are skipped.
IGNORED_CALENDAR_NAMES: list[str] = [
    "holidays in australia",
    "birthdays",
    "contacts",
    "other calendars",
]

# If True, skip any calendar whose name starts with "_"
SKIP_UNDERSCORE_PREFIX = True

# ── Helpers ─────────────────────────────────────────────────────────────────── #

_YEAR_RE = re.compile(r"\bYear\s+\d+\b", re.IGNORECASE)


def _name_to_channel_slug(name: str) -> str:
    """'Year 11 Advanced Maths' → 'year-11-advanced-maths'"""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug[:100]  # Discord channel name limit


def _infer_subject(name: str) -> str:
    """Best-effort subject extraction — removes 'Year N' prefix tokens."""
    without_year = _YEAR_RE.sub("", name).strip()
    # Remove leading/trailing noise words
    subject = re.sub(r"^[\s\-–:]+|[\s\-–:]+$", "", without_year)
    return subject or name  # fallback to full name


def _infer_year_level(name: str) -> str | None:
    match = _YEAR_RE.search(name)
    return match.group(0) if match else None


def _should_skip(calendar: dict) -> bool:
    name = calendar["name"]
    if not name:
        return True
    if SKIP_UNDERSCORE_PREFIX and name.startswith("_"):
        return True
    # Skip the account's PRIMARY calendar — it is never a real class calendar and
    # must not be auto-provisioned as a ghost class. Use Google's authoritative
    # `primary` flag from list_calendars, NOT an email-shaped-ID heuristic: every
    # secondary class calendar ALSO has an email-shaped ID
    # (…@group.calendar.google.com), so matching that shape skips every real
    # class and breaks auto-provisioning entirely. [DEF-9]
    if calendar.get("primary"):
        log.debug("Skipping primary account calendar: %r", calendar.get("id", ""))
        return True
    name_lower = name.lower()
    for ignored in IGNORED_CALENDAR_NAMES:
        if ignored.lower() in name_lower:
            return True
    return False


# ── Cog ────────────────────────────────────────────────────────────────────── #

class ClassDiscoveryCog(commands.Cog):
    """Automatically provisions portal classes + Discord structure from Google Calendar."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._discovery_loop.start()

    def cog_unload(self) -> None:
        self._discovery_loop.cancel()

    @tasks.loop(minutes=config.CALENDAR_SYNC_INTERVAL)
    async def _discovery_loop(self) -> None:
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            return
        started = datetime.now(timezone.utc).isoformat()
        try:
            created, updated = await self._run_discovery(guild)
            completed = datetime.now(timezone.utc).isoformat()
            if created:
                log.info("Class discovery: %d new class(es) provisioned.", created)
            if updated:
                log.info("Class discovery: %d class(es) had Discord IDs updated.", updated)
            # Push sync audit log
            if config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
                async with PortalAPIClient() as client:
                    await client.push_sync_log(
                        sync_type="classes",
                        status="success",
                        started_at=started,
                        completed_at=completed,
                        records_created=created,
                        records_updated=updated,
                    )
        except Exception as exc:
            log.exception("Class discovery loop error.")
            if config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
                try:
                    async with PortalAPIClient() as client:
                        await client.push_sync_log(
                            sync_type="classes",
                            status="failed",
                            started_at=started,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            error_message=str(exc),
                        )
                except Exception:
                    pass

    @_discovery_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── Core discovery logic ──────────────────────────────────────────────── #

    async def _run_discovery(self, guild: discord.Guild) -> tuple[int, int]:
        """
        Scan Google Calendars vs portal classes.
        Returns (classes_created, discord_ids_updated).
        """
        if not config.DASHBOARD_API_KEY or not config.PORTAL_API_URL:
            log.warning("PORTAL_API_URL or DASHBOARD_API_KEY not set — class discovery disabled.")
            return 0, 0

        # Run blocking Google API call in executor (it's a sync library)
        calendars: list[dict] = await self.bot.loop.run_in_executor(None, list_calendars)

        async with PortalAPIClient() as client:
            portal_classes: list[dict] = await client.get_classes()

        # Build lookup: google_calendar_id → portal class dict
        portal_by_cal_id: dict[str, dict] = {
            cls["google_calendar_id"]: cls
            for cls in portal_classes
            if cls.get("google_calendar_id")
        }

        created = 0
        updated = 0

        for calendar in calendars:
            if _should_skip(calendar):
                log.debug("Skipping calendar %r.", calendar["name"])
                continue

            cal_id = calendar["id"]
            cal_name = calendar["name"]

            existing_cls = portal_by_cal_id.get(cal_id)

            async with PortalAPIClient() as client:
                if existing_cls is None:
                    # ── New calendar — provision everything ───────────────
                    log.info("New calendar detected: %r (%s) — provisioning class.", cal_name, cal_id)

                    # 1. Create Discord role + channel
                    role, channel = await self._provision_discord(guild, cal_name)

                    # 2. Register class in portal (idempotent)
                    result = await client.create_class(
                        name=               cal_name,
                        subject=            _infer_subject(cal_name),
                        google_calendar_id= cal_id,
                        discord_channel_id= str(channel.id) if channel else None,
                        discord_role_id=    str(role.id)    if role    else None,
                        year_level=         _infer_year_level(cal_name),
                        description=        calendar.get("description") or None,
                    )
                    if result.get("created"):
                        created += 1
                    # Add to local lookup so we don't re-process this calendar
                    portal_by_cal_id[cal_id] = result.get("class", {})

                else:
                    # ── Existing class — ensure Discord IDs are stored ────
                    missing_channel = not existing_cls.get("discord_channel_id")
                    missing_role    = not existing_cls.get("discord_role_id")

                    if missing_channel or missing_role:
                        role, channel = await self._provision_discord(
                            guild, cal_name,
                            existing_role_id    = existing_cls.get("discord_role_id"),
                            existing_channel_id = existing_cls.get("discord_channel_id"),
                        )
                        patch: dict = {}
                        if missing_channel and channel:
                            patch["discord_channel_id"] = str(channel.id)
                        if missing_role and role:
                            patch["discord_role_id"] = str(role.id)
                        if patch:
                            await client.update_class_discord(existing_cls["id"], **patch)
                            updated += 1

        return created, updated

    # ── Discord provisioning ──────────────────────────────────────────────── #

    async def _provision_discord(
        self,
        guild: discord.Guild,
        class_name: str,
        existing_role_id: str | None = None,
        existing_channel_id: str | None = None,
    ) -> tuple[discord.Role | None, discord.TextChannel | None]:
        """
        Ensure a Discord role and text channel exist for this class.
        If existing IDs are provided, resolves them instead of creating new ones.
        Returns (role, channel) — either may be None if creation failed.
        """
        role    = await self._get_or_create_role(guild, class_name, existing_role_id)
        channel = await self._get_or_create_channel(guild, class_name, role, existing_channel_id)
        return role, channel

    async def _get_or_create_role(
        self,
        guild: discord.Guild,
        class_name: str,
        existing_id: str | None,
    ) -> discord.Role | None:
        # 1. Try to resolve by stored ID
        if existing_id:
            role = guild.get_role(int(existing_id))
            if role:
                return role

        # 2. Try to find by name (case-insensitive)
        existing = next((r for r in guild.roles if r.name.lower() == class_name.lower()), None)
        if existing:
            return existing

        # 3. Create
        try:
            role = await guild.create_role(
                name=   class_name,
                colour= discord.Colour.blurple(),
                reason= "Auto-created by Ryze bot (Google Calendar discovery)",
            )
            log.info("Created Discord role %r (id=%d) for class %r.", role.name, role.id, class_name)
            return role
        except discord.Forbidden:
            log.error("Missing permission to create Discord role for %r.", class_name)
            return None
        except discord.HTTPException as exc:
            log.error("Failed to create Discord role for %r: %s", class_name, exc)
            return None

    async def _get_or_create_channel(
        self,
        guild: discord.Guild,
        class_name: str,
        role: discord.Role | None,
        existing_id: str | None,
    ) -> discord.TextChannel | None:
        channel_slug = _name_to_channel_slug(class_name)

        # 1. Try to resolve by stored ID
        if existing_id:
            ch = guild.get_channel(int(existing_id))
            if isinstance(ch, discord.TextChannel):
                return ch

        # 2. Try to find by slug name
        existing = next(
            (c for c in guild.text_channels if c.name == channel_slug),
            None,
        )
        if existing:
            return existing

        # 3. Get or create the class category
        category = await self._get_or_create_category(guild)

        # 4. Create channel with permission overwrites so only role members + admins can see it.
        #
        # GUARDRAIL: These overwrites apply to a TEXT channel only — this function
        # never creates or modifies voice channels.  Speak / Connect / UseVAD
        # permissions are intentionally absent here; they must never be denied here.
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
        }
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                read_messages=True,
                send_messages=True,
                read_message_history=True,
                # NOTE: Do NOT add speak/connect/use_voice_activation here.
                # This is a text channel; voice permissions live on voice channels only.
            )
        # Give admins access
        admin_role = next((r for r in guild.roles if r.name.lower() == "admin"), None)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True)

        try:
            channel = await guild.create_text_channel(
                name=          channel_slug,
                category=      category,
                overwrites=    overwrites,
                topic=         f"Class channel for {class_name}",
                reason=        "Auto-created by Ryze bot (Google Calendar discovery)",
            )
            log.info("Created Discord channel #%s (id=%d) for class %r.", channel_slug, channel.id, class_name)
            return channel
        except discord.Forbidden:
            log.error("Missing permission to create Discord channel for %r.", class_name)
            return None
        except discord.HTTPException as exc:
            log.error("Failed to create Discord channel for %r: %s", class_name, exc)
            return None

    async def _get_or_create_category(self, guild: discord.Guild) -> discord.CategoryChannel | None:
        existing = next(
            (c for c in guild.categories if c.name == CLASSES_CATEGORY_NAME),
            None,
        )
        if existing:
            return existing
        try:
            category = await guild.create_category(
                name=   CLASSES_CATEGORY_NAME,
                reason= "Auto-created by Ryze bot for class channels",
            )
            log.info("Created Discord category %r.", CLASSES_CATEGORY_NAME)
            return category
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.error("Could not create class category: %s", exc)
            return None

    # ── /discover_classes (manual trigger) ───────────────────────────────── #

    @app_commands.command(
        name="discover_classes",
        description="Manually scan Google Calendar for new classes and provision Discord channels/roles.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def discover_classes(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            created, updated = await self._run_discovery(interaction.guild)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Discovery failed: {exc}", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🗓️  Class Discovery Complete",
            colour=discord.Colour.green() if (created or updated) else discord.Colour.blue(),
        )
        embed.add_field(name="✅ New classes created", value=str(created), inline=True)
        embed.add_field(name="🔗 Discord IDs updated", value=str(updated), inline=True)
        embed.description = (
            "All Google Calendars have been scanned.\n"
            "New classes have been registered in the portal with Discord channels and roles."
            if (created or updated)
            else "Everything is already up to date — no new calendars found."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClassDiscoveryCog(bot))
