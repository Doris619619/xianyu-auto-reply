"""
本文件实现砍价队列的单飞 Worker。

它属于 worker 模块：全局最多一个 active 会话；超时暂挂后自动领取下一家；
卖家回复后锁定深聊直至同意/拒绝/手动停/轮次上限。

可通过注入 Fake 聊天工厂与 Fake LLM 做离线测试，不启动真实浏览器。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import sessionmaker

from app.crawler.chat_client import ChatSafetyError, ChatSendUncertainError
from app.crawler.chat_runtime import OpenedXianyuChat
from app.models import QueueItemStatus
from app.repositories.queue import QueueRepository
from app.schemas.queue import BrowserConnectionOut
from app.seller_chat.config import load_spike_config
from app.seller_chat.goal_outcome import seller_agreed_to_price_cut, seller_refused_price_cut
from app.seller_chat.guardrails import scan_outbound_draft
from app.seller_chat.llm import SellerChatDraftGenerator, SellerChatLlmError, TranscriptEntry
from app.seller_chat.prompts import DEFAULT_GOAL, build_opening_brief, build_system_prompt
from app.seller_chat.session import SellerChatSession
from app.services.xianyu_account_guard import normalize_account_guard

logger = logging.getLogger(__name__)

SLEEP_CALLABLE = Callable[[float], Awaitable[None]]


@dataclass(slots=True)
class ManualReplyCommand:
    """承载面板已确认文本及其页面发送确认结果。"""

    text: str
    completion: asyncio.Future[int]


def _seller_texts_after_last_me(
    transcript: tuple[TranscriptEntry, ...] | list[TranscriptEntry],
) -> tuple[str, ...]:
    """
    取出聊天记录里最后一条「我」之后的全部卖家文本。

    用于续跑时判断卖家是否已经回复过、无需再空等。
    """

    pending: list[str] = []
    for entry in reversed(transcript):
        if entry.speaker == "me":
            break
        if entry.speaker == "seller":
            pending.append(entry.text)
    pending.reverse()
    return tuple(pending)


class ChatOpenFactory(Protocol):
    """Worker 需要的最小聊天工厂协议。"""

    async def start(self) -> None:
        """启动浏览器资源（可为空操作）。"""

    async def stop(self) -> None:
        """释放浏览器资源。"""

    def open(self, **kwargs: Any) -> Any:
        """返回异步上下文，产出 OpenedXianyuChat。"""


@dataclass(slots=True)
class BargainWorker:
    """
    单飞砍价 Worker。

    同一时刻只处理一个 active 队列项；外部插队/停止通过数据库状态与 cancel 事件协作。
    """

    session_factory: sessionmaker
    chat_factory: ChatOpenFactory
    draft_generator: SellerChatDraftGenerator
    expected_account_id: str
    manual_chat_factory: ChatOpenFactory | None = None
    sleep: SLEEP_CALLABLE = asyncio.sleep
    poll_idle_seconds: float = 2.0
    _stop_event: asyncio.Event = field(init=False, repr=False)
    _cancel_current: asyncio.Event = field(init=False, repr=False)
    _task: asyncio.Task[None] | None = field(init=False, default=None, repr=False)
    _running: bool = field(init=False, default=False, repr=False)
    _manual_commands: asyncio.Queue[ManualReplyCommand] = field(init=False, repr=False)
    _manual_item_id: int | None = field(init=False, default=None, repr=False)
    _manual_command_pending: bool = field(init=False, default=False, repr=False)
    _manual_browser_connected: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        """初始化协作事件与运行标志。"""

        self._stop_event = asyncio.Event()
        self._cancel_current = asyncio.Event()
        self._task = None
        self._running = False
        self._manual_commands = asyncio.Queue()
        self._manual_item_id = None
        self._manual_command_pending = False
        self._manual_browser_connected = False

    @property
    def running(self) -> bool:
        """返回后台任务是否在跑。"""

        return self._running and self._task is not None and not self._task.done()

    def request_cancel_current(self) -> None:
        """请求取消当前会话（插队或手动停止）。"""

        self._cancel_current.set()

    def manual_browser_status(self) -> BrowserConnectionOut:
        """返回手动回复所需 CDP 浏览器的非敏感连接状态。"""

        if self.manual_chat_factory is None:
            return BrowserConnectionOut(
                configured=False,
                connected=False,
                message="未配置 XIANYU_CDP_ENDPOINT，手动回复需要连接可见 Chrome",
            )
        if self._manual_browser_connected:
            return BrowserConnectionOut(
                configured=True,
                connected=True,
                message="已连接可见 Chrome",
            )
        return BrowserConnectionOut(
            configured=True,
            connected=False,
            message="已配置 CDP，等待手动会话连接可见 Chrome",
        )

    def manual_send_available(self, item_id: int | None) -> bool:
        """仅当前手动会话且没有未完成提交时允许面板发送。"""

        return bool(
            item_id is not None
            and self._manual_item_id == item_id
            and not self._manual_command_pending
        )

    async def submit_manual_reply(self, text: str) -> int:
        """接收一条人工确认文本，并等待聊天页面回读确认后返回轮次。"""

        normalized = text.strip()
        if not normalized:
            raise ValueError("回复内容不能为空")
        if not self.manual_send_available(self._manual_item_id):
            raise ValueError("当前没有可发送的手动会话，或上一条消息仍在确认")
        self._manual_command_pending = True
        completion: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        await self._manual_commands.put(ManualReplyCommand(text=normalized, completion=completion))
        try:
            return await completion
        finally:
            self._manual_command_pending = False

    async def start(self) -> None:
        """启动后台循环；已在运行则忽略。"""

        if self.running:
            return
        self._stop_event.clear()
        self._cancel_current.clear()
        self._running = True
        await self.chat_factory.start()
        self._task = asyncio.create_task(self._loop(), name="bargain-worker")

    async def stop(self) -> None:
        """停止后台循环并关闭浏览器。"""

        self._stop_event.set()
        self._cancel_current.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        self._running = False
        self._manual_item_id = None
        self._reject_pending_manual_command("Worker 已停止，未发送该条手动回复")
        await self.chat_factory.stop()
        if (
            self.manual_chat_factory is not None
            and self.manual_chat_factory is not self.chat_factory
        ):
            await self.manual_chat_factory.stop()
        self._manual_browser_connected = False

    async def _loop(self) -> None:
        """主循环：有队列则处理，否则空闲等待。"""

        try:
            while not self._stop_event.is_set():
                if not self._is_worker_enabled():
                    await self.sleep(self.poll_idle_seconds)
                    continue
                item_id = self._claim_next_id()
                if item_id is None:
                    await self.sleep(self.poll_idle_seconds)
                    continue
                self._cancel_current.clear()
                try:
                    await self._process_item(item_id)
                except Exception as error:
                    logger.exception("处理队列项 %s 时发生未捕获异常", item_id)
                    self._fail_item(
                        item_id,
                        code="worker_crash",
                        summary=f"Worker异常[{type(error).__name__}]：{error}",
                    )
        finally:
            self._running = False

    def _is_worker_enabled(self) -> bool:
        """读取数据库中的 worker_enabled 开关。"""

        with self.session_factory() as session:
            return bool(QueueRepository(session).get_settings().worker_enabled)

    def _claim_next_id(self) -> int | None:
        """优先恢复唯一 active 会话，否则领取下一项并返回主键。"""

        with self.session_factory() as session:
            repo = QueueRepository(session)
            settings = repo.get_settings()
            item = repo.get_active() or repo.claim_next_queued(reply_mode=settings.reply_mode)
            return item.id if item else None

    def _fail_item(self, item_id: int, *, code: str, summary: str) -> None:
        """将项标记为 failed。"""

        with self.session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is None or item.status != QueueItemStatus.ACTIVE:
                return
            repo.mark_status(item, QueueItemStatus.FAILED, summary=summary, fail_code=code)

    def _should_abort(self, item_id: int) -> bool:
        """外部取消或状态已非 active 时返回 True。"""

        if self._cancel_current.is_set() or self._stop_event.is_set():
            return True
        with self.session_factory() as session:
            return QueueRepository(session).clear_active_if_preempted(item_id)

    async def _process_item(self, item_id: int) -> None:
        """
        处理单个 active 队列项的完整生命周期。
        """

        with self.session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            settings = repo.get_settings()
            if item is None:
                return
            detail_url = item.detail_url
            source_item_id = item.item_id
            title = item.title
            expected_seller_id = item.seller_id
            timeout_seconds = float(settings.reply_timeout_seconds)
            max_rounds = int(settings.max_rounds)
            auto_send = bool(settings.auto_send)
            already_sent_rounds = int(item.rounds_sent or 0)
            reply_mode = item.processing_reply_mode or settings.reply_mode

        if reply_mode == "ai" and not auto_send:
            self._fail_item(item_id, code="auto_send_disabled", summary="未开启自动发送，无法议价")
            return
        if reply_mode not in {"ai", "manual"}:
            self._fail_item(item_id, code="invalid_reply_mode", summary="回复模式无效，已停止处理")
            return
        if reply_mode == "manual" and self.manual_chat_factory is None:
            self._fail_item(
                item_id,
                code="manual_browser_not_configured",
                summary="手动回复需要配置并连接本机可见 Chrome（XIANYU_CDP_ENDPOINT）",
            )
            return

        system_prompt = build_system_prompt(DEFAULT_GOAL)
        from app.seller_chat.item_url import ItemReference

        opening = build_opening_brief(
            ItemReference(item_id=source_item_id, detail_url=detail_url),
            title,
        )

        factory = self.manual_chat_factory if reply_mode == "manual" else self.chat_factory
        assert factory is not None

        try:
            if reply_mode == "manual":
                try:
                    await factory.start()
                except Exception as error:  # noqa: BLE001
                    raise ChatSafetyError(
                        "manual_browser_not_connected",
                        "无法连接本机可见 Chrome，请检查 XIANYU_CDP_ENDPOINT 和浏览器调试端口",
                    ) from error
                self._manual_browser_connected = True
            async with factory.open(
                item_url=detail_url,
                source_item_id=source_item_id,
                expected_seller_id=expected_seller_id,
                expected_account_id=self.expected_account_id,
            ) as opened:
                if reply_mode == "manual":
                    await self._run_manual_session(item_id=item_id, opened=opened)
                else:
                    await self._run_session(
                        item_id=item_id,
                        opened=opened,
                        system_prompt=system_prompt,
                        opening_brief=opening,
                        timeout_seconds=timeout_seconds,
                        max_rounds=max_rounds,
                        already_sent_rounds=already_sent_rounds,
                    )
        except ChatSendUncertainError as error:
            self._fail_item(
                item_id,
                code=getattr(error, "code", "send_uncertain"),
                summary="发送结果无法确认，已停止且不会自动重试",
            )
        except ChatSafetyError as error:
            code = getattr(error, "code", "chat_safety")
            self._fail_item(
                item_id,
                code=code,
                summary=f"聊天安全失败[{code}]：{error}",
            )
        except SellerChatLlmError as error:
            self._fail_item(
                item_id,
                code=getattr(error, "code", "llm_error"),
                summary=str(error) or "生成议价草稿失败",
            )
        finally:
            if reply_mode == "manual":
                self._manual_item_id = None
                self._reject_pending_manual_command("手动会话已结束，未发送该条回复")

    async def _run_session(
        self,
        *,
        item_id: int,
        opened: OpenedXianyuChat,
        system_prompt: str,
        opening_brief: str,
        timeout_seconds: float,
        max_rounds: int,
        already_sent_rounds: int = 0,
    ) -> None:
        """在已打开的聊天页上执行议价状态机。"""

        with self.session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is not None and opened.binding.seller_id:
                repo.set_seller_id(item, opened.binding.seller_id)

        session_obj = SellerChatSession(
            client=opened.client,
            generator=self.draft_generator,
            system_prompt=system_prompt,
            opening_brief=opening_brief,
            sleep=self.sleep,
        )
        await session_obj.start()
        full = session_obj.transcript
        # 面板默认不刷整段历史；续跑时从最后一条「我」开始展示。
        last_me = max(
            (i for i, e in enumerate(full) if e.speaker == "me"),
            default=-1,
        )
        skip_first = last_me if already_sent_rounds > 0 and last_me >= 0 else len(full)
        pending_seller = _seller_texts_after_last_me(full)
        self._persist_transcript(item_id, session_obj, skip_first=skip_first)

        if self._should_abort(item_id):
            return

        # 续跑：上次已发过，不要再发一条重复开场，先消化页面上已有的卖家回复或继续等。
        if already_sent_rounds > 0:
            if pending_seller:
                self._persist_transcript(item_id, session_obj, skip_first=skip_first)
                if self._finish_if_outcome(item_id, pending_seller):
                    return
            else:
                self._set_waiting_summary(item_id, "续跑中：等待卖家新回复…")
                reply = await self._wait_with_abort(item_id, session_obj, timeout_seconds)
                if self._should_abort(item_id):
                    return
                if reply is None:
                    with self.session_factory() as session:
                        repo = QueueRepository(session)
                        item = repo.get_item(item_id)
                        if item is not None and item.status == QueueItemStatus.ACTIVE:
                            repo.mark_status(
                                item,
                                QueueItemStatus.PARKED,
                                summary="等待卖家超时，已暂挂",
                                fail_code="seller_timeout",
                            )
                    return
                self._persist_transcript(item_id, session_obj, skip_first=skip_first)
                if self._finish_if_outcome(item_id, reply.texts):
                    return
            await self._deep_chat_loop(
                item_id=item_id,
                session_obj=session_obj,
                skip_first=skip_first,
                timeout_seconds=timeout_seconds,
                max_rounds=max_rounds,
            )
            return

        # 新任务：先发议价，再等卖家。
        send_result = await self._send_one_round(item_id, session_obj, skip_first=skip_first)
        if send_result != "continue":
            return

        self._set_waiting_summary(item_id, "已发送，正在等待卖家新回复…")
        reply = await self._wait_with_abort(item_id, session_obj, timeout_seconds)
        if self._should_abort(item_id):
            return
        if reply is None:
            # 历史会话里的旧卖家消息不是本轮回复。只要本轮超时，就必须暂挂，不能因为
            # 聊天历史存在卖家文本而继续发下一条，避免连续催促或重复发送。
            with self.session_factory() as session:
                repo = QueueRepository(session)
                item = repo.get_item(item_id)
                if item is not None and item.status == QueueItemStatus.ACTIVE:
                    repo.mark_status(
                        item,
                        QueueItemStatus.PARKED,
                        summary="等待卖家超时，已暂挂",
                        fail_code="seller_timeout",
                    )
            return
        else:
            self._persist_transcript(item_id, session_obj, skip_first=skip_first)
            if self._finish_if_outcome(item_id, reply.texts):
                return

        await self._deep_chat_loop(
            item_id=item_id,
            session_obj=session_obj,
            skip_first=skip_first,
            timeout_seconds=timeout_seconds,
            max_rounds=max_rounds,
        )

    async def _run_manual_session(self, *, item_id: int, opened: OpenedXianyuChat) -> None:
        """保持一条 CDP 聊天会话，等待卖家消息或用户确认的手动回复。"""

        session_obj = SellerChatSession(
            client=opened.client,
            generator=self.draft_generator,
            system_prompt=build_system_prompt(DEFAULT_GOAL),
            opening_brief="",
            sleep=self.sleep,
        )
        await session_obj.start()
        self._persist_transcript(item_id, session_obj)
        self._manual_item_id = item_id
        self._set_waiting_summary(item_id, "手动回复模式：等待你输入消息或卖家新回复")
        try:
            while not self._should_abort(item_id):
                command_task = asyncio.create_task(self._manual_commands.get())
                seller_task = asyncio.create_task(
                    session_obj.wait_for_seller_reply(timeout_seconds=5.0)
                )
                done, pending = await asyncio.wait(
                    {command_task, seller_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                reply = seller_task.result() if seller_task in done else None
                if reply is not None:
                    self._persist_transcript(item_id, session_obj)
                    self._set_waiting_summary(item_id, "卖家已回复，等待你决定下一句")

                if command_task not in done:
                    continue
                command = command_task.result()
                findings = scan_outbound_draft(command.text)
                if findings:
                    codes = "、".join(finding.code for finding in findings)
                    command.completion.set_exception(ValueError(f"回复内容未通过安全检查：{codes}"))
                    continue
                try:
                    await session_obj.send(command.text)
                except Exception as error:
                    if not command.completion.done():
                        command.completion.set_exception(ValueError(f"发送失败：{error}"))
                    raise
                with self.session_factory() as session:
                    repo = QueueRepository(session)
                    item = repo.get_item(item_id)
                    if item is not None and item.status == QueueItemStatus.ACTIVE:
                        repo.bump_rounds(item)
                self._persist_transcript(item_id, session_obj)
                if not command.completion.done():
                    with self.session_factory() as session:
                        item = QueueRepository(session).get_item(item_id)
                        command.completion.set_result(int(item.rounds_sent) if item else 0)
                self._set_waiting_summary(item_id, "消息已发送，等待你输入或卖家新回复")
        finally:
            self._manual_item_id = None

    def _reject_pending_manual_command(self, message: str) -> None:
        """让尚未被 Worker 消费的手动发送请求失败返回，不保留草稿。"""

        while not self._manual_commands.empty():
            command = self._manual_commands.get_nowait()
            if not command.completion.done():
                command.completion.set_exception(ValueError(message))

    async def _deep_chat_loop(
        self,
        *,
        item_id: int,
        session_obj: SellerChatSession,
        skip_first: int,
        timeout_seconds: float,
        max_rounds: int,
    ) -> None:
        """卖家已开口后的多轮议价循环。"""

        while True:
            if self._should_abort(item_id):
                return
            with self.session_factory() as session:
                item = QueueRepository(session).get_item(item_id)
                rounds = int(item.rounds_sent) if item else 0
            if rounds >= max_rounds:
                with self.session_factory() as session:
                    repo = QueueRepository(session)
                    item = repo.get_item(item_id)
                    if item is not None and item.status == QueueItemStatus.ACTIVE:
                        repo.mark_status(
                            item,
                            QueueItemStatus.DONE_MANUAL,
                            summary=f"已达最大轮次 {max_rounds}",
                            fail_code="max_rounds",
                        )
                return

            send_result = await self._send_one_round(
                item_id, session_obj, skip_first=skip_first
            )
            if send_result != "continue":
                return
            self._set_waiting_summary(item_id, "深聊中，正在等待卖家新回复…")
            reply = await self._wait_with_abort(item_id, session_obj, timeout_seconds)
            if self._should_abort(item_id):
                return
            if reply is None:
                continue
            self._persist_transcript(item_id, session_obj, skip_first=skip_first)
            if self._finish_if_outcome(item_id, reply.texts):
                return

    async def _send_one_round(
        self,
        item_id: int,
        session_obj: SellerChatSession,
        *,
        skip_first: int = 0,
    ) -> str:
        """
        生成并发送一条议价消息。

        返回 ``continue`` 表示需继续等待卖家；``done`` 表示已终态或应停止；
        ``failed`` 表示已失败关闭。
        """

        if self._should_abort(item_id):
            return "done"
        proposal = await session_obj.next_draft()
        if proposal.findings:
            codes = ",".join(f.code for f in proposal.findings)
            self._fail_item(
                item_id,
                code="draft_blocked",
                summary=f"草稿命中黑名单：{codes}",
            )
            return "failed"
        outcome = await session_obj.send(proposal.text)
        with self.session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is not None:
                repo.bump_rounds(item)
                repo.mark_waiting(item)
        self._persist_transcript(item_id, session_obj, skip_first=skip_first)
        if outcome.seller_texts and self._finish_if_outcome(item_id, outcome.seller_texts):
            return "done"
        return "continue"

    async def _wait_with_abort(
        self,
        item_id: int,
        session_obj: SellerChatSession,
        timeout_seconds: float,
    ):
        """
        分段等待卖家回复，便于及时取消，并刷新面板等待提示。

        每段最多 5 秒，避免长时间睡死导致「卖家已回但面板毫无反应」。
        """

        waited = 0.0
        polls = 0
        while waited < timeout_seconds:
            if self._should_abort(item_id):
                return None
            chunk = min(5.0, timeout_seconds - waited)
            self._set_waiting_summary(
                item_id,
                f"等待卖家新回复中…已等 {int(waited)}/{int(timeout_seconds)} 秒",
            )
            reply = await session_obj.wait_for_seller_reply(timeout_seconds=chunk)
            polls += 1
            if reply is not None:
                logger.info("队列项 %s 第 %s 次检查读到卖家回复", item_id, polls)
                return reply
            waited += chunk
        return None

    def _finish_if_outcome(self, item_id: int, texts: tuple[str, ...]) -> bool:
        """
        若卖家同意或拒绝降价则写终态并返回 True。
        """

        if seller_refused_price_cut(texts):
            with self.session_factory() as session:
                repo = QueueRepository(session)
                item = repo.get_item(item_id)
                if item is not None and item.status == QueueItemStatus.ACTIVE:
                    repo.mark_status(
                        item,
                        QueueItemStatus.DONE_REFUSED,
                        summary="卖家明确不降价",
                        fail_code="refused",
                    )
            return True
        if seller_agreed_to_price_cut(texts):
            with self.session_factory() as session:
                repo = QueueRepository(session)
                item = repo.get_item(item_id)
                if item is not None and item.status == QueueItemStatus.ACTIVE:
                    repo.mark_status(
                        item,
                        QueueItemStatus.DONE_AGREED,
                        summary="卖家同意降价",
                        fail_code="agreed",
                    )
            return True
        return False

    def _set_waiting_summary(self, item_id: int, summary: str) -> None:
        """更新进行中项的等待说明，供面板展示。"""

        with self.session_factory() as session:
            repo = QueueRepository(session)
            item = repo.get_item(item_id)
            if item is None or item.status != QueueItemStatus.ACTIVE:
                return
            item.result_summary = summary
            session.commit()

    def _persist_transcript(
        self,
        item_id: int,
        session_obj: SellerChatSession,
        *,
        skip_first: int = 0,
    ) -> None:
        """把本轮新消息写入数据库供面板展示（可跳过打开会话时的历史）。"""

        entries = [(e.speaker, e.text) for e in session_obj.transcript[skip_first:]]
        with self.session_factory() as session:
            QueueRepository(session).replace_messages(item_id, entries)


def build_default_worker(session_factory: sessionmaker) -> BargainWorker:
    """
    基于环境配置构造默认 Worker。

    配置缺失时抛出配置校验异常。
    """

    from app.core.config import get_settings
    from app.crawler.persistent_chat import PersistentPlaywrightChatFactory

    config = load_spike_config(settings=get_settings(), headless=get_settings().xianyu_headless)
    guard = normalize_account_guard(None)
    ai_settings = config.settings.model_copy(update={"xianyu_cdp_endpoint": None})
    factory = PersistentPlaywrightChatFactory(ai_settings, guard)
    manual_factory = (
        PersistentPlaywrightChatFactory(config.settings, guard)
        if config.settings.xianyu_cdp_endpoint
        else None
    )
    generator = SellerChatDraftGenerator(config.deepseek)
    return BargainWorker(
        session_factory=session_factory,
        chat_factory=factory,
        draft_generator=generator,
        expected_account_id=config.expected_account_id,
        manual_chat_factory=manual_factory,
    )
