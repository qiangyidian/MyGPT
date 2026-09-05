"""Connector request/response schemas (Task 9)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectorCreate(BaseModel):
    """Body for creating a connector. Credentials arrive in plaintext and are
    encrypted before persistence — the plaintext never touches the DB."""

    name: str = Field(..., min_length=1, max_length=128)
    provider: str
    credentials: dict[str, Any]
    oauth_scopes: list[str] = Field(default_factory=list)
    command_or_url: str | None = None
    transport: str | None = None
    enabled: bool = False
    extra: dict[str, Any] | None = None


class ConnectorUpdate(BaseModel):
    """Body for updating a connector's mutable, non-credential fields."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    oauth_scopes: list[str] | None = None
    extra: dict[str, Any] | None = None


class ConnectorRotate(BaseModel):
    """Body for rotating a connector's credentials."""

    credentials: dict[str, Any]


class ConnectorOut(BaseModel):
    """Connector response. NEVER includes plaintext credentials."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    provider: str
    manifest: dict[str, Any]
    transport: str
    command_or_url: str
    oauth_scopes: list[str]
    enabled: bool
    extra: dict[str, Any] | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProviderManifestOut(BaseModel):
    """A catalog manifest entry (read-only)."""

    name: str
    kind: str
    transport: str
    command_or_url: str
    required_scopes: list[str]
    description: str
