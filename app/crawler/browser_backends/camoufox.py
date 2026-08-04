"""
本文件实现 Camoufox（Firefox 系）浏览器会话。

它属于 crawler.browser_backends 模块，通过可选依赖 camoufox 启动，按需加载 storage_state。
不连接 CDP，不处理商品聊天业务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext

from app.crawler.chat_client import ChatSafetyError


class CamoufoxBrowserSession:
    """
    使用 Camoufox 启动 Firefox 系浏览器，并加载独立登录态。
    """

    def __init__(self, *, storage_state_path: Path | None, headless: bool) -> None:
        """
        保存启动参数；不立即启动浏览器。

        参数：
            storage_state_path: 该后端专用登录态路径；为 None 时启动空白 context。
            headless: 是否无头启动。
        """

        self._storage_state_path = storage_state_path
        self._headless = headless
        self._launcher: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def attached_over_cdp(self) -> bool:
        """Camoufox 会话从不通过 CDP 附着。"""

        return False

    @property
    def context(self) -> BrowserContext | None:
        """返回已创建的 BrowserContext，未启动则为 None。"""

        return self._context

    async def start(self) -> BrowserContext:
        """
        启动 Camoufox，并按需加载登录态。

        缺少可选依赖或配置的登录态文件时抛出明确错误；启动失败会先清理再抛出。
        """

        if self._browser is not None and self._context is not None:
            return self._context
        if self._browser is not None or self._launcher is not None:
            await self.stop()

        if self._storage_state_path is not None and not self._storage_state_path.is_file():
            raise ChatSafetyError("login_state_missing", "闲鱼登录态文件不存在")

        async_camoufox_cls = _import_async_camoufox()
        try:
            self._launcher = async_camoufox_cls(headless=self._headless)
            self._browser = await self._launcher.__aenter__()
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
        """关闭 context 与 Camoufox；可重复调用。"""

        if self._context is not None:
            await self._context.close()
        self._context = None
        self._browser = None
        if self._launcher is not None:
            try:
                await self._launcher.__aexit__(None, None, None)
            finally:
                self._launcher = None


def _import_async_camoufox() -> Any:
    """
    延迟导入 camoufox.async_api.AsyncCamoufox。

    未安装可选依赖时抛出带安装提示的 RuntimeError。
    """

    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as error:
        raise RuntimeError(
            "当前 XIANYU_BROWSER_BACKEND=camoufox，但未安装 camoufox。"
            '请执行：pip install -e ".[camoufox]"'
        ) from error
    return AsyncCamoufox
