"""
机器人配置管理模块
支持动态参数调整，所有参数可通过管理员面板修改
"""

import json
import os
import asyncio
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# ==================== 文件路径配置 ====================
CONFIG_BASE_DIR = os.getenv("CONFIG_DIR", "/data")
os.makedirs(CONFIG_BASE_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_BASE_DIR, "config.json")
REPORTS_FILE = os.path.join(CONFIG_BASE_DIR, "reports.json")
BIO_KEYWORDS_FILE = os.path.join(CONFIG_BASE_DIR, "bio_keywords.json")
BLACKLIST_CONFIG_FILE = os.path.join(CONFIG_BASE_DIR, "blacklist_config.json")
LOG_DIR = os.path.join(CONFIG_BASE_DIR, "logs")

os.makedirs(LOG_DIR, exist_ok=True)

# ==================== 不可修改的核心配置 ====================
# 这三个变量必须通过环境变量设置，不可动态修改
IMMUTABLE_CONFIG = {
    "BOT_TOKEN": os.getenv("BOT_TOKEN", ""),
    "GROUP_IDS": set(),
    "ADMIN_IDS": set(),
}

def validate_immutable_config():
    """验证必需的环境变量"""
    if not IMMUTABLE_CONFIG["BOT_TOKEN"]:
        raise ValueError("❌ 缺少 BOT_TOKEN 环境变量")
    
    group_ids_str = os.getenv("GROUP_IDS", "").strip()
    if not group_ids_str:
        raise ValueError("❌ 缺少 GROUP_IDS 环境变量，格式: '123456 789012'")
    
    try:
        for gid in group_ids_str.split():
            IMMUTABLE_CONFIG["GROUP_IDS"].add(int(gid.strip()))
    except ValueError as e:
        raise ValueError(f"❌ 无效的群组 ID: {e}")
    
    admin_ids_str = os.getenv("ADMIN_IDS", "").strip()
    if not admin_ids_str:
        raise ValueError("❌ 缺少 ADMIN_IDS 环境变量，格式: '111222 333444'")
    
    try:
        for uid in admin_ids_str.split():
            IMMUTABLE_CONFIG["ADMIN_IDS"].add(int(uid.strip()))
    except ValueError as e:
        raise ValueError(f"❌ 无效的管理员 ID: {e}")
    
    logger.info(f"✅ 环境变量验证成功: {len(IMMUTABLE_CONFIG['GROUP_IDS'])} 个群组, {len(IMMUTABLE_CONFIG['ADMIN_IDS'])} 个管理员")

# ==================== 可动态修改的默认配置 ====================
DEFAULT_CONFIG = {
    # 清理任务配置（秒）
    "cleanup_check_interval": 600,              # 清理检查间隔（10分钟）
    "report_expiry_time": 3600,                 # 举报记录过期时间（1小时）
    "deleted_message_cleanup_delay": 10,        # 删除警告消息延迟（10秒）
    "max_reports_in_memory": 1000,              # 最多保留举报数
    "batch_cleanup_size": 5,                    # 批量清理消息数
    
    # 举报配置
    "auto_ban_threshold": 3,                    # 自动通知管理员的举报阈值
    "ban_duration_24h": 86400,                  # 24小时禁言（秒）
    "ban_duration_week": 604800,                # 1周禁言（秒）
    
    # 速率限制配置
    "rate_limit_window": 3600,                  # 速率限制窗口（1小时）
    "max_reports_per_hour": 5,                  # 每小时最多举报次数
    "max_keyword_queries_per_hour": 10,         # 每小时最多关键词查询次数
    
    # 关键词检测配置
    "enable_bio_check": True,                   # 是否启用简介检查
    "enable_display_name_check": True,          # 是否启用显示名检查
    "enable_fuzzy_match": False,                # 是否启用模糊匹配（未来功能）
    
    # 消息配置
    "enable_delete_after_ban": True,            # 禁言后是否删除消息
    "delete_warning_timeout": 10,               # 删除警告消息的延迟
    "warning_message_timeout": 3600,            # 警告消息保留时间
    
    # 黑名单配置
    "default_blacklist_duration": 0,            # 默认黑名单时长（0=永久）
    "enable_auto_blacklist": False,             # 是否启用自动黑名单
    
    # 日志配置
    "log_level": "INFO",                        # 日志级别
    "log_retention_days": 7,                    # 日志保留天数
    
    # 性能配置
    "max_concurrent_operations": 10,            # 最多并发操作数
    "api_call_timeout": 30,                     # API 调用超时（秒）
}

class ConfigManager:
    """配置管理器"""
    
    def __init__(self):
        self.config: Dict[str, Any] = DEFAULT_CONFIG.copy()
        self.lock = asyncio.Lock()
        self._load_config()
    
    def _load_config(self):
        """从文件加载配置"""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved_config = json.load(f)
                    # 只加载已定义的配置项，忽略未知项
                    for key in DEFAULT_CONFIG:
                        if key in saved_config:
                            self.config[key] = saved_config[key]
                logger.info(f"✅ 从 {CONFIG_FILE} 加载配置成功")
            else:
                self._save_config()
        except json.JSONDecodeError as e:
            logger.error(f"配置文件损坏: {e}，使用默认配置")
            self.config = DEFAULT_CONFIG.copy()
            self._save_config()
        except Exception as e:
            logger.error(f"加载配置异常: {e}，使用默认配置")
            self.config = DEFAULT_CONFIG.copy()
    
    def _save_config(self):
        """保存配置到文件"""
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ 配置已保存到 {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"❌ 保存配置失败: {e}")
    
    async def save_config(self):
        """异步保存配置"""
        async with self.lock:
            self._save_config()
    
    def get(self, key: str, default=None) -> Any:
        """获取配置值"""
        return self.config.get(key, default)
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self.config.copy()
    
    async def update(self, key: str, value: Any) -> bool:
        """更新配置"""
        if key not in DEFAULT_CONFIG:
            logger.warning(f"尝试更新未定义的配置项: {key}")
            return False
        
        # 类型检查
        expected_type = type(DEFAULT_CONFIG[key])
        if type(value) != expected_type:
            logger.warning(f"配置类型错误: {key} 期望 {expected_type}, 得到 {type(value)}")
            return False
        
        # 数值范围检查
        if isinstance(value, int):
            if value < 0:
                logger.warning(f"配置值不能为负数: {key} = {value}")
                return False
        
        async with self.lock:
            self.config[key] = value
            self._save_config()
        
        logger.info(f"✅ 配置已更新: {key} = {value}")
        return True
    
    def reset_to_default(self):
        """重置为默认配置"""
        self.config = DEFAULT_CONFIG.copy()
        self._save_config()
        logger.info("✅ 配置已重置为默认值")
    
    async def get_config_description(self, key: str) -> Optional[str]:
        """获取配置项的描述"""
        descriptions = {
            # 清理任务配置
            "cleanup_check_interval": "清理检查间隔（秒）\n最小值：60，最大值：3600",
            "report_expiry_time": "举报记录过期时间（秒）\n最小值：600，最大值：86400",
            "deleted_message_cleanup_delay": "删除警告消息延迟（秒）\n最小值：5，最大值：60",
            "max_reports_in_memory": "最多保留举报数\n最小值：100，最大值：5000",
            "batch_cleanup_size": "批量清理消息数\n最小值：1，最大值：20",
            
            # 举报配置
            "auto_ban_threshold": "自动通知管理员的举报阈值\n最小值：1，最大值：10",
            "ban_duration_24h": "24小时禁言时长（秒）\n默认：86400",
            "ban_duration_week": "1周禁言时长（秒）\n默认：604800",
            
            # 速率限制配置
            "rate_limit_window": "速率限制窗口（秒）\n最小值：600，最大值：86400",
            "max_reports_per_hour": "每小时最多举报次数\n最小值：1，最大值：50",
            "max_keyword_queries_per_hour": "每小时最多关键词查询次数\n最小值：1，最大值：100",
            
            # 关键词检测配置
            "enable_bio_check": "是否启用简介检查",
            "enable_display_name_check": "是否启用显示名检查",
            "enable_fuzzy_match": "是否启用模糊匹配（实验功能）",
            
            # 消息配置
            "enable_delete_after_ban": "禁言后是否删除消息",
            "delete_warning_timeout": "删除警告消息的延迟（秒）\n最小值：5，最大值：60",
            "warning_message_timeout": "警告消息保留时间（秒）\n最小值：300，最大值：86400",
            
            # 黑名单配置
            "default_blacklist_duration": "默认黑名单时长（秒）\n0=永久",
            "enable_auto_blacklist": "是否启用自动黑名单",
            
            # 日志配置
            "log_level": "日志级别（DEBUG/INFO/WARNING/ERROR）",
            "log_retention_days": "日志保留天数\n最小值：1，最大值：30",
            
            # 性能配置
            "max_concurrent_operations": "最多并发操作数\n最小值：5，最大值：50",
            "api_call_timeout": "API 调用超时（秒）\n最小值：10，最大值：120",
        }
        return descriptions.get(key)

# 创建全局配置管理器实例
config_manager = ConfigManager()

# ==================== 辅助函数 ====================
def get_config(key: str, default=None) -> Any:
    """便捷函数：获取配置"""
    return config_manager.get(key, default)

async def update_config(key: str, value: Any) -> bool:
    """便捷函数：更新配置"""
    return await config_manager.update(key, value)

def format_config_value(value: Any) -> str:
    """格式化配置值以供显示"""
    if isinstance(value, bool):
        return "✅ 启用" if value else "❌ 禁用"
    elif isinstance(value, int):
        return str(value)
    else:
        return str(value)

def get_all_configurable_keys() -> List[str]:
    """获取所有可配置的键（按分类）"""
    categories = {
        "🧹 清理任务": [
            "cleanup_check_interval",
            "report_expiry_time",
            "deleted_message_cleanup_delay",
            "max_reports_in_memory",
            "batch_cleanup_size",
        ],
        "📊 举报系统": [
            "auto_ban_threshold",
            "ban_duration_24h",
            "ban_duration_week",
        ],
        "⚡ 速率限制": [
            "rate_limit_window",
            "max_reports_per_hour",
            "max_keyword_queries_per_hour",
        ],
        "🔍 关键词检测": [
            "enable_bio_check",
            "enable_display_name_check",
            "enable_fuzzy_match",
        ],
        "💬 消息管理": [
            "enable_delete_after_ban",
            "delete_warning_timeout",
            "warning_message_timeout",
        ],
        "🚫 黑名单": [
            "default_blacklist_duration",
            "enable_auto_blacklist",
        ],
        "📝 日志配置": [
            "log_level",
            "log_retention_days",
        ],
        "⚙️ 性能配置": [
            "max_concurrent_operations",
            "api_call_timeout",
        ],
    }
    return categories
