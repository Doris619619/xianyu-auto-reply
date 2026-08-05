"""
本文件实现长驻浏览器上的商品聊天打开工厂。

它属于 crawler 模块：浏览器与 context 在 Worker 生命周期内复用，每个商品使用新 page。
浏览器启动细节由 browser_backends 会话负责；本文件不生成草稿、不访问业务数据库。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import Settings
from app.crawler.browser_backends.base import BrowserSession
from app.crawler.chat_client import (
    ChatSafetyError,
    XianyuChatClient,
    discover_chat_binding,
    item_url_matches_binding,
)
from app.crawler.chat_runtime import OpenedXianyuChat, RiskRecoveryHook
from app.crawler.product_context import extract_product_context
from app.crawler.risk_control import detect_risk_response
from app.services.xianyu_account_guard import AccountAccessGuard

product_context_logger = logging.getLogger("app.decision_diagnostic")


class PersistentPlaywrightChatFactory:
    """
    复用同一 BrowserContext，按商品打开新页面并产出受限聊天客户端。
    """

    def __init__(
        self,
        settings: Settings,
        account_guard: AccountAccessGuard,
        *,
        browser_session: BrowserSession,
    ) -> None:
        """
        保存配置、账号锁与已注入的浏览器会话；不立即启动浏览器。

        参数：
            settings: 含超时等运行参数的配置。
            account_guard: 账号访问锁。
            browser_session: AI 独立浏览器或手动 CDP 会话实现。
        """

        self._settings = settings
        self._account_guard = account_guard
        self._browser_session = browser_session

    async def start(self) -> None:
        """
        启动底层浏览器会话并准备可复用 context。

        登录态缺失或 CDP 不可用时抛出 ChatSafetyError / 连接异常。
        若上次启动半途失败，会话实现会先清理再重建。
        """

        await self._browser_session.start()

    async def stop(self) -> None:
        """关闭或断开浏览器会话；可重复调用。"""

        await self._browser_session.stop()

    @asynccontextmanager
    async def open(
        self,
        *,
        item_url: str,
        source_item_id: str,
        expected_seller_id: str | None,
        expected_account_id: str,
        risk_recovery: RiskRecoveryHook | None = None,
    ) -> AsyncIterator[OpenedXianyuChat]:
        """
        在长驻 context 上打开商品详情并产出聊天客户端；退出时关闭该 page。
        """

        if not item_url_matches_binding(item_url, source_item_id):
            raise ChatSafetyError("item_url_mismatch", "商品 URL 与绑定不一致")
        if self._browser_session.context is None:
            await self.start()
        context = self._browser_session.context
        assert context is not None

        for attempt in range(2):
            blocked_reason: str | None = None
            page: Page | None = None
            listener_registered = False
            initialized = False
            try:
                page = await context.new_page()

                def observe_status(response: Response) -> None:
                    """记录首次风控信号。"""

                    nonlocal blocked_reason
                    reason = detect_risk_response(response.url, response.status)
                    if reason and blocked_reason is None:
                        blocked_reason = reason

                context.on("response", observe_status)
                listener_registered = True
                async with self._account_guard.hold():
                    navigation = await page.goto(
                        item_url,
                        wait_until="domcontentloaded",
                        timeout=self._settings.xianyu_verify_timeout_seconds * 1000,
                    )
                navigation_status = navigation.status if navigation is not None else 0
                navigation_reason = detect_risk_response(page.url, navigation_status)
                risk_reason = blocked_reason or navigation_reason
                if risk_reason:
                    recovered = False
                    if risk_recovery is not None:
                        recovered = await risk_recovery(page)
                    if recovered:
                        blocked_reason = None
                        async with self._account_guard.hold():
                            await page.reload(
                                wait_until="domcontentloaded",
                                timeout=self._settings.xianyu_verify_timeout_seconds * 1000,
                            )
                        risk_reason = blocked_reason or detect_risk_response(page.url, 0)
                    if risk_reason:
                        raise ChatSafetyError("http_risk_blocked", risk_reason)
                product = await extract_product_context(page)
                product_context_logger.info(
                    "source_item_id=%s product_context_source=%s list_price=%s freight=%s",
                    source_item_id,
                    product.source,
                    product.list_price,
                    product.freight,
                )
                binding = await discover_chat_binding(
                    page,
                    source_item_id=source_item_id,
                    expected_account_id=expected_account_id,
                    account_guard=self._account_guard,
                )
                if expected_seller_id is not None and binding.seller_id != expected_seller_id:
                    raise ChatSafetyError(
                        "seller_identity_mismatch",
                        "页面卖家身份与任务已锁定卖家不一致",
                    )
                initialized = True
                yield OpenedXianyuChat(
                    binding=binding,
                    client=XianyuChatClient(page, binding, self._account_guard),
                    product=product,
                )
                return
            except ChatSafetyError:
                raise
            except PlaywrightTimeoutError:
                if attempt == 0 and not initialized:
                    continue
                if blocked_reason:
                    raise ChatSafetyError("http_risk_blocked", blocked_reason) from None
                raise ChatSafetyError("chat_page_timeout", "闲鱼聊天页面访问超时") from None
            except Exception as error:
                # 重试仅覆盖进入会话前的读页面阶段；yield 之后可能已经有发送操作，
                # 必须原样传播会话内部错误，既不能重试，也不能把 LLM/同步错误误报成页面错误。
                if initialized:
                    raise
                if attempt == 0 and not initialized:
                    continue
                raise ChatSafetyError("chat_page_error", "闲鱼聊天页面无法安全确认") from error
            finally:
                if listener_registered:
                    context.remove_listener("response", observe_status)
                if page is not None:
                    await page.close()
