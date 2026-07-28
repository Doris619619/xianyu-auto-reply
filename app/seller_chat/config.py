"""
本文件负责议价工具启动前的配置读取与失败关闭校验。

它属于 seller_chat 模块，只从 Settings 中挑出真正需要的几项，
并在缺失或不合法时抛出带有可直接照做修复步骤的中文错误。

本文件不启动浏览器、不调用大模型、不访问数据库，也不读取或打印登录态文件内容。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from app.ai.deepseek import DeepSeekConfig
from app.core.config import Settings, get_settings

# 账号身份只接受 tracknick SHA-256，避免把昵称原值写进配置或日志。
ACCOUNT_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DRAFT_MAX_TOKENS = 512
DRAFT_TEMPERATURE = 0.4


class SpikeConfigError(RuntimeError):
    """
    表示实验无法安全启动的配置错误。

    异常文本只包含配置项名称和修复步骤，绝不包含密钥、Cookie 或登录态内容。
    """


@dataclass(frozen=True, slots=True)
class SellerChatSpikeConfig:
    """
    保存本次运行所需的全部只读配置。

    settings 直接交给 Playwright 工厂使用；其余字段是已经过校验、可以安全直接使用的派生值。
    """

    settings: Settings
    deepseek: DeepSeekConfig
    storage_state_path: Path
    expected_account_id: str


def load_spike_config(
    *,
    settings: Settings | None = None,
    headless: bool = False,
) -> SellerChatSpikeConfig:
    """
    读取并校验议价所需配置，缺一项就拒绝启动。

    参数 settings 为空时读取进程级缓存配置；headless 覆盖浏览器模式，默认有头。
    任一必填项缺失、格式不合法或登录态文件不存在时抛出 SpikeConfigError。
    """

    resolved = settings if settings is not None else get_settings()
    resolved = resolved.model_copy(update={"xianyu_headless": headless})

    api_key = resolved.deepseek_api_key
    if api_key is None:
        raise SpikeConfigError(
            "缺少 DEEPSEEK_API_KEY。请在仓库根目录 .env 中配置 DEEPSEEK_API_KEY。"
        )

    account_id = (resolved.xianyu_expected_account_id or "").strip()
    if not ACCOUNT_FINGERPRINT_PATTERN.fullmatch(account_id):
        raise SpikeConfigError(
            "缺少合法的 XIANYU_EXPECTED_ACCOUNT_ID。它必须是 tracknick Cookie 的 "
            "SHA-256（64 位小写十六进制）。获取方式：先运行 scripts/login_xianyu.py "
            "刷新登录态，再运行 python scripts/print_account_fingerprint.py。"
        )

    storage_state_path = Path(resolved.xianyu_storage_state_path)
    if not storage_state_path.is_file():
        raise SpikeConfigError(
            f"闲鱼登录态文件不存在：{storage_state_path}。"
            "请先运行 scripts/login_xianyu.py 完成人工登录并生成登录态。"
        )

    try:
        deepseek = DeepSeekConfig(
            api_key=api_key,
            base_url=resolved.deepseek_base_url,
            model=resolved.deepseek_model,
            timeout_seconds=resolved.deepseek_timeout_seconds,
            max_tokens=DRAFT_MAX_TOKENS,
            temperature=DRAFT_TEMPERATURE,
        )
    except ValueError as error:
        raise SpikeConfigError(f"DeepSeek 配置不合法：{error}") from None

    return SellerChatSpikeConfig(
        settings=resolved,
        deepseek=deepseek,
        storage_state_path=storage_state_path,
        expected_account_id=account_id,
    )
