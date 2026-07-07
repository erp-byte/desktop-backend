"""Pydantic models for /api/v1/packing-details endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PackingDetailCreate(BaseModel):
    """POST body. `details` is a free-form JSON object (defaults to {})."""

    model_config = ConfigDict(extra="forbid")

    batch_code: str = Field(min_length=1)
    article_name: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class PackingDetailUpdate(BaseModel):
    """PATCH body — every field optional; only fields the client sends are
    written (the router uses ``model_dump(exclude_unset=True)`` to tell an
    omitted field from an explicit null). When `details` is present it REPLACES
    the stored JSON body wholesale (not a deep merge), matching the plain-column
    semantics of the other fields."""

    model_config = ConfigDict(extra="forbid")

    batch_code: str | None = Field(default=None, min_length=1)
    article_name: str | None = Field(default=None, min_length=1)
    details: dict[str, Any] | None = None


class PackingDetailOut(BaseModel):
    """Response shape for a single packing_details row."""

    model_config = ConfigDict(extra="ignore")

    packing_id: int
    batch_code: str
    article_name: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None
    created_at: str
    updated_at: str


# ── encrypted batch-token access ───────────────────────────────────────────


class BatchTokenMintRequest(BaseModel):
    """POST body for minting a batch token from a plaintext batch_code."""

    model_config = ConfigDict(extra="forbid")

    batch_code: str = Field(min_length=1)


class BatchTokenResponse(BaseModel):
    """The AES-256-GCM ciphertext the frontend stores and later sends back."""

    batch_token: str


class EncryptedBatchRequest(BaseModel):
    """POST body carrying ONLY the encrypted batch number."""

    model_config = ConfigDict(extra="forbid")

    batch_token: str = Field(min_length=1)
