"""
本文件离线验证链接解析、同意/拒绝判定、黑名单与队列状态机。

它属于 tests，不启动 Playwright、不访问闲鱼、不调用真实 DeepSeek。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import QueueItemStatus, init_db
from app.repositories.queue import QueueRepository
from app.seller_chat.goal_outcome import seller_agreed_to_price_cut, seller_refused_price_cut
from app.seller_chat.guardrails import scan_outbound_draft
from app.seller_chat.item_url import ItemUrlError, parse_item_reference
from app.services.queue_service import QueueService, QueueServiceError

LIVE_TEST_URL = (
    "https://www.goofish.com/item"
    "?spm=a21ybx.home.feedsCnxh.1.4c053da6UUS9nl&id=1067489371529&categoryId=126856825"
)
LIVE_TEST_ITEM_ID = "1067489371529"


def test_parse_item_reference_strips_tracking() -> None:
    """粘贴带追踪参数的链接应规范化为官方详情页。"""

    ref = parse_item_reference(LIVE_TEST_URL)
    assert ref.item_id == LIVE_TEST_ITEM_ID
    assert ref.detail_url == f"https://www.goofish.com/item?id={LIVE_TEST_ITEM_ID}"


def test_parse_item_id_only() -> None:
    """纯数字商品 ID 也可入队。"""

    ref = parse_item_reference(LIVE_TEST_ITEM_ID)
    assert ref.item_id == LIVE_TEST_ITEM_ID


def test_parse_rejects_non_goofish() -> None:
    """非官方域名应拒绝。"""

    with pytest.raises(ItemUrlError):
        parse_item_reference("https://example.com/item?id=1")


def test_agree_and_refuse_detection() -> None:
    """同意与拒绝降价的确定性判定。"""

    assert seller_agreed_to_price_cut(["可以便宜一点"])
    assert seller_refused_price_cut(["不能再便宜了，一口价"])
    assert not seller_agreed_to_price_cut(["不能再便宜了，一口价"])


def test_outbound_allows_negotiation_blocks_payment() -> None:
    """允许议价词，拦截付款承诺与店铺机器人腔。"""

    assert scan_outbound_draft("老板能便宜点吗") == ()
    findings = scan_outbound_draft("那我马上付款拍下")
    assert any(f.code == "purchase_commitment" for f in findings)
    bot = scan_outbound_draft("没有匹配到指令，我会把您的消息记录下来并通知店长。")
    assert any(f.code == "store_bot_voice" for f in bot)


@pytest.fixture()
def session_factory() -> sessionmaker:
    """创建临时 SQLite 会话工厂并写入默认设置。"""

    import uuid

    base = Path(__file__).resolve().parents[1] / ".pytest_tmp"
    base.mkdir(exist_ok=True)
    db_path = base / f"queue-{uuid.uuid4().hex}.db"
    factory = init_db(f"sqlite:///{db_path.as_posix()}")
    with factory() as session:
        QueueRepository(session).ensure_settings(
            reply_timeout_seconds=180,
            max_rounds=6,
            auto_send=True,
        )
    return factory


def test_enqueue_appends_and_reports_rank(session_factory: sessionmaker) -> None:
    """新链接追加队尾并返回排队名次。"""

    service = QueueService(session_factory)
    first = service.enqueue(LIVE_TEST_URL)
    second = service.enqueue("1067489371530")
    assert first.position_rank == 1
    assert second.position_rank == 2
    assert "排在第 2" in second.message


def test_only_one_active_claim(session_factory: sessionmaker) -> None:
    """全局最多一个 active。"""

    service = QueueService(session_factory)
    a = service.enqueue("1067489371531")
    b = service.enqueue("1067489371532")
    with session_factory() as session:
        repo = QueueRepository(session)
        first = repo.claim_next_queued()
        second = repo.claim_next_queued()
        assert first is not None and first.id == a.id
        assert first.status == QueueItemStatus.ACTIVE
        assert second is None
        parked = repo.get_item(a.id)
        assert parked is not None
        repo.mark_status(parked, QueueItemStatus.PARKED, summary="timeout")
        nxt = repo.claim_next_queued()
        assert nxt is not None and nxt.id == b.id


def test_prioritize_preempts_active(session_factory: sessionmaker) -> None:
    """优先插队会结束当前 active 并把目标拉到队首。"""

    service = QueueService(session_factory)
    a = service.enqueue("1067489371541")
    b = service.enqueue("1067489371542")
    with session_factory() as session:
        QueueRepository(session).claim_next_queued()
    out = service.prioritize(b.id)
    assert out.status == QueueItemStatus.QUEUED
    snapshot = service.list_queue()
    by_id = {item.id: item for item in snapshot.items}
    assert by_id[a.id].status == QueueItemStatus.DONE_MANUAL
    assert by_id[a.id].fail_code == "preempted"
    assert by_id[b.id].status == QueueItemStatus.QUEUED


def test_retry_only_parked(session_factory: sessionmaker) -> None:
    """只有暂挂项可以重试。"""

    service = QueueService(session_factory)
    item = service.enqueue("1067489371551")
    with pytest.raises(QueueServiceError):
        service.retry(item.id)
    with session_factory() as session:
        repo = QueueRepository(session)
        row = repo.get_item(item.id)
        assert row is not None
        repo.mark_status(row, QueueItemStatus.PARKED, summary="timeout")
    retried = service.retry(item.id)
    assert retried.status == QueueItemStatus.QUEUED
