"""
机器人日志系统
提供结构化、分级的日志记录
"""

import logging
import logging.handlers
import os
from datetime import datetime
from bot_config import LOG_DIR

def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """
    配置日志系统
    
    参数:
        log_level: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)
    
    返回:
        logger 实例
    """
    
    # 确保日志目录存在
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # 创建 logger
    logger = logging.getLogger("telegram_bot")
    
    # 设置日志级别
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(numeric_level)
    
    # 移除已有的处理器（防止重复）
    logger.handlers.clear()
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 1. 文件处理器 - INFO 级别（日志轮转，每天一个）
    info_handler = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, "bot.log"),
        when="midnight",
        interval=1,
        backupCount=7,  # 保留 7 天
        encoding="utf-8"
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    info_handler.addFilter(lambda record: record.levelno < logging.ERROR)  # 只记录 INFO/WARNING
    logger.addHandler(info_handler)
    
    # 2. 错误日志处理器 - ERROR 及以上（大小轮转）
    error_handler = logging.handlers.RotatingFileHandler(
        filename=os.path.join(LOG_DIR, "error.log"),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    # 3. 控制台处理器 - 用于本地调试
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    logger.info(f"✅ 日志系统已初始化 (级别: {log_level})")
    
    return logger

# 初始化全局 logger
logger = setup_logging()
