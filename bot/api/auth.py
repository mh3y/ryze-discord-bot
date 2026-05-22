"""Authentication for the Ryze Education portal API.

Two auth methods:
  1. Discord OAuth2 — for students, tutors, and admins.
     They already have Discord accounts (required to attend class).
     Flow: frontend redirects to Discord → Discord redirects back with code
           → backend exchanges code for access token → gets Discord user ID
           → looks up User in DB → issues JWT

  2. Email + password — for parents only.
     Parents don't necessarily use Discord.
     Admin creates a ParentProfile and sends an invite email.
     Parent sets password → can log in with email/password → gets JWT

JWT tokens are returned to the frontend and sent as Bearer tokens on
subsequent API requests.

The old static X-API-Key header is kept for bot-to-api server-to-server
calls (health, sync, etc.) and is not used for portal user sessions.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.api.deps import get_db
from bot import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_USE_A_REAL_SECRET")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 24 hours

DISCORD_CLIENT_ID: str = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET: str = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI: str = os.getenv(
    "DISCORD_REDIRECT_URI", "http://localhost:5173/auth/discord/callback"
)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/parent/login", auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# Discord OAuth helpers
# ---------------------------------------------------------------------------

DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"


async def exchange_discord_code(code: str) -> dict:
    """Exchange an OAuth2 code for a Discord access token + user info."""
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        user_resp = await client.get(
            DISCORD_USER_URL,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        user_resp.raise_for_status()
        return user_resp.json()


# ---------------------------------------------------------------------------
# Current-user dependency — used by protected endpoints
# ---------------------------------------------------------------------------

class AuthUser:
    """Resolved authenticated user, attached to request state."""

    def __init__(
        self,
        *,
        user_id: Optional[int] = None,
        parent_profile_id: Optional[int] = None,
        discord_user_id: Optional[int] = None,
        role: str,
        email: Optional[str] = None,
        name: str,
    ):
        self.user_id = user_id                        # users.id (Discord users)
        self.parent_profile_id = parent_profile_id    # parent_profiles.id
        self.discord_user_id = discord_user_id
        self.role = role
        self.email = email
        self.name = name

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_tutor(self) -> bool:
        return self.role == "tutor"

    @property
    def is_student(self) -> bool:
        return self.role == "student"

    @property
    def is_parent(self) -> bool:
        return self.role == "parent"


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    api_key: Optional[str] = Depends(api_key_header),
    session: AsyncSession = Depends(get_db),
) -> AuthUser:
    """Resolve the current user from a JWT Bearer token.

    Also accepts the static X-API-Key for server-to-server bot calls
    (returns a synthetic admin AuthUser in that case).
    """
    # ── Static API key (bot → API server-to-server) ──────────────────── #
    if api_key and api_key == getattr(config, "DASHBOARD_API_KEY", None):
        return AuthUser(role="admin", name="BotService")

    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc

    try:
        payload = decode_token(token)
        sub: str = payload.get("sub", "")
        role: str = payload.get("role", "")
        if not sub or not role:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    # ── Parent (email auth) ───────────────────────────────────────────── #
    if role == "parent":
        from bot.models.portal_auth import ParentProfile
        parent_id = int(sub.removeprefix("parent:"))
        result = await session.execute(
            select(ParentProfile).where(ParentProfile.id == parent_id, ParentProfile.active.is_(True))
        )
        parent = result.scalar_one_or_none()
        if not parent:
            raise credentials_exc
        return AuthUser(
            parent_profile_id=parent.id,
            role="parent",
            email=parent.email,
            name=parent.full_name,
        )

    # ── Discord user (student / tutor / admin) ────────────────────────── #
    from bot.models.user import User
    user_id = int(sub.removeprefix("user:"))
    result = await session.execute(
        select(User).where(User.id == user_id, User.active.is_(True))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise credentials_exc
    return AuthUser(
        user_id=user.id,
        discord_user_id=user.discord_user_id,
        role=user.role.value,
        email=user.email,
        name=user.full_name,
    )


async def require_admin(current: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not current.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return current


async def require_admin_or_tutor(current: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not (current.is_admin or current.is_tutor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tutor or admin access required.")
    return current


async def require_parent(current: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not current.is_parent:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Parent access required.")
    return current


# Legacy: kept for bot-to-api server calls on existing read routes
async def require_api_key(api_key: Optional[str] = Depends(api_key_header)) -> str:
    expected = getattr(config, "DASHBOARD_API_KEY", None)
    if not expected or api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )
    return api_key
