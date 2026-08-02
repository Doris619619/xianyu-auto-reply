"""
本文件验证真实 Edge 调试连接的本机地址约束。

它只构造配置对象，不启动浏览器、不读取登录态，也不访问网络。
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_accepts_loopback_cdp_endpoint() -> None:
    """本机带端口 HTTP 调试地址可以启用真实 Edge 连接模式。"""

    settings = Settings(xianyu_cdp_endpoint="http://127.0.0.1:9222/")

    assert settings.xianyu_cdp_endpoint == "http://127.0.0.1:9222"


def test_rejects_non_loopback_cdp_endpoint() -> None:
    """远程调试地址必须被拒绝，避免将受控会话暴露给网络。"""

    with pytest.raises(ValidationError, match="本机 HTTP 地址"):
        Settings(xianyu_cdp_endpoint="http://example.com:9222")
