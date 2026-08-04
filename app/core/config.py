"""
本文件负责从环境变量读取应用配置。

它属于 core 模块，为 API、Worker、Playwright 与 DeepSeek 提供只读配置。
不读取登录态内容，不发起网络请求。
"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.crawler.browser_backends.storage import (
    BrowserBackend,
    SUPPORTED_BROWSER_BACKENDS,
    resolve_storage_state_path,
)


class Settings(BaseSettings):
    """
    定义应用环境配置。

    输入来自环境变量和可选 `.env`；校验失败时抛出 Pydantic 异常，无外部副作用。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/bargain.db"
    xianyu_browser_backend: BrowserBackend = "chromium"
    xianyu_storage_state_path: str | None = None
    xianyu_headless: bool = False
    xianyu_cdp_endpoint: str | None = None
    xianyu_verify_timeout_seconds: int = Field(default=12, ge=5, le=60)
    xianyu_expected_account_id: str | None = None
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8787, ge=1, le=65535)
    default_reply_timeout_seconds: int = Field(default=180, ge=30, le=900)
    default_max_rounds: int = Field(default=6, ge=1, le=20)
    default_auto_send: bool = True
    log_level: str = "INFO"

    @field_validator("xianyu_browser_backend", mode="before")
    @classmethod
    def normalize_browser_backend(cls, value: object) -> object:
        """
        规范化浏览器后端名称并拒绝非法值。

        输入原始环境值；返回小写后端名；非法值抛出 ValueError。
        """

        if value is None:
            return "chromium"
        text = str(value).strip().lower()
        if text not in SUPPORTED_BROWSER_BACKENDS:
            raise ValueError(
                "XIANYU_BROWSER_BACKEND 必须是 chromium、camoufox 或 cloakbrowser"
            )
        return text

    @field_validator("xianyu_storage_state_path", mode="before")
    @classmethod
    def normalize_optional_storage_path(cls, value: object) -> object | None:
        """
        将空字符串规范化为未配置路径，以便按后端使用默认分文件。

        输入原始环境值；返回非空字符串或 None。
        """

        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("deepseek_api_key", mode="before")
    @classmethod
    def normalize_optional_secret(cls, value: object) -> object | None:
        """
        将空字符串规范化为未配置密钥。

        输入原始环境值；返回原值或 None；不展示 SecretStr 内容。
        """

        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value if value.get_secret_value().strip() else None
        return value if str(value).strip() else None

    @field_validator("xianyu_cdp_endpoint")
    @classmethod
    def validate_local_cdp_endpoint(cls, value: str | None) -> str | None:
        """
        仅允许连接本机 Edge 调试端点，避免把受控会话暴露给远程地址。

        输入可选 HTTP 地址；返回去除空白后的地址；非法地址抛出校验异常；不访问网络。
        """

        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
        ):
            raise ValueError("XIANYU_CDP_ENDPOINT 必须是带端口的本机 HTTP 地址")
        return normalized

    def resolved_storage_state_path(self) -> Path:
        """
        按当前浏览器后端解析登录态文件路径。

        无输入；返回 Path；不检查文件是否存在，也不读取文件内容。
        """

        return resolve_storage_state_path(
            backend=self.xianyu_browser_backend,
            configured_path=self.xianyu_storage_state_path,
        )


@lru_cache
def get_settings() -> Settings:
    """
    返回进程级缓存的配置单例。

    无输入；首次调用时读取环境；后续返回同一实例。测试可调用 ``get_settings.cache_clear()``。
    """

    return Settings()
