"""Encrypted tenant-scoped MCP connector definitions (Task 9).

Public API:
  * :class:`~app.connectors.models.Connector` — the ORM model.
  * :class:`~app.connectors.service.ConnectorService` — encrypted CRUD +
    scope enforcement.
  * :mod:`app.connectors.catalog` — provider manifests.
"""
from app.connectors.models import Connector
from app.connectors.service import (
    ConnectorNotFoundError,
    ConnectorService,
    InsufficientScopesError,
)

__all__ = [
    "Connector",
    "ConnectorService",
    "ConnectorNotFoundError",
    "InsufficientScopesError",
]
