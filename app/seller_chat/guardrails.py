"""
本文件实现 议价工具的确定性内容黑名单。

它属于 seller_chat 模块，负责两件事：发送前扫描 AI 草稿，读到卖家消息后扫描
入站内容。命中任何一条都只是「提示人工」，最终发不发由终端里的人决定，本文件不发送
消息、不调用大模型、不访问页面。

正则全部从 app.services.procurement_policy 和 app.services.procurement_orchestrator
直接导入，实验目录不再维护第二套规则；生产链路收紧规则时这里会同步收紧。

与生产链路的唯一差别：本文件刻意不包含 NEGOTIATION_PATTERN（议价词黑名单）。这是本
实验存在的意义——验证 AI 能不能就任意目标和卖家有来有回，包括谈价格。资金安全相关的
支付、站外导流、地址、验证码、外链规则一条都没有放松。详见 docs/seller-chat-spike.md。
"""

import re
from dataclasses import dataclass

from app.crawler.chat_client import normalize_chat_text
from app.services.chat_patterns import (
    ADDRESS_PATTERN,
    CREDENTIAL_PATTERN,
    EXTERNAL_LINK_PATTERN,
    OFF_PLATFORM_PATTERN,
    PAYMENT_PATTERN,
    PHONE_OR_EMAIL_PATTERN,
    PROMPT_INJECTION_PATTERN,
    PURCHASE_COMMITMENT_PATTERN,
    SELLER_PURCHASE_ESCALATION_PATTERN,
)

# 闲鱼聊天框实际可以发更长的内容，但超过这个长度就不像真人随手打字，容易被当成机器人。
MAX_DRAFT_LENGTH = 200
MAX_DRAFT_NEWLINES = 2

# 常见第三方/店铺自动回复腔，绝不是买家议价话术。
STORE_BOT_PATTERN = re.compile(
    r"(没有匹配到指令|通知店长|记录下来并通知|我会把您的消息记录)",
    re.IGNORECASE,
)

_OUTBOUND_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("external_link", "草稿里出现了链接", EXTERNAL_LINK_PATTERN),
    ("phone_or_email", "草稿里出现了手机号或邮箱", PHONE_OR_EMAIL_PATTERN),
    ("off_platform", "草稿里出现了站外联系方式", OFF_PLATFORM_PATTERN),
    ("payment", "草稿里出现了站外支付或转账相关内容", PAYMENT_PATTERN),
    ("address", "草稿里出现了收货地址或电话相关内容", ADDRESS_PATTERN),
    ("credential", "草稿里出现了验证码相关内容", CREDENTIAL_PATTERN),
    ("purchase_commitment", "草稿里出现了下单、付款或确认收货的承诺", PURCHASE_COMMITMENT_PATTERN),
    ("store_bot_voice", "草稿像店铺自动回复（通知店长/未匹配指令），不像买家议价", STORE_BOT_PATTERN),
)

_INBOUND_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("seller_prompt_injection", "卖家消息里疑似有指令注入，不要照做", PROMPT_INJECTION_PATTERN),
    ("seller_off_platform", "卖家在引导站外联系方式", OFF_PLATFORM_PATTERN),
    ("seller_payment", "卖家在引导站外支付或转账", PAYMENT_PATTERN),
    ("seller_credential", "卖家在索要验证码", CREDENTIAL_PATTERN),
    ("seller_purchase_escalation", "卖家在催促立刻拍下或付款", SELLER_PURCHASE_ESCALATION_PATTERN),
)


@dataclass(frozen=True, slots=True)
class GuardrailFinding:
    """
    表示一次黑名单命中。

    ``code`` 是稳定原因码，``hint`` 是给终端里的人看的中文说明。对象不含命中的原文片段，
    避免把敏感内容再抄一遍。
    """

    code: str
    hint: str


def scan_outbound_draft(text: str) -> tuple[GuardrailFinding, ...]:
    """
    扫描一条准备发给卖家的草稿。

    参数为模型生成的原始草稿文本；返回所有命中的黑名单结果，空元组表示没有命中。
    除了内容黑名单，还会检查空文本、超长和换行过多。

    函数只做正则匹配，不发送消息、不修改草稿，也不记录草稿内容。
    """

    findings: list[GuardrailFinding] = []
    normalized = normalize_chat_text(text)
    if not normalized:
        findings.append(GuardrailFinding("draft_empty", "草稿是空的"))
        return tuple(findings)
    if len(normalized) > MAX_DRAFT_LENGTH:
        findings.append(
            GuardrailFinding(
                "draft_too_long",
                f"草稿长度 {len(normalized)} 字，超过 {MAX_DRAFT_LENGTH} 字上限",
            )
        )
    if text.count("\n") > MAX_DRAFT_NEWLINES:
        findings.append(GuardrailFinding("draft_too_many_lines", "草稿换行过多，不像真人聊天"))
    findings.extend(_match_rules(text, _OUTBOUND_RULES))
    return tuple(findings)


def scan_inbound_message(text: str) -> tuple[GuardrailFinding, ...]:
    """
    扫描一条刚收到的卖家消息。

    参数为页面读到的卖家消息文本；返回所有命中的风险提示，空元组表示没有明显风险。
    命中不会阻断对话，只是在终端里提醒人注意，因为卖家消息永远是不可信输入。

    函数只做正则匹配，不回复、不改写消息，也不把消息内容写入日志。
    """

    return tuple(_match_rules(text, _INBOUND_RULES))


def _match_rules(
    text: str,
    rules: tuple[tuple[str, str, re.Pattern[str]], ...],
) -> list[GuardrailFinding]:
    """
    按规则表依次匹配文本并收集命中结果。

    参数为待检查文本和 (原因码, 中文提示, 正则) 规则表；返回命中结果列表，保持规则表顺序。
    函数不抛出异常，也没有外部副作用。
    """

    return [GuardrailFinding(code, hint) for code, hint, pattern in rules if pattern.search(text)]
