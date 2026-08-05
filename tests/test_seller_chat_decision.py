"""
本文件离线验证结构化议价裁决的解析与失败关闭边界。

它不访问真实模型或闲鱼，只向客户端解析函数传入内存 HTTP 响应。
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.ai.deepseek import DeepSeekConfig
from app.seller_chat.llm import (
    LlmDecisionOutputError,
    NegotiationDecision,
    SellerChatDraftGenerator,
)


def _generator() -> SellerChatDraftGenerator:
    """构造不发起网络请求的生成器实例。"""

    return SellerChatDraftGenerator(DeepSeekConfig(api_key=SecretStr("x" * 32)))


def _response(content: str) -> httpx.Response:
    """构造符合既有完成响应包络的内存 HTTP 响应。"""

    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        },
    )


def test_parse_decision_accepts_other_concession_as_agreed() -> None:
    """包邮、赠品等明确让利可作为谈成信号，不需要外发收尾消息。"""

    decision = _generator()._parse_decision(
        _response('{"action":"agreed","reason_code":"other_concession","message":null}')
    )

    assert decision == NegotiationDecision(
        action="agreed",
        reason_code="other_concession",
        message=None,
    )


@pytest.mark.parametrize(
    "content",
    [
        '{"action":"agreed","reason_code":"price_cut","message":"谢谢"}',
        '{"action":"continue","reason_code":"uncertain","message":null}',
        '{"action":"refused","reason_code":"other_concession","message":null}',
        '{"action":"unknown","reason_code":"uncertain","message":"再问问"}',
        "不是 JSON",
    ],
)
def test_parse_decision_rejects_invalid_or_unsafe_contract(content: str) -> None:
    """非法裁决无法进入 Worker，避免误发文本或错误结束任务。"""

    with pytest.raises(LlmDecisionOutputError):
        _generator()._parse_decision(_response(content))
