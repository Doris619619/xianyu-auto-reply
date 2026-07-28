"""
本文件负责进程内串行化闲鱼账号访问。

它属于 services 模块：本工具单机运行，只使用 asyncio.Lock，不依赖 PostgreSQL。
不启动 Playwright、不读取登录态。
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol, TypeAlias


class AccountAccessGuard(Protocol):
    """
    定义闲鱼账号独占访问的异步上下文接口。
    """

    def hold(self) -> AbstractAsyncContextManager[None]:
        """返回一次账号独占租约的异步上下文。"""


AccountGuardInput: TypeAlias = AccountAccessGuard | asyncio.Lock


class AsyncioLockAccountGuard:
    """
    将 ``asyncio.Lock`` 适配为统一账号访问协议。
    """

    def __init__(self, lock: asyncio.Lock) -> None:
        """
        保存调用方提供的进程内锁。

        参数为 ``asyncio.Lock``；不立即获取锁。
        """

        self.lock = lock

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """
        在已有进程内锁中提供一次独占访问。
        """

        async with self.lock:
            yield


def normalize_account_guard(account_guard: AccountGuardInput | None) -> AccountAccessGuard:
    """
    将可选 guard 或 ``asyncio.Lock`` 转换为统一协议。

    参数为空时创建新的进程内 guard。
    """

    if account_guard is None:
        return AsyncioLockAccountGuard(asyncio.Lock())
    if isinstance(account_guard, asyncio.Lock):
        return AsyncioLockAccountGuard(account_guard)
    return account_guard
