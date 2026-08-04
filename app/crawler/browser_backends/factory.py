"""
本文件根据配置创建 AI 或手动模式的浏览器会话。

它属于 crawler.browser_backends 模块，是 Worker 与聊天工厂的唯一分发入口。
不在风控后自动轮换后端，不打开商品页。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.crawler.browser_backends.base import BrowserSession
from app.crawler.browser_backends.camoufox import CamoufoxBrowserSession
from app.crawler.browser_backends.cdp import EdgeCdpBrowserSession
from app.crawler.browser_backends.chromium import ChromiumBrowserSession
from app.crawler.browser_backends.cloakbrowser import CloakBrowserSession
from app.crawler.browser_backends.storage import BrowserBackend


def create_ai_browser_session(settings: Settings) -> BrowserSession:
    """
    按 ``XIANYU_BROWSER_BACKEND`` 创建 AI Worker 使用的独立浏览器会话。

    忽略 ``xianyu_cdp_endpoint``；CDP 仅用于手动模式。
    未知后端名在 Settings 校验阶段已拒绝；此处不再静默回退。
    """

    return _create_independent_browser_session(
        settings.xianyu_browser_backend,
        storage_state_path=settings.resolved_storage_state_path(),
        headless=settings.xianyu_headless,
    )


def create_login_browser_session(settings: Settings) -> BrowserSession:
    """
    按 ``XIANYU_BROWSER_BACKEND`` 创建人工登录使用的有头空白浏览器会话。

    不加载旧登录态，也不使用 ``xianyu_cdp_endpoint``；人工登录完成后由调用方显式保存状态。
    """

    return _create_independent_browser_session(
        settings.xianyu_browser_backend,
        storage_state_path=None,
        headless=False,
    )


def _create_independent_browser_session(
    backend: BrowserBackend,
    *,
    storage_state_path: Path | None,
    headless: bool,
) -> BrowserSession:
    """按后端名创建独立浏览器会话；不处理 CDP 或聊天业务。"""

    if backend == "chromium":
        return ChromiumBrowserSession(storage_state_path=storage_state_path, headless=headless)
    if backend == "camoufox":
        return CamoufoxBrowserSession(storage_state_path=storage_state_path, headless=headless)
    if backend == "cloakbrowser":
        return CloakBrowserSession(storage_state_path=storage_state_path, headless=headless)
    raise ValueError(f"不支持的浏览器后端：{backend}")


def create_manual_cdp_session(settings: Settings) -> BrowserSession | None:
    """
    在配置了本机 CDP 时创建手动回复用的附着会话。

    未配置 ``XIANYU_CDP_ENDPOINT`` 时返回 None；与 AI 浏览器后端无关。
    """

    endpoint = settings.xianyu_cdp_endpoint
    if not endpoint:
        return None
    return EdgeCdpBrowserSession(cdp_endpoint=endpoint)
