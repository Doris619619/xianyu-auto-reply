"""
本文件实现长驻 Playwright 浏览器上的商品聊天打开工厂。

它属于 crawler 模块：浏览器与 context 在 Worker 生命周期内复用，每个商品使用新 page。
不生成草稿、不访问业务数据库。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import Settings
from app.crawler.chat_client import (
    ChatSafetyError,
    XianyuChatClient,
    discover_chat_binding,
    item_url_matches_binding,
)
from app.crawler.chat_runtime import OpenedXianyuChat, RiskRecoveryHook
from app.crawler.risk_control import detect_risk_response
from app.services.xianyu_account_guard import AccountAccessGuard


class PersistentPlaywrightChatFactory:
    """
    复用同一 Chromium context，按商品打开新页面并产出受限聊天客户端。
    """

    def __init__(self, settings: Settings, account_guard: AccountAccessGuard) -> None:
        """
        保存配置与账号锁；不立即启动浏览器。
        """

        self._settings = settings
        self._account_guard = account_guard
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._attached_over_cdp = False

    async def start(self) -> None:
        """
        启动 Playwright 并加载登录态。

        登录态文件缺失时抛出 ChatSafetyError。
        若上次启动半途失败（有 browser 无 context），会先清理再重建，避免 open 时断言失败。
        """

        if self._browser is not None and self._context is not None:
            return
        if self._browser is not None or self._playwright is not None:
            await self.stop()

        try:
            self._playwright = await async_playwright().start()
            if self._settings.xianyu_cdp_endpoint:
                # 真实 Edge 的调试会话由用户持有；Worker 停止时只能断开，不能关闭其页面。
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self._settings.xianyu_cdp_endpoint
                )
                contexts = self._browser.contexts
                if not contexts:
                    raise ChatSafetyError("cdp_context_missing", "Edge 调试会话没有可用页面上下文")
                self._context = contexts[0]
                self._attached_over_cdp = True
            else:
                state_path = Path(self._settings.xianyu_storage_state_path)
                if not state_path.is_file():
                    raise ChatSafetyError("login_state_missing", "闲鱼登录态文件不存在")
                self._browser = await self._playwright.chromium.launch(
                    headless=self._settings.xianyu_headless
                )
                self._context = await self._browser.new_context(storage_state=str(state_path))
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """关闭浏览器与 Playwright；可重复调用。"""

        if self._context is not None and not self._attached_over_cdp:
            await self._context.close()
        self._context = None
        if self._browser is not None and not self._attached_over_cdp:
            await self._browser.close()
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._attached_over_cdp = False

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
        if self._context is None:
            await self.start()
        assert self._context is not None

        for attempt in range(2):
            blocked_reason: str | None = None
            page: Page | None = None
            listener_registered = False
            initialized = False
            try:
                page = await self._context.new_page()

                def observe_status(response: Response) -> None:
                    """记录首次风控信号。"""

                    nonlocal blocked_reason
                    reason = detect_risk_response(response.url, response.status)
                    if reason and blocked_reason is None:
                        blocked_reason = reason

                self._context.on("response", observe_status)
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
                    self._context.remove_listener("response", observe_status)
                if page is not None:
                    await page.close()
