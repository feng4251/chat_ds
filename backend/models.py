import uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    String,
    Text,
    Integer,
    DateTime,
    ForeignKey,
    Boolean,
    JSON,
    Index,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base
from model_routing import DEFAULT_AGENT_MODEL_ID


def generate_uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    conversations: Mapped[List["Conversation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    custom_models: Mapped[List["CustomModelConfig"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    model_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_AGENT_MODEL_ID
    )
    enabled_tools: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fallback_model_ids: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled_user_skills: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    forked_from_conversation_id: Mapped[Optional[str]] = mapped_column(
        String(32),
        nullable=True,
        index=True,
    )
    fork_snapshot_sha256: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )
    workspace_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    goal_objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goal_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    goal_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goal_token_budget: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    goal_started_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[List["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_progress: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="chat")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class SkillPackage(Base):
    __tablename__ = "skill_packages"
    __table_args__ = (
        Index(
            "ux_skill_packages_user_session_name",
            "user_id",
            "session_id",
            "name",
            unique=True,
            sqlite_where=text("session_id IS NOT NULL"),
            postgresql_where=text("session_id IS NOT NULL"),
        ),
        Index(
            "ux_skill_packages_user_name",
            "user_id",
            "name",
            unique=True,
            sqlite_where=text("session_id IS NULL"),
            postgresql_where=text("session_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    description: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    bundle_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    bundle_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    bundle_root_name: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    bundle_source_path: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CustomModelConfig(Base):
    __tablename__ = "custom_model_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # openai / anthropic / custom
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False)
    is_multimodal: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_headers: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-encoded
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="custom_models")


class AgentRun(Base):
    """One auditable model run within a conversation."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    root_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    delegation_tool_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    agent_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="primary")
    agent_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workspace_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="shared_session")
    workspace_ref: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    requested_tools: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    effective_tools: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    policy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="chat")
    requested_model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resolved_model_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    finish_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_events: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentRunEvent(Base):
    """Append-only normalized event stream for agent run trees."""

    __tablename__ = "agent_run_events"
    __table_args__ = (
        Index(
            "ux_agent_run_events_conversation_run_type_seq",
            "conversation_id",
            "run_id",
            "event_type",
            "seq",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, index=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Artifact(Base):
    """Query projection for durable session artifacts produced by agent runs."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    root_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="file")
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    preview_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_tool_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    source_tool_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    source_event_key: Mapped[Optional[str]] = mapped_column(String(192), nullable=True, index=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TaskItem(Base):
    """Query projection for run/agent/verifier task status."""

    __tablename__ = "task_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    root_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    task_key: Mapped[str] = mapped_column(String(192), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="run")
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    agent_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ScheduledJob(Base):
    """User-owned scheduled agent task scoped to a session workspace."""

    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_value: Mapped[str] = mapped_column(String(256), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    model_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    enabled_tools: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delete_after_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ScheduledJobRun(Base):
    __tablename__ = "scheduled_job_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scheduled_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class EventHook(Base):
    """Webhook-style lifecycle hook, optionally scoped to one conversation."""

    __tablename__ = "event_hooks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    secret: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
