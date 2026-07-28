"""
本文件负责为单个订单绑定商品创建短生命周期 Playwright 聊天会话。

它属于 crawler 模块，使用既有 storage state 打开唯一闲鱼商品详情、发现并核验三方身份，
然后把受限 ``XianyuChatClient`` 交给上层。它不生成草稿、不访问业务数据库，也不点击购买、
付款、地址或确认订单控件；403、429、登录、验证码和页面漂移均失败关闭且不重试。
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.async_api import Page, Response, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# 可选恢复钩子：返回 True 表示已处理验证并应重新检查页面风险。
RiskRecoveryHook = Callable[[Page], Awaitable[bool]]

from app.core.config import Settings
from app.crawler.chat_client import (
    ChatBinding,
    ChatMessageSnapshot,
    ChatSafetyError,
    PolicyAllowedDraft,
    SendEvidence,
    XianyuChatClient,
    discover_chat_binding,
    item_url_matches_binding,
)
from app.crawler.risk_control import detect_risk_response
from app.services.xianyu_account_guard import AccountAccessGuard


class ProcurementChatClient(Protocol):
    """
    定义编排器允许调用的最小聊天页面能力。

    协议只含打开、读取和发送已放行草稿，不暴露通用点击器或交易动作。
    """

    async def open_conversation(self) -> ChatMessageSnapshot:
        """打开绑定聊天并返回最新消息；页面不确定时抛出安全异常。"""

    async def read_latest_message(self) -> ChatMessageSnapshot:
        """读取绑定会话最新消息；无页面写入副作用。"""

    async def read_messages_after(
        self,
        baseline_fingerprint: str,
    ) -> list[ChatMessageSnapshot]:
        """读取基线之后全部可见消息；找不到基线时失败关闭。"""

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        """发送已由策略放行的唯一草稿；不得自行重试。"""


@dataclass(frozen=True, slots=True)
class OpenedXianyuChat:
    """
    组合经页面核验的不可变三方绑定与受限聊天客户端。

    对象只在工厂上下文内有效；离开上下文后底层页面与浏览器会关闭。
    """

    binding: ChatBinding
    client: ProcurementChatClient


class ProcurementChatFactory(Protocol):
    """
    定义可用离线 fake 替换的订单绑定聊天上下文工厂。

    工厂只能打开调用方指定的单个商品，不允许扫描其他私聊。
    """

    def open(
        self,
        *,
        item_url: str,
        source_item_id: str,
        expected_seller_id: str | None,
        expected_account_id: str,
    ) -> AbstractAsyncContextManager[OpenedXianyuChat]:
        """
        返回异步上下文管理器；具体协议由 ``async with`` 消费并产出 ``OpenedXianyuChat``。

        输入订单绑定；无立即网络副作用；实现可在进入上下文时访问页面。
        """


class PlaywrightXianyuChatFactory:
    """
    从本地登录态为一个订单绑定商品创建单次、无重试聊天客户端。

    调用方必须显式提供预期账号 ID；卖家 ID 可在首次进入详情页时安全发现并由上层持久化。
    """

    def __init__(self, settings: Settings, account_guard: AccountAccessGuard) -> None:
        """
        保存浏览器配置与跨进程账号 guard。

        输入类型安全配置和 guard；无返回；初始化不读取登录态、不启动浏览器或访问网络。
        """

        self._settings = settings
        self._account_guard = account_guard

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
        打开一次绑定详情页、发现身份并产出受限聊天客户端。

        输入服务端商品 URL、商品 ID、可选已锁定卖家 ID 和必填账号 ID；产出上下文内客户端；
        登录态缺失、HTTP 403/429、身份不匹配或页面超时抛出 ``ChatSafetyError``。
        可选 ``risk_recovery`` 仅在检测到风控信号时调用一次（例如 spike 滑块）；
        生产编排不得传入该钩子，以保持失败关闭默认策略。
        """

        if not item_url_matches_binding(item_url, source_item_id):
            raise ChatSafetyError("item_url_mismatch", "服务端商品 URL 与任务绑定不一致")
        state_path = Path(self._settings.xianyu_storage_state_path)
        if not state_path.is_file():
            raise ChatSafetyError("login_state_missing", "闲鱼登录态文件不存在")

        blocked_reason: str | None = None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=self._settings.xianyu_headless)
                try:
                    context = await browser.new_context(storage_state=str(state_path))
                    page = await context.new_page()

                    def observe_status(response: Response) -> None:
                        """
                        记录当前页面生命周期首次访问控制信号，不读取响应正文。

                        输入 Playwright 响应；无返回；副作用仅更新闭包中的粗粒度原因。
                        除 403/429 外，闲鱼返回 HTTP 200 的 TMD 风控页也必须失败关闭。
                        """

                        nonlocal blocked_reason
                        reason = detect_risk_response(response.url, response.status)
                        if reason and blocked_reason is None:
                            blocked_reason = reason

                    # 使用上下文级监听覆盖商品页和点击后新打开的聊天页。
                    context.on("response", observe_status)
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
                                    timeout=(
                                        self._settings.xianyu_verify_timeout_seconds * 1000
                                    ),
                                )
                            risk_reason = blocked_reason or detect_risk_response(
                                page.url,
                                0,
                            )
                        if risk_reason:
                            raise ChatSafetyError(
                                "http_risk_blocked",
                                risk_reason,
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
                    yield OpenedXianyuChat(
                        binding=binding,
                        client=XianyuChatClient(page, binding, self._account_guard),
                    )
                finally:
                    await browser.close()
        except ChatSafetyError:
            raise
        except PlaywrightTimeoutError:
            if blocked_reason:
                raise ChatSafetyError("http_risk_blocked", blocked_reason) from None
            raise ChatSafetyError("chat_page_timeout", "闲鱼聊天页面访问超时") from None
        except Exception:
            raise ChatSafetyError("chat_page_error", "闲鱼聊天页面无法安全确认") from None
