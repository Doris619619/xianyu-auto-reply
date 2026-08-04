"""
本文件实现单个已知闲鱼商品的保守聊天页面适配层。

它属于 crawler 模块：只校验页面与账号身份、读取最新消息、打开聊天控件，并发送已经由
上层策略批准的草稿。所有写操作都在账号级 ``AccountAccessGuard`` 内执行，并在任何身份、
页面结构、登录态、风控或消息并发不确定时失败关闭。

本文件不生成 AI 文案、不判断采购结果、不访问数据库，也绝不点击购买、付款、地址或订单
确认控件。选择器统一定义在 ``chat_selectors``，实际发送仍需调用方显式开启自动发送。
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, unquote_plus, urljoin, urlparse

from playwright.async_api import Locator, Page, Request, Response, WebSocket

from app.crawler.chat_selectors import (
    ACCOUNT_IDENTITY_COOKIE_NAME,
    BODY_SELECTOR,
    CHAT_ENTRY_WAIT_MILLISECONDS,
    CHAT_INPUT_SELECTOR,
    CHAT_MESSAGE_LIST_SELECTOR,
    CHAT_MESSAGE_SELECTOR,
    CHAT_PANEL_SELECTOR,
    CHAT_READY_WAIT_MILLISECONDS,
    CHAT_SEND_SELECTOR,
    MESSAGE_DIRECTION_ATTRIBUTES,
    MESSAGE_ID_ATTRIBUTES,
    MESSAGE_TIMESTAMP_ATTRIBUTES,
    OPEN_CHAT_SELECTOR,
    OWN_CHAT_MESSAGE_SELECTOR,
)
from app.crawler.risk_control import (
    RiskControlBlocked,
    detect_risk,
    detect_risk_response,
)
from app.services.xianyu_account_guard import AccountAccessGuard

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SELF_DIRECTIONS = {"self", "outgoing", "buyer", "mine"}
SELLER_DIRECTIONS = {"seller", "incoming", "other"}
MAX_MESSAGE_NODES = 500
SEND_CONFIRMATION_ATTEMPTS = 40
SEND_CONFIRMATION_DELAY_MS = 250
SEND_CONFIRMATION_STABILITY_MS = 2_000
SEND_POINTER_MOVE_STEPS = 8
SEND_POINTER_SETTLE_MS = 90
SEND_POINTER_PRESS_MS = 70
SEND_REQUEST_WAIT_ATTEMPTS = 20
SEND_REQUEST_WAIT_DELAY_MS = 100
CHAT_ENTRY_STABILITY_ATTEMPTS = 20
CHAT_ENTRY_STABILITY_DELAY_MS = 100
ALLOWED_SEND_REQUEST_METHODS = {"POST", "PUT", "PATCH"}
ALLOWED_SEND_REQUEST_RESOURCE_TYPES = {"fetch", "xhr"}
ALLOWED_SEND_REQUEST_HOST_SUFFIXES = (
    "goofish.com",
    "taobao.com",
    "alibaba.com",
)
SEND_BUTTON_HIT_TEST_SCRIPT = """(button, point) => {
    const top = document.elementFromPoint(point.x, point.y);
    return Boolean(top && (top === button || button.contains(top)));
}"""
SEND_BUTTON_CLICK_FRACTIONS = (
    (0.5, 0.5),
    (0.2, 0.5),
    (0.8, 0.5),
    (0.5, 0.2),
    (0.5, 0.8),
    (0.2, 0.2),
    (0.8, 0.2),
    (0.2, 0.8),
    (0.8, 0.8),
)


class ChatSafetyError(RiskControlBlocked):
    """
    表示聊天适配层因稳定安全边界而停止。

    调用方可读取 ``code`` 做状态映射；异常消息只描述安全分类，不包含登录态或凭据。
    """

    def __init__(self, code: str, message: str) -> None:
        """
        保存稳定错误码和可诊断消息。

        参数为非敏感错误码与消息；无返回；副作用仅为初始化异常对象。
        """

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SendRequestEvidence:
    """
    保存一次发送窗口内观察到的最小脱敏网络证据。

    只记录请求是否出现、传输类型、端点 SHA-256、HTTP 方法和粗粒度响应状态；不保存 URL、
    请求正文、Cookie、卖家昵称或账号凭据。
    """

    request_observed: bool
    transport: str | None = None
    endpoint_sha256: str | None = None
    method: str | None = None
    response_observed: bool = False
    response_status: int | None = None


@dataclass(frozen=True, slots=True)
class SendAttemptDiagnostic:
    """
    保存一次不确定发送的最小页面诊断。

    诊断只描述点击和确认阶段的布尔状态，以及已有的脱敏网络证据；绝不包含消息正文、
    Cookie、页面文本、账号或卖家标识。该对象可安全持久化到队列项并展示在本地面板。
    """

    phase: str
    button_center_obscured: bool | None
    click_attempted: bool
    confirmation_observed: bool
    risk_detected_after_click: bool
    last_safety_code: str | None
    request_evidence: SendRequestEvidence

    def as_persisted_json(self) -> str:
        """返回可持久化的脱敏 JSON，不读取页面也不包含任何聊天内容。"""

        return json.dumps(
            {
                "schema_version": 1,
                "phase": self.phase,
                "button_center_obscured": self.button_center_obscured,
                "click_attempted": self.click_attempted,
                "confirmation_observed": self.confirmation_observed,
                "risk_detected_after_click": self.risk_detected_after_click,
                "last_safety_code": self.last_safety_code,
                "request_observed": self.request_evidence.request_observed,
                "transport": self.request_evidence.transport,
                "response_observed": self.request_evidence.response_observed,
                "response_status": self.request_evidence.response_status,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class ChatSendUncertainError(ChatSafetyError):
    """
    表示唯一鼠标提交后的网络或页面结果无法同时确认。

    异常携带脱敏网络证据，供上层持久化审计；调用方必须转人工，不能再次点击或改用 Enter。
    """

    def __init__(
        self,
        code: str,
        message: str,
        request_evidence: SendRequestEvidence,
        diagnostic: SendAttemptDiagnostic | None = None,
    ) -> None:
        """
        保存稳定错误码与脱敏网络证据。

        输入安全错误信息和证据；无返回；不记录正文、不访问页面也不执行重试。
        """

        super().__init__(code, message)
        self.request_evidence = request_evidence
        self.diagnostic = diagnostic


@dataclass(slots=True)
class _ActiveSendObservation:
    """
    在唯一点击窗口内暂存网络监听状态。

    对象只存在于内存，草稿正文仅用于即时匹配，点击结束后立即清除且不会写入日志或数据库。
    """

    draft_text: str
    request_observed: bool = False
    transport: str | None = None
    endpoint_sha256: str | None = None
    method: str | None = None
    response_observed: bool = False
    response_status: int | None = None
    request_identity: int | None = None

    def snapshot(self) -> SendRequestEvidence:
        """
        生成不含草稿正文和请求内容的不可变证据。

        无输入；返回脱敏证据；没有页面、网络或持久化副作用。
        """

        return SendRequestEvidence(
            request_observed=self.request_observed,
            transport=self.transport,
            endpoint_sha256=self.endpoint_sha256,
            method=self.method,
            response_observed=self.response_observed,
            response_status=self.response_status,
        )


@dataclass(slots=True)
class _SendAttemptState:
    """在一次唯一点击内累积可持久化的脱敏页面诊断。"""

    phase: str = "prepared"
    button_center_obscured: bool | None = None
    click_attempted: bool = False
    confirmation_observed: bool = False
    risk_detected_after_click: bool = False
    last_safety_code: str | None = None

    def record_safety_error(self, code: str) -> None:
        """按稳定错误码记录失败阶段，不保留页面原文。"""

        self.last_safety_code = code
        if code == "chat_send_button_obscured":
            self.phase = "button_obscured"
        elif code == "send_confirmation_missing":
            self.phase = "confirmation_timeout"
        elif self.click_attempted and code in {"risk_or_login_blocked", "http_risk_blocked"}:
            self.phase = "risk_detected_after_click"
            self.risk_detected_after_click = True
        elif self.click_attempted:
            self.phase = "safety_failed_after_click"
        else:
            self.phase = "safety_failed_before_click"

    def snapshot(self, request_evidence: SendRequestEvidence) -> SendAttemptDiagnostic:
        """冻结当前诊断状态，以供异常路径安全持久化。"""

        return SendAttemptDiagnostic(
            phase=self.phase,
            button_center_obscured=self.button_center_obscured,
            click_attempted=self.click_attempted,
            confirmation_observed=self.confirmation_observed,
            risk_detected_after_click=self.risk_detected_after_click,
            last_safety_code=self.last_safety_code,
            request_evidence=request_evidence,
        )


@dataclass(frozen=True, slots=True)
class ChatBinding:
    """
    固定一次聊天允许操作的商品、卖家与当前买家账号身份。

    三个标识必须由可信任务快照提供；对象不负责查询或推断任何身份。
    """

    source_item_id: str
    seller_id: str
    account_id: str

    def __post_init__(self) -> None:
        """
        在页面访问前验证三个绑定标识是非空稳定字符串。

        无显式返回；非法标识抛出 ``ChatSafetyError``；不访问页面且没有外部副作用。
        """

        for field_name, value in (
            ("source_item_id", self.source_item_id),
            ("seller_id", self.seller_id),
            ("account_id", self.account_id),
        ):
            if not IDENTIFIER_PATTERN.fullmatch(value):
                raise ChatSafetyError(
                    "invalid_chat_binding",
                    f"聊天绑定字段 {field_name} 不是可确认的稳定标识",
                )


@dataclass(frozen=True, slots=True)
class PolicyAllowedDraft:
    """
    表示已经由上层确定性策略放行的单条聊天草稿。

    适配层只校验文本基本边界和策略决策 ID，不重新执行 LLM 或业务审核。
    """

    text: str
    policy_decision_id: str

    def __post_init__(self) -> None:
        """
        验证草稿文本与策略决策 ID，避免空消息或无审计来源的发送。

        无显式返回；边界不合法时抛出 ``ChatSafetyError``；不访问页面。
        """

        normalized = normalize_chat_text(self.text)
        if not normalized or len(normalized) > 500:
            raise ChatSafetyError("invalid_allowed_draft", "已放行草稿必须为 1 至 500 个字符")
        if not IDENTIFIER_PATTERN.fullmatch(self.policy_decision_id):
            raise ChatSafetyError("invalid_policy_decision", "草稿缺少稳定策略决策标识")


@dataclass(frozen=True, slots=True)
class ChatMessageSnapshot:
    """
    保存聊天窗口最新一条可见消息的稳定只读快照。

    空会话使用 ``direction=none`` 和确定性指纹，不把 DOM 节点或登录态带出适配层。
    """

    message_id: str | None
    direction: str
    text: str
    timestamp: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SendEvidence:
    """
    表示一次单次提交后由网络请求与页面本人消息共同确认的最小证据。

    证据只含绑定 ID、策略决策 ID、草稿摘要、确认消息指纹和脱敏网络事实，不保存账号凭据。
    """

    source_item_id: str
    seller_id: str
    account_id: str
    policy_decision_id: str
    draft_sha256: str
    confirmed_message_fingerprint: str
    request_evidence: SendRequestEvidence


def _endpoint_sha256(url: str) -> str:
    """
    将网络端点规范化为只含协议、主机和路径的 SHA-256。

    输入请求或 WebSocket URL；返回十六进制摘要；查询参数和正文不会进入摘要或日志。
    """

    parsed = urlparse(url)
    endpoint = f"{parsed.scheme.casefold()}://{(parsed.hostname or '').casefold()}{parsed.path}"
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _is_allowed_send_host(url: str) -> bool:
    """
    判断网络事件是否来自闲鱼聊天可能使用的官方服务域名。

    输入 URL；仅官方域名或其子域返回 True；解析失败返回 False，无网络副作用。
    """

    host = (urlparse(url).hostname or "").casefold()
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in ALLOWED_SEND_REQUEST_HOST_SUFFIXES
    )


def _payload_contains_draft(payload: str | bytes | None, draft_text: str) -> bool:
    """
    在内存中确认请求载荷包含本次完整草稿，避免把心跳或统计请求误作发送证据。

    输入短生命周期载荷与草稿；返回布尔值；不返回、记录或持久化原始载荷。
    """

    if payload is None:
        return False
    raw = payload.decode("utf-8", errors="ignore") if isinstance(payload, bytes) else payload
    normalized_draft = normalize_chat_text(draft_text)
    candidates = (
        raw,
        unquote_plus(raw),
        raw.replace(json.dumps(draft_text, ensure_ascii=True)[1:-1], draft_text),
    )
    return any(normalized_draft in normalize_chat_text(candidate) for candidate in candidates)


def normalize_chat_text(value: str) -> str:
    """
    将消息文本规范化为适合比较和指纹计算的稳定形式。

    参数为页面或草稿文本；返回 Unicode NFKC 且空白折叠后的字符串；无异常和副作用。
    """

    return " ".join(unicodedata.normalize("NFKC", value).split())


def item_url_matches_binding(url: str, source_item_id: str) -> bool:
    """
    严格确认当前 URL 是绑定商品的闲鱼详情页。

    参数为页面 URL 和已知商品 ID；仅官方主机、``/item`` 路径和唯一 ``id`` 参数完全一致
    时返回 ``True``；解析失败返回 ``False``，不访问网络。
    """

    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    item_ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
    return (
        parsed.scheme == "https"
        and host in {"goofish.com", "www.goofish.com"}
        and parsed.path.rstrip("/") == "/item"
        and item_ids == [source_item_id]
    )


def chat_url_matches_binding(
    url: str,
    source_item_id: str,
    seller_id: str,
) -> bool:
    """
    严格确认当前 URL 是绑定商品和卖家的闲鱼聊天页。

    参数为页面 URL、商品 ID 和卖家 ID；只接受官方 HTTPS ``/im`` 页面且 ``itemId``、
    ``peerUserId`` 各自唯一并完全一致。解析失败返回 ``False``，没有网络副作用。
    """

    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() in {"goofish.com", "www.goofish.com"}
        and parsed.path.rstrip("/") == "/im"
        and query.get("itemId") == [source_item_id]
        and query.get("peerUserId") == [seller_id]
    )


def _parse_chat_entry_href(
    page_url: str,
    href: str,
    source_item_id: str,
) -> str:
    """
    从商品页聊天入口提取唯一卖家 ID。

    输入当前页、入口 href 和商品 ID；返回 ``peerUserId``。URL 非官方、商品不一致或卖家
    参数缺失/重复时抛出安全异常；只解析字符串。
    """

    parsed = urlparse(urljoin(page_url, href))
    query = parse_qs(parsed.query, keep_blank_values=True)
    seller_ids = query.get("peerUserId", [])
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in {"goofish.com", "www.goofish.com"}
        or parsed.path.rstrip("/") != "/im"
        or query.get("itemId") != [source_item_id]
        or len(seller_ids) != 1
        or not IDENTIFIER_PATTERN.fullmatch(seller_ids[0])
    ):
        raise ChatSafetyError("chat_entry_identity_invalid", "聊天入口身份参数无法安全确认")
    return seller_ids[0]


def build_message_fingerprint(
    *, message_id: str | None, direction: str, text: str, timestamp: str | None
) -> str:
    """
    根据消息稳定字段生成 SHA-256 指纹。

    参数为可选消息 ID、方向、规范化文本和可选时间；返回小写十六进制摘要；不读取页面。
    """

    payload = json.dumps(
        {
            "direction": direction,
            "message_id": message_id,
            "text": normalize_chat_text(text),
            "timestamp": timestamp,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _unique_visible_locator(page: Page, selector: str, label: str) -> Locator:
    """
    从一个集中选择器中返回唯一可见元素。

    参数为页面、选择器和非敏感标签；找不到或出现多个可见元素时抛出 ``ChatSafetyError``；
    只读取 DOM 可见性，不点击或输入。
    """

    locator = page.locator(selector)
    visible: list[Locator] = []
    for index in range(await locator.count()):
        node = locator.nth(index)
        if await node.is_visible():
            visible.append(node)
            if len(visible) > 1:
                break
    if len(visible) != 1:
        raise ChatSafetyError(
            "ambiguous_chat_dom",
            f"{label} 必须且只能匹配一个可见元素",
        )
    return visible[0]


async def _read_stable_visible_body(page: Page) -> str:
    """
    等待 React 重绘结束后读取唯一可见页面主体文本。

    商品页在点击聊天前可能短暂同时保留新旧 ``body`` 节点；本函数最多等待两秒，只有稳定
    的唯一主体才会继续风险检测。超时仍失败关闭，不会选择任意一个节点猜测页面状态。
    """

    last_error: ChatSafetyError | None = None
    for attempt in range(CHAT_ENTRY_STABILITY_ATTEMPTS):
        try:
            body = await _unique_visible_locator(page, BODY_SELECTOR, "页面主体")
            return await body.inner_text(timeout=5_000)
        except ChatSafetyError as exc:
            if exc.code != "ambiguous_chat_dom":
                raise
            last_error = exc
        if attempt + 1 < CHAT_ENTRY_STABILITY_ATTEMPTS:
            await page.wait_for_timeout(CHAT_ENTRY_STABILITY_DELAY_MS)
    if last_error is not None:
        raise last_error
    raise ChatSafetyError("ambiguous_chat_dom", "页面主体必须且只能匹配一个可见元素")


async def _read_stable_chat_entry(page: Page) -> tuple[Locator, str]:
    """
    在客户端渲染竞态内读取唯一聊天入口及其非空身份 URL。

    参数为商品页；返回稳定 locator 与 href；候选长期缺失、歧义或 href 未就绪时抛出
    ``ChatSafetyError``。副作用仅为短暂轮询 DOM，不导航、不点击、不输入也不发送。
    """

    last_error: ChatSafetyError | None = None
    for attempt in range(CHAT_ENTRY_STABILITY_ATTEMPTS):
        locator = page.locator(OPEN_CHAT_SELECTOR)
        # 闲鱼会替换刚显示的 React 节点。可见性和 href 必须在浏览器同一次执行中读取，
        # 否则两个 await 之间的重渲染会让 locator 指向另一个尚未带 href 的节点。
        raw_entries: object = await locator.evaluate_all(
            """nodes => nodes
              .map((node, index) => {
                const style = window.getComputedStyle(node);
                return {
                  index,
                  href: node.getAttribute('href'),
                  visible: style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && node.getClientRects().length > 0,
                };
              })
              .filter(entry => entry.visible)"""
        )
        entries: list[tuple[int, str | None]] = []
        if isinstance(raw_entries, list):
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    continue
                index = raw_entry.get("index")
                href = raw_entry.get("href")
                if isinstance(index, int) and (href is None or isinstance(href, str)):
                    entries.append((index, href))

        if len(entries) > 1:
            last_error = ChatSafetyError(
                "ambiguous_chat_dom",
                "聊天入口必须且只能匹配一个可见元素",
            )
        elif len(entries) == 1:
            index, href = entries[0]
            if href:
                return locator.nth(index), href
            last_error = ChatSafetyError(
                "chat_entry_identity_invalid",
                "聊天入口缺少身份 URL",
            )
        else:
            last_error = ChatSafetyError(
                "ambiguous_chat_dom",
                "聊天入口必须且只能匹配一个可见元素",
            )
        if attempt + 1 < CHAT_ENTRY_STABILITY_ATTEMPTS:
            await page.wait_for_timeout(CHAT_ENTRY_STABILITY_DELAY_MS)

    if last_error is not None:
        raise last_error
    raise ChatSafetyError("chat_entry_identity_invalid", "聊天入口身份未稳定")


async def _read_unique_attribute(
    locator: Locator, attribute_names: tuple[str, ...], label: str
) -> str:
    """
    从唯一元素的白名单属性中读取唯一非空身份值。

    属性缺失或互相冲突时抛出 ``ChatSafetyError``；不使用页面文本猜测身份且没有写操作。
    """

    values = {
        value.strip()
        for name in attribute_names
        if (value := await locator.get_attribute(name)) is not None and value.strip()
    }
    if len(values) != 1:
        raise ChatSafetyError(
            "identity_not_confirmed",
            f"{label} 缺少唯一可确认的身份属性",
        )
    return next(iter(values))


async def _assert_account_cookie_identity(page: Page, expected_account_id: str) -> None:
    """
    用 ``tracknick`` Cookie 的原值或 SHA-256 指纹确认当前登录买家账号。

    生产配置推荐保存 64 位摘要而非昵称原值。函数只读取当前官方域 Cookie，不返回或记录
    原值；缺失、重复或不匹配均失败关闭。
    """

    cookies = await page.context.cookies("https://www.goofish.com")
    values = {
        unquote(str(cookie.get("value") or "")).strip()
        for cookie in cookies
        if str(cookie.get("name") or "").casefold() == ACCOUNT_IDENTITY_COOKIE_NAME.casefold()
        and str(cookie.get("value") or "").strip()
    }
    if len(values) != 1:
        raise ChatSafetyError("account_identity_not_confirmed", "无法唯一确认当前闲鱼账号")
    actual = next(iter(values))
    actual_fingerprint = hashlib.sha256(actual.encode("utf-8")).hexdigest()
    if expected_account_id not in {actual, actual_fingerprint}:
        raise ChatSafetyError("chat_identity_mismatch", "当前闲鱼账号与配置绑定不一致")


async def _read_optional_attribute(
    locator: Locator, attribute_names: tuple[str, ...]
) -> str | None:
    """
    从消息节点读取至多一个非空属性值。

    参数为消息节点和属性白名单；没有值返回 ``None``，冲突时抛出 ``ChatSafetyError``；
    无页面写入副作用。
    """

    values = {
        value.strip()
        for name in attribute_names
        if (value := await locator.get_attribute(name)) is not None and value.strip()
    }
    if len(values) > 1:
        raise ChatSafetyError("message_identity_ambiguous", "消息稳定属性存在冲突")
    return next(iter(values), None)


def _normalize_direction(value: str | None) -> str:
    """
    将页面消息方向映射为 ``self`` 或 ``seller``。

    输入页面属性；无法确定方向时抛出 ``ChatSafetyError``；函数没有外部副作用。
    """

    normalized = (value or "").strip().casefold()
    if (
        normalized in SELF_DIRECTIONS
        or "msg-text-right--" in normalized
        or "message-text-right--" in normalized
    ):
        return "self"
    if (
        normalized in SELLER_DIRECTIONS
        or "msg-text-left--" in normalized
        or "message-text-left--" in normalized
    ):
        return "seller"
    raise ChatSafetyError("message_direction_not_confirmed", "无法确认最新消息发送方向")


async def _snapshot_message(
    locator: Locator, forced_direction: str | None = None
) -> ChatMessageSnapshot:
    """
    将一个可见消息节点转换为稳定快照。

    参数为消息节点和可选可信方向；空文本或方向不明时抛出 ``ChatSafetyError``；只读取 DOM。
    """

    text = normalize_chat_text(await locator.inner_text(timeout=2_000))
    if not text:
        raise ChatSafetyError("empty_chat_message", "最新可见消息文本为空")
    message_id = await _read_optional_attribute(locator, MESSAGE_ID_ATTRIBUTES)
    timestamp = await _read_optional_attribute(locator, MESSAGE_TIMESTAMP_ATTRIBUTES)
    direction = forced_direction or _normalize_direction(
        await _read_optional_attribute(locator, MESSAGE_DIRECTION_ATTRIBUTES)
    )
    fingerprint = build_message_fingerprint(
        message_id=message_id,
        direction=direction,
        text=text,
        timestamp=timestamp,
    )
    return ChatMessageSnapshot(message_id, direction, text, timestamp, fingerprint)


def _empty_conversation_snapshot() -> ChatMessageSnapshot:
    """
    返回无可见消息时的确定性空会话快照。

    无输入和异常；返回固定指纹；不访问页面且没有副作用。
    """

    fingerprint = build_message_fingerprint(
        message_id=None,
        direction="none",
        text="",
        timestamp=None,
    )
    return ChatMessageSnapshot(None, "none", "", None, fingerprint)


async def discover_chat_binding(
    page: Page,
    *,
    source_item_id: str,
    expected_account_id: str,
    account_guard: AccountAccessGuard,
) -> ChatBinding:
    """
    在已验证商品详情页上发现卖家 ID，并同时锁定商品与当前账号身份。

    输入页面、商城订单绑定商品 ID、配置中的预期账号 ID 和账号 guard；返回三方不可变
    ``ChatBinding``。任一 DOM 身份缺失、歧义、URL 不匹配、登录或风控信号都会失败关闭；
    只读页面，不打开聊天、不发送消息，也不接触购买、付款或地址控件。
    """

    if not IDENTIFIER_PATTERN.fullmatch(source_item_id):
        raise ChatSafetyError("invalid_source_item_id", "商品 ID 不是可确认的稳定标识")
    if not IDENTIFIER_PATTERN.fullmatch(expected_account_id):
        raise ChatSafetyError("invalid_expected_account", "配置账号 ID 不是可确认的稳定标识")
    async with account_guard.hold():
        visible_text = await _read_stable_visible_body(page)
        reason = detect_risk(page.url, visible_text)
        if reason:
            raise ChatSafetyError("risk_or_login_blocked", reason)
        if not item_url_matches_binding(page.url, source_item_id):
            raise ChatSafetyError("item_url_mismatch", "当前 URL 与绑定闲鱼商品不一致")

        await _assert_account_cookie_identity(page, expected_account_id)
        # 聊天入口由客户端延迟渲染；动态类名和文案不作为身份依据，完整 URL 参数与
        # 唯一可见节点才是安全边界，后续仍会逐项核对商品、卖家与买家账号。
        await page.wait_for_selector(
            OPEN_CHAT_SELECTOR,
            state="visible",
            timeout=CHAT_ENTRY_WAIT_MILLISECONDS,
        )
        _entry, href = await _read_stable_chat_entry(page)
        seller_id = _parse_chat_entry_href(page.url, href, source_item_id)
        return ChatBinding(
            source_item_id=source_item_id,
            seller_id=seller_id,
            account_id=expected_account_id,
        )


class XianyuChatClient:
    """
    在一个已打开页面上执行身份锁定、只读消息检查和受控聊天发送。

    实例永久绑定一个商品、卖家和账号；所有公开页面操作均持有调用方提供的账号 guard。
    """

    def __init__(
        self,
        page: Page,
        binding: ChatBinding,
        account_guard: AccountAccessGuard,
    ) -> None:
        """
        保存页面、不可变身份绑定和账号访问 guard。

        参数缺少 guard 时抛出 ``ChatSafetyError``；不读取页面、不开启浏览器且不获取锁。
        """

        if account_guard is None:
            raise ChatSafetyError("account_guard_required", "聊天发送必须持有账号访问 guard")
        self._page = page
        self._binding = binding
        self._account_guard = account_guard
        self._blocked_risk_reason: str | None = None
        self._active_send_observation: _ActiveSendObservation | None = None
        self._observed_page_ids: set[int] = set()

        def observe_status(response: Response) -> None:
            """
            记录聊天上下文出现的首个访问控制信号。

            输入 Playwright 响应；无返回；只保存粗粒度原因，不读取或记录响应正文。
            HTTP 200 的 TMD 风控流程也属于阻塞，不应继续等待聊天 DOM 超时。
            """

            response_url = str(
                getattr(response, "url", getattr(response.request, "url", ""))
            )
            reason = detect_risk_response(response_url, response.status)
            if reason and self._blocked_risk_reason is None:
                self._blocked_risk_reason = reason

        # 生产 Playwright BrowserContext 提供 on；离线 FakeContext 不注册网络监听器。
        if hasattr(self._page.context, "on"):
            # “聊一聊”可能新开聊天页。必须在新页刚创建时就注册 WebSocket 监听，
            # 否则等 open_conversation 找到该页时，长连接已经建立，后续无法观察发送帧。
            self._page.context.on("page", self._register_page_send_observers)
            self._page.context.on("response", observe_status)
            self._page.context.on("request", self._observe_send_request)
            self._page.context.on("response", self._observe_send_response)
        self._register_page_send_observers(self._page)

    def _register_page_send_observers(self, page: Page) -> None:
        """
        为当前聊天页注册 WebSocket 帧监听器且避免重复注册。

        输入页面；无返回；只监听本次客户端生命周期内的新 WebSocket，不读取历史帧或正文。
        """

        page_identity = id(page)
        if page_identity in self._observed_page_ids or not hasattr(page, "on"):
            return
        self._observed_page_ids.add(page_identity)
        page.on("websocket", self._observe_websocket)

    def _observe_send_request(self, request: Request) -> None:
        """
        识别点击窗口内携带完整草稿的官方 XHR/fetch 请求。

        输入 Playwright 请求；无返回；只更新内存中的脱敏事实，不保存 URL、正文或请求头。
        """

        observation = self._active_send_observation
        if observation is None or observation.request_observed:
            return
        if (
            request.method.upper() not in ALLOWED_SEND_REQUEST_METHODS
            or request.resource_type not in ALLOWED_SEND_REQUEST_RESOURCE_TYPES
            or not _is_allowed_send_host(request.url)
            or not _payload_contains_draft(request.post_data, observation.draft_text)
        ):
            return
        observation.request_observed = True
        observation.transport = "http"
        observation.endpoint_sha256 = _endpoint_sha256(request.url)
        observation.method = request.method.upper()
        observation.request_identity = id(request)

    def _observe_send_response(self, response: Response) -> None:
        """
        为已识别的 HTTP 发送请求记录粗粒度响应状态。

        输入 Playwright 响应；无返回；不读取响应正文、响应头或重定向地址。
        """

        observation = self._active_send_observation
        if (
            observation is None
            or observation.transport != "http"
            or observation.request_identity is None
            or id(response.request) != observation.request_identity
        ):
            return
        observation.response_observed = True
        observation.response_status = response.status

    def _observe_websocket(self, websocket: WebSocket) -> None:
        """
        监听当前聊天页新 WebSocket 的出站帧，只匹配本次完整草稿。

        输入 Playwright WebSocket；无返回；原始帧仅在回调内短暂检查且不会保存或记录。
        """

        def observe_frame(payload: str | bytes) -> None:
            """
            将携带完整草稿的官方 WebSocket 帧记为已发出。

            输入单帧载荷；无返回；只写入脱敏摘要，原始载荷不会离开闭包。
            """

            observation = self._active_send_observation
            if (
                observation is None
                or observation.request_observed
                or not _is_allowed_send_host(websocket.url)
                or not _payload_contains_draft(payload, observation.draft_text)
            ):
                return
            observation.request_observed = True
            observation.transport = "websocket"
            observation.endpoint_sha256 = _endpoint_sha256(websocket.url)
            observation.method = "FRAME"
            observation.response_observed = False
            observation.response_status = None

        websocket.on("framesent", observe_frame)

    async def open_conversation(self) -> ChatMessageSnapshot:
        """
        在严格身份确认后点击唯一聊天入口并返回当前最新消息快照。

        无输入；返回只读消息快照；登录、风控、身份或 DOM 不确定时抛出 ``ChatSafetyError``。
        副作用仅为点击聊天入口，不点击任何交易控件。
        """

        async with self._account_guard.hold():
            await self._assert_bound_identity()
            trigger, _href = await _read_stable_chat_entry(self._page)
            if not await trigger.is_enabled():
                raise ChatSafetyError("chat_entry_disabled", "聊天入口当前不可用")
            previous_pages = set(self._page.context.pages)
            await trigger.click(timeout=5_000)
            candidates: list[Page] = []
            for _ in range(20):
                new_pages = [
                    page for page in self._page.context.pages if page not in previous_pages
                ]
                candidates = [
                    page
                    for page in [self._page, *new_pages]
                    if chat_url_matches_binding(
                        page.url,
                        self._binding.source_item_id,
                        self._binding.seller_id,
                    )
                ]
                if candidates:
                    break
                await self._page.wait_for_timeout(250)
            if len(candidates) != 1:
                raise ChatSafetyError(
                    "chat_navigation_not_confirmed",
                    "点击入口后无法唯一确认绑定聊天页",
                )
            self._page = candidates[0]
            self._register_page_send_observers(self._page)
            await self._page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            return await self._read_latest_message_unlocked()

    async def read_latest_message(self) -> ChatMessageSnapshot:
        """
        在账号 guard 内读取绑定会话的最新可见消息。

        无输入；返回稳定快照；身份、登录、风控或聊天 DOM 不确定时失败关闭；没有写操作。
        """

        async with self._account_guard.hold():
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            return await self._read_latest_message_unlocked()

    async def read_messages_after(
        self,
        baseline_fingerprint: str,
    ) -> list[ChatMessageSnapshot]:
        """
        按页面顺序读取任务基线之后的全部可见消息。

        输入最近一次已持久化页面消息指纹；返回所有新增快照。基线从可见历史消失、格式
        无效或消息方向不明时失败关闭，避免漏掉卖家连续回复。
        """

        if not FINGERPRINT_PATTERN.fullmatch(baseline_fingerprint):
            raise ChatSafetyError("invalid_message_baseline", "聊天消息基线指纹无效")
        async with self._account_guard.hold():
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            snapshots = await self._read_visible_messages_unlocked()
            if not snapshots:
                return []
            empty_fingerprint = _empty_conversation_snapshot().fingerprint
            if baseline_fingerprint == empty_fingerprint:
                return snapshots
            baseline_indexes = [
                index
                for index, snapshot in enumerate(snapshots)
                if snapshot.fingerprint == baseline_fingerprint
            ]
            if not baseline_indexes:
                raise ChatSafetyError(
                    "chat_baseline_not_visible",
                    "消息基线已不在可见历史中，禁止猜测缺失回复",
                )
            # 基线表示调用方已确认的“最新”消息。闲鱼同文消息常没有稳定 message id，
            # 因而指纹可能重复；选择时间顺序中最后一次出现，才能从真正最新基线继续监听，
            # 不会把更早的同文历史误认成新回复。
            return snapshots[baseline_indexes[-1] + 1 :]

    async def send_policy_allowed_draft(
        self,
        draft: PolicyAllowedDraft,
        *,
        expected_latest_fingerprint: str,
        auto_send_enabled: bool,
    ) -> SendEvidence:
        """
        发送一条已放行草稿，并等待页面出现本人同文消息作为确认。

        调用方必须显式传入 ``auto_send_enabled=True`` 和读取阶段的最新消息指纹。方法在账号
        guard 内再次确认身份、风险和消息未变化，任何不确定均抛出 ``ChatSafetyError``；
        唯一提交动作是在逐字键盘输入后点击语义确认的“发送”按钮，且不会自动重试。
        """

        if auto_send_enabled is not True:
            raise ChatSafetyError("auto_send_disabled", "自动发送开关未显式开启")
        if not FINGERPRINT_PATTERN.fullmatch(expected_latest_fingerprint):
            raise ChatSafetyError("invalid_expected_fingerprint", "缺少有效的最新消息指纹")

        async with self._account_guard.hold():
            await self._assert_bound_identity()
            chat_input, send_button = await self._assert_chat_ready()
            # 标签可在空输入时检查；启用态必须等草稿写入后再查（空框时常 disabled）。
            await self._assert_send_button_label(send_button)
            latest = await self._read_latest_message_unlocked()
            self._assert_unchanged(latest, expected_latest_fingerprint)
            own_count_before = await self._count_matching_own_messages(draft.text)

            # 闲鱼受控文本框对直接 fill 显示文字，但真实 Canary 未形成可提交的内部状态。
            # 先清空，再逐字产生键盘事件；随后还会读取 value 做同文校验。
            await chat_input.fill("", timeout=5_000)
            await chat_input.press_sequentially(draft.text, delay=50)
            if normalize_chat_text(await chat_input.input_value()) != normalize_chat_text(
                draft.text
            ):
                raise ChatSafetyError(
                    "chat_input_not_confirmed",
                    "聊天输入框未能确认完整草稿",
                )
            await self._assert_bound_identity()
            latest_before_click = await self._read_latest_message_unlocked()
            self._assert_unchanged(latest_before_click, expected_latest_fingerprint)

            _, send_button = await self._assert_chat_ready()
            await self._assert_send_button_ready(send_button)
            observation = _ActiveSendObservation(draft_text=draft.text)
            attempt = _SendAttemptState()
            self._active_send_observation = observation
            try:
                # 只执行一次可见鼠标轨迹点击；不注入脚本点击、不回退 Enter，也不再次点击。
                await self._click_send_button_with_mouse(send_button, attempt)
                attempt.phase = "waiting_confirmation"
                confirmation = await self._wait_for_own_confirmation(
                    draft.text,
                    own_count_before,
                )
                attempt.confirmation_observed = True
                attempt.phase = "confirmation_observed"
                # 本人同文回显是发送成功的硬证据。网络侧明文载荷匹配是增强证据；
                # 闲鱼常把正文放进加密/二进制帧，抓不到请求时不得把已回显消息判失败。
                request_evidence = await self._wait_for_send_request_evidence(observation)
            except ChatSendUncertainError:
                raise
            except ChatSafetyError as exc:
                attempt.record_safety_error(exc.code)
                request_evidence = observation.snapshot()
                raise ChatSendUncertainError(
                    exc.code,
                    str(exc),
                    request_evidence,
                    attempt.snapshot(request_evidence),
                ) from None
            except Exception:
                if attempt.click_attempted:
                    attempt.phase = "unexpected_after_click"
                else:
                    attempt.phase = "unexpected_before_click"
                request_evidence = observation.snapshot()
                raise ChatSendUncertainError(
                    "send_pointer_result_uncertain",
                    "唯一鼠标提交后的结果无法安全确认，禁止自动重试",
                    request_evidence,
                    attempt.snapshot(request_evidence),
                ) from None
            finally:
                self._active_send_observation = None
            return SendEvidence(
                source_item_id=self._binding.source_item_id,
                seller_id=self._binding.seller_id,
                account_id=self._binding.account_id,
                policy_decision_id=draft.policy_decision_id,
                draft_sha256=hashlib.sha256(
                    normalize_chat_text(draft.text).encode("utf-8")
                ).hexdigest(),
                confirmed_message_fingerprint=confirmation.fingerprint,
                request_evidence=request_evidence,
            )

    async def _click_send_button_with_mouse(
        self,
        send_button: Locator,
        attempt: _SendAttemptState,
    ) -> None:
        """
        使用可见鼠标轨迹在发送按钮中心完成一次按下与松开。

        输入已通过语义和唯一性校验的按钮；无返回；按钮不可见或边界异常时失败关闭。

        """

        await send_button.scroll_into_view_if_needed(timeout=5_000)
        box = await send_button.bounding_box()
        if (
            box is None
            or box["width"] < 4
            or box["height"] < 4
        ):
            raise ChatSafetyError(
                "chat_send_button_geometry_invalid",
                "聊天发送按钮没有可确认的可点击区域",
            )
        attempt.phase = "checking_button"
        click_point: tuple[float, float] | None = None
        try:
            for x_fraction, y_fraction in SEND_BUTTON_CLICK_FRACTIONS:
                point_x = box["x"] + box["width"] * x_fraction
                point_y = box["y"] + box["height"] * y_fraction
                targets_button = await send_button.evaluate(
                    SEND_BUTTON_HIT_TEST_SCRIPT,
                    {"x": point_x, "y": point_y},
                )
                if (x_fraction, y_fraction) == (0.5, 0.5):
                    attempt.button_center_obscured = not bool(targets_button)
                if targets_button:
                    click_point = (point_x, point_y)
                    break
        except Exception as error:
            attempt.phase = "button_hit_test_failed"
            raise ChatSafetyError(
                "chat_send_button_hit_test_failed",
                "无法确认发送按钮是否存在可安全点击区域",
            ) from error
        if click_point is None:
            attempt.phase = "button_obscured"
            raise ChatSafetyError(
                "chat_send_button_obscured",
                "发送按钮没有可确认的安全点击区域，已停止且未点击",
            )
        click_x, click_y = click_point
        await self._page.mouse.move(
            click_x,
            click_y,
            steps=SEND_POINTER_MOVE_STEPS,
        )
        await self._page.wait_for_timeout(SEND_POINTER_SETTLE_MS)
        attempt.click_attempted = True
        attempt.phase = "click_dispatched"
        await self._page.mouse.down()
        await self._page.wait_for_timeout(SEND_POINTER_PRESS_MS)
        await self._page.mouse.up()

    async def _wait_for_send_request_evidence(
        self,
        observation: _ActiveSendObservation,
    ) -> SendRequestEvidence:
        """
        有界等待与草稿正文匹配的 XHR/fetch 请求或 WebSocket 出站帧。

        输入当前点击窗口；返回脱敏证据；不读取响应正文且等待结束后不会触发重试。
        """

        for _ in range(SEND_REQUEST_WAIT_ATTEMPTS):
            if observation.request_observed:
                return observation.snapshot()
            await self._page.wait_for_timeout(SEND_REQUEST_WAIT_DELAY_MS)
        return observation.snapshot()

    async def _assert_safe(self) -> None:
        """
        使用项目统一 ``detect_risk`` 检查登录、验证码和风控可见信号。

        无输入和返回；风险信号或主体两秒内无法稳定时抛出 ``ChatSafetyError``；只读取页面。
        """

        if self._blocked_risk_reason:
            raise ChatSafetyError("http_risk_blocked", self._blocked_risk_reason)
        visible_text = await _read_stable_visible_body(self._page)
        reason = detect_risk(self._page.url, visible_text)
        if reason:
            raise ChatSafetyError("risk_or_login_blocked", reason)

    async def _assert_bound_identity(self) -> None:
        """
        同时确认 URL、商品、卖家和当前账号均与不可变绑定一致。

        无输入和返回；任一身份缺失、冲突或不一致时抛出 ``ChatSafetyError``；只读页面。
        """

        await self._assert_safe()
        await _assert_account_cookie_identity(self._page, self._binding.account_id)
        if item_url_matches_binding(self._page.url, self._binding.source_item_id):
            _entry, href = await _read_stable_chat_entry(self._page)
            seller_id = _parse_chat_entry_href(
                self._page.url,
                href,
                self._binding.source_item_id,
            )
            if seller_id != self._binding.seller_id:
                raise ChatSafetyError("chat_identity_mismatch", "卖家身份与任务绑定不一致")
            return
        if not chat_url_matches_binding(
            self._page.url,
            self._binding.source_item_id,
            self._binding.seller_id,
        ):
            raise ChatSafetyError("chat_identity_mismatch", "聊天页商品或卖家身份不一致")

    async def _assert_chat_ready(self) -> tuple[Locator, Locator]:
        """
        确认聊天面板、输入框和发送按钮均只有一个可见元素。

        无输入；返回输入框与发送按钮；DOM 缺失或歧义时抛出 ``ChatSafetyError``；只读 DOM。
        """

        # 闲鱼聊天页在 DOMContentLoaded 后仍会异步挂载 React 控件；先做有界等待，
        # 再执行唯一性检查，避免把“尚未渲染”误判成结构歧义，同时保留失败关闭边界。
        for selector in (
            CHAT_PANEL_SELECTOR,
            CHAT_MESSAGE_LIST_SELECTOR,
            CHAT_INPUT_SELECTOR,
            CHAT_SEND_SELECTOR,
        ):
            await self._page.wait_for_selector(
                selector,
                state="visible",
                timeout=CHAT_READY_WAIT_MILLISECONDS,
            )

        await _unique_visible_locator(self._page, CHAT_PANEL_SELECTOR, "聊天面板")
        await _unique_visible_locator(self._page, CHAT_MESSAGE_LIST_SELECTOR, "消息列表")
        chat_input = await _unique_visible_locator(self._page, CHAT_INPUT_SELECTOR, "聊天输入框")
        send_button = await _unique_visible_locator(self._page, CHAT_SEND_SELECTOR, "聊天发送按钮")
        return chat_input, send_button

    async def _assert_send_button_label(self, send_button: Locator) -> None:
        """
        确认唯一按钮公开标签仍为“发送”，不要求此时已启用。

        输入由 ``_assert_chat_ready`` 返回的唯一 Locator；无返回；标签漂移时抛出
        ``ChatSafetyError``。空输入时按钮常为 disabled，因此标签检查可在录入前执行。
        """

        send_label = "".join(
            normalize_chat_text(await send_button.inner_text(timeout=2_000)).split()
        )
        if send_label != "发送":
            raise ChatSafetyError(
                "chat_send_label_changed",
                "聊天发送按钮语义与标定结果不一致",
            )

    async def _assert_send_button_ready(self, send_button: Locator) -> None:
        """
        确认唯一按钮已启用且公开标签仍为“发送”。

        输入由 ``_assert_chat_ready`` 返回的唯一 Locator；无返回；禁用或语义漂移时抛出
        ``ChatSafetyError``。函数只读取按钮状态，不输入、不按键也不点击。
        """

        if not await send_button.is_enabled():
            raise ChatSafetyError("chat_send_disabled", "聊天发送按钮当前不可用")
        await self._assert_send_button_label(send_button)

    async def _read_latest_message_unlocked(self) -> ChatMessageSnapshot:
        """
        在调用方已持有账号 guard 时读取最后一个可见消息节点。

        无输入；返回稳定快照或确定性空会话；节点过多或消息不确定时抛出 ``ChatSafetyError``。
        """

        snapshots = await self._read_visible_messages_unlocked()
        return snapshots[-1] if snapshots else _empty_conversation_snapshot()

    async def _read_visible_messages_unlocked(self) -> list[ChatMessageSnapshot]:
        """
        在调用方持有账号 guard 时读取全部可见消息快照。

        无输入；按 DOM 顺序返回快照；历史过长、文本为空或方向不明时失败关闭。
        """

        messages = self._page.locator(CHAT_MESSAGE_SELECTOR)
        count = await messages.count()
        if count > MAX_MESSAGE_NODES:
            raise ChatSafetyError("chat_history_too_large", "聊天消息节点数量超出安全上限")
        positioned_snapshots: list[tuple[float, int, ChatMessageSnapshot]] = []
        for index in range(count):
            candidate = messages.nth(index)
            if await candidate.is_visible():
                top = await candidate.evaluate("node => node.getBoundingClientRect().top")
                if not isinstance(top, (int, float)):
                    raise ChatSafetyError("message_order_not_confirmed", "无法确认聊天消息页面位置")
                positioned_snapshots.append((float(top), index, await _snapshot_message(candidate)))
        # 闲鱼当前消息列表的 flex 声明与实际渲染顺序可不一致。页面从上到下就是从旧到新，
        # 因而以真实几何位置排序，而不是猜测 CSS flexDirection。
        positioned_snapshots.sort(key=lambda entry: (entry[0], entry[1]))
        return [snapshot for _, _, snapshot in positioned_snapshots]

    def _assert_unchanged(
        self, latest: ChatMessageSnapshot, expected_latest_fingerprint: str
    ) -> None:
        """
        比较当前最新消息与调用方读取阶段的指纹。

        参数为当前快照和预期指纹；不一致时抛出 ``ChatSafetyError``；没有页面副作用。
        """

        if latest.fingerprint != expected_latest_fingerprint:
            raise ChatSafetyError(
                "conversation_changed_before_send",
                "最新消息在发送前发生变化，必须重新生成并审核草稿",
            )

    async def _count_matching_own_messages(self, text: str) -> int:
        """
        统计当前可见的本人同文消息，用于排除历史重复文本的误确认。

        参数为草稿文本；返回匹配数量；节点异常向上抛出且只读取 DOM。
        """

        expected = normalize_chat_text(text)
        messages = self._page.locator(OWN_CHAT_MESSAGE_SELECTOR)
        count = 0
        for index in range(await messages.count()):
            node = messages.nth(index)
            if (
                await node.is_visible()
                and normalize_chat_text(await node.inner_text(timeout=2_000)) == expected
            ):
                count += 1
        return count

    async def _wait_for_own_confirmation(
        self, text: str, previous_matching_count: int
    ) -> ChatMessageSnapshot:
        """
        等待可见本人同文消息数量比点击前增加，并返回新增消息快照。

        参数为草稿和提交前数量；最长约十秒的固定轮询内未确认时抛出 ``ChatSafetyError``；
        只读取页面，不会再次输入、按键或点击发送。
        """

        expected = normalize_chat_text(text)
        for _ in range(SEND_CONFIRMATION_ATTEMPTS):
            await self._assert_bound_identity()
            await self._assert_chat_ready()
            messages = self._page.locator(OWN_CHAT_MESSAGE_SELECTOR)
            matching: list[Locator] = []
            for index in range(await messages.count()):
                node = messages.nth(index)
                if (
                    await node.is_visible()
                    and normalize_chat_text(await node.inner_text(timeout=2_000)) == expected
                ):
                    matching.append(node)
                if len(matching) > previous_matching_count:
                    # 闲鱼客户端会在本地先渲染一个右侧气泡；若服务端未接收，气泡会很快撤销。
                    # 因此不能把首次出现当作成功，必须确认它在完整消息列表中稳定保留。
                    await self._page.wait_for_timeout(SEND_CONFIRMATION_STABILITY_MS)
                    stable_messages = await self._read_visible_messages_unlocked()
                    stable_matches = [
                        message
                        for message in stable_messages
                        if message.direction == "self"
                        and normalize_chat_text(message.text) == expected
                    ]
                    if len(stable_matches) > previous_matching_count:
                        return stable_matches[-1]
                await self._page.wait_for_timeout(SEND_CONFIRMATION_DELAY_MS)
        raise ChatSafetyError(
            "send_confirmation_missing",
            "提交发送后未能确认本人同文消息，禁止自动重试",
        )
