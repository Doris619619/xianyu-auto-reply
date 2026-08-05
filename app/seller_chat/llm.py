"""
本文件实现 spike 卖家对话实验使用的极简 DeepSeek 多轮文本客户端。

它属于 spike/seller_chat 实验模块。生产链路的 app.ai.deepseek.DeepSeekDraftGenerator
绑定了采购五类目标的 JSON Schema，并在校验时调用会否决议价的 scan_draft_risks，
无法用于自由目标的对话实验，因此这里只保留最小能力：把聊天记录拼成标准 messages 数组，
拿回一段纯文本。

复用 app.ai.deepseek 的 DeepSeekConfig 与响应包络解析，保证密钥处理和「必须自然结束」
的契约与生产链路完全一致。

本文件不做发送前安全校验（由 guardrails.py 负责）、不接触 Playwright、不访问数据库。
裁决请求会把供应商响应写入仅本机保留的调试日志；不记录密钥、请求提示词或卖家原文。
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.ai.base import AiOutputError, ProcurementAiOutputError
from app.ai.deepseek import DeepSeekConfig, _extract_completed_content
from app.seller_chat.prompts import FOLLOW_UP_NUDGE

MAX_DRAFT_CHARACTERS = 500
MAX_DECISION_RESPONSE_LOG_CHARACTERS = 30_000

decision_payload_logger = logging.getLogger("app.decision_payload")

# 只兼容整个回答被 Markdown 代码块包裹的 JSON，不能容忍前后夹带解释文字。
_COMPLETE_JSON_CODE_FENCE = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<content>.*?)\r?\n?```\Z",
    re.IGNORECASE | re.DOTALL,
)
_PLAIN_AMOUNT = re.compile(r"(?<!\d)\d{1,9}(?:\.\d{1,2})?(?!\d)")

Speaker = Literal["me", "seller"]
NegotiationAction = Literal["continue", "agreed", "refused"]
NegotiationReasonCode = Literal[
    "price_cut",
    "other_concession",
    "no_concession",
    "uncertain",
]


class SellerChatLlmError(RuntimeError):
    """
    表示一次草稿生成失败，且异常文本不含密钥、提示词或卖家原文。

    子类只暴露稳定错误码和固定中文消息，调用方据此决定重试或转人工。
    """

    code = "llm_error"
    safe_message = "生成消息草稿失败"

    def __init__(self) -> None:
        """
        使用类级固定文案构造安全异常。

        无输入；异常文本为脱敏常量；除构造异常外没有任何副作用。
        """

        super().__init__(self.safe_message)


class LlmTimeoutError(SellerChatLlmError):
    """表示 DeepSeek 请求超时且没有拿到可用草稿。"""

    code = "llm_timeout"
    safe_message = "DeepSeek 请求超时，没有生成草稿"


class LlmTransportError(SellerChatLlmError):
    """表示网络传输失败，没有得到任何可信响应。"""

    code = "llm_transport_error"
    safe_message = "DeepSeek 网络请求失败，没有生成草稿"


class LlmHttpError(SellerChatLlmError):
    """表示 DeepSeek 返回非 200 状态，响应正文不会进入异常。"""

    code = "llm_http_error"
    safe_message = "DeepSeek 返回了非成功状态码，可能是密钥或余额问题"


class LlmOutputError(SellerChatLlmError):
    """表示响应包络异常、被截断或正文为空。"""

    code = "llm_output_invalid"
    safe_message = "DeepSeek 返回的内容不完整或为空"


class LlmDecisionOutputError(LlmOutputError):
    """表示模型未按约定返回可安全执行的结构化议价裁决。"""

    code = "llm_decision_output_invalid"
    safe_message = "DeepSeek 返回的议价裁决格式不可用"

    def __init__(self, diagnostic_code: str = "decision_unknown") -> None:
        """
        使用固定异常文案和脱敏诊断码构造裁决输出异常。

        diagnostic_code 只能由本模块的固定分支产生，供 Worker 写入诊断日志；不包含模型
        原文、聊天记录或密钥。
        """

        self.diagnostic_code = diagnostic_code
        super().__init__()


class NegotiationDecision(BaseModel):
    """
    表示模型对一轮卖家回复的唯一可执行裁决。

    终态不携带外发文本；只有 continue 可以给出下一条消息。该模型只校验结构，
    发送前仍必须经过既有硬性安全规则和页面确认。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    action: NegotiationAction
    reason_code: NegotiationReasonCode
    message: str | None = None
    offer_price_yuan: int | None = None

    @model_validator(mode="after")
    def validate_action_contract(self) -> Self:
        """校验 action、原因码与消息字段的固定组合。"""

        if self.action == "continue":
            if self.reason_code != "uncertain":
                raise ValueError("continue 必须携带 uncertain 原因")
            if self.offer_price_yuan is not None:
                if self.offer_price_yuan <= 0 or self.message is not None:
                    raise ValueError("报价 continue 只能携带正整数 offer_price_yuan")
                return self
            if not self.message:
                raise ValueError("非报价 continue 必须携带 message")
            if len(self.message) > 200:
                raise ValueError("continue 的 message 超过 200 字")
            if _PLAIN_AMOUNT.search(self.message):
                raise ValueError("普通 continue message 不得包含数值报价")
            return self
        if self.message is not None or self.offer_price_yuan is not None:
            raise ValueError("终态裁决不得携带 message 或 offer_price_yuan")
        if self.action == "agreed" and self.reason_code in {
            "price_cut",
            "other_concession",
        }:
            return self
        if self.action == "refused" and self.reason_code == "no_concession":
            return self
        raise ValueError("终态裁决的 action 与 reason_code 不匹配")


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """
    表示聊天记录里的一条消息。

    ``speaker`` 只区分「我方」与「卖家」两种角色；文本已由页面适配层规范化。
    该对象是模型输入契约，本身不判断内容是否安全。
    """

    speaker: Speaker
    text: str


def build_chat_messages(
    *,
    system_prompt: str,
    opening_brief: str,
    transcript: Sequence[TranscriptEntry],
) -> list[dict[str, str]]:
    """
    把系统提示、商品背景和聊天记录拼成 DeepSeek 的 messages 数组。

    我方消息映射为 assistant，卖家消息映射为 user；连续同一说话人的消息会合并成一条，
    避免出现多条相邻同角色消息。若拼装后最后一条是 assistant，会追加一条固定跟进提示，
    保证模型必须产出新的一轮。

    参数为非空系统提示、非空商品背景和只读聊天记录；返回可直接提交的消息列表。
    函数只做字符串处理，不发起网络请求。
    """

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": opening_brief},
    ]
    for speaker, text in _merge_consecutive(transcript):
        messages.append(
            {
                "role": "assistant" if speaker == "me" else "user",
                "content": text,
            }
        )
    if messages[-1]["role"] == "assistant":
        messages.append({"role": "user", "content": FOLLOW_UP_NUDGE})
    return messages


def _merge_consecutive(
    transcript: Sequence[TranscriptEntry],
) -> list[tuple[Speaker, str]]:
    """
    合并聊天记录中连续来自同一说话人的消息。

    参数为只读聊天记录；返回 (说话人, 合并文本) 列表，合并时用换行连接。空文本条目会被
    跳过，避免产生空的模型消息。函数无外部副作用。
    """

    merged: list[tuple[Speaker, str]] = []
    for entry in transcript:
        text = entry.text.strip()
        if not text:
            continue
        if merged and merged[-1][0] == entry.speaker:
            previous_speaker, previous_text = merged[-1]
            merged[-1] = (previous_speaker, f"{previous_text}\n{text}")
            continue
        merged.append((entry.speaker, text))
    return merged


class SellerChatDraftGenerator:
    """
    调用 DeepSeek 生成一条要发给卖家的纯文本消息。

    调用方可注入自定义 httpx.Client（例如测试用 MockTransport）；实例不会关闭外部注入的
    Client。生成器只负责拿到文本，不判断内容是否可以发送。
    """

    def __init__(self, config: DeepSeekConfig, *, client: httpx.Client | None = None) -> None:
        """
        保存显式配置并接收可选 HTTP Client。

        参数为不可变 DeepSeek 配置和可注入 Client；未注入时创建自有 Client。
        构造过程不发起任何网络请求。
        """

        self._config = config
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client()

    def generate(
        self,
        *,
        system_prompt: str,
        opening_brief: str,
        transcript: Sequence[TranscriptEntry],
    ) -> str:
        """
        生成下一条要发给卖家的消息文本。

        参数为系统提示、商品背景和当前聊天记录；返回去除首尾空白、长度不超过 500 字的
        文本。超时、网络失败、非 200 状态、响应被截断或正文为空时抛出
        ``SellerChatLlmError`` 的具体子类。方法只发起一次请求，不自动重试。
        """

        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": build_chat_messages(
                system_prompt=system_prompt,
                opening_brief=opening_brief,
                transcript=transcript,
            ),
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        response = self._request_completion(payload)
        return self._parse_draft(response)

    def decide(
        self,
        *,
        system_prompt: str,
        opening_brief: str,
        transcript: Sequence[TranscriptEntry],
    ) -> NegotiationDecision:
        """
        基于完整对话返回继续、谈成或未谈成的结构化裁决。

        输入为只读提示词和聊天记录；返回经 Pydantic 校验的固定决策。
        格式错误、超时或传输失败抛出 SellerChatLlmError，调用方必须失败关闭且不得发送。
        """

        last_error: LlmDecisionOutputError | None = None
        for attempt, json_mode in enumerate((True, False), start=1):
            retry_prompt = system_prompt if json_mode else (
                f"{system_prompt}\n上一轮没有得到可用内容。现在必须直接输出非空 JSON 对象。"
            )
            payload: dict[str, Any] = {
                "model": self._config.model,
                "messages": build_chat_messages(
                    system_prompt=retry_prompt,
                    opening_brief=opening_brief,
                    transcript=transcript,
                ),
                "temperature": self._config.temperature,
                "max_tokens": self._config.max_tokens,
                "stream": False,
                "thinking": {"type": "disabled"},
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            response = self._request_completion(payload)
            try:
                return self._parse_decision(
                    response,
                    attempt=attempt,
                    mode="json_object" if json_mode else "text_json_fallback",
                )
            except LlmDecisionOutputError as error:
                last_error = error
        assert last_error is not None
        raise LlmDecisionOutputError(f"decision_retry_exhausted_{last_error.diagnostic_code}")

    def close(self) -> None:
        """
        关闭由本实例创建的 HTTP Client，保留调用方注入 Client 的所有权。

        无输入和返回值；只释放自有连接资源，不发送请求，也不记录配置内容。
        """

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        """
        返回生成器自身以支持 with 语句下的受控关闭。

        无输入并返回自身；不发起网络请求且没有其他副作用。
        """

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """
        离开上下文时只关闭实例自建的 Client。

        输入标准异常上下文且无返回值；不吞掉异常，也不关闭外部注入的 Client。
        """

        del exc_type, exc_value, traceback
        self.close()

    def _request_completion(self, payload: dict[str, Any]) -> httpx.Response:
        """
        发起一次非流式 DeepSeek 请求并把底层异常收敛成安全异常。

        参数为请求 JSON；返回 HTTP 200 响应。超时抛 ``LlmTimeoutError``，传输失败抛
        ``LlmTransportError``，非 200 抛 ``LlmHttpError``；异常文本不含密钥或正文。
        """

        try:
            response = self._client.post(
                self._config.chat_completions_url,
                headers={
                    "Authorization": f"Bearer {self._config.api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._config.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise LlmTimeoutError from None
        except httpx.RequestError:
            raise LlmTransportError from None

        if response.status_code != 200:
            raise LlmHttpError
        return response

    def _parse_draft(self, response: httpx.Response) -> str:
        """
        从响应中取出唯一一段自然结束的文本并做长度收敛。

        参数为 HTTP 200 响应；返回去除首尾空白的草稿文本。包络异常、被截断（finish_reason
        非 stop）、正文为空或超过 500 字时抛出 ``LlmOutputError``。

        包络解析直接复用 app.ai.deepseek 的实现，保证与生产链路使用同一份契约，
        不在实验目录里重复维护一套 DeepSeek 响应格式判断。
        """

        try:
            body: object = response.json()
        except (ValueError, UnicodeDecodeError):
            raise LlmOutputError from None

        try:
            content = _extract_completed_content(body)
        except ProcurementAiOutputError:
            raise LlmOutputError from None

        draft = content.strip()
        if not draft or len(draft) > MAX_DRAFT_CHARACTERS:
            raise LlmOutputError
        return draft

    def _parse_decision(
        self, response: httpx.Response, *, attempt: int = 1, mode: str = "test"
    ) -> NegotiationDecision:
        """从完成响应中解析并严格校验结构化议价裁决。"""

        _log_decision_response(response, attempt=attempt, mode=mode)
        try:
            body = response.json()
            content = _extract_completed_content(body)
        except (AiOutputError, UnicodeDecodeError, ValueError, TypeError):
            raise LlmDecisionOutputError("completion_envelope_invalid") from None

        try:
            parsed = json.loads(_unwrap_complete_json_code_fence(content))
        except (UnicodeDecodeError, ValueError, TypeError):
            raise LlmDecisionOutputError("decision_json_invalid") from None
        if not isinstance(parsed, dict):
            raise LlmDecisionOutputError("decision_not_object")
        try:
            return NegotiationDecision.model_validate(parsed)
        except ValidationError as error:
            raise LlmDecisionOutputError(_validation_diagnostic_code(error)) from None


def _unwrap_complete_json_code_fence(content: str) -> str:
    """
    仅移除完整包裹响应的 JSON Markdown 代码块。

    参数为模型已完成的非空正文；若正文不是完整代码块则原样去首尾空白返回，
    因此任何前后解释文字仍会在 JSON 解析阶段失败关闭。函数不记录模型原文。
    """

    normalized = content.strip()
    match = _COMPLETE_JSON_CODE_FENCE.fullmatch(normalized)
    if match is None:
        return normalized
    return match.group("content").strip()


def _validation_diagnostic_code(error: ValidationError) -> str:
    """
    将 Pydantic 校验错误压缩成不含模型值的稳定诊断码。

    输入为结构校验异常；返回仅由字段位置和错误类型组成的短码，避免把模型输出写入日志。
    """

    details = error.errors(include_input=False)
    if not details:
        return "decision_schema_invalid"
    first = details[0]
    location = "_".join(str(part) for part in first.get("loc", ())) or "root"
    error_type = str(first.get("type", "invalid")).replace(".", "_")
    return f"decision_schema_{location}_{error_type}"[:120]


def _log_decision_response(response: httpx.Response, *, attempt: int, mode: str) -> None:
    """
    将裁决请求的供应商响应保存到本机 JSONL 调试日志。

    输入为 HTTP 响应；仅记录状态码、完成包络元数据、模型 assistant_content 和原始响应体，
    以定位供应商格式问题。请求提示词、Authorization、Cookie 和登录态均不在此处记录；
    响应体超过上限会截断并标记，避免无限制占用本机磁盘。
    """

    raw_response = response.text
    was_truncated = len(raw_response) > MAX_DECISION_RESPONSE_LOG_CHARACTERS
    record: dict[str, object] = {
        "event": "deepseek_decision_response",
        "attempt": attempt,
        "mode": mode,
        "status_code": response.status_code,
        "raw_response": raw_response[:MAX_DECISION_RESPONSE_LOG_CHARACTERS],
        "raw_response_truncated": was_truncated,
    }
    try:
        body = json.loads(raw_response)
    except (UnicodeDecodeError, ValueError, TypeError):
        record["response_json_decoded"] = False
    else:
        record["response_json_decoded"] = True
        _append_completion_metadata(record, body)
    decision_payload_logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def _append_completion_metadata(record: dict[str, object], body: object) -> None:
    """
    从供应商响应提取不经校验的完成包络元数据和模型正文。

    输入为日志记录和已解码响应；即使包络非法也仅写入可观察的类型、数量与 content，
    不抛异常、不修改响应，也不记录请求侧信息。
    """

    if not isinstance(body, dict):
        record["response_body_type"] = type(body).__name__
        return
    choices = body.get("choices")
    record["choices_type"] = type(choices).__name__
    record["choices_count"] = len(choices) if isinstance(choices, list) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return
    choice = choices[0]
    record["finish_reason"] = choice.get("finish_reason")
    message = choice.get("message")
    record["message_type"] = type(message).__name__
    if not isinstance(message, dict):
        return
    record["message_role"] = message.get("role")
    content = message.get("content")
    record["assistant_content"] = content if isinstance(content, str) else None
    record["assistant_content_type"] = type(content).__name__
    record["assistant_content_length"] = len(content) if isinstance(content, str) else None
