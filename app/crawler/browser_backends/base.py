"""
本文件定义浏览器会话的统一协议。

它属于 crawler.browser_backends 模块，供各后端实现与 PersistentPlaywrightChatFactory 使用。
不负责具体浏览器启动，不打开商品页或发送消息。
"""

from __future__ import annotations

from typing import Protocol

from playwright.async_api import BrowserContext


class BrowserSession(Protocol):
    """
    长驻浏览器会话的最小协议：提供可复用的 BrowserContext。
    """

    @property
    def attached_over_cdp(self) -> bool:
        """是否通过 CDP 附着到用户浏览器（停止时不得关闭对方页面）。"""

    @property
    def context(self) -> BrowserContext | None:
        """已启动时返回 Playwright BrowserContext；未启动返回 None。"""

    async def start(self) -> BrowserContext:
        """
        启动或附着浏览器并返回可复用的 context。

        登录态缺失、CDP 不可用或依赖未安装时抛出异常；可安全重复调用。
        """

    async def stop(self) -> None:
        """释放本进程持有的浏览器资源；CDP 会话只断开不关用户浏览器。"""
