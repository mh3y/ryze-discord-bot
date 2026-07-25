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
from bot.utils.task_safety import install_loop_restart

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
        install_loop_restart(self._sync_loop, "members.sync", self.bot)
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
        Classify a Discord member by EXPLICITLY CONFIGURED ROLE IDs, never by
        role name. [DEF-16] A role literally named "Admin" is creatable by anyone
        with Manage Roles and must never grant CRM privileges; the configured IDs
        (STAFF_ROLE_IDS / TUTOR_ROLE_IDS / PARENT_ROLE_IDS env vars) are pinned
        by the owner and cannot be minted from inside the guild.

        Priority: staff > tutor > parent > student. When no IDs are configured
        this returns student for everyone (fail closed) and _sync_all_members
        additionally refuses to push ANY role data to the CRM.
        """
        member_role_ids = {r.id for r in member.roles}
        if member_role_ids & config.STAFF_ROLE_IDS:
            return UserRole.admin
        if member_role_ids & config.TUTOR_ROLE_IDS:
            return UserRole.tutor
        if member_role_ids & config.PARENT_ROLE_IDS:
            return UserRole.parent
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
        # Fail-closed role handling [DEF-16]: with no configured staff/tutor role
        # IDs the bot must not write ANY role data to the CRM — a wrong guess
        # could escalate a member or demote a real admin. Local records and class
        # enrolment still run; only the portal push is withheld.
        classification_on = config.role_classification_configured()
        if not classification_on:
            log.warning(
                "[members] STAFF_ROLE_IDS / TUTOR_ROLE_IDS are not configured — "
                "FAIL-CLOSED: no member data will be pushed to the CRM this sync, so any "
                "student who joined via Discord will be INVISIBLE to the CRM and their "
                "voice attendance cannot be matched to a student. Set the role-ID env "
                "vars (see REHOST.md) to enable the portal member sync."
            )
        else:
            # Partial configuration is a fail-OPEN hazard: if a tier's role IDs are
            # missing, its holders fall through to 'student' and can be mis-filed in
            # the CRM. Make the partial state loud. [review: partial role IDs push parents as students]
            if not config.PARENT_ROLE_IDS:
                log.warning(
                    "[members] PARENT_ROLE_IDS is not configured while classification is ON — "
                    "a parent with no existing local record cannot be distinguished from a "
                    "student and would be pushed to the CRM as a student. Set PARENT_ROLE_IDS "
                    "(see REHOST.md)."
                )
            if not config.TUTOR_ROLE_IDS:
                log.warning(
                    "[members] TUTOR_ROLE_IDS is not configured while classification is ON — "
                    "tutors cannot be distinguished from students. Set TUTOR_ROLE_IDS (see REHOST.md)."
                )

        async with get_session() as session:
            # ── Fetch existing state in bulk ──────────────────────────────
            existing_ids_result = await session.execute(
                select(User.discord_user_id, User.id, User.role, User.active)
            )
            discord_to_db_id: dict[int, int] = {}
            local_role_by_discord_id: dict[int, UserRole] = {}
            local_active_by_discord_id: dict[int, bool] = {}
            for row in existing_ids_result.all():
                discord_to_db_id[row[0]] = row[1]
                local_role_by_discord_id[row[0]] = row[2]
                local_active_by_discord_id[row[0]] = row[3]

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
                local_role = local_role_by_discord_id.get(member.id)
                user_is_active = local_active_by_discord_id.get(member.id, True)

                # NEVER-DEMOTE-VIA-INFERENCE GUARD [DEF-16]: the bot must not
                # rewrite a CRM-classified non-student down to a lower role just
                # because the Discord role that identifies their tier is missing
                # (dropped, or its *_ROLE_IDS env var unset). Demotions belong in
                # the CRM, not in Discord role drift. Cases held:
                #  • admin → anything else (strongest protection, always on)
                #  • tutor → student when TUTOR_ROLE_IDS is unset
                #  • parent → student when PARENT_ROLE_IDS is unset
                # Held members keep their local role, are omitted from the push,
                # and a human is told.
                demote_hold = False
                hold_detail = ""
                if local_role == UserRole.admin and portal_role != UserRole.admin:
                    demote_hold, hold_detail = True, "admin (no configured staff role)"
                elif (local_role == UserRole.tutor and portal_role == UserRole.student
                        and not config.TUTOR_ROLE_IDS):
                    demote_hold, hold_detail = True, "tutor (TUTOR_ROLE_IDS unset)"
                elif (local_role == UserRole.parent and portal_role == UserRole.student
                        and not config.PARENT_ROLE_IDS):
                    demote_hold, hold_detail = True, "parent (PARENT_ROLE_IDS unset)"
                # Only worth telling a human when classification is ON — in
                # fail-closed mode nothing is pushed for anyone, so the hold is
                # moot and its warning would just bury the fail-closed one above.
                # [review: admin_hold warning spams when classification unconfigured]
                if demote_hold and classification_on:
                    log.warning(
                        "[members] %s (%d) is %s locally but Discord roles now imply a "
                        "lower role — HOLDING (%s), not pushing a demotion. Resolve manually.",
                        member.display_name, member.id,
                        local_role.value if local_role else "?", hold_detail,
                    )

                # ── Ensure a User record exists / keep its role current ───
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
                    local_role_by_discord_id[member.id] = portal_role
                    created += 1
                    log.debug("Provisional record created for %s (%d) as %s.", member, member.id, portal_role.value)
                elif classification_on and not demote_hold and local_role != portal_role:
                    # Real local role update (the old code here was a dead no-op
                    # SELECT, so local roles drifted forever).
                    user_result = await session.execute(
                        select(User).where(User.discord_user_id == member.id)
                    )
                    db_user = user_result.scalar_one_or_none()
                    if db_user is not None:
                        db_user.role = portal_role
                        local_role_by_discord_id[member.id] = portal_role

                # ── Build the portal payload entry ────────────────────────
                # Withheld entirely when classification is unconfigured (fail
                # closed — the CRM writes `role` unconditionally, so any push
                # could demote a CRM-set admin or escalate a guessed staff).
                # Parents are never pushed as students [DEF-16]: the CRM wire
                # contract has no parent role, and mis-filing a parent as a
                # student corrupts the CRM's data classification.
                # A soft-deactivated user (via /delete_user) must not be re-pushed
                # to the CRM as a current member — the bot would silently resurrect
                # the account it just deactivated. [review: deactivated users still pushed]
                if classification_on and not demote_hold and portal_role != UserRole.parent and user_is_active:
                    avatar = str(member.display_avatar.url) if member.display_avatar else None
                    portal_members.append({
                        "discord_user_id": str(member.id),
                        "full_name":       member.display_name,
                        "avatar_url":      avatar,
                        "role":            portal_role.value,
                    })

                db_user_id = discord_to_db_id[member.id]
                member_role_ids = {r.id for r in member.roles}

                # Do not silently re-enrol a soft-deactivated user just because
                # they still hold a mapped Discord role — that would reverse the
                # deactivation /delete_user just reported. [review: soft-deactivated user auto re-enrolled]
                if not user_is_active:
                    continue

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
                "Add both to the deploy env vars and restart the bot."
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

        # Push to portal API — same fail-closed rules as the bulk sync [DEF-16]:
        # no push at all when role classification is unconfigured, and parents
        # are never pushed as students.
        if (
            config.DASHBOARD_API_KEY
            and config.PORTAL_API_URL
            and config.role_classification_configured()
            and portal_role != UserRole.parent
        ):
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
        elif not config.role_classification_configured():
            log.warning(
                "[members] on_member_join: role IDs unconfigured — %s (%d) recorded "
                "locally only, NOT pushed to the CRM (fail-closed).",
                member.display_name, member.id,
            )

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

            # Do not re-enrol a soft-deactivated user on a role re-add — that would
            # reverse a deactivation an admin made via /delete_user. An existing
            # inactive record must be explicitly reactivated first. [review: soft-deactivated re-enrolled]
            if not db_user.active:
                log.info(
                    "[members] on_member_update: %s (%d) is deactivated locally — not "
                    "auto-enrolling on role change. Reactivate the user first if intended.",
                    after, after.id,
                )
                return

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
