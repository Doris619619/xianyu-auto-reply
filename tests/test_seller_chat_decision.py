"""
本文件离线验证结构化议价裁决的解析与失败关闭边界。

它不访问真实模型或闲鱼，只向客户端解析函数传入内存 HTTP 响应。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.ai.deepseek import DeepSeekConfig
from app.seller_chat.llm import (
    LlmDecisionOutputError,
    NegotiationDecision,
    SellerChatDraftGenerator,
)


def _generator(*, client: httpx.Client | None = None) -> SellerChatDraftGenerator:
    """构造不发起网络请求的生成器实例。"""

    return SellerChatDraftGenerator(
        DeepSeekConfig(api_key=SecretStr("x" * 32)), client=client
    )


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


def test_parse_decision_accepts_complete_json_code_fence() -> None:
    """完整 JSON 代码块可兼容解析，但不会放宽字段与终态约束。"""

    decision = _generator()._parse_decision(
        _response(
            "```json\n"
            '{"action":"continue","reason_code":"uncertain","message":"您开个价我看看"}'
            "\n```"
        )
    )

    assert decision == NegotiationDecision(
        action="continue",
        reason_code="uncertain",
        message="您开个价我看看",
    )


def test_decision_request_uses_json_mode_but_draft_request_does_not() -> None:
    """仅裁决请求要求供应商输出 JSON，普通消息草稿继续使用文本模式。"""

    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        content = (
            "老板能便宜一点吗"
            if len(payloads) == 1
            else '{"action":"refused","reason_code":"no_concession","message":null}'
        )
        return _response(content)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        generator = _generator(client=client)
        assert generator.generate(
            system_prompt="普通草稿提示词",
            opening_brief="商品信息",
            transcript=[],
        ) == "老板能便宜一点吗"
        assert generator.decide(
            system_prompt="请输出 JSON 裁决",
            opening_brief="商品信息",
            transcript=[],
        ).action == "refused"

    assert "response_format" not in payloads[0]
    assert payloads[1]["response_format"] == {"type": "json_object"}

@pytest.mark.parametrize(
    "content",
    [
        '{"action":"agreed","reason_code":"price_cut","message":"谢谢"}',
        '{"action":"continue","reason_code":"uncertain","message":null}',
        '{"action":"refused","reason_code":"other_concession","message":null}',
        '{"action":"unknown","reason_code":"uncertain","message":"再问问"}',
        '["action", "agreed"]',
        '```json\n{"action":"agreed","reason_code":"price_cut","message":null}\n```\n解释',
        '```python\n{"action":"agreed","reason_code":"price_cut","message":null}\n```',
        "",
        "不是 JSON",
    ],
)
def test_parse_decision_rejects_invalid_or_unsafe_contract(content: str) -> None:
    """非法裁决无法进入 Worker，避免误发文本或错误结束任务。"""

    with pytest.raises(LlmDecisionOutputError):
        _generator()._parse_decision(_response(content))
