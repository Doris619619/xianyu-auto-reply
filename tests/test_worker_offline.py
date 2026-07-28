"""
本文件离线验证 BargainWorker 超时暂挂与同意降价结束。

使用内存 Fake 聊天工厂与固定草稿生成器，不启动浏览器、不联网。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from app.ai.deepseek import DeepSeekConfig
from app.crawler.chat_client import (
    ChatBinding,
    ChatMessageSnapshot,
    ChatSafetyError,
    PolicyAllowedDraft,
    SendEvidence,
    SendRequestEvidence,
    build_message_fingerprint,
)
from app.crawler.chat_runtime import OpenedXianyuChat
from app.models import QueueItemStatus, init_db
from app.repositories.queue import QueueRepository
from app.seller_chat.llm import SellerChatDraftGenerator
from app.seller_chat.session import empty_conversation_fingerprint
from app.services.queue_service import QueueService
from app.worker.bargain_worker import BargainWorker

ACCOUNT = "a" * 64


def _snapshot(direction: str, text: str, message_id: str) -> ChatMessageSnapshot:
    """构造与页面适配层一致的消息快照。"""

    fingerprint = build_message_fingerprint(
        message_id=message_id,
        direction=direction,
        text=text,
        timestamp=None,
    )
    return ChatMessageSnapshot(message_id, direction, text, None, fingerprint)


class FakeChatClient:
    """内存 Fake 聊天客户端。"""

    def __init__(self, *, inbound_script: list[list[ChatMessageSnapshot]] | None = None) -> None:
        self.messages: list[ChatMessageSnapshot] = []
        self.inbound_script = [list(batch) for batch in (inbound_script or [])]
        self.sent: list[str] = []
        self._inject_ready = False

    async def open_conversation(self) -> ChatMessageSnapshot:
        return await self.read_latest_message()

    async def read_latest_message(self) -> ChatMessageSnapshot:
        if not self.messages:
            return ChatMessageSnapshot(None, "none", "", None, empty_conversation_fingerprint())
        return self.messages[-1]

    async def read_messages_after(self, baseline_fingerprint: str) -> list[ChatMessageSnapshot]:
        # 仅在发送后才注入卖家回复，避免 start() 读历史时提前消费脚本。
        if self._inject_ready and self.inbound_script:
            self.messages.extend(self.inbound_script.pop(0))
            self._inject_ready = False
        if baseline_fingerprint == empty_conversation_fingerprint():
            return list(self.messages)
        indexes = [
            index
            for index, snapshot in enumerate(self.messages)
            if snapshot.fingerprint == baseline_fingerprint
        ]
        if len(indexes) != 1:
            raise ChatSafetyError("chat_baseline_not_visible", "消息基线已不在可见历史中")
        return self.messages[indexes[0] + 1 :]

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        if auto_send_enabled is not True:
            raise ChatSafetyError("auto_send_disabled", "自动发送开关未显式开启")
        current = (await self.read_latest_message()).fingerprint
        if current != expected_latest_fingerprint:
            raise ChatSafetyError(
                "conversation_changed_before_send",
                "最新消息在发送前发生变化",
            )
        own = _snapshot("self", draft.text, f"out-{len(self.sent) + 1}")
        self.messages.append(own)
        self.sent.append(draft.text)
        self._inject_ready = True
        return SendEvidence(
            source_item_id="1001",
            seller_id="seller-1",
            account_id=ACCOUNT,
            policy_decision_id=draft.policy_decision_id,
            draft_sha256="0" * 64,
            confirmed_message_fingerprint=own.fingerprint,
            request_evidence=SendRequestEvidence(
                request_observed=True,
                transport="http",
                endpoint_sha256="1" * 64,
                method="POST",
                response_observed=True,
                response_status=200,
            ),
        )


class FakeFactory:
    """按商品 ID 返回预设 Fake 客户端。"""

    def __init__(self, clients: dict[str, FakeChatClient]) -> None:
        self.clients = clients

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @asynccontextmanager
    async def open(self, **kwargs: Any):
        item_id = kwargs["source_item_id"]
        client = self.clients[item_id]
        binding = ChatBinding(
            source_item_id=item_id,
            seller_id="seller1",
            account_id=ACCOUNT,
        )
        yield OpenedXianyuChat(binding=binding, client=client)


class FixedDraftGenerator(SellerChatDraftGenerator):
    """返回固定短句，避免真实 HTTP。"""

    def __init__(self, text: str = "老板能便宜一点吗") -> None:
        super().__init__(DeepSeekConfig(api_key=SecretStr("x" * 32)))
        self._text = text

    def generate(self, **kwargs: Any) -> str:  # type: ignore[override]
        del kwargs
        return self._text


@pytest.fixture()
def session_factory() -> sessionmaker:
    """临时库。"""

    import uuid

    base = Path(__file__).resolve().parents[1] / ".pytest_tmp"
    base.mkdir(exist_ok=True)
    db_path = base / f"worker-{uuid.uuid4().hex}.db"
    factory = init_db(f"sqlite:///{db_path.as_posix()}")
    with factory() as session:
        QueueRepository(session).ensure_settings(
            reply_timeout_seconds=2,
            max_rounds=3,
            auto_send=True,
        )
        QueueRepository(session).update_settings(worker_enabled=True)
    return factory


@pytest.mark.asyncio
async def test_worker_timeout_parks_then_next(session_factory: sessionmaker) -> None:
    """首轮超时暂挂后应能领取下一家，下一家同意则结束。"""

    service = QueueService(session_factory)
    first = service.enqueue("1001")
    second = service.enqueue("1002")
    clients = {
        "1001": FakeChatClient(inbound_script=[]),
        "1002": FakeChatClient(
            inbound_script=[[_snapshot("seller", "可以便宜十块", "m1")]]
        ),
    }

    async def fake_sleep(seconds: float) -> None:
        # 必须让出事件循环，否则 _wait_with_abort 忙等会饿死 wait 任务。
        await asyncio.sleep(0)
        del seconds

    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory(clients),
        draft_generator=FixedDraftGenerator(),
        expected_account_id=ACCOUNT,
        sleep=fake_sleep,
        poll_idle_seconds=0.01,
    )
    await worker.chat_factory.start()
    id1 = worker._claim_next_id()
    assert id1 == first.id
    await worker._process_item(id1)
    with session_factory() as session:
        row = QueueRepository(session).get_item(first.id)
        assert row is not None
        assert row.status == QueueItemStatus.PARKED

    id2 = worker._claim_next_id()
    assert id2 == second.id
    await worker._process_item(id2)
    with session_factory() as session:
        row = QueueRepository(session).get_item(second.id)
        assert row is not None
        assert row.status == QueueItemStatus.DONE_AGREED
    await worker.chat_factory.stop()


@pytest.mark.asyncio
async def test_enqueue_does_not_preempt_active(session_factory: sessionmaker) -> None:
    """入队不打断当前 active。"""

    service = QueueService(session_factory)
    a = service.enqueue("2001")
    with session_factory() as session:
        QueueRepository(session).claim_next_queued()
    b = service.enqueue("2002")
    snapshot = service.list_queue()
    by_id = {i.id: i for i in snapshot.items}
    assert by_id[a.id].status == QueueItemStatus.ACTIVE
    assert by_id[b.id].status == QueueItemStatus.QUEUED
    assert b.position_rank == 1
