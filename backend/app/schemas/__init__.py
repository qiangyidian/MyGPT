"""Pydantic schemas (request/response DTOs). Re-exported for convenient imports."""
from app.schemas.agent import (
    AgentRunOut,
    AgentStepOut,
    ApproveRequest,
    ActionResult,
    RejectRequest,
    ToolApprovalOut,
)
from app.schemas.admin import AuditLogOut, AdminUserUpdate, SystemStatus, UsageStat
from app.schemas.auth import (
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.schemas.chat import (
    ChatMessage,
    ChatRequest,
    ChatRole,
    Citation,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.schemas.common import ErrorDetail, Ok, ORMModel, Page
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.document import DocumentOut, ReindexResult
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from app.schemas.message import MessageOut
from app.schemas.model_config import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelTestResult,
)
from app.schemas.tool import (
    ToolCallOut,
    ToolInfo,
    ToolParameter,
    ToolTestRequest,
    ToolTestResult,
)

__all__ = [
    # common
    "Ok", "ErrorDetail", "ORMModel", "Page",
    # agent runs (Phase 3)
    "AgentRunOut", "AgentStepOut", "ToolApprovalOut",
    "ApproveRequest", "RejectRequest", "ActionResult",
    # auth
    "RegisterRequest", "LoginRequest", "TokenResponse", "RefreshResponse", "UserOut",
    # model config
    "ModelConfigCreate", "ModelConfigUpdate", "ModelConfigOut", "ModelTestResult",
    # conversation / message
    "ConversationCreate", "ConversationUpdate", "ConversationOut", "ConversationDetail",
    "MessageOut",
    # chat
    "ChatRequest", "ChatMessage", "ChatRole", "Citation",
    "MetaEvent", "TokenEvent", "CitationEvent", "ToolCallEvent", "ToolResultEvent",
    "DoneEvent", "ErrorEvent",
    # knowledge base / documents
    "KnowledgeBaseCreate", "KnowledgeBaseUpdate", "KnowledgeBaseOut",
    "DocumentOut", "ReindexResult",
    # tools
    "ToolInfo", "ToolParameter", "ToolTestRequest", "ToolTestResult", "ToolCallOut",
    # admin
    "AdminUserUpdate", "UsageStat", "SystemStatus", "AuditLogOut",
]
