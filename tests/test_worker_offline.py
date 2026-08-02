"""
本文件离线验证 BargainWorker 超时暂挂与同意降价结束。

使用内存 Fake 聊天工厂与固定草稿生成器，不启动浏览器、不联网。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
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
    ChatSendUncertainError,
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

    async def refresh_conversation(self) -> None:
        """离线 Fake 不需要刷新页面。"""

        return None

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


class UnconfirmedFakeChatClient(FakeChatClient):
    """模拟页面未确认发送结果的失败关闭场景。"""

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        del expected_latest_fingerprint, auto_send_enabled
        del draft
        raise ChatSendUncertainError(
            "send_confirmation_missing",
            "本人消息未能稳定回读",
            SendRequestEvidence(request_observed=False),
        )


class PageConfirmedWithoutTransportFakeChatClient(FakeChatClient):
    """模拟本人消息已稳定可见，但传输正文不可由 Playwright 解析。"""

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        evidence = await super().send_policy_allowed_draft(
            draft,
            expected_latest_fingerprint=expected_latest_fingerprint,
            auto_send_enabled=auto_send_enabled,
        )
        return replace(
            evidence,
            request_evidence=SendRequestEvidence(request_observed=False),
        )


class ConfirmedButRescanEmptyFakeChatClient(FakeChatClient):
    """模拟适配层已确认发送、但紧随其后的列表重绘暂时为空。"""

    def __init__(self) -> None:
        """初始化一次发送后的空扫描标记。"""

        super().__init__()
        self._return_empty_once = False

    async def read_messages_after(self, baseline_fingerprint: str) -> list[ChatMessageSnapshot]:
        """仅让发送确认后的首次增量扫描为空，后续刷新恢复正常记录。"""

        if self._return_empty_once:
            self._return_empty_once = False
            return []
        return await super().read_messages_after(baseline_fingerprint)

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        del expected_latest_fingerprint, auto_send_enabled
        confirmed = _snapshot("self", draft.text, "confirmed-1")
        self.messages.append(confirmed)
        self._return_empty_once = True
        return SendEvidence(
            source_item_id="1001",
            seller_id="seller-1",
            account_id=ACCOUNT,
            policy_decision_id=draft.policy_decision_id,
            draft_sha256="0" * 64,
            confirmed_message_fingerprint=confirmed.fingerprint,
            request_evidence=SendRequestEvidence(request_observed=False),
        )


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
async def test_historical_seller_message_does_not_trigger_second_send_after_timeout(
    session_factory: sessionmaker,
) -> None:
    """旧卖家记录不能替代本轮回复，超时后只能暂挂而不能继续发送。"""

    service = QueueService(session_factory)
    item = service.enqueue("1003")
    client = FakeChatClient()
    client.messages.append(_snapshot("seller", "之前问过一次", "history-1"))

    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)
        del seconds

    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory({"1003": client}),
        draft_generator=FixedDraftGenerator("老板还能便宜吗"),
        expected_account_id=ACCOUNT,
        sleep=fake_sleep,
    )
    item_id = worker._claim_next_id()
    assert item_id == item.id
    await worker._process_item(item_id)

    with session_factory() as session:
        repo = QueueRepository(session)
        row = repo.get_item(item.id)
        assert row is not None
        assert row.status == QueueItemStatus.PARKED
        assert row.rounds_sent == 1
        assert client.sent == ["老板还能便宜吗"]


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


@pytest.mark.asyncio
async def test_worker_claim_resumes_existing_active_item(session_factory: sessionmaker) -> None:
    """重启后的 Worker 必须恢复唯一 active 项，而非跳过它去取队首。"""

    service = QueueService(session_factory)
    active = service.enqueue("2003")
    queued = service.enqueue("2004")
    with session_factory() as session:
        QueueRepository(session).claim_next_queued()

    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory({}),
        draft_generator=FixedDraftGenerator(),
        expected_account_id=ACCOUNT,
    )

    assert worker._claim_next_id() == active.id
    with session_factory() as session:
        row = QueueRepository(session).get_item(queued.id)
        assert row is not None
        assert row.status == QueueItemStatus.QUEUED


@pytest.mark.asyncio
async def test_unconfirmed_send_is_not_persisted_as_sent(session_factory: sessionmaker) -> None:
    """本人消息未回读时不得增加轮次或给面板伪造“我”的消息。"""

    service = QueueService(session_factory)
    item = service.enqueue("3001")
    client = UnconfirmedFakeChatClient()
    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory({"3001": client}),
        draft_generator=FixedDraftGenerator("你好"),
        expected_account_id=ACCOUNT,
    )
    item_id = worker._claim_next_id()
    assert item_id == item.id
    await worker._process_item(item_id)

    with session_factory() as session:
        repo = QueueRepository(session)
        row = repo.get_item(item.id)
        assert row is not None
        assert row.status == QueueItemStatus.FAILED
        assert row.fail_code == "send_confirmation_missing"
        assert row.rounds_sent == 0
        assert repo.list_messages(item.id) == []


@pytest.mark.asyncio
async def test_page_confirmed_send_without_transport_body_is_persisted(
    session_factory: sessionmaker,
) -> None:
    """本人消息稳定可见时，即使网络正文不可见也应正常记录发送。"""

    service = QueueService(session_factory)
    item = service.enqueue("3002")
    client = PageConfirmedWithoutTransportFakeChatClient()

    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)
        del seconds

    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory({"3002": client}),
        draft_generator=FixedDraftGenerator("你好"),
        expected_account_id=ACCOUNT,
        sleep=fake_sleep,
    )
    item_id = worker._claim_next_id()
    assert item_id == item.id
    await worker._process_item(item_id)

    with session_factory() as session:
        repo = QueueRepository(session)
        row = repo.get_item(item.id)
        assert row is not None
        assert row.status == QueueItemStatus.PARKED
        assert row.rounds_sent == 1
        assert [message.text for message in repo.list_messages(item.id)] == ["你好"]


@pytest.mark.asyncio
async def test_confirmed_send_is_persisted_when_immediate_rescan_is_empty(
    session_factory: sessionmaker,
) -> None:
    """适配层确认成功后，短暂空扫描不能把已发消息误判为失败。"""

    service = QueueService(session_factory)
    item = service.enqueue("3003")
    client = ConfirmedButRescanEmptyFakeChatClient()

    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)
        del seconds

    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory({"3003": client}),
        draft_generator=FixedDraftGenerator("你好"),
        expected_account_id=ACCOUNT,
        sleep=fake_sleep,
    )
    item_id = worker._claim_next_id()
    assert item_id == item.id
    await worker._process_item(item_id)

    with session_factory() as session:
        repo = QueueRepository(session)
        row = repo.get_item(item.id)
        assert row is not None
        assert row.status == QueueItemStatus.PARKED
        assert row.rounds_sent == 1
        assert [message.text for message in repo.list_messages(item.id)] == ["你好"]


@pytest.mark.asyncio
async def test_manual_mode_only_sends_user_confirmed_text_and_stays_active(
    session_factory: sessionmaker,
) -> None:
    """手动模式不调用 AI，只有面板提交的原文经页面确认后才增加轮次。"""

    service = QueueService(session_factory)
    service.update_settings(reply_mode="manual")
    item = service.enqueue("4001")
    client = FakeChatClient()

    async def paced_sleep(seconds: float) -> None:
        await asyncio.sleep(min(seconds, 0.01))

    factory = FakeFactory({"4001": client})
    worker = BargainWorker(
        session_factory=session_factory,
        chat_factory=FakeFactory({}),
        manual_chat_factory=factory,
        draft_generator=FixedDraftGenerator("这句 AI 不应被使用"),
        expected_account_id=ACCOUNT,
        sleep=paced_sleep,
    )
    item_id = worker._claim_next_id()
    assert item_id == item.id
    task = asyncio.create_task(worker._process_item(item_id))
    for _ in range(50):
        if worker.manual_send_available(item.id):
            break
        await asyncio.sleep(0.01)
    assert worker.manual_send_available(item.id)

    rounds = await worker.submit_manual_reply("老板能便宜一点吗")
    assert rounds == 1
    assert client.sent == ["老板能便宜一点吗"]
    with session_factory() as session:
        row = QueueRepository(session).get_item(item.id)
        assert row is not None
        assert row.status == QueueItemStatus.ACTIVE
        assert row.rounds_sent == 1

    worker.request_cancel_current()
    await asyncio.wait_for(task, timeout=1)
