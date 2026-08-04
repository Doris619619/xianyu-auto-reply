"""
本文件实现通过 CDP 附着本机可见浏览器的会话。

它属于 crawler.browser_backends 模块，仅服务手动回复模式。
不加载 storage_state，停止时不断开用户浏览器页面。
"""

from __future__ import annotations

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from app.crawler.chat_client import ChatSafetyError


class EdgeCdpBrowserSession:
    """
    通过本机 CDP 端点附着用户已打开的 Chromium 系浏览器。
    """

    def __init__(self, *, cdp_endpoint: str) -> None:
        """
        保存本机 CDP 地址；不立即连接。

        参数：
            cdp_endpoint: 已通过 Settings 校验的本机 HTTP 调试地址。
        """

        self._cdp_endpoint = cdp_endpoint
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def attached_over_cdp(self) -> bool:
        """CDP 会话始终附着到用户浏览器。"""

        return True

    @property
    def context(self) -> BrowserContext | None:
        """返回已附着的 BrowserContext，未连接则为 None。"""

        return self._context

    async def start(self) -> BrowserContext:
        """
        连接本机调试端口并复用第一个 context。

        无可用 context 时抛出 ChatSafetyError；连接失败会清理本进程侧资源后抛出。
        """

        if self._browser is not None and self._context is not None:
            return self._context
        if self._browser is not None or self._playwright is not None:
            await self.stop()

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self._cdp_endpoint
            )
            contexts = self._browser.contexts
            if not contexts:
                raise ChatSafetyError(
                    "cdp_context_missing",
                    "调试会话没有可用页面上下文",
                )
            self._context = contexts[0]
            return self._context
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """仅断开 Playwright 连接，不关闭用户浏览器或页面。"""

        self._context = None
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
