"""
本文件实现 CloakBrowser（自定义 Chromium）浏览器会话。

它属于 crawler.browser_backends 模块，通过可选依赖 cloakbrowser 启动，按需加载 storage_state。
不连接 CDP，不处理商品聊天业务。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext

from app.crawler.chat_client import ChatSafetyError


class CloakBrowserSession:
    """
    使用 CloakBrowser 启动自定义 Chromium，并加载独立登录态。
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
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    @property
    def attached_over_cdp(self) -> bool:
        """CloakBrowser 会话从不通过 CDP 附着。"""

        return False

    @property
    def context(self) -> BrowserContext | None:
        """返回已创建的 BrowserContext，未启动则为 None。"""

        return self._context

    async def start(self) -> BrowserContext:
        """
        启动 CloakBrowser，并按需加载登录态。

        缺少可选依赖或配置的登录态文件时抛出明确错误；启动失败会先清理再抛出。
        """

        if self._browser is not None and self._context is not None:
            return self._context
        if self._browser is not None:
            await self.stop()

        if self._storage_state_path is not None and not self._storage_state_path.is_file():
            raise ChatSafetyError("login_state_missing", "闲鱼登录态文件不存在")

        launch_async = _import_cloakbrowser_launch()
        try:
            self._browser = await launch_async(headless=self._headless)
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
        """关闭 context 与 CloakBrowser；可重复调用。"""

        if self._context is not None:
            await self._context.close()
        self._context = None
        if self._browser is not None:
            await self._browser.close()
        self._browser = None


def _import_cloakbrowser_launch() -> Any:
    """
    延迟导入 cloakbrowser.launch_async。

    未安装可选依赖时抛出带安装提示的 RuntimeError。
    """

    try:
        from cloakbrowser import launch_async
    except ImportError as error:
        raise RuntimeError(
            "当前 XIANYU_BROWSER_BACKEND=cloakbrowser，但未安装 cloakbrowser。"
            '请执行：pip install -e ".[cloakbrowser]"'
        ) from error
    return launch_async
