"""日志工具：控制台 + 滚动文件，两路输出。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_initialized = False


def setup_logger(name: str = "screener", log_file: str | None = None, level: int = logging.INFO) -> logging.Logger:
    """初始化根 logger。重复调用只执行一次文件句柄创建。

    - log_file 为空时只输出到控制台；
    - 目录不存在会自动创建。
    """
    global _initialized
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter(_FMT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file and not _initialized:
        try:
            d = os.path.dirname(log_file)
            if d:
                os.makedirs(d, exist_ok=True)
            fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as exc:  # 日志文件不可写时不影响主流程
            logger.warning("无法创建日志文件 %s: %s", log_file, exc)
        _initialized = True
    return logger
