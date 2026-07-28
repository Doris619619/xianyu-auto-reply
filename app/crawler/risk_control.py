"""
本文件识别登录失效、访问限制和异常页面。

它属于 crawler 模块，只分类风险信号，不尝试验证码、重试或绕过限制。
"""

from app.crawler.selectors import LOGIN_URL_FRAGMENTS, RISK_TEXT_SIGNALS

RISK_RESPONSE_URL_FRAGMENTS = (
    "/_____tmd_____/punish",
)


class RiskControlBlocked(RuntimeError):
    """
    表示任务必须因认证或风控立即停止。

    输入安全分类后的原因；调用方负责记录安全消息，无其他副作用。
    """


def detect_risk_response(url: str, status: int | None = None) -> str | None:
    """
    根据单个页面响应的 URL 与状态识别不可继续的访问控制。

    输入响应 URL 和可选 HTTP 状态；返回不含原始 URL 的稳定原因或 ``None``。本函数只做
    字符串分类，不读取响应正文、不发起重试，也不尝试绕过平台风控。
    """

    if status in {403, 429}:
        return f"页面请求返回 HTTP {status}"
    lowered_url = url.casefold()
    if any(fragment in lowered_url for fragment in RISK_RESPONSE_URL_FRAGMENTS):
        return "页面请求进入平台风控流程"
    return None


def detect_risk(url: str, visible_text: str, blocked_status: int | None = None) -> str | None:
    """
    根据 URL、可见文本和 HTTP 状态返回阻塞原因。

    输入页面公开状态，返回安全分类或 None；不读取凭据、不访问网络。
    """

    lowered_url = url.casefold()
    if any(fragment in lowered_url for fragment in LOGIN_URL_FRAGMENTS):
        return "登录状态失效或页面跳转到登录页"
    response_reason = detect_risk_response(url, blocked_status)
    if response_reason:
        return response_reason
    signal = next((text for text in RISK_TEXT_SIGNALS if text in visible_text), None)
    if signal:
        return f"页面出现风险提示：{signal}"
    return None
