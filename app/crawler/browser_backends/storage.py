"""
本文件负责按浏览器后端解析闲鱼登录态文件路径。

它属于 crawler.browser_backends 模块，供配置、登录脚本与会话工厂共用。
不读取登录态内容，不启动浏览器。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

BrowserBackend = Literal["chromium", "camoufox", "cloakbrowser"]

SUPPORTED_BROWSER_BACKENDS: frozenset[str] = frozenset(
    {"chromium", "camoufox", "cloakbrowser"}
)

LEGACY_CHROMIUM_STORAGE_STATE = Path("storage_state.json")


def default_storage_state_path(backend: BrowserBackend) -> Path:
    """
    返回指定后端的默认登录态路径。

    参数：
        backend: AI Worker 使用的浏览器后端名称。

    返回：
        ``data/browser_states/{backend}_storage_state.json``。
    """

    return Path("data") / "browser_states" / f"{backend}_storage_state.json"


def resolve_storage_state_path(
    *,
    backend: BrowserBackend,
    configured_path: str | None,
) -> Path:
    """
    解析实际应使用的登录态文件路径。

    规则：
    1. 显式配置了路径则使用该路径；
    2. 否则使用后端默认分文件路径；
    3. chromium 且默认文件不存在、根目录旧 ``storage_state.json`` 存在时回退旧路径。

    参数：
        backend: 浏览器后端。
        configured_path: 环境变量 ``XIANYU_STORAGE_STATE_PATH`` 的值；空串视为未配置。

    返回：
        解析后的 Path；本函数不检查文件是否存在。
    """

    if configured_path is not None and configured_path.strip():
        return Path(configured_path.strip())

    backend_default = default_storage_state_path(backend)
    if (
        backend == "chromium"
        and not backend_default.is_file()
        and LEGACY_CHROMIUM_STORAGE_STATE.is_file()
    ):
        return LEGACY_CHROMIUM_STORAGE_STATE
    return backend_default
