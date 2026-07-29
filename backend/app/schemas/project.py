"""Projects schemas (Phase 3)."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    color: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str | None = None
    color: str
    created_at: datetime
    updated_at: datetime
