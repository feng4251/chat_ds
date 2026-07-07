from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


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
    skill_id: Optional[str] = None


class ChatStreamChunk(BaseModel):
    delta: str
    conversation_id: str
    message_id: str


class CustomModelConfigCreate(BaseModel):
    model_id: str
    model_name: str
    provider: str
    base_url: str
    api_key: str
    is_multimodal: bool = False
    extra_headers: Optional[str] = None


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
