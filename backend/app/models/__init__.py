"""Import every model so SQLAlchemy registers them on Base.metadata
(matters for create_all and alembic autogenerate)."""
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message
from app.models.model_config import ModelConfig
from app.models.tool_call import ToolCall
from app.models.user import User

__all__ = [
    "User",
    "ModelConfig",
    "Conversation",
    "Message",
    "KnowledgeBase",
    "Document",
    "DocumentChunk",
    "ToolCall",
]
