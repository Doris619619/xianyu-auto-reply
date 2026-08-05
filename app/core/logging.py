"""
本文件负责配置应用日志。

它属于 core 模块，只设置标准库 logging 格式与级别，不写业务逻辑。
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
    decision_log_dir = Path("data/logs")
    decision_log_dir.mkdir(parents=True, exist_ok=True)
    decision_logger = logging.getLogger("app.decision_diagnostic")
    decision_logger.handlers.clear()
    decision_handler = RotatingFileHandler(
        decision_log_dir / "decision-diagnostics.log",
        encoding="utf-8",
        maxBytes=1_000_000,
        backupCount=3,
    )
    decision_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    decision_logger.addHandler(decision_handler)
    decision_logger.setLevel(logging.INFO)
    decision_logger.propagate = False

    decision_payload_logger = logging.getLogger("app.decision_payload")
    decision_payload_logger.handlers.clear()
    decision_payload_handler = RotatingFileHandler(
        decision_log_dir / "deepseek-decision-responses.jsonl",
        encoding="utf-8",
        maxBytes=2_000_000,
        backupCount=3,
    )
    decision_payload_handler.setFormatter(logging.Formatter("%(message)s"))
    decision_payload_logger.addHandler(decision_payload_handler)
    decision_payload_logger.setLevel(logging.INFO)
    decision_payload_logger.propagate = False
