"""
本文件离线验证结构化议价裁决的解析与失败关闭边界。

它不访问真实模型或闲鱼，只向客户端解析函数传入内存 HTTP 响应。
"""

from __future__ import annotations

import json
import logging

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


@pytest.mark.parametrize(
    ("content", "action", "reason_code"),
    [
        (
            '{"action":"available","reason_code":"in_stock","message":null}',
            "available",
            "in_stock",
        ),
        (
            '{"action":"unavailable","reason_code":"out_of_stock","message":null}',
            "unavailable",
            "out_of_stock",
        ),
    ],
)
def test_parse_decision_accepts_inventory_terminal_signals(
    content: str, action: str, reason_code: str
) -> None:
    """库存终态只能返回信号，不能携带待发送的文本或报价。"""

    decision = _generator()._parse_decision(_response(content))

    assert decision.action == action
    assert decision.reason_code == reason_code
    assert decision.message is None
    assert decision.offer_price_yuan is None

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


@pytest.mark.parametrize(
    ("content", "diagnostic_code"),
    [
        ("", "completion_envelope_invalid"),
        ("不是 JSON", "decision_json_invalid"),
        ('["action", "agreed"]', "decision_not_object"),
        (
            '{"action":"continue","reason_code":"uncertain","message":null}',
            "decision_schema_root_value_error",
        ),
    ],
)
def test_parse_decision_exposes_only_safe_diagnostic_code(
    content: str, diagnostic_code: str
) -> None:
    """解析失败仅暴露固定诊断码，既可定位原因也不记录模型内容。"""

    with pytest.raises(LlmDecisionOutputError) as caught:
        _generator()._parse_decision(_response(content))

    assert caught.value.diagnostic_code == diagnostic_code

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


def test_blank_json_mode_response_retries_once_in_text_json_mode() -> None:
    """JSON mode 空白时只重试一次，且第二次不带 response_format。"""

    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return _response("   ")
        return _response('{"action":"refused","reason_code":"no_concession","message":null}')

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        decision = _generator(client=client).decide(
            system_prompt="请输出 JSON 裁决",
            opening_brief="商品信息",
            transcript=[],
        )

    assert decision.action == "refused"
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in payloads[1]


def test_two_invalid_decision_attempts_expose_retry_exhausted_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """两次不合法响应只抛脱敏重试耗尽码，并留下两条可诊断响应记录。"""

    def respond(request: httpx.Request) -> httpx.Response:
        del request
        return _response("   ")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with caplog.at_level(logging.INFO, logger="app.decision_payload"):
            with pytest.raises(LlmDecisionOutputError) as caught:
                _generator(client=client).decide(
                    system_prompt="请输出 JSON 裁决",
                    opening_brief="商品信息",
                    transcript=[],
                )

    assert caught.value.diagnostic_code == "decision_retry_exhausted_completion_envelope_invalid"
    records = [json.loads(record.getMessage()) for record in caplog.records]
    assert [(record["attempt"], record["mode"]) for record in records] == [
        (1, "json_object"),
        (2, "text_json_fallback"),
    ]


def test_decision_response_log_keeps_model_content_without_request_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """调试日志保存模型响应正文与包络元数据，不记录请求提示词或认证信息。"""

    content = '{"action":"refused","reason_code":"no_concession","message":null}'
    with caplog.at_level(logging.INFO, logger="app.decision_payload"):
        _generator()._parse_decision(_response(content))

    record = json.loads(caplog.records[-1].getMessage())
    assert record["event"] == "deepseek_decision_response"
    assert record["assistant_content"] == content
    assert record["finish_reason"] == "stop"
    assert "Authorization" not in record["raw_response"]


@pytest.mark.parametrize(
    "content",
    [
        '{"action":"continue","reason_code":"uncertain","message":"300 可以吗"}',
        '{"action":"continue","reason_code":"uncertain","message":"再聊聊","offer_price_yuan":300}',
        '{"action":"continue","reason_code":"uncertain","message":null,"offer_price_yuan":0}',
    ],
)
def test_decision_rejects_uncontrolled_numeric_offer(content: str) -> None:
    """普通消息不能夹带数字报价，金额只能经独立受控字段交给 Worker。"""

    with pytest.raises(LlmDecisionOutputError):
        _generator()._parse_decision(_response(content))

@pytest.mark.parametrize(
    "content",
    [
        '{"action":"agreed","reason_code":"price_cut","message":"谢谢"}',
        '{"action":"continue","reason_code":"uncertain","message":null}',
        '{"action":"refused","reason_code":"other_concession","message":null}',
        '{"action":"available","reason_code":"in_stock","message":"还在"}',
        '{"action":"unavailable","reason_code":"out_of_stock","offer_price_yuan":1}',
        '{"action":"available","reason_code":"out_of_stock","message":null}',
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
