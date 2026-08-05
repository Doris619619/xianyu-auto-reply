"""
本文件定义砍价队列相关的 Pydantic 请求与响应模型。

它属于 schemas 模块，只做校验与序列化，不含业务副作用。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

ReplyMode = Literal["ai", "manual"]
ConversationPhase = Literal["availability", "negotiation", "finished"]


class EnqueueRequest(BaseModel):
    """入队请求：商品链接或纯数字 ID。"""

    url: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, max_length=512)


class EnqueueResponse(BaseModel):
    """入队响应：返回主键与排队名次。"""

    id: int
    item_id: str
    position_rank: int
    status: str
    message: str


class SendDiagnosticOut(BaseModel):
    """发送不确定时的脱敏诊断快照。"""

    schema_version: int
    phase: str
    button_center_obscured: bool | None
    click_attempted: bool
    confirmation_observed: bool
    risk_detected_after_click: bool
    last_safety_code: str | None
    request_observed: bool
    transport: str | None
    response_observed: bool
    response_status: int | None


class QueueItemOut(BaseModel):
    """队列项对外快照。"""

    id: int
    item_id: str
    detail_url: str
    title: str | None
    list_price_yuan: Decimal | None = None
    price_source: str | None = None
    status: str
    position: int
    position_rank: int | None = None
    seller_id: str | None
    processing_reply_mode: ReplyMode | None = None
    conversation_phase: ConversationPhase
    goods_available: bool | None
    over: bool
    result_summary: str | None
    fail_code: str | None
    send_diagnostic: SendDiagnosticOut | None = None
    rounds_sent: int
    waiting_since: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


class QueueListResponse(BaseModel):
    """队列列表响应。"""

    items: list[QueueItemOut]
    worker_enabled: bool
    worker_running: bool = False
    active_id: int | None


class WorkerControlResponse(BaseModel):
    """Worker 启停结果。"""

    worker_enabled: bool
    worker_running: bool = False
    message: str


class ClearQueueResponse(BaseModel):
    """清空队列后的结果。"""

    deleted_count: int
    worker_enabled: bool
    worker_running: bool
    message: str


class DeleteQueueItemResponse(BaseModel):
    """单条队列记录彻底删除后的结果。"""

    deleted_id: int
    was_active: bool
    message: str


class SettingsOut(BaseModel):
    """运行时设置快照。"""

    reply_timeout_seconds: int
    max_rounds: int
    auto_send: bool
    reply_mode: ReplyMode
    worker_enabled: bool


class SettingsUpdateRequest(BaseModel):
    """运行时设置更新请求。"""

    reply_timeout_seconds: int | None = Field(default=None, ge=30, le=900)
    max_rounds: int | None = Field(default=None, ge=1, le=20)
    auto_send: bool | None = None
    reply_mode: ReplyMode | None = None


class MessageOut(BaseModel):
    """会话消息快照。"""

    speaker: str
    text: str
    created_at: datetime | None = None


class BrowserConnectionOut(BaseModel):
    """手动回复所需本机 CDP 浏览器连接状态。"""

    configured: bool
    connected: bool
    message: str


class CurrentSessionResponse(BaseModel):
    """当前锁定会话的面板数据。"""

    item: QueueItemOut | None
    messages: list[MessageOut]
    draft_preview: str | None = None
    manual_send_available: bool = False
    browser: BrowserConnectionOut | None = None


class ManualReplyRequest(BaseModel):
    """用户确认后发送的一条手动聊天文本。"""

    text: str = Field(min_length=1, max_length=500)


class ManualReplyResponse(BaseModel):
    """手动发送经页面回读确认后的结果。"""

    rounds_sent: int
    message: str
