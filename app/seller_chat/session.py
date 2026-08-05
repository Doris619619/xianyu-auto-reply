"""
本文件负责 spike 卖家对话实验的单会话编排。

它属于 spike/seller_chat 实验模块，把「读页面消息 → 交给 DeepSeek 生成草稿 → 扫黑名单 →
发送 → 退避轮询卖家回复」串成一个可复用的状态对象，并维护两个关键状态：内存里的聊天
上下文，以及页面适配层要求的最新消息指纹。

本文件不决定「发不发」——它只在调用方明确调用 send 时才发送，人工确认由 cli.py 完成。
它也不打开浏览器（由 app.crawler.chat_runtime 的工厂负责）、不读取配置、不写数据库。
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from uuid import uuid4

from app.crawler.chat_client import (
    ChatMessageSnapshot,
    ChatSendUncertainError,
    PolicyAllowedDraft,
    SendEvidence,
    build_message_fingerprint,
    normalize_chat_text,
)
from app.crawler.chat_runtime import ProcurementChatClient
from app.crawler.product_context import ProductContext
from app.seller_chat.guardrails import (
    GuardrailFinding,
    scan_inbound_message,
    scan_outbound_draft,
)
from app.seller_chat.llm import (
    NegotiationDecision,
    SellerChatDraftGenerator,
    TranscriptEntry,
)

# 等卖家回复的退避节奏：先密后疏，只读聊天 DOM，不执行整页刷新。
SELLER_REPLY_POLL_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 3.0, 5.0, 8.0, 12.0, 15.0)

SLEEP_CALLABLE = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DraftProposal:
    """
    表示一条等待人工裁决的待发送草稿。

    ``findings`` 非空表示命中了确定性黑名单，调用方应当拒绝直接发送并要求人工改写。
    """

    text: str
    findings: tuple[GuardrailFinding, ...]


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    """
    表示模型结构化裁决及其 continue 消息的安全扫描结果。

    agreed / refused 裁决不包含外发文本，因此 findings 必须为空；continue 的 message
    在 Worker 真正发送前仍需沿用既有确定性黑名单校验。
    """

    decision: NegotiationDecision
    findings: tuple[GuardrailFinding, ...]


@dataclass(frozen=True, slots=True)
class SellerReply:
    """
    表示轮询到的一批卖家新消息。

    ``texts`` 保持页面顺序；``findings`` 是这批消息合并去重后的风险提示，仅用于提醒人工。
    """

    texts: tuple[str, ...]
    findings: tuple[GuardrailFinding, ...]


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """
    表示一次发送的结果。

    ``evidence`` 是页面适配层给出的双重确认证据；``seller_texts`` 是发送后立刻读到的卖家
    消息，通常为空，只有在卖家恰好同时说话时才不为空。
    """

    evidence: SendEvidence
    seller_texts: tuple[str, ...]


def empty_conversation_fingerprint() -> str:
    """
    返回「会话内没有任何可见消息」时的确定性指纹。

    无输入；返回与页面适配层完全一致的空会话指纹，用于把基线设到历史最开头，从而一次性
    读回全部可见消息。函数不访问页面，也没有副作用。
    """

    return build_message_fingerprint(message_id=None, direction="none", text="", timestamp=None)


class SellerChatSession:
    """
    在一个已打开的闲鱼聊天页上编排多轮 AI 对话。

    实例生命周期必须完全落在聊天工厂的 async with 上下文内，因为退出上下文时浏览器就会
    关闭。对象本身不做任何自动发送决策：只有调用方显式调用 ``send`` 才会真正发消息。
    """

    def __init__(
        self,
        *,
        client: ProcurementChatClient,
        generator: SellerChatDraftGenerator,
        system_prompt: str,
        opening_brief: str,
        product: ProductContext | None = None,
        decision_system_prompt: str | None = None,
        sleep: SLEEP_CALLABLE | None = None,
    ) -> None:
        """
        保存页面客户端、草稿生成器和提示词，并初始化空的会话状态。

        参数 ``sleep`` 供测试注入以跳过真实等待，默认使用 asyncio.sleep。
        构造过程不访问页面、不调用模型，也不获取账号锁。
        """

        self._client = client
        self._generator = generator
        self._system_prompt = system_prompt
        self._decision_system_prompt = decision_system_prompt or system_prompt
        self._opening_brief = opening_brief
        self.product = product or ProductContext()
        self._sleep: SLEEP_CALLABLE = sleep if sleep is not None else _default_sleep
        self._transcript: list[TranscriptEntry] = []
        self._latest_fingerprint = empty_conversation_fingerprint()

    @property
    def transcript(self) -> tuple[TranscriptEntry, ...]:
        """
        返回当前会话上下文的只读快照。

        无输入；返回按页面顺序排列的消息条目元组。属性不访问页面，也没有副作用。
        """

        return tuple(self._transcript)

    @property
    def latest_fingerprint(self) -> str:
        """
        返回当前已确认的最新可见消息指纹。

        无输入；返回 64 位十六进制指纹，发送时会用它确认页面在思考期间没有变化。
        属性不访问页面，也没有副作用。
        """

        return self._latest_fingerprint

    async def start(self) -> tuple[TranscriptEntry, ...]:
        """
        点开绑定商品的聊天入口并读回全部可见历史消息。

        无输入；返回按页面顺序排列的既有聊天记录，空元组表示还没聊过。
        登录失效、风控拦截、身份不匹配或聊天 DOM 不确定时向上抛出 ``ChatSafetyError``。

        副作用是点击一次「聊一聊」入口并把会话状态初始化为页面当前真实状态，不发送消息。
        """

        await self._client.open_conversation()
        self._transcript.clear()
        self._latest_fingerprint = empty_conversation_fingerprint()
        return await self._sync_new_messages()

    async def next_draft(self) -> DraftProposal:
        """
        基于当前会话上下文生成下一条待发送草稿并做黑名单扫描。

        无输入；返回包含草稿文本和命中结果的提案。模型超时、网络失败或输出异常时抛出
        ``SellerChatLlmError`` 的子类。

        生成过程放在工作线程里执行，避免同步 HTTP 请求阻塞 Playwright 所在的事件循环。
        本方法只生成不发送，也不改变会话状态。
        """

        text = await asyncio.to_thread(
            self._generator.generate,
            system_prompt=self._system_prompt,
            opening_brief=self._opening_brief,
            transcript=self.transcript,
        )
        draft = text.strip()
        return DraftProposal(text=draft, findings=scan_outbound_draft(draft))

    async def next_decision(self, *, system_prompt: str | None = None) -> DecisionProposal:
        """
        基于完整聊天记录请求本轮继续或结束的结构化议价裁决。

        返回库存或议价终态时不会包含外发消息；返回 continue 时会扫描模型给出的唯一
        后续消息。可选 ``system_prompt`` 让 Worker 按当前库存/议价阶段约束裁决；模型输出
        异常向上抛出，调用方必须失败关闭而不能回退关键词规则。
        """

        decision = await asyncio.to_thread(
            self._generator.decide,
            system_prompt=system_prompt or self._decision_system_prompt,
            opening_brief=self._opening_brief,
            transcript=self.transcript,
        )
        findings = (
            scan_outbound_draft(decision.message)
            if decision.action == "continue" and decision.message is not None
            else ()
        )
        return DecisionProposal(decision=decision, findings=findings)

    async def send(self, text: str) -> SendOutcome:
        """
        真正把一条消息发给卖家，并把页面新增消息同步进会话上下文。

        参数 ``text`` 必须是已经过人工确认的最终文本；返回发送证据和发送瞬间读到的卖家消息。

        草稿越界、页面在思考期间发生变化、身份不匹配或发送结果无法确认时抛出
        ``ChatSafetyError``（发送结果不确定时是 ``ChatSendUncertainError``，此时绝不能重试）。

        这里向页面适配层传入 ``auto_send_enabled=True``：本实验的「策略层」就是终端里的人，
        因此调用方必须在拿到人工确认之后才允许调用本方法。
        """

        draft = PolicyAllowedDraft(text=text, policy_decision_id=uuid4().hex)
        evidence = await self._client.send_policy_allowed_draft(
            draft,
            expected_latest_fingerprint=self._latest_fingerprint,
            auto_send_enabled=True,
        )
        entries = await self._sync_new_messages()
        if not any(
            entry.speaker == "me"
            and normalize_chat_text(entry.text) == normalize_chat_text(text)
            for entry in entries
        ):
            if entries:
                raise ChatSendUncertainError(
                    "confirmed_message_sync_inconsistent",
                    "已确认本人消息稳定可见，但增量同步返回了不含该消息的其他内容，禁止猜测顺序或自动重试",
                    evidence.request_evidence,
                )
            # 页面适配层已在两秒后从完整消息列表确认同文右侧气泡仍存在。某些闲鱼 React
            # 刷新会让紧随其后的第二次增量扫描暂时为空；此处只消费已确认的证据，不再伪造
            # 未确认的本地消息，也不执行第二次发送。
            entries = (TranscriptEntry(speaker="me", text=normalize_chat_text(text)),)
            self._transcript.extend(entries)
            self._latest_fingerprint = evidence.confirmed_message_fingerprint
        seller_texts = tuple(entry.text for entry in entries if entry.speaker == "seller")
        return SendOutcome(evidence=evidence, seller_texts=seller_texts)

    async def wait_for_seller_reply(self, *, timeout_seconds: float) -> SellerReply | None:
        """
        按退避节奏轮询卖家回复，直到收到消息或超时。

        先读页面再等待，避免卖家秒回后还要空等一整段退避时间才发现。
        参数 timeout_seconds 是本轮最长等待秒数；返回卖家新消息，超时返回 None。
        """

        waited = 0.0
        attempt = 0
        while True:
            entries = await self._sync_new_messages()
            seller_texts = tuple(entry.text for entry in entries if entry.speaker == "seller")
            if seller_texts:
                return SellerReply(
                    texts=seller_texts,
                    findings=_dedupe_findings(
                        finding for text in seller_texts for finding in scan_inbound_message(text)
                    ),
                )
            if waited >= timeout_seconds:
                return None
            backoff = SELLER_REPLY_POLL_BACKOFF_SECONDS[
                min(attempt, len(SELLER_REPLY_POLL_BACKOFF_SECONDS) - 1)
            ]
            delay = min(backoff, max(timeout_seconds - waited, 0.0))
            if delay <= 0:
                return None
            await self._sleep(delay)
            waited += delay
            attempt += 1


    def append_manual_message(self, text: str) -> None:
        """
        把一条人工在浏览器里手动发出的消息补进会话上下文。

        参数为消息文本；无返回值。仅在人工绕开本工具直接操作页面时使用，保证模型看到的
        上下文与真实聊天一致。方法不访问页面，也不推进消息指纹。
        """

        normalized = text.strip()
        if normalized:
            self._transcript.append(TranscriptEntry(speaker="me", text=normalized))

    async def _sync_new_messages(self) -> tuple[TranscriptEntry, ...]:
        """
        读取基线之后的全部新消息，写入会话上下文并推进基线。

        无输入；返回本次新增的消息条目。基线从可见历史消失或消息方向无法确认时向上抛出
        ``ChatSafetyError``，绝不猜测缺失的消息。

        副作用是修改会话上下文和最新消息指纹，只读页面不写页面。
        """

        snapshots = await self._client.read_messages_after(self._latest_fingerprint)
        if not snapshots:
            return ()
        entries = tuple(
            entry for entry in (_to_entry(snapshot) for snapshot in snapshots) if entry is not None
        )
        self._transcript.extend(entries)
        self._latest_fingerprint = snapshots[-1].fingerprint
        return entries


async def _default_sleep(seconds: float) -> None:
    """
    默认等待实现，供轮询退避使用。

    参数为等待秒数；无返回值；等待期间可被取消。除让出事件循环外没有其他副作用。
    """

    await asyncio.sleep(seconds)


def _to_entry(snapshot: ChatMessageSnapshot) -> TranscriptEntry | None:
    """
    把页面消息快照转换为模型可用的上下文条目。

    参数为页面适配层给出的只读快照；返回上下文条目，空会话占位快照返回 ``None``。
    函数只做字段映射，没有外部副作用。
    """

    if snapshot.direction == "self":
        return TranscriptEntry(speaker="me", text=snapshot.text)
    if snapshot.direction == "seller":
        return TranscriptEntry(speaker="seller", text=snapshot.text)
    return None


def _dedupe_findings(findings: Iterable[GuardrailFinding]) -> tuple[GuardrailFinding, ...]:
    """
    按原因码去重风险提示并保持首次出现顺序。

    参数为可能重复的命中结果；返回去重后的元组。函数没有外部副作用。
    """

    seen: dict[str, GuardrailFinding] = {}
    for finding in findings:
        seen.setdefault(finding.code, finding)
    return tuple(seen.values())


def render_transcript(entries: Sequence[TranscriptEntry]) -> str:
    """
    把会话上下文渲染成便于终端阅读的多行文本。

    参数为只读上下文条目；返回每行带说话人前缀的字符串，空上下文返回固定提示。
    函数只做字符串拼接，不访问页面或网络。
    """

    if not entries:
        return "（这个会话还没有任何聊天记录）"
    labels = {"me": "我", "seller": "卖家"}
    return "\n".join(f"  [{labels[entry.speaker]}] {entry.text}" for entry in entries)
