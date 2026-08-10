"""Import every model so SQLAlchemy registers them on Base.metadata
(matters for create_all and alembic autogenerate)."""
from app.models.agent_attempt import AgentAttempt
from app.models.audit_event import AuditEvent
from app.models.agent_run import AgentRun
from app.models.agent_step import AgentStep
from app.models.background_task import BackgroundTask
from app.models.chat_attachment import ChatAttachment
from app.models.conversation import Conversation
from app.models.conversation_memory import ConversationMemory
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.message_feedback import MessageFeedback
from app.models.model_config import ModelConfig
from app.models.project import Project
from app.models.run_command import RunCommand
from app.models.run_event import RunEvent
from app.models.run_lease import RunLease
from app.models.tool_approval import ToolApproval
from app.models.tool_call import ToolCall
from app.models.user import User

__all__ = [
    "AuditEvent",
    "User",
    "ModelConfig",
    "Conversation",
    "Message",
    "KnowledgeBase",
    "Document",
    "DocumentChunk",
    "ToolCall",
    # ---- agent platform (Phase 0-4) ----
    "AgentRun",
    "AgentStep",
    "ConversationMemory",
    "ToolApproval",
    # ---- Phase 1: product upgrade ----
    "ChatAttachment",
    "MessageFeedback",
    # ---- Phase 3: projects + background tasks ----
    "Project",
    "BackgroundTask",
    # ---- Task 4: durable workflow (events, commands, leases, attempts) ----
    "RunEvent",
    "RunCommand",
    "RunLease",
    "AgentAttempt",
]
