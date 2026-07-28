"""
本文件负责配置应用日志。

它属于 core 模块，只设置标准库 logging 格式与级别，不写业务逻辑。
"""

import logging

from app.core.config import get_settings


def setup_logging() -> None:
    """
    按配置初始化根日志格式。

    无输入输出；副作用为配置 root logger。可重复调用，后一次覆盖前一次。
    """

    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
