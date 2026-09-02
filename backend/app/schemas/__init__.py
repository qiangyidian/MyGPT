"""Pydantic schemas (request/response DTOs). Re-exported for convenient imports."""
from app.schemas.agent import (
    AgentRunOut,
    AgentStepOut,
    ApproveRequest,
    ActionResult,
    PlanStepIn,
    PlanUpdateRequest,
    RejectRequest,
    RunInstructionRequest,
    ToolApprovalOut,
    ToolCallAuditOut,
)
from app.schemas.admin import AuditLogOut, AdminUserUpdate, SystemStatus, UsageStat
from app.schemas.auth import (
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.schemas.connector import (
    ConnectorCreate,
    ConnectorOut,
    ConnectorRotate,
    ConnectorUpdate,
    ProviderManifestOut,
)
from app.schemas.chat import ChatRequest, Citation
from app.schemas.chat_attachment import ChatAttachmentOut, SaveToKbRequest
from app.schemas.common import ORMModel
from app.schemas.conversation import (
    ConversationBranchRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.document import DocumentOut, DocumentPreview, ReindexResult
from app.schemas.feedback import MessageFeedbackOut, MessageFeedbackRequest
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
)
from app.schemas.memory import MemoryOut, MemoryUpdate
from app.schemas.user_memory import (
    UserMemoryBulkAction,
    UserMemoryEdit,
    UserMemoryOut,
    UserMemoryPropose,
)
from app.schemas.message import MessageOut
from app.schemas.model_config import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelTestResult,
)
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from app.schemas.tool import (
    ToolInfo,
    ToolParameter,
    ToolTestRequest,
    ToolTestResult,
)

__all__ = [
    # common
    "ORMModel",
    # agent runs (Phase 3)
    "AgentRunOut", "AgentStepOut", "ToolApprovalOut", "ToolCallAuditOut",
    "ApproveRequest", "RejectRequest", "ActionResult",
    "PlanStepIn", "PlanUpdateRequest", "RunInstructionRequest",
    # auth
    "RegisterRequest", "LoginRequest", "TokenResponse", "RefreshResponse", "UserOut",
    # model config
    "ModelConfigCreate", "ModelConfigUpdate", "ModelConfigOut", "ModelTestResult",
    # conversation / message
    "ConversationCreate", "ConversationUpdate", "ConversationOut", "ConversationDetail",
    "ConversationBranchRequest",
    "MessageOut",
    # chat
    "ChatRequest", "Citation",
    # chat attachments + feedback (Phase 1)
    "ChatAttachmentOut", "SaveToKbRequest",
    "MessageFeedbackOut", "MessageFeedbackRequest",
    # projects + memories (Phase 3)
    "ProjectCreate", "ProjectUpdate", "ProjectOut",
    "MemoryOut", "MemoryUpdate",
    # Task 7: opt-in semantic user memory
    "UserMemoryOut", "UserMemoryPropose", "UserMemoryEdit", "UserMemoryBulkAction",
    # Task 9: MCP connectors
    "ConnectorCreate", "ConnectorUpdate", "ConnectorRotate", "ConnectorOut",
    "ProviderManifestOut",
    # knowledge base / documents
    "KnowledgeBaseCreate", "KnowledgeBaseOut",
    "DocumentOut", "DocumentPreview", "ReindexResult",
    # tools
    "ToolInfo", "ToolParameter", "ToolTestRequest", "ToolTestResult",
    # admin
    "AdminUserUpdate", "UsageStat", "SystemStatus", "AuditLogOut",
]
