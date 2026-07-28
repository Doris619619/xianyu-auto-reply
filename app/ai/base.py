"""
本文件定义 AI 层的安全异常基类。

它属于 ai 模块，只提供脱敏错误类型；不发起网络请求，不访问页面。
"""


class AiError(RuntimeError):
    """
    表示一次模型调用失败，异常文本不含密钥、提示词或卖家原文。
    """

    code = "ai_error"
    safe_message = "模型调用失败"

    def __init__(self) -> None:
        """使用类级固定文案构造异常；无外部副作用。"""

        super().__init__(self.safe_message)


class AiTimeoutError(AiError):
    """表示模型请求超时。"""

    code = "ai_timeout"
    safe_message = "模型请求超时"


class AiTransportError(AiError):
    """表示模型请求网络传输失败。"""

    code = "ai_transport"
    safe_message = "模型请求网络失败"


class AiHttpError(AiError):
    """表示模型端点返回非 200。"""

    code = "ai_http"
    safe_message = "模型端点返回错误"


class AiOutputError(AiError):
    """表示模型响应包络或内容不可用。"""

    code = "ai_output"
    safe_message = "模型输出不可用"


# 兼容 seller_chat.llm 既有导入名。
ProcurementAiOutputError = AiOutputError
