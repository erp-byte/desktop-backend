"""Pydantic models for the documented end-user auth API.

Shapes mirror the spec exactly. IDs are strings in responses (the spec
example uses UUIDs); we cast int PKs to strings at the boundary to keep
clients type-stable for an eventual UUID migration.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── /login ────────────────────────────────────────────────────────────────


class DeviceInfo(BaseModel):
    model_config = ConfigDict(extra="allow")  # forward-compatible
    device_id: str | None = None
    device_name: str | None = None
    app_version: str | None = None
    platform: str | None = None


class LoginRequest(BaseModel):
    phone: str = Field(min_length=1)
    password: str = Field(min_length=1)
    device_info: DeviceInfo | None = None


class RoleOut(BaseModel):
    role_id: str
    code: str
    label: str
    is_admin: bool


class UserOut(BaseModel):
    user_id: str
    phone: str
    full_name: str | None = None
    email: str | None = None
    is_admin: bool
    roles: list[RoleOut]
    # User-level scope (same shape as MeResponse). Surfaced here so the
    # client can render factory/floor-aware UI without a follow-up /me call.
    # Empty list = "no restriction at the user level"; intersect with
    # permission-row scope server-side for the real gate.
    entities:   list[str] = []
    warehouses: list[str] = []
    floors:     list[str] = []


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_expires_in: int
    must_change_password: bool
    user: UserOut


# ── /refresh ──────────────────────────────────────────────────────────────


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_expires_in: int


# ── /logout ───────────────────────────────────────────────────────────────


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


# ── /logout-all ───────────────────────────────────────────────────────────


class LogoutAllResponse(BaseModel):
    revoked_count: int


# ── /me ───────────────────────────────────────────────────────────────────


class PermissionOut(BaseModel):
    module: str
    sub_module: str | None = None
    action: str


class MeResponse(BaseModel):
    user_id: str
    phone: str
    full_name: str | None = None
    email: str | None = None
    status: str
    must_change_password: bool
    is_admin: bool
    roles: list[RoleOut]
    permissions: list[PermissionOut]
    entities: list[str]
    warehouses: list[str]
    floors: list[str]
    last_login_at: str | None = None
    password_changed_at: str | None = None


# ── /password/change ──────────────────────────────────────────────────────


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)
    confirm_password: str = Field(min_length=1)


class ChangePasswordResponse(BaseModel):
    message: str
    revoked_count: int


# ── admin reset-password ─────────────────────────────────────────────────


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)


class AdminResetPasswordResponse(BaseModel):
    user_id: str
    message: str
    revoked_count: int
    temp_password_set: bool


# ── /password/reset (OTP-based) ──────────────────────────────────────────


class SendResetOtpRequest(BaseModel):
    """Step 1: ask the server to deliver an OTP to the registered WhatsApp
    number for `phone`. We intentionally do NOT reveal whether `phone` is
    registered; the endpoint returns 200 either way and the user only learns
    the answer if their phone actually pings.
    """
    phone: str = Field(min_length=1, max_length=32)


class SendResetOtpResponse(BaseModel):
    # Generic envelope — see the docstring above on why we don't leak
    # registration status. `expires_in_seconds` is fixed at the configured
    # TTL so the UI can show a countdown without an extra round-trip.
    message: str
    expires_in_seconds: int


class VerifyResetOtpRequest(BaseModel):
    """Step 2: present the OTP + the new password. The server matches by
    (phone, otp), rotates the password, and erases the OTP row.

    `new_password` carries the value verbatim — no length/strength rules,
    per the 2026-05-30 product decision. The pydantic constraint here is
    just a sanity cap so an attacker can't shovel megabytes of garbage.
    """
    phone: str = Field(min_length=1, max_length=32)
    otp: str = Field(min_length=4, max_length=12)
    new_password: str = Field(min_length=1, max_length=256)


class VerifyResetOtpResponse(BaseModel):
    message: str
    revoked_count: int


# ── /sessions ─────────────────────────────────────────────────────────────


class SessionOut(BaseModel):
    token_id: str
    token_type: str = "refresh"
    issued_at: str
    expires_at: str
    ip: str | None = None
    user_agent: str | None = None
    device_info: dict[str, Any] | None = None
    is_current: bool


class SessionsResponse(BaseModel):
    sessions: list[SessionOut]
    total: int
