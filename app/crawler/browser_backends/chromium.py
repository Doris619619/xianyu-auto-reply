"""
本文件实现标准 Playwright Chromium 浏览器会话。

它属于 crawler.browser_backends 模块，可从本地 storage_state 启动独立 Chromium，
也可为人工登录启动空白 context。
不连接 CDP，不处理商品聊天业务。
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from app.crawler.chat_client import ChatSafetyError


class ChromiumBrowserSession:
    """
    使用 Playwright 官方 Chromium + storage_state 的长驻会话。
    """

    def __init__(self, *, storage_state_path: Path | None, headless: bool) -> None:
        """
        保存启动参数；不立即启动浏览器。

        参数：
            storage_state_path: 登录态文件路径；为 None 时启动空白 context 供人工登录。
            headless: 是否无头启动。
        """

        self._storage_state_path = storage_state_path
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def attached_over_cdp(self) -> bool:
        """标准 Chromium 会话从不通过 CDP 附着。"""

        return False

    @property
    def context(self) -> BrowserContext | None:
        """返回已创建的 BrowserContext，未启动则为 None。"""

        return self._context

    async def start(self) -> BrowserContext:
        """
        启动 Chromium，并按需加载登录态。

        配置登录态但文件缺失时抛出 ChatSafetyError；启动失败会先清理再抛出。
        """

        if self._browser is not None and self._context is not None:
            return self._context
        if self._browser is not None or self._playwright is not None:
            await self.stop()

        if self._storage_state_path is not None and not self._storage_state_path.is_file():
            raise ChatSafetyError("login_state_missing", "闲鱼登录态文件不存在")

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self._headless)
            context_options = (
                {"storage_state": str(self._storage_state_path)}
                if self._storage_state_path is not None
                else {}
            )
            self._context = await self._browser.new_context(**context_options)
            return self._context
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        """关闭 context、browser 与 Playwright；可重复调用。"""

        if self._context is not None:
            await self._context.close()
        self._context = None
        if self._browser is not None:
            await self._browser.close()
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
