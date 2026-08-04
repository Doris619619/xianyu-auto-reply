"""
本文件验证卖家回复等待只读取聊天 DOM，不触发整页刷新。

它使用内存聊天客户端，不启动浏览器或访问闲鱼。
"""

from __future__ import annotations

import pytest

from app.crawler.chat_client import ChatMessageSnapshot
from app.seller_chat.session import SellerChatSession


class _PollingClient:
    """记录增量读取和已废弃刷新接口的调用次数。"""

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.read_after_calls = 0

    async def refresh_conversation(self) -> None:
        """保留旧接口以证明等待逻辑不会再调用它。"""

        self.refresh_calls += 1

    async def read_messages_after(
        self,
        _baseline_fingerprint: str,
    ) -> list[ChatMessageSnapshot]:
        """始终没有新卖家消息。"""

        self.read_after_calls += 1
        return []


@pytest.mark.asyncio
async def test_wait_for_seller_reply_does_not_reload_page() -> None:
    """轮询等待只能读取 DOM 并退避，不能调用旧的整页刷新接口。"""

    client = _PollingClient()
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """记录退避时长但不真实等待。"""

        delays.append(seconds)

    session = SellerChatSession(
        client=client,  # type: ignore[arg-type]
        generator=object(),  # type: ignore[arg-type]
        system_prompt="",
        opening_brief="",
        sleep=fake_sleep,
    )

    assert await session.wait_for_seller_reply(timeout_seconds=5.0) is None
    assert client.refresh_calls == 0
    assert client.read_after_calls == 3
    assert delays == [2.0, 3.0]
