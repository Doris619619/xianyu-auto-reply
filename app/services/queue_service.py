"""
本文件实现队列业务入口：入队、插队、重试、停止与设置读写。

它属于 services 模块，调用仓储层；不直接操作 Playwright。
Worker 启停通过回调通知，避免 API 直接依赖 Worker 实现细节。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from sqlalchemy.orm import sessionmaker

from app.models import QueueItem, QueueItemStatus
from app.repositories.queue import QueueRepository
from app.schemas.queue import (
    CurrentSessionResponse,
    EnqueueResponse,
    MessageOut,
    QueueItemOut,
    QueueListResponse,
    SendDiagnosticOut,
    SettingsOut,
)
from app.seller_chat.item_url import ItemUrlError, parse_item_reference


class QueueServiceError(ValueError):
    """表示队列业务规则错误。"""


class QueueService:
    """
    砍价队列业务服务。
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        on_worker_enabled_change: Callable[[bool], None] | None = None,
        on_preempt: Callable[[], None] | None = None,
        on_stop_active: Callable[[], None] | None = None,
    ) -> None:
        """
        保存会话工厂与可选 Worker 回调。

        回调在设置变更、插队、手动停止时触发，便于 Worker 协作。
        """

        self._session_factory = session_factory
        self._on_worker_enabled_change = on_worker_enabled_change
        self._on_preempt = on_preempt
        self._on_stop_active = on_stop_active

    def enqueue(self, url: str, title: str | None = None) -> EnqueueResponse:
        """
        解析链接并追加到队尾。

        链接不合法时抛出 QueueServiceError。
        """

        try:
            ref = parse_item_reference(url)
        except ItemUrlError as error:
            raise QueueServiceError(str(error)) from error
        with self._session_factory() as session:
            repo = QueueRepository(session)
            item = repo.enqueue(item_id=ref.item_id, detail_url=ref.detail_url, title=title)
            rank = repo.queued_position_rank(item)
            return EnqueueResponse(
                id=item.id,
                item_id=item.item_id,
                position_rank=rank,
                status=item.status,
                message=f"已加入队列，排在第 {rank} 个",
            )

    def list_queue(self, *, worker_running: bool = False) -> QueueListResponse:
        """返回队列快照；worker_running 为进程内真实运行状态。"""

        with self._session_factory() as session:
            repo = QueueRepository(session)
            settings = repo.get_settings()
            active = repo.get_active()
            items: list[QueueItemOut] = []
            for item in repo.list_items():
                rank = (
                    repo.queued_position_rank(item)
                    if item.status == QueueItemStatus.QUEUED
                    else None
                )
                items.append(_to_item_out(item, rank))
            return QueueListResponse(
                items=items,
                worker_enabled=settings.worker_enabled,
                worker_running=worker_running,
                active_id=active.id if active else None,
            )

    def recover_interrupted(self) -> int:
        """恢复因重启残留的 active 项为 queued。"""

        with self._session_factory() as session:
            return QueueRepository(session).recover_interrupted_active()

    def get_settings(self) -> SettingsOut:
        """读取运行时设置。"""

        with self._session_factory() as session:
            row = QueueRepository(session).get_settings()
            return SettingsOut(
                reply_timeout_seconds=row.reply_timeout_seconds,
                max_rounds=row.max_rounds,
                auto_send=row.auto_send,
                reply_mode=row.reply_mode,
                worker_enabled=row.worker_enabled,
            )

    def update_settings(
        self,
        *,
        reply_timeout_seconds: int | None = None,
        max_rounds: int | None = None,
        auto_send: bool | None = None,
        reply_mode: str | None = None,
    ) -> SettingsOut:
        """更新运行时设置（不含 worker_enabled）。"""

        with self._session_factory() as session:
            row = QueueRepository(session).update_settings(
                reply_timeout_seconds=reply_timeout_seconds,
                max_rounds=max_rounds,
                auto_send=auto_send,
                reply_mode=reply_mode,
            )
            return SettingsOut(
                reply_timeout_seconds=row.reply_timeout_seconds,
                max_rounds=row.max_rounds,
                auto_send=row.auto_send,
                reply_mode=row.reply_mode,
                worker_enabled=row.worker_enabled,
            )

    def set_worker_enabled(self, enabled: bool) -> SettingsOut:
        """启停 Worker 标志并通知回调。"""

        with self._session_factory() as session:
            row = QueueRepository(session).update_settings(worker_enabled=enabled)
            out = SettingsOut(
                reply_timeout_seconds=row.reply_timeout_seconds,
                max_rounds=row.max_rounds,
                auto_send=row.auto_send,
                reply_mode=row.reply_mode,
                worker_enabled=row.worker_enabled,
            )
        if self._on_worker_enabled_change is not None:
            self._on_worker_enabled_change(enabled)
        return out

    def prioritize(self, item_id: int) -> QueueItemOut:
        """暂停当前并优先插队。"""

        with self._session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is None:
                raise QueueServiceError("队列项不存在")
            updated = repo.prioritize(item)
            rank = repo.queued_position_rank(updated)
            out = _to_item_out(updated, rank)
        if self._on_preempt is not None:
            self._on_preempt()
        return out

    def retry(self, item_id: int) -> QueueItemOut:
        """将暂挂项重新入队。"""

        with self._session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is None:
                raise QueueServiceError("队列项不存在")
            try:
                updated = repo.retry_parked(item)
            except ValueError as error:
                raise QueueServiceError(str(error)) from error
            return _to_item_out(updated, repo.queued_position_rank(updated))

    def resume_monitoring(self, item_id: int) -> QueueItemOut:
        """恢复已发送失败项的卖家回复监听，不会重新发送开场。"""

        with self._session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is None:
                raise QueueServiceError("队列项不存在")
            try:
                updated = repo.resume_failed_monitoring(item)
            except ValueError as error:
                raise QueueServiceError(str(error)) from error
            return _to_item_out(updated, repo.queued_position_rank(updated))

    def stop(self, item_id: int | None = None) -> QueueItemOut | None:
        """
        手动结束指定项或当前 active 项。

        返回被结束的项；若无 active 且未指定 id 则返回 None。
        """

        with self._session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id) if item_id is not None else repo.get_active()
            if item is None:
                return None
            if item.status not in {
                QueueItemStatus.ACTIVE,
                QueueItemStatus.QUEUED,
                QueueItemStatus.PARKED,
            }:
                raise QueueServiceError("该项已结束，无法再停止")
            updated = repo.mark_status(
                item,
                QueueItemStatus.DONE_MANUAL,
                summary="用户手动结束",
                fail_code="manual_stop",
            )
            out = _to_item_out(updated, None)
        if self._on_stop_active is not None:
            self._on_stop_active()
        return out

    def clear_all(self) -> int:
        """清空全部队列项及面板会话记录；调用方须先停止 Worker。"""

        with self._session_factory() as session:
            return QueueRepository(session).clear_all()

    def delete_item(self, item_id: int) -> bool:
        """彻底删除一条记录；删除当前会话时通知 Worker 立即取消。"""

        with self._session_factory() as session:
            was_active = QueueRepository(session).delete_item(item_id)
        if was_active is None:
            raise QueueServiceError("队列项不存在")
        if was_active and self._on_stop_active is not None:
            self._on_stop_active()
        return was_active

    def current_session(self) -> CurrentSessionResponse:
        """返回当前 active 会话消息。"""

        with self._session_factory() as session:
            repo = QueueRepository(session)
            active = repo.get_active()
            if active is None:
                return CurrentSessionResponse(item=None, messages=[])
            messages = [
                MessageOut(speaker=m.speaker, text=m.text, created_at=m.created_at)
                for m in repo.list_messages(active.id)
            ]
            return CurrentSessionResponse(item=_to_item_out(active, None), messages=messages)


def _to_item_out(item: QueueItem, rank: int | None) -> QueueItemOut:
    """将 ORM 行转为对外模型。"""

    return QueueItemOut(
        id=item.id,
        item_id=item.item_id,
        detail_url=item.detail_url,
        title=item.title,
        status=item.status,
        position=item.position,
        position_rank=rank,
        seller_id=item.seller_id,
        processing_reply_mode=item.processing_reply_mode,
        result_summary=item.result_summary,
        fail_code=item.fail_code,
        send_diagnostic=_parse_send_diagnostic(item.send_diagnostic),
        rounds_sent=item.rounds_sent,
        waiting_since=item.waiting_since,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _parse_send_diagnostic(value: str | None) -> SendDiagnosticOut | None:
    """解析数据库中的脱敏发送诊断；损坏的历史记录不影响队列读取。"""

    if not value:
        return None
    try:
        return SendDiagnosticOut.model_validate_json(value)
    except (ValueError, json.JSONDecodeError):
        return None
