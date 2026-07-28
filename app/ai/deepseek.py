"""
本文件提供 DeepSeek Chat Completions 的最小配置与响应解析。

它属于 ai 模块：只定义配置与从包络提取文本的纯函数。
议价草稿生成由 app.seller_chat.llm 负责；本文件不访问环境变量或 Playwright。
"""

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr
from pydantic import field_validator as pydantic_field_validator

from app.ai.base import AiOutputError

MAX_PROVIDER_CONTENT_LENGTH = 20_000


class DeepSeekConfig(BaseModel):
    """
    表示调用 DeepSeek 所需的显式、不可变配置。

    API 密钥使用 SecretStr；配置不从环境变量自动读取，也不产生网络副作用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    api_key: SecretStr = Field(min_length=16)
    base_url: AnyHttpUrl = Field(default=AnyHttpUrl("https://api.deepseek.com"))
    model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=128)
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    max_tokens: int = Field(default=1200, ge=256, le=4096)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)

    @pydantic_field_validator("base_url")
    @classmethod
    def require_https_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """
        要求模型端点使用 HTTPS。

        输入已解析 URL；非 HTTPS 时抛出 ValueError。
        """

        if value.scheme != "https":
            raise ValueError("DeepSeek base_url 必须使用 HTTPS")
        return value

    @property
    def chat_completions_url(self) -> str:
        """
        返回 Chat Completions 完整地址。

        无输入；不读取密钥。
        """

        return f"{str(self.base_url).rstrip('/')}/chat/completions"


def _extract_completed_content(body: object) -> str:
    """
    从 DeepSeek 非流式包络提取自然结束的字符串内容。

    输入已解码 JSON 对象；任一结构偏离或非 stop 结束时抛出 AiOutputError。
    """

    if not isinstance(body, dict):
        raise AiOutputError
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiOutputError
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise AiOutputError
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise AiOutputError
    content = message.get("content")
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > MAX_PROVIDER_CONTENT_LENGTH
    ):
        raise AiOutputError
    return content
