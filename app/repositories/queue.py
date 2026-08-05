"""
本文件负责队列项与会话消息的数据库访问。

它属于 repositories 模块，只做 CRUD 与状态查询，不启动 Worker、不访问闲鱼页面。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import AppSetting, QueueItem, QueueItemStatus, SessionMessage


class QueueRepository:
    """
    封装队列与设置的 SQLite 读写。
    """

    def __init__(self, session: Session) -> None:
        """保存当前 ORM 会话。"""

        self._session = session

    def ensure_settings(
        self,
        *,
        reply_timeout_seconds: int,
        max_rounds: int,
        auto_send: bool,
        reply_mode: str = "ai",
    ) -> AppSetting:
        """
        确保存在唯一一行运行时设置；不存在则按默认值创建。
        """

        row = self._session.scalar(select(AppSetting).limit(1))
        if row is None:
            row = AppSetting(
                reply_timeout_seconds=reply_timeout_seconds,
                max_rounds=max_rounds,
                auto_send=auto_send,
                reply_mode=reply_mode,
                worker_enabled=False,
            )
            self._session.add(row)
            self._session.commit()
            self._session.refresh(row)
        return row

    def get_settings(self) -> AppSetting:
        """返回当前设置行；不存在时抛出 RuntimeError。"""

        row = self._session.scalar(select(AppSetting).limit(1))
        if row is None:
            raise RuntimeError("运行时设置尚未初始化")
        return row

    def update_settings(
        self,
        *,
        reply_timeout_seconds: int | None = None,
        max_rounds: int | None = None,
        auto_send: bool | None = None,
        reply_mode: str | None = None,
        worker_enabled: bool | None = None,
    ) -> AppSetting:
        """按非空字段更新运行时设置并返回最新行。"""

        row = self.get_settings()
        if reply_timeout_seconds is not None:
            row.reply_timeout_seconds = reply_timeout_seconds
        if max_rounds is not None:
            row.max_rounds = max_rounds
        if auto_send is not None:
            row.auto_send = auto_send
        if reply_mode is not None:
            row.reply_mode = reply_mode
        if worker_enabled is not None:
            row.worker_enabled = worker_enabled
        self._session.commit()
        self._session.refresh(row)
        return row

    def next_position(self) -> int:
        """返回队尾下一个 position（从 1 递增）。"""

        current = self._session.scalar(select(func.max(QueueItem.position))) or 0
        return int(current) + 1

    def enqueue(self, *, item_id: str, detail_url: str, title: str | None = None) -> QueueItem:
        """
        将商品追加到队尾，状态为 queued。

        返回新建队列项。
        """

        item = QueueItem(
            item_id=item_id,
            detail_url=detail_url,
            title=title,
            status=QueueItemStatus.QUEUED,
            position=self.next_position(),
        )
        self._session.add(item)
        self._session.commit()
        self._session.refresh(item)
        return item

    def list_items(self) -> list[QueueItem]:
        """按 position 升序返回全部队列项。"""

        return list(
            self._session.scalars(select(QueueItem).order_by(QueueItem.position.asc())).all()
        )

    def get_item(self, item_pk: int) -> QueueItem | None:
        """按主键读取队列项。"""

        return self._session.get(QueueItem, item_pk)

    def get_active(self) -> QueueItem | None:
        """返回当前唯一的 active 项（若有）。"""

        return self._session.scalar(
            select(QueueItem).where(QueueItem.status == QueueItemStatus.ACTIVE).limit(1)
        )

    def queued_position_rank(self, item: QueueItem) -> int:
        """
        计算某 queued 项在排队中的名次（从 1 起，含 active 之前的 queued）。

        若项不是 queued，返回 0。
        """

        if item.status != QueueItemStatus.QUEUED:
            return 0
        ahead = self._session.scalar(
            select(func.count())
            .select_from(QueueItem)
            .where(
                QueueItem.status == QueueItemStatus.QUEUED,
                QueueItem.position < item.position,
            )
        )
        return int(ahead or 0) + 1

    def claim_next_queued(self, *, reply_mode: str = "ai") -> QueueItem | None:
        """
        领取 position 最小的 queued 项并标记为 active。

        若已有 active 项则返回 None，保证全局最多一个 active。
        """

        if self.get_active() is not None:
            return None
        item = self._session.scalar(
            select(QueueItem)
            .where(QueueItem.status == QueueItemStatus.QUEUED)
            .order_by(QueueItem.position.asc())
            .limit(1)
        )
        if item is None:
            return None
        item.status = QueueItemStatus.ACTIVE
        if item.processing_reply_mode is None:
            item.processing_reply_mode = reply_mode
        item.waiting_since = None
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def mark_status(
        self,
        item: QueueItem,
        status: QueueItemStatus,
        *,
        summary: str | None = None,
        fail_code: str | None = None,
        send_diagnostic: str | None = None,
    ) -> QueueItem:
        """更新队列项终态或暂挂状态。"""

        item.status = status
        if status in {
            QueueItemStatus.DONE_UNAVAILABLE,
            QueueItemStatus.DONE_AGREED,
            QueueItemStatus.DONE_REFUSED,
            QueueItemStatus.DONE_MANUAL,
        }:
            item.conversation_phase = "finished"
            item.over = True
        if summary is not None:
            item.result_summary = summary
        if fail_code is not None:
            item.fail_code = fail_code
        if send_diagnostic is not None:
            item.send_diagnostic = send_diagnostic
        item.waiting_since = None
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def mark_waiting(self, item: QueueItem) -> QueueItem:
        """标记刚发出议价后开始等待卖家。"""

        item.waiting_since = datetime.now(UTC)
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def bump_rounds(self, item: QueueItem) -> QueueItem:
        """发送成功后增加轮次计数。"""

        item.rounds_sent = int(item.rounds_sent or 0) + 1
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def set_seller_id(self, item: QueueItem, seller_id: str) -> QueueItem:
        """保存页面发现的卖家 ID。"""

        item.seller_id = seller_id
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def set_title(self, item: QueueItem, title: str) -> QueueItem:
        """保存可选商品标题。"""

        item.title = title
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def set_conversation_state(
        self,
        item: QueueItem,
        *,
        phase: str,
        goods_available: bool | None = None,
    ) -> QueueItem:
        """
        保存可恢复的对话阶段及已确认的库存信号。

        参数 ``phase`` 由上层状态机传入；仅在 ``goods_available`` 非空时更新库存结果。
        副作用为提交当前队列项，供超时重试或服务重启后继续同一阶段。
        """

        item.conversation_phase = phase
        if goods_available is not None:
            item.goods_available = goods_available
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def set_product_context(
        self,
        item: QueueItem,
        *,
        title: str | None,
        list_price_yuan: str | None,
        price_source: str,
    ) -> QueueItem:
        """保存本次打开详情页得到的主价读取结果，供面板和后续报价共用。"""

        if title:
            item.title = title
        item.list_price_yuan = list_price_yuan
        item.price_source = price_source
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def retry_parked(self, item: QueueItem) -> QueueItem:
        """
        将 parked 项重新入队到队尾。

        非 parked 状态抛出 ValueError。
        """

        if item.status != QueueItemStatus.PARKED:
            raise ValueError("只有暂挂项可以重试")
        item.status = QueueItemStatus.QUEUED
        item.processing_reply_mode = None
        item.position = self.next_position()
        item.result_summary = None
        item.fail_code = None
        item.send_diagnostic = None
        item.waiting_since = None
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def resume_failed_monitoring(self, item: QueueItem) -> QueueItem:
        """
        将已发送过消息但监听失败的项重新入队，只恢复监听而不重发开场。

        仅 ``failed`` 且已有发送轮次的项可恢复；其他失败原因不能借此绕过发送确认边界。
        """

        if item.status != QueueItemStatus.FAILED or item.rounds_sent < 1:
            raise ValueError("只有已发过消息的失败项可以恢复监听")
        item.status = QueueItemStatus.QUEUED
        item.processing_reply_mode = None
        item.position = self.next_position()
        item.result_summary = "恢复监听中，等待卖家新回复"
        item.fail_code = None
        item.send_diagnostic = None
        item.waiting_since = None
        item.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(item)
        return item

    def prioritize(self, target: QueueItem) -> QueueItem:
        """
        将目标项插到队首（最小 position - 1），并结束当前 active（若有）。

        目标必须是 queued 或 parked；返回更新后的目标项。
        """

        if target.status not in {QueueItemStatus.QUEUED, QueueItemStatus.PARKED}:
            raise ValueError("只能优先处理排队中或暂挂的项")
        active = self.get_active()
        if active is not None and active.id != target.id:
            self.mark_status(
                active,
                QueueItemStatus.DONE_MANUAL,
                summary="被优先插队顶替",
                fail_code="preempted",
            )
        min_pos = self._session.scalar(select(func.min(QueueItem.position))) or 1
        target.status = QueueItemStatus.QUEUED
        target.position = int(min_pos) - 1
        target.result_summary = None
        target.fail_code = None
        target.send_diagnostic = None
        target.waiting_since = None
        target.updated_at = datetime.now(UTC)
        self._session.commit()
        self._session.refresh(target)
        return target

    def replace_messages(self, queue_item_id: int, entries: list[tuple[str, str]]) -> None:
        """
        用最新转录覆盖某队列项的会话消息展示。

        参数 entries 为 (speaker, text) 列表。
        """

        existing = list(
            self._session.scalars(
                select(SessionMessage).where(SessionMessage.queue_item_id == queue_item_id)
            ).all()
        )
        for row in existing:
            self._session.delete(row)
        for speaker, text in entries:
            self._session.add(
                SessionMessage(queue_item_id=queue_item_id, speaker=speaker, text=text)
            )
        self._session.commit()

    def list_messages(self, queue_item_id: int) -> list[SessionMessage]:
        """按时间返回某队列项的会话消息。"""

        return list(
            self._session.scalars(
                select(SessionMessage)
                .where(SessionMessage.queue_item_id == queue_item_id)
                .order_by(SessionMessage.id.asc())
            ).all()
        )

    def clear_all(self) -> int:
        """删除全部队列项及其仅供展示的会话记录，并返回删除的队列项数。"""

        item_count = int(self._session.scalar(select(func.count()).select_from(QueueItem)) or 0)
        self._session.execute(delete(SessionMessage))
        self._session.execute(delete(QueueItem))
        self._session.commit()
        return item_count

    def delete_item(self, item_id: int) -> bool | None:
        """彻底删除指定队列项及其会话记录；返回其删除前是否为 active。"""

        item = self.get_item(item_id)
        if item is None:
            return None
        was_active = item.status == QueueItemStatus.ACTIVE
        self._session.execute(
            delete(SessionMessage).where(SessionMessage.queue_item_id == item.id)
        )
        self._session.delete(item)
        self._session.commit()
        return was_active

    def recover_interrupted_active(self) -> int:
        """
        将因进程崩溃/重启残留的 active 项重新降为 queued。

        返回被恢复的条数。不启动 Worker。
        """

        rows = list(
            self._session.scalars(
                select(QueueItem).where(QueueItem.status == QueueItemStatus.ACTIVE)
            ).all()
        )
        for item in rows:
            item.status = QueueItemStatus.QUEUED
            item.waiting_since = None
            item.result_summary = "服务重启，已重新入队等待继续"
            item.updated_at = datetime.now(UTC)
        if rows:
            self._session.commit()
        return len(rows)

    def clear_active_if_preempted(self, item_id: int) -> bool:
        """
        检查指定 active 项是否已被外部标为非 active（例如插队）。

        返回 True 表示应停止当前会话。
        """

        item = self.get_item(item_id)
        return item is None or item.status != QueueItemStatus.ACTIVE

