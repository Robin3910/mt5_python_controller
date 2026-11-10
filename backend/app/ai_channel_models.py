"""AI 频道的人工操作契约。"""
from typing import Literal

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    reason: str = Field(default="", max_length=255)


class ApproveBatchRequest(BaseModel):
    request_ids: list[int] = Field(min_length=1)


class MemberStateRequest(BaseModel):
    group_id: str = Field(min_length=1, max_length=32)
    follower_user_id: int | None = None
    paused: bool


class ChannelDeleteScope(BaseModel):
    group_id: str = Field(min_length=1, max_length=32)
    follower_user_id: int | None = None


class ChannelDeleteRequest(ChannelDeleteScope):
    version: str = Field(min_length=64, max_length=64)
    node_ids: list[str] = Field(min_length=1)


class NodeDeleteRequest(BaseModel):
    version: str = Field(min_length=64, max_length=64)


class AiUiEvent(BaseModel):
    action: Literal["page_view", "search", "filter", "open_picker", "select_nodes", "cancel_dialog", "collapse_group", "open_delete", "refresh", "follow_step", "follow_settings"]
    group_id: str | None = Field(default=None, max_length=32)
    node_ids: list[str] = Field(default_factory=list, max_length=50)
    value: str | None = Field(default=None, max_length=200)
