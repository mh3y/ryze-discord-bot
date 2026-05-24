"""Cog: Fully-automated member onboarding.

Automation layers
─────────────────
1. Startup scan    — on bot ready, scans every guild member and creates
                     provisional records for anyone not yet in the DB.
2. on_member_join  — captures Discord ID the instant someone joins.
3. on_member_update — when an admin assigns a Discord role that is mapped
                     to a class group, the member is auto-enrolled.
4. Periodic resync — repeats the full scan every 6 hours so nothing is
                     ever missed (e.g. members who joined while bot was
                     offline, or roles assigned outside Discord).

Admin commands (fallback / override)
─────────────────────────────────────
/enrol            — manual one-step link + class assignment
/unenrol          — remove from a class
/pending_members  — list members with no class assignment yet
/link_class_role  — map a Discord role → class group (enables auto-enrol)
"""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from sqlalchemy import select, and_

from bot import config
from bot.database import get_session
from bot.models.class_group import ClassGroup, ClassGroupMember, MemberRole
from bot.models.user import User, UserRole
from bot.services.portal_api import PortalAPIClient, PortalAPIError

log = logging.getLogger(__name__)

# ── Discord role helpers ──────────────────────────────────────────────────── #

_BASE_ROLE_MAP: dict[str, str] = {
    "student": "Student",
    "tutor":   "Tutor",
    "admin":   "Admin",
    "parent":  "Parent",
}

_CLASS_ROLES: list[str] = [
    "Year 7", "Year 8", "Year 9", "Year 10",
    "Year 11 Advanced", "Year 11 Extension 1",
    "Year 12 Advanced", "Year 12 Extension 1", "Year 12 Extension 2",
    "Maths",
]


def _find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    target = name.lower()
    return next((r for r in guild.roles if r.name.lower() == target), None)


async def _assign_discord_role(guild: discord.Guild, member: discord.Member, role_name: str) -> str:
    role = _find_role(guild, role_name)
    if role is None:
        return f"ℹ️ No Discord role named **{role_name}**."
    if role in member.roles:
        return f"Already has **{role.name}**."
    try:
        await member.add_roles(role, reason="Assigned via Ryze bot")
        log.info(
            "[role] ADD  user=%s (id=%d)  role=%r  source=members_cog",
            member.display_name, member.id, role.name,
        )
        return f"✅ Assigned **{role.name}**."
    except discord.Forbidden:
        log.warning(
            "[role] ADD FAILED (Forbidden)  user=%s (id=%d)  role=%r  source=members_cog",
            member.display_name, member.id, role.name,
        )
        return f"⚠️ Could not assign **{role.name}** — check bot role hierarchy."


async def _set_nickname(member: discord.Member, full_name: str) -> str:
    try:
        await member.edit(nick=full_name, reason="Set via Ryze bot")
        return f"✅ Nickname → **{full_name}**."
    except discord.Forbidden:
        return "⚠️ Could not set nickname — check bot role hierarchy."
    except discord.HTTPException as exc:
        return f"⚠️ Nickname failed: {exc}"


# ── Cog ──────────────────────────────────────────────────────────────────── #

class MembersCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._sync_loop.start()

    def cog_unload(self) -> None:
        self._sync_loop.cancel()

    # ── Periodic + startup sync ───────────────────────────────────────────── #

    @tasks.loop(hours=6)
    async def _sync_loop(self) -> None:
        """Runs on startup (after bot is ready) and every 6 hours thereafter."""
        guild = self.bot.get_guild(config.DISCORD_GUILD_ID)
        if not guild:
            return
        try:
            created, enrolled = await self._sync_all_members(guild)
            if created or enrolled:
                log.info(
                    "Member sync: %d new provisional record(s), %d auto-enrolment(s).",
                    created, enrolled,
                )
        except Exception:
            log.exception("Member sync loop error.")

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── Role detection helper ─────────────────────────────────────────────── #

    @staticmethod
    def _detect_portal_role(member: discord.Member) -> UserRole:
        """
        Determine the portal role for a Discord member by checking which of the
        three base roles they hold.  Priority: Admin > Tutor > Student.
        Returns UserRole.student if none of the base roles are found.
        """
        member_role_names = {r.name.lower() for r in member.roles}
        if "admin" in member_role_names:
            return UserRole.admin
        if "tutor" in member_role_names:
            return UserRole.tutor
        return UserRole.student

    # ── Full member sync ──────────────────────────────────────────────────── #

    async def _sync_all_members(self, guild: discord.Guild) -> tuple[int, int]:
        """
        Scan every human guild member.
        • Creates a provisional User record for anyone not in the bot's local DB.
        • Detects Admin / Tutor / Student Discord roles and stores the correct role.
        • Auto-enrolls members whose Discord roles map to a class group.
        • Pushes the full member snapshot to the portal API (Supabase).
        Returns (new_records_created, new_enrolments_created).
        """
        async with get_session() as session:
            # ── Fetch existing state in bulk ──────────────────────────────
            existing_ids_result = await session.execute(select(User.discord_user_id, User.id))
            discord_to_db_id: dict[int, int] = {row[0]: row[1] for row in existing_ids_result.all()}

            # Class groups that have a Discord role mapped
            cg_result = await session.execute(
                select(ClassGroup).where(
                    and_(ClassGroup.active.is_(True), ClassGroup.discord_role_id.isnot(None))
                )
            )
            role_mapped_groups: list[ClassGroup] = list(cg_result.scalars().all())

            # Existing memberships (user_id → set of class_group_ids)
            mem_result = await session.execute(
                select(ClassGroupMember.user_id, ClassGroupMember.class_group_id)
                .where(ClassGroupMember.active.is_(True))
            )
            enrolled_pairs: set[tuple[int, int]] = {(r[0], r[1]) for r in mem_result.all()}

            created = 0
            enrolled = 0

            # Accumulate the portal API payload while scanning
            portal_members: list[dict] = []

            for member in guild.members:
                if member.bot:
                    continue

                portal_role = self._detect_portal_role(member)

                # ── Ensure a User record exists ───────────────────────────
                if member.id not in discord_to_db_id:
                    db_user = User(
                        discord_user_id=member.id,
                        full_name=member.display_name,
                        role=portal_role,
                        active=True,
                    )
                    session.add(db_user)
                    await session.flush()
                    discord_to_db_id[member.id] = db_user.id
                    created += 1
                    log.debug("Provisional record created for %s (%d) as %s.", member, member.id, portal_role.value)
                else:
                    # Update role in the local DB too
                    await session.execute(
                        # Use raw update to avoid re-fetching each user
                        select(User).where(User.discord_user_id == member.id)
                    )

                # Build portal payload entry
                avatar = str(member.display_avatar.url) if member.display_avatar else None
                portal_members.append({
                    "discord_user_id": str(member.id),
                    "full_name":       member.display_name,
                    "avatar_url":      avatar,
                    "role":            portal_role.value,
                })

                db_user_id = discord_to_db_id[member.id]
                member_role_ids = {r.id for r in member.roles}

                # ── Auto-enrol based on Discord roles ─────────────────────
                for group in role_mapped_groups:
                    if group.discord_role_id not in member_role_ids:
                        continue
                    if (db_user_id, group.id) in enrolled_pairs:
                        continue

                    # Determine enrolment role: tutor if they are the group's tutor
                    enrol_as = MemberRole.tutor if group.tutor_user_id == db_user_id else MemberRole.student

                    membership = ClassGroupMember(
                        class_group_id=group.id,
                        user_id=db_user_id,
                        role=enrol_as,
                        active=True,
                    )
                    session.add(membership)
                    enrolled_pairs.add((db_user_id, group.id))
                    enrolled += 1
                    log.info(
                        "Auto-enrolled %s (%d) into class group %r (%d) via Discord role %d.",
                        member, member.id, group.name, group.id, group.discord_role_id,
                    )

            if created or enrolled:
                await session.flush()

        # ── Push snapshot to portal API (fire-and-forget with error logging) ──
        if portal_members and config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
            started = datetime.now(timezone.utc).isoformat()
            try:
                async with PortalAPIClient() as client:
                    result = await client.sync_members(portal_members)
                    completed = datetime.now(timezone.utc).isoformat()
                    log.info(
                        "Portal member sync complete: %d synced, %d created, %d updated.",
                        result.get("synced", 0),
                        result.get("created", 0),
                        result.get("updated", 0),
                    )
                    await client.push_sync_log(
                        sync_type="members",
                        status="success",
                        started_at=started,
                        completed_at=completed,
                        records_created=result.get("created", 0),
                        records_updated=result.get("updated", 0),
                    )
            except PortalAPIError as exc:
                log.error("Portal member sync failed: %s", exc)
                async with PortalAPIClient() as client:
                    await client.push_sync_log(
                        sync_type="members",
                        status="failed",
                        started_at=started,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                        error_message=str(exc),
                    )
            except Exception as exc:
                log.exception("Unexpected error pushing members to portal API.")
                try:
                    async with PortalAPIClient() as client:
                        await client.push_sync_log(
                            sync_type="members",
                            status="failed",
                            started_at=started,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            error_message=str(exc),
                        )
                except Exception:
                    pass
        elif not config.DASHBOARD_API_KEY or not config.PORTAL_API_URL:
            log.warning(
                "Member sync skipped for portal — DASHBOARD_API_KEY or PORTAL_API_URL not set. "
                "Add both to the OCI .env and restart the bot."
            )

        return created, enrolled

    # ── on_member_join ────────────────────────────────────────────────────── #

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Capture a provisional record the instant someone joins."""
        if member.bot:
            return

        portal_role = self._detect_portal_role(member)

        async with get_session() as session:
            existing = await session.execute(
                select(User).where(User.discord_user_id == member.id)
            )
            db_user = existing.scalar_one_or_none()

            if db_user:
                log.info("Known member %s (%d) rejoined.", member, member.id)
                return  # Already captured — nothing to do

            db_user = User(
                discord_user_id=member.id,
                full_name=member.display_name,
                role=portal_role,
                active=True,
            )
            session.add(db_user)
            await session.flush()
            log.info(
                "Provisional record created for new member %s (%d) as %s.",
                member, member.id, portal_role.value,
            )

        # Push to portal API
        if config.DASHBOARD_API_KEY and config.PORTAL_API_URL:
            try:
                avatar = str(member.display_avatar.url) if member.display_avatar else None
                async with PortalAPIClient() as client:
                    await client.sync_members([{
                        "discord_user_id": str(member.id),
                        "full_name":       member.display_name,
                        "avatar_url":      avatar,
                        "role":            portal_role.value,
                    }])
            except Exception:
                log.exception("Could not push new member %s to portal API.", member.id)

        await self._notify_admin(
            member.guild,
            title="👋  New Member Joined",
            description=(
                f"{member.mention} (**{member.display_name}**) just joined the server.\n"
                f"Their Discord ID has been captured automatically.\n\n"
                f"Assign a Discord class role to enrol them automatically, "
                f"or use `/enrol` to do it manually."
            ),
            colour=discord.Colour.green(),
            footer=f"Discord ID: {member.id}",
            thumbnail=member.display_avatar.url,
        )

    # ── on_member_update ─────────────────────────────────────────────────── #

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """When a Discord role is added to a member, auto-enrol them into the matching class group."""
        if after.bot:
            return

        added_role_ids = {r.id for r in after.roles} - {r.id for r in before.roles}
        if not added_role_ids:
            return  # No roles were added

        async with get_session() as session:
            # Find class groups whose discord_role_id matches any newly added role
            cg_result = await session.execute(
                select(ClassGroup).where(
                    and_(
                        ClassGroup.active.is_(True),
                        ClassGroup.discord_role_id.in_(added_role_ids),
                    )
                )
            )
            matching_groups: list[ClassGroup] = list(cg_result.scalars().all())
            if not matching_groups:
                return

            # Ensure the user record exists
            user_result = await session.execute(
                select(User).where(User.discord_user_id == after.id)
            )
            db_user = user_result.scalar_one_or_none()
            if not db_user:
                db_user = User(
                    discord_user_id=after.id,
                    full_name=after.display_name,
                    role=UserRole.student,
                    active=True,
                )
                session.add(db_user)
                await session.flush()
                log.info("Provisional record created for %s (%d) during role update.", after, after.id)

            newly_enrolled: list[ClassGroup] = []

            for group in matching_groups:
                # Check for an existing membership (active or inactive)
                mem_result = await session.execute(
                    select(ClassGroupMember).where(
                        and_(
                            ClassGroupMember.class_group_id == group.id,
                            ClassGroupMember.user_id == db_user.id,
                        )
                    )
                )
                membership = mem_result.scalar_one_or_none()

                if membership:
                    if membership.active:
                        continue  # Already enrolled
                    membership.active = True  # Reactivate
                else:
                    enrol_as = MemberRole.tutor if group.tutor_user_id == db_user.id else MemberRole.student
                    membership = ClassGroupMember(
                        class_group_id=group.id,
                        user_id=db_user.id,
                        role=enrol_as,
                        active=True,
                    )
                    session.add(membership)

                newly_enrolled.append(group)
                log.info(
                    "Auto-enrolled %s (%d) into %r via role assignment.",
                    after, after.id, group.name,
                )

            if newly_enrolled:
                await session.flush()

        if newly_enrolled:
            class_list = "\n".join(f"• **{g.name}**" for g in newly_enrolled)
            await self._notify_admin(
                after.guild,
                title="🎓  Auto-Enrolment",
                description=(
                    f"{after.mention} (**{after.display_name}**) was automatically enrolled:\n\n"
                    f"{class_list}\n\n"
                    f"Triggered by Discord role assignment."
                ),
                colour=discord.Colour.blue(),
                footer=f"Discord ID: {after.id}",
                thumbnail=after.display_avatar.url,
            )

    # ── Admin notification helper ─────────────────────────────────────────── #

    async def _notify_admin(
        self,
        guild: discord.Guild,
        title: str,
        description: str,
        colour: discord.Colour,
        footer: str = "",
        thumbnail: str | None = None,
    ) -> None:
        channel_id = config.ADMIN_NOTIFICATION_CHANNEL_ID
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(title=title, description=description, colour=colour)
        if footer:
            embed.set_footer(text=footer)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Could not send admin notification: %s", exc)

    # ── /link_class_role ──────────────────────────────────────────────────── #

    @app_commands.command(
        name="link_class_role",
        description="Map a Discord role to a class group so members are auto-enrolled when the role is assigned.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        discord_role="The Discord role to map (e.g. 'Year 11 Advanced')",
        class_group_id="Class group ID to link it to",
    )
    @app_commands.default_permissions(administrator=True)
    async def link_class_role(
        self,
        interaction: Interaction,
        discord_role: discord.Role,
        class_group_id: int,
    ) -> None:
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
            group.discord_role_id = discord_role.id

        embed = discord.Embed(
            title="🔗  Class Role Linked",
            description=(
                f"**{discord_role.name}** → **{group.name}**\n\n"
                f"From now on, any member assigned the **{discord_role.name}** role "
                f"will be automatically enrolled in **{group.name}**.\n\n"
                f"Run `/scan_members` or wait for the next sync (every 6 hours) to "
                f"catch existing members who already have this role."
            ),
            colour=discord.Colour.green(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Immediately sync existing members who already hold this role
        guild = interaction.guild
        async with get_session() as session:
            # Re-fetch group with fresh session
            grp_result = await session.execute(
                select(ClassGroup).where(ClassGroup.id == class_group_id)
            )
            group = grp_result.scalar_one_or_none()

            enrolled_now = 0
            for member in guild.members:
                if member.bot:
                    continue
                if discord_role not in member.roles:
                    continue

                user_result = await session.execute(
                    select(User).where(User.discord_user_id == member.id)
                )
                db_user = user_result.scalar_one_or_none()
                if not db_user:
                    db_user = User(
                        discord_user_id=member.id,
                        full_name=member.display_name,
                        role=UserRole.student,
                        active=True,
                    )
                    session.add(db_user)
                    await session.flush()

                mem_result = await session.execute(
                    select(ClassGroupMember).where(
                        and_(
                            ClassGroupMember.class_group_id == group.id,
                            ClassGroupMember.user_id == db_user.id,
                        )
                    )
                )
                membership = mem_result.scalar_one_or_none()
                if membership:
                    if not membership.active:
                        membership.active = True
                        enrolled_now += 1
                else:
                    session.add(ClassGroupMember(
                        class_group_id=group.id,
                        user_id=db_user.id,
                        role=MemberRole.student,
                        active=True,
                    ))
                    enrolled_now += 1

            if enrolled_now:
                await session.flush()

        if enrolled_now:
            await interaction.followup.send(
                f"✅ Immediately enrolled **{enrolled_now}** existing member(s) who already have "
                f"the **{discord_role.name}** role.",
                ephemeral=True,
            )

    # ── /enrol (manual override) ──────────────────────────────────────────── #

    @app_commands.command(
        name="enrol",
        description="Manually link a member to Ryze Education and assign them to a class.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        discord_user="The Discord member to enrol",
        class_group_id="Class group ID to assign them to",
        full_name="Full name (uses Discord display name if omitted)",
        role="Class role — Student or Tutor (default: Student)",
        class_role="Optional Discord year-level / subject role to assign",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Student", value="student"),
        app_commands.Choice(name="Tutor",   value="tutor"),
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
    async def enrol(
        self,
        interaction: Interaction,
        discord_user: discord.Member,
        class_group_id: int,
        full_name: str | None = None,
        role: str = "student",
        class_role: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            result = await session.execute(
                select(User).where(User.discord_user_id == discord_user.id)
            )
            db_user = result.scalar_one_or_none()

            if db_user:
                action = "Updated"
                if full_name:
                    db_user.full_name = full_name
                db_user.role = UserRole(role)
                db_user.active = True
            else:
                db_user = User(
                    discord_user_id=discord_user.id,
                    full_name=full_name or discord_user.display_name,
                    role=UserRole(role),
                    active=True,
                )
                session.add(db_user)
                await session.flush()
                action = "Created"

            resolved_name = db_user.full_name

            grp_result = await session.execute(
                select(ClassGroup).where(ClassGroup.id == class_group_id)
            )
            group = grp_result.scalar_one_or_none()
            if not group:
                await interaction.followup.send(
                    f"No class group found with ID `{class_group_id}`.", ephemeral=True
                )
                return

            mem_result = await session.execute(
                select(ClassGroupMember).where(
                    and_(
                        ClassGroupMember.class_group_id == class_group_id,
                        ClassGroupMember.user_id == db_user.id,
                    )
                )
            )
            membership = mem_result.scalar_one_or_none()
            member_role = MemberRole.tutor if role == "tutor" else MemberRole.student

            if membership:
                membership.role = member_role
                membership.active = True
                enrol_action = "Re-enrolled"
            else:
                session.add(ClassGroupMember(
                    class_group_id=class_group_id,
                    user_id=db_user.id,
                    role=member_role,
                    active=True,
                ))
                enrol_action = "Enrolled"

        lines: list[str] = [
            f"✅ **{action}** user record — **{resolved_name}** ({role.title()}).",
            f"✅ **{enrol_action}** in **{group.name}**.",
            await _set_nickname(discord_user, resolved_name),
        ]
        base_role_name = _BASE_ROLE_MAP.get(role)
        if base_role_name:
            lines.append(await _assign_discord_role(interaction.guild, discord_user, base_role_name))
        if class_role:
            lines.append(await _assign_discord_role(interaction.guild, discord_user, class_role))

        embed = discord.Embed(
            title="🎓  Enrolment Complete",
            description="\n".join(lines),
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Member", value=discord_user.mention, inline=True)
        embed.add_field(name="Class",  value=group.name, inline=True)
        embed.add_field(name="Role",   value=role.title(), inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unenrol ─────────────────────────────────────────────────────────── #

    @app_commands.command(
        name="unenrol",
        description="Remove a member from a class group.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        discord_user="The Discord member to remove",
        class_group_id="Class group ID to remove them from",
    )
    @app_commands.default_permissions(administrator=True)
    async def unenrol(
        self,
        interaction: Interaction,
        discord_user: discord.Member,
        class_group_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            user_result = await session.execute(
                select(User).where(User.discord_user_id == discord_user.id)
            )
            db_user = user_result.scalar_one_or_none()
            if not db_user:
                await interaction.followup.send(
                    f"{discord_user.mention} has no linked record.", ephemeral=True
                )
                return

            grp_result = await session.execute(
                select(ClassGroup).where(ClassGroup.id == class_group_id)
            )
            group = grp_result.scalar_one_or_none()
            if not group:
                await interaction.followup.send(
                    f"No class group found with ID `{class_group_id}`.", ephemeral=True
                )
                return

            mem_result = await session.execute(
                select(ClassGroupMember).where(
                    and_(
                        ClassGroupMember.class_group_id == class_group_id,
                        ClassGroupMember.user_id == db_user.id,
                        ClassGroupMember.active.is_(True),
                    )
                )
            )
            membership = mem_result.scalar_one_or_none()
            if not membership:
                await interaction.followup.send(
                    f"{discord_user.mention} is not actively enrolled in **{group.name}**.",
                    ephemeral=True,
                )
                return

            membership.active = False

        await interaction.followup.send(
            f"✅ Removed {discord_user.mention} from **{group.name}**.", ephemeral=True
        )

    # ── /pending_members ──────────────────────────────────────────────────── #

    @app_commands.command(
        name="pending_members",
        description="List members who have no class assignment yet.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def pending_members(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            enrolled_subq = (
                select(ClassGroupMember.user_id)
                .where(ClassGroupMember.active.is_(True))
                .scalar_subquery()
            )
            result = await session.execute(
                select(User)
                .where(and_(User.active.is_(True), User.id.not_in(enrolled_subq)))
                .order_by(User.full_name)
            )
            pending = list(result.scalars().all())

        if not pending:
            embed = discord.Embed(
                title="✅  No Pending Members",
                description="Every linked member is assigned to at least one class.",
                colour=discord.Colour.green(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        lines = [f"• {u.full_name}  <@{u.discord_user_id}>" for u in pending]
        embed = discord.Embed(
            title=f"⏳  Pending Members ({len(pending)})",
            description=(
                "These members are captured but not yet assigned to a class.\n"
                "Assign them a linked Discord role for **automatic enrolment**, "
                "or use `/enrol` to assign manually.\n\n"
                + "\n".join(lines)
            ),
            colour=discord.Colour.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /scan_members (manual trigger) ───────────────────────────────────── #

    @app_commands.command(
        name="scan_members",
        description="Manually trigger a member sync (normally runs automatically every 6 hours).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def scan_members(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        created, enrolled = await self._sync_all_members(interaction.guild)

        embed = discord.Embed(
            title="🔍  Member Sync Complete",
            colour=discord.Colour.blue(),
        )
        embed.add_field(name="✅  New records", value=str(created), inline=True)
        embed.add_field(name="🎓  Auto-enrolments", value=str(enrolled), inline=True)
        embed.description = (
            "All human members have been scanned.\n"
            "Run `/pending_members` to see who still needs a class assignment."
            if created or enrolled
            else "Everything is already up to date."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MembersCog(bot))
