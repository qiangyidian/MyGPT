"""Connector CRUD router (Task 9).

Tenant-scoped + audited: every route filters by the authenticated user, and
security-relevant actions (create / rotate / enable / disable / delete) emit
an :class:`~app.models.AuditEvent` so there's a compliance trail for every
change to a connector's credentials or enablement state.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.catalog import PROVIDER_CATALOG
from app.connectors.service import (
    ConnectorNotFoundError,
    ConnectorService,
    InsufficientScopesError,
)
from app.core.deps import get_current_user
from app.db import get_db
from app.models import User
from app.schemas import (
    ConnectorCreate,
    ConnectorOut,
    ConnectorRotate,
    ConnectorUpdate,
    ProviderManifestOut,
)
from app.services import audit_service

router = APIRouter(prefix="/api/connectors", tags=["connectors"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


@router.get("/providers", response_model=list[ProviderManifestOut])
async def list_providers(
    user: User = Depends(get_current_user),
) -> list[ProviderManifestOut]:
    """List the catalog of provider manifests (minimum OAuth scopes included)."""
    return [
        ProviderManifestOut(**m.to_dict()) for m in PROVIDER_CATALOG.values()
    ]


@router.get("", response_model=list[ConnectorOut])
async def list_connectors(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorOut]:
    svc = ConnectorService(db)
    items = await svc.list_for_user(user.id)
    return [ConnectorOut.model_validate(c) for c in items]


@router.post("", response_model=ConnectorOut, status_code=status.HTTP_201_CREATED)
async def create_connector(
    payload: ConnectorCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    svc = ConnectorService(db)
    try:
        conn = await svc.create(
            user_id=user.id,
            name=payload.name,
            provider=payload.provider,
            credentials=payload.credentials,
            oauth_scopes=payload.oauth_scopes,
            command_or_url=payload.command_or_url,
            transport=payload.transport,
            enabled=payload.enabled,
            extra=payload.extra,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except InsufficientScopesError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await db.commit()
    await db.refresh(conn)
    await audit_service.log(
        actor_id=user.id,
        action="connector:create",
        target=str(conn.id),
        detail={"provider": conn.provider, "name": conn.name, "enabled": conn.enabled},
    )
    return ConnectorOut.model_validate(conn)


@router.get("/{connector_id}", response_model=ConnectorOut)
async def get_connector(
    connector_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    svc = ConnectorService(db)
    conn = await svc.get_for_user(user.id, connector_id)
    if conn is None:
        raise HTTPException(NOT_FOUND, "Connector not found")
    return ConnectorOut.model_validate(conn)


@router.patch("/{connector_id}", response_model=ConnectorOut)
async def update_connector(
    connector_id: uuid.UUID,
    payload: ConnectorUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    svc = ConnectorService(db)
    try:
        conn = await svc.update(
            user.id,
            connector_id,
            name=payload.name,
            oauth_scopes=payload.oauth_scopes,
            extra=payload.extra,
        )
    except ConnectorNotFoundError:
        raise HTTPException(NOT_FOUND, "Connector not found")
    await db.commit()
    await db.refresh(conn)
    await audit_service.log(
        actor_id=user.id,
        action="connector:update",
        target=str(conn.id),
        detail={"fields": list(payload.model_dump(exclude_unset=True).keys())},
    )
    return ConnectorOut.model_validate(conn)


@router.post("/{connector_id}/rotate", response_model=ConnectorOut)
async def rotate_connector(
    connector_id: uuid.UUID,
    payload: ConnectorRotate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    svc = ConnectorService(db)
    try:
        conn = await svc.rotate_credentials(user.id, connector_id, payload.credentials)
    except ConnectorNotFoundError:
        raise HTTPException(NOT_FOUND, "Connector not found")
    await db.commit()
    await db.refresh(conn)
    await audit_service.log(
        actor_id=user.id,
        action="connector:rotate",
        target=str(conn.id),
        detail={"provider": conn.provider},
    )
    return ConnectorOut.model_validate(conn)


@router.post("/{connector_id}/activate", response_model=ConnectorOut)
async def activate_connector(
    connector_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    svc = ConnectorService(db)
    try:
        conn = await svc.enable(user.id, connector_id)
    except ConnectorNotFoundError:
        raise HTTPException(NOT_FOUND, "Connector not found")
    except InsufficientScopesError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await db.commit()
    await db.refresh(conn)
    await audit_service.log(
        actor_id=user.id,
        action="connector:enable",
        target=str(conn.id),
        detail={"provider": conn.provider},
    )
    return ConnectorOut.model_validate(conn)


@router.post("/{connector_id}/deactivate", response_model=ConnectorOut)
async def deactivate_connector(
    connector_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    svc = ConnectorService(db)
    try:
        conn = await svc.disable(user.id, connector_id)
    except ConnectorNotFoundError:
        raise HTTPException(NOT_FOUND, "Connector not found")
    await db.commit()
    await db.refresh(conn)
    await audit_service.log(
        actor_id=user.id,
        action="connector:disable",
        target=str(conn.id),
        detail={"provider": conn.provider},
    )
    return ConnectorOut.model_validate(conn)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(
    connector_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    svc = ConnectorService(db)
    await svc.delete(user.id, connector_id)
    await db.commit()
    await audit_service.log(
        actor_id=user.id,
        action="connector:delete",
        target=str(connector_id),
    )
