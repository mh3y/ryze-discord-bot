"""Auth endpoints.

POST /auth/discord/callback   Discord OAuth2 code → JWT
GET  /auth/discord/url        Returns the Discord OAuth2 redirect URL
POST /auth/parent/login       Email + password login for parents
POST /auth/parent/set-password Complete invite (first-time password set)
GET  /auth/me                 Resolve current JWT → user info
POST /auth/logout             Client-side only (JWT is stateless); returns 200
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.auth import (
    AuthUser,
    create_access_token,
    exchange_discord_code,
    get_current_user,
    hash_password,
    verify_password,
    DISCORD_CLIENT_ID,
    DISCORD_REDIRECT_URI,
)
from bot.api.deps import get_db
from bot.models.user import User
from bot.models.portal_auth import ParentProfile, PortalCredential

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    name: str
    user_id: int | None = None
    parent_profile_id: int | None = None


class MeResponse(BaseModel):
    role: str
    name: str
    email: str | None
    user_id: int | None
    parent_profile_id: int | None
    discord_user_id: int | None


class ParentLoginRequest(BaseModel):
    email: EmailStr
    password: str


class SetPasswordRequest(BaseModel):
    invite_token: str
    new_password: str


class DiscordUrlResponse(BaseModel):
    url: str


# ---------------------------------------------------------------------------
# Discord OAuth
# ---------------------------------------------------------------------------

@router.get("/discord/url", response_model=DiscordUrlResponse)
async def discord_oauth_url():
    """Return the URL the frontend should redirect to for Discord login."""
    scopes = "identify email"
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scopes.replace(' ', '%20')}"
    )
    return DiscordUrlResponse(url=url)


@router.post("/discord/callback", response_model=TokenResponse)
async def discord_callback(
    code: str = Body(..., embed=True),
    session: AsyncSession = Depends(get_db),
):
    """Exchange a Discord OAuth2 code for a portal JWT.

    The frontend receives the code from Discord's redirect and POSTs it here.
    We exchange it for the user's Discord ID, look them up in the users table,
    and issue a JWT.
    """
    try:
        discord_user = await exchange_discord_code(code)
    except Exception as exc:
        log.warning("Discord OAuth exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to authenticate with Discord. Please try again.",
        )

    discord_user_id = int(discord_user["id"])
    result = await session.execute(
        select(User).where(User.discord_user_id == discord_user_id, User.active.is_(True))
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your Discord account is not registered with Ryze Education. "
                "Please contact your tutor or admin to get set up."
            ),
        )

    token = create_access_token({"sub": f"user:{user.id}", "role": user.role.value})
    return TokenResponse(
        access_token=token,
        role=user.role.value,
        name=user.full_name,
        user_id=user.id,
    )


# ---------------------------------------------------------------------------
# Parent email / password login
# ---------------------------------------------------------------------------

@router.post("/parent/login", response_model=TokenResponse)
async def parent_login(
    body: ParentLoginRequest,
    session: AsyncSession = Depends(get_db),
):
    """Email + password login for parents."""
    result = await session.execute(
        select(ParentProfile).where(
            ParentProfile.email == body.email.lower(),
            ParentProfile.active.is_(True),
        )
    )
    parent = result.scalar_one_or_none()

    if not parent:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    cred_result = await session.execute(
        select(PortalCredential).where(PortalCredential.parent_profile_id == parent.id)
    )
    cred = cred_result.scalar_one_or_none()

    if not cred or not cred.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account setup not complete. Please check your invite email.",
        )

    if not verify_password(body.password, cred.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # Update last login
    cred.last_login_at = datetime.now(timezone.utc)
    await session.flush()

    token = create_access_token({"sub": f"parent:{parent.id}", "role": "parent"})
    return TokenResponse(
        access_token=token,
        role="parent",
        name=parent.full_name,
        parent_profile_id=parent.id,
    )


@router.post("/parent/set-password", response_model=TokenResponse)
async def parent_set_password(
    body: SetPasswordRequest,
    session: AsyncSession = Depends(get_db),
):
    """Complete the invite flow — set password using the invite token."""
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters.",
        )

    result = await session.execute(
        select(PortalCredential).where(
            PortalCredential.invite_token == body.invite_token
        )
    )
    cred = result.scalar_one_or_none()

    if not cred:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invite token.")

    if cred.invite_expires_at and datetime.now(timezone.utc) > cred.invite_expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invite token has expired. Please ask admin to resend.",
        )

    if cred.invite_accepted_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invite has already been used.",
        )

    cred.password_hash = hash_password(body.new_password)
    cred.invite_accepted_at = datetime.now(timezone.utc)
    cred.invite_token = None          # consume the token
    cred.last_login_at = datetime.now(timezone.utc)
    await session.flush()

    parent_result = await session.execute(
        select(ParentProfile).where(ParentProfile.id == cred.parent_profile_id)
    )
    parent = parent_result.scalar_one()

    token = create_access_token({"sub": f"parent:{parent.id}", "role": "parent"})
    return TokenResponse(
        access_token=token,
        role="parent",
        name=parent.full_name,
        parent_profile_id=parent.id,
    )


# ---------------------------------------------------------------------------
# /me — resolve token to user info
# ---------------------------------------------------------------------------

@router.get("/me", response_model=MeResponse)
async def me(current: AuthUser = Depends(get_current_user)):
    """Return the currently authenticated user's info."""
    return MeResponse(
        role=current.role,
        name=current.name,
        email=current.email,
        user_id=current.user_id,
        parent_profile_id=current.parent_profile_id,
        discord_user_id=current.discord_user_id,
    )


@router.post("/logout")
async def logout():
    """JWT is stateless — logout is handled client-side by discarding the token."""
    return {"message": "Logged out. Please discard your token."}
