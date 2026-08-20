import json
from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    email: Optional[str] = None
    password: str = Field(min_length=4)


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    email: Optional[str]
    avatar_url: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: Optional[str]
    image_urls: Optional[str]
    model_id: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: str
    title: Optional[str]
    model_id: str
    engine_id: str = "claude_code"
    created_at: datetime
    updated_at: datetime
    last_message: Optional[str] = None
    message_count: int = 0
    model_config = {"from_attributes": True}


class ConversationTitle(BaseModel):
    title: str


class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    content: str
    image_urls: Optional[list[str]] = None
    model_id: Optional[str] = None
    engine_id: Optional[
        Literal["claude_code", "deepseek_harness"]
    ] = None


class ConversationSettingsUpdate(BaseModel):
    engine_id: Optional[
        Literal["claude_code", "deepseek_harness"]
    ] = None
    model_id: Optional[str] = None
    enabled_user_skills: Optional[list[str]] = None
    permission_preset: Optional[
        Literal["read_only", "workspace_write", "session_full"]
    ] = None


class ApprovalDecision(BaseModel):
    model_config = {"extra": "forbid"}

    decision: Literal["allow", "deny"]
    request_seq: int = Field(ge=1)
    answers: Optional[dict[str, str]] = None

    @field_validator("answers")
    @classmethod
    def validate_answers(cls, value):
        if value is None:
            return None
        if (
            not 1 <= len(value) <= 4
            or any(
                not key
                or len(key) > 4_000
                or not answer.strip()
                or len(answer) > 4_000
                or "\x00" in key
                or "\x00" in answer
                for key, answer in value.items()
            )
        ):
            raise ValueError("Native question answers are invalid")
        return value


class GoalUpdate(BaseModel):
    objective: Optional[str] = Field(default=None, max_length=8000)
    status: Optional[
        Literal["active", "paused", "blocked", "complete", "budget_limited"]
    ] = None
    note: Optional[str] = Field(default=None, max_length=4000)
    token_budget: Optional[int] = Field(default=None, ge=1)


class WorkspaceFileWrite(BaseModel):
    content: str = Field(max_length=200_000)


class ScheduledJobCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=40_000)
    schedule: str = Field(min_length=1, max_length=256)
    conversation_id: Optional[str] = None
    timezone: str = Field(default="UTC", max_length=64)
    model_id: Optional[str] = None
    platform_capabilities: Optional[list[str]] = None
    delete_after_run: bool = False
    max_runs: Optional[int] = Field(default=None, ge=1, le=10_000)
    expires_at: Optional[datetime] = None


class ScheduledJobUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=40_000)
    schedule: Optional[str] = Field(default=None, min_length=1, max_length=256)
    timezone: Optional[str] = Field(default=None, max_length=64)
    model_id: Optional[str] = None
    platform_capabilities: Optional[list[str]] = None
    enabled: Optional[bool] = None
    delete_after_run: Optional[bool] = None
    max_runs: Optional[int] = Field(default=None, ge=1, le=10_000)
    expires_at: Optional[datetime] = None


class EventHookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    events: list[str] = Field(min_length=1)
    url: str = Field(min_length=8, max_length=1024)
    conversation_id: Optional[str] = None
    secret: Optional[str] = Field(default=None, max_length=256)
    enabled: bool = True


class EventHookUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    events: Optional[list[str]] = Field(default=None, min_length=1)
    url: Optional[str] = Field(default=None, min_length=8, max_length=1024)
    secret: Optional[str] = Field(default=None, max_length=256)
    enabled: Optional[bool] = None


class ChatStreamChunk(BaseModel):
    delta: str
    conversation_id: str
    message_id: str


class CustomModelConfigCreate(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=128)
    provider: Literal["openai", "anthropic", "custom"]
    base_url: str = Field(min_length=8, max_length=512)
    api_key: str = Field(max_length=256)
    is_multimodal: bool = False
    extra_headers: Optional[str] = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return normalized

    @field_validator("extra_headers")
    @classmethod
    def validate_extra_headers(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("extra_headers must be a valid JSON object") from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in parsed.items()
        ):
            raise ValueError("extra_headers must map string names to string values")
        return json.dumps(parsed, ensure_ascii=False)


class CustomModelConfigOut(BaseModel):
    id: str
    model_id: str
    model_name: str
    provider: str
    base_url: str
    is_multimodal: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class ModelOption(BaseModel):
    id: str
    name: str
    provider: str
    is_multimodal: bool
    description: str = ""
