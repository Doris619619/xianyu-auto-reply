"""
本文件负责为单个订单绑定商品创建短生命周期 Playwright 聊天会话。

它属于 crawler 模块，使用既有 storage state 打开唯一闲鱼商品详情、发现并核验三方身份，
然后把受限 ``XianyuChatClient`` 交给上层。它不生成草稿、不访问业务数据库，也不点击购买、
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from playwright.async_api import Page

from app.crawler.chat_client import (
    ChatBinding,
    ChatMessageSnapshot,
    PolicyAllowedDraft,
    SendEvidence,
)
from app.crawler.product_context import ProductContext

# 可选恢复钩子：返回 True 表示已处理验证并应重新检查页面风险。
RiskRecoveryHook = Callable[[Page], Awaitable[bool]]


class ProcurementChatClient(Protocol):
    """
    定义编排器允许调用的最小聊天页面能力。

    协议只含打开、读取和发送已放行草稿，不暴露通用点击器或交易动作。
    """

    async def open_conversation(self) -> ChatMessageSnapshot:
        """打开绑定聊天并返回最新消息；页面不确定时抛出安全异常。"""

    async def read_latest_message(self) -> ChatMessageSnapshot:
        """读取绑定会话最新消息；无页面写入副作用。"""

    async def read_messages_after(
        self,
        baseline_fingerprint: str,
    ) -> list[ChatMessageSnapshot]:
        """读取基线之后全部可见消息；找不到基线时失败关闭。"""

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        """发送已由策略放行的唯一草稿；不得自行重试。"""


@dataclass(frozen=True, slots=True)
class OpenedXianyuChat:
    """
    组合经页面核验的不可变三方绑定与受限聊天客户端。

    对象只在工厂上下文内有效；离开上下文后底层页面与浏览器会关闭。
    """

    binding: ChatBinding
    client: ProcurementChatClient
    product: ProductContext = ProductContext()


class ProcurementChatFactory(Protocol):
    """
    定义可用离线 fake 替换的订单绑定聊天上下文工厂。

    工厂只能打开调用方指定的单个商品，不允许扫描其他私聊。
    """

    def open(
        self,
        *,
        item_url: str,
        source_item_id: str,
        expected_seller_id: str | None,
        expected_account_id: str,
    ) -> AbstractAsyncContextManager[OpenedXianyuChat]:
        """
        返回异步上下文管理器；具体协议由 ``async with`` 消费并产出 ``OpenedXianyuChat``。

        输入订单绑定；无立即网络副作用；实现可在进入上下文时访问页面。
        """
