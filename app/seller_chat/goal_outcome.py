"""
本文件判定 议价目标是否已经达成（卖家是否同意降价）。

它属于 seller_chat 模块，只做确定性文本规则，不调用模型、不访问页面、不发送消息。
会话编排在读到卖家回复后调用本文件，决定是否结束对话。
"""

from __future__ import annotations

import re

from app.crawler.chat_client import normalize_chat_text

# 明确拒绝降价：命中后不算「同意」。
_REFUSAL_PATTERN = re.compile(
    r"(不(?:能|可以|行)?(?:再)?(?:便宜|降价|少|减)|"
    r"价格(?:已经)?(?:最低|很实在|不能再)|"
    r"免(?:谈|议)|一口价|不刀|不讲价)",
    re.IGNORECASE,
)

# 卖家同意降价或给出可接受的让利表述。
_AGREE_PATTERN = re.compile(
    r"(可以(?:再)?(?:便宜|少|降|减)|"
    r"(?:给你|帮你|跟你)(?:便宜|少|降|减|优惠)|"
    r"(?:便宜|少|降|减)(?:一点|点儿|一些)|"
    r"(?:同意|行|好的?)(?:吧)?.*(?:便宜|降价|少|减)|"
    r"(?:降|减|改)(?:到|成|为)?\s*\d|"
    r"最低(?:给你)?|"
    r"优惠(?:给你)?|"
    r"改价|"
    r"少\s*\d|"
    r"便宜\s*\d)",
    re.IGNORECASE,
)


def seller_agreed_to_price_cut(texts: list[str] | tuple[str, ...]) -> bool:
    """
    判断一批卖家新消息是否表示同意降价。

    参数为卖家原文列表；任一条在规范化后命中同意规则且未命中拒绝规则时返回 True。
    纯函数，无副作用。
    """

    for raw in texts:
        normalized = normalize_chat_text(raw)
        if not normalized:
            continue
        if _REFUSAL_PATTERN.search(normalized):
            continue
        if _AGREE_PATTERN.search(normalized):
            return True
    return False


def seller_refused_price_cut(texts: list[str] | tuple[str, ...]) -> bool:
    """
    判断一批卖家新消息是否明确拒绝降价。

    参数为卖家原文列表；任一条规范化后命中拒绝规则时返回 True。
    纯函数，无副作用。
    """

    for raw in texts:
        normalized = normalize_chat_text(raw)
        if not normalized:
            continue
        if _REFUSAL_PATTERN.search(normalized):
            return True
    return False

