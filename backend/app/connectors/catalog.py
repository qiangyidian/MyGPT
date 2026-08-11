"""Provider catalog manifests for MCP connectors (Task 9).

Each manifest records how to reach a provider's MCP server and the **minimum**
OAuth scopes a tenant connector must hold before it can be enabled. The
:class:`~app.connectors.service.ConnectorService` consults this catalog to
snapshot the manifest onto a connector row and to enforce the minimum-scope
gate at enable time.

Manifests are deliberately versioned data (frozen dataclasses) — not live
configuration — so a connector's stored snapshot stays stable even if the
catalog is later edited.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderManifest:
    """One provider's MCP reachability + minimum OAuth scope contract."""

    name: str
    kind: str
    transport: str  # "stdio" | "http"
    command_or_url: str
    required_scopes: list[str] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "transport": self.transport,
            "command_or_url": self.command_or_url,
            "required_scopes": list(self.required_scopes),
            "description": self.description,
        }


# --------------------------------------------------------------------------- #
# The catalog. command_or_url uses the canonical public MCP server endpoint or
# the well-known npm package for stdio providers. required_scopes is the
# *minimum* set a tenant connector must present to be enabled.
# --------------------------------------------------------------------------- #
PROVIDER_CATALOG: dict[str, ProviderManifest] = {
    "github": ProviderManifest(
        name="GitHub",
        kind="github",
        transport="http",
        command_or_url="https://api.githubcopilot.com/mcp/",
        required_scopes=["repo", "gist"],
        description="GitHub repositories, issues, PRs, and code search.",
    ),
    "gmail": ProviderManifest(
        name="Gmail",
        kind="gmail",
        transport="stdio",
        command_or_url="npx -y @gworkspace/gmail-mcp",
        required_scopes=["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly"],
        description="Gmail message read/send.",
    ),
    "outlook_mail": ProviderManifest(
        name="Outlook Mail",
        kind="outlook_mail",
        transport="http",
        command_or_url="https://mcp.microsoft.com/outlook/mail",
        required_scopes=["Mail.Read", "Mail.Send"],
        description="Outlook mail read/send via Microsoft Graph.",
    ),
    "google_calendar": ProviderManifest(
        name="Google Calendar",
        kind="google_calendar",
        transport="stdio",
        command_or_url="npx -y @gworkspace/calendar-mcp",
        required_scopes=[
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.events",
        ],
        description="Google Calendar events and scheduling.",
    ),
    "outlook_calendar": ProviderManifest(
        name="Outlook Calendar",
        kind="outlook_calendar",
        transport="http",
        command_or_url="https://mcp.microsoft.com/outlook/calendar",
        required_scopes=["Calendars.Read", "Calendars.ReadWrite"],
        description="Outlook calendar via Microsoft Graph.",
    ),
    "slack": ProviderManifest(
        name="Slack",
        kind="slack",
        transport="http",
        command_or_url="https://mcp.slack.com/sse",
        required_scopes=["chat:write", "channels:read"],
        description="Slack messaging and channel reads.",
    ),
    "teams": ProviderManifest(
        name="Microsoft Teams",
        kind="teams",
        transport="http",
        command_or_url="https://mcp.microsoft.com/teams",
        required_scopes=["ChannelMessage.Read.All", "ChannelMessage.Send"],
        description="Microsoft Teams messaging via Graph.",
    ),
    "notion": ProviderManifest(
        name="Notion",
        kind="notion",
        transport="stdio",
        command_or_url="npx -y @notionhq/notion-mcp-server",
        required_scopes=["notion.pages.read", "notion.pages.write"],
        description="Notion pages and databases.",
    ),
    "drive": ProviderManifest(
        name="Google Drive",
        kind="drive",
        transport="stdio",
        command_or_url="npx -y @gworkspace/drive-mcp",
        required_scopes=[
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ],
        description="Google Drive file access.",
    ),
    "sharepoint": ProviderManifest(
        name="SharePoint",
        kind="sharepoint",
        transport="http",
        command_or_url="https://mcp.microsoft.com/sharepoint",
        required_scopes=["Sites.Read.All", "Files.Read.All"],
        description="SharePoint sites and files via Graph.",
    ),
    "box": ProviderManifest(
        name="Box",
        kind="box",
        transport="http",
        command_or_url="https://api.box.com/mcp",
        required_scopes=["box.readwrite", "box.content.read"],
        description="Box file storage.",
    ),
    "atlassian": ProviderManifest(
        name="Atlassian (Jira/Confluence)",
        kind="atlassian",
        transport="http",
        command_or_url="https://mcp.atlassian.com/v1/sse",
        required_scopes=["read:jira-work", "write:jira-work", "read:confluence-content"],
        description="Jira issues and Confluence pages.",
    ),
    "figma": ProviderManifest(
        name="Figma",
        kind="figma",
        transport="http",
        command_or_url="https://mcp.figma.com/sse",
        required_scopes=["file_content:read", "library_assets:read"],
        description="Figma design files and library assets.",
    ),
}


def get_manifest(provider: str) -> ProviderManifest | None:
    """Look up a manifest by catalog key (kind). None if unknown."""
    return PROVIDER_CATALOG.get(provider)
