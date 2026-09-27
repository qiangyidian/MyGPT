"""Pydantic schemas (request/response DTOs). Re-exported for convenient imports."""
from app.schemas.admin import (
    AdminUserUpdate,
    AuditEventPage,
    AuditEventRow,
    AuditLogOut,
    FeatureFlagOut,
    FeatureFlagPage,
    SystemStatus,
    UsageMetrics,
    UsageReportPage,
    UsageReportRow,
    UsageStat,
)
from app.schemas.agent import (
    ActionResult,
    AgentRunOut,
    AgentStepOut,
    ApproveRequest,
    PlanStepIn,
    PlanUpdateRequest,
    RejectRequest,
    PlanGateRequest,
    RunInstructionRequest,
    ToolApprovalOut,
    ToolCallAuditOut,
)
from app.schemas.auth import (
    AuthMessageOut,
    ChangePasswordRequest,
    DeleteAccountRequest,
    EmailCodeRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserOut,
    WechatBindingOut,
    WechatCodeLoginRequest,
)
from app.schemas.chat import ChatRequest, Citation
from app.schemas.chat_attachment import ChatAttachmentOut, SaveToKbRequest
from app.schemas.common import ORMModel
from app.schemas.connector import (
    ConnectorCreate,
    ConnectorOut,
    ConnectorRotate,
    ConnectorUpdate,
    ProviderManifestOut,
)
from app.schemas.credit import (
    BatchStatusFilter,
    CreditAccountOut,
    CreditAccountRowOut,
    CreditAdjustRequest,
    LedgerEntryOut,
    LedgerPageOut,
    RedeemBatchCreate,
    RedeemBatchCreateOut,
    RedeemBatchOut,
    RedeemBatchProgressOut,
    RedeemCodeOut,
    RedeemRequest,
    RedeemResultOut,
    VoidBatchOut,
)
from app.schemas.conversation import (
    ConversationBranchRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
)
from app.schemas.document import (
    DocumentOut,
    DocumentPreview,
    ReindexResult,
    UploadCapabilities,
)
from app.schemas.feedback import MessageFeedbackOut, MessageFeedbackRequest
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseOut,
)
from app.schemas.memory import MemoryOut, MemoryUpdate
from app.schemas.message import MessageOut
from app.schemas.model_config import (
    ModelConfigCreate,
    ModelConfigOut,
    ModelConfigUpdate,
    ModelTestResult,
)
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from app.schemas.prompt_template import (
    PromptTemplateCreate,
    PromptTemplateOut,
    PromptTemplateUpdate,
)
from app.schemas.tool import (
    ToolInfo,
    ToolParameter,
    ToolTestRequest,
    ToolTestResult,
    ToolToggleRequest,
)
from app.schemas.user_memory import (
    UserMemoryBulkAction,
    UserMemoryEdit,
    UserMemoryOut,
    UserMemoryPropose,
)

__all__ = [
    # common
    "ORMModel",
    # agent runs (Phase 3)
    "AgentRunOut", "AgentStepOut", "ToolApprovalOut", "ToolCallAuditOut",
    "ApproveRequest", "RejectRequest", "ActionResult",
    "PlanGateRequest", "PlanStepIn", "PlanUpdateRequest", "RunInstructionRequest",
    # auth
    "RegisterRequest", "LoginRequest", "TokenResponse", "RefreshResponse", "UserOut",
    "WechatCodeLoginRequest", "WechatBindingOut",
    "DeleteAccountRequest",
    "EmailCodeRequest",
    "ChangePasswordRequest", "ForgotPasswordRequest", "ResetPasswordRequest",
    "AuthMessageOut",
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
    # 提示词库（个人模板 + 系统预置）
    "PromptTemplateCreate", "PromptTemplateUpdate", "PromptTemplateOut",
    # Task 7: opt-in semantic user memory
    "UserMemoryOut", "UserMemoryPropose", "UserMemoryEdit", "UserMemoryBulkAction",
    # Task 9: MCP connectors
    "ConnectorCreate", "ConnectorUpdate", "ConnectorRotate", "ConnectorOut",
    "ProviderManifestOut",
    # knowledge base / documents
    "KnowledgeBaseCreate", "KnowledgeBaseOut", "KnowledgeBaseUpdate",
    "DocumentOut", "DocumentPreview", "ReindexResult", "UploadCapabilities",
    # tools
    "ToolInfo", "ToolParameter", "ToolTestRequest", "ToolTestResult",
    "ToolToggleRequest",
    # admin
    "AdminUserUpdate", "UsageStat", "SystemStatus", "AuditLogOut",
    "UsageMetrics", "UsageReportRow", "UsageReportPage",
    "AuditEventRow", "AuditEventPage",
    "FeatureFlagOut", "FeatureFlagPage",
    # credits + redeem codes
    "CreditAccountOut", "RedeemRequest", "RedeemResultOut",
    "LedgerEntryOut", "LedgerPageOut",
    "RedeemBatchCreate", "RedeemBatchCreateOut", "RedeemBatchOut",
    "RedeemBatchProgressOut", "RedeemCodeOut", "VoidBatchOut",
    "CreditAccountRowOut", "CreditAdjustRequest", "BatchStatusFilter",
]
