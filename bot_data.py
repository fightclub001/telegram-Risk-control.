"""
机器人数据管理模块
处理报告、关键词、黑名单等数据的持久化存储
"""

import json
import os
import asyncio
import logging
import time
from typing import Dict, List, Set, Optional, Any
from pathlib import Path

from bot_config import (
    CONFIG_BASE_DIR, REPORTS_FILE, BIO_KEYWORDS_FILE,
    BLACKLIST_CONFIG_FILE
)

logger = logging.getLogger(__name__)

# ==================== 数据管理器 ====================
class ReportDataManager:
    """举报数据管理器"""
    
    def __init__(self):
        self.reports: Dict[int, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self._load()
    
    def _load(self):
        """从文件加载举报数据"""
        try:
            if os.path.exists(REPORTS_FILE):
                with open(REPORTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.reports = {}
                    for k, v in data.items():
                        try:
                            message_id = int(k)
                            self.reports[message_id] = {
                                "warning_id": v["warning_id"],
                                "suspect_id": v["suspect_id"],
                                "chat_id": v["chat_id"],
                                "reporters": set(v.get("reporters", [])),
                                "original_text": v.get("original_text", ""),
                                "original_message_id": v.get("original_message_id"),
                                "timestamp": v.get("timestamp", time.time())
                            }
                        except (ValueError, KeyError) as e:
                            logger.warning(f"跳过无效的报告记录 {k}: {e}")
                            continue
                logger.info(f"✅ 已加载 {len(self.reports)} 条举报记录")
            else:
                self.reports = {}
                logger.info("没有找到历史举报数据，初始化新数据库")
        except json.JSONDecodeError as e:
            logger.error(f"举报数据文件损坏: {e}，重新初始化")
            self.reports = {}
            self._backup_corrupted_file(REPORTS_FILE)
        except Exception as e:
            logger.error(f"加载举报数据异常: {e}")
            self.reports = {}
    
    async def save(self):
        """异步保存举报数据"""
        try:
            safe_reports = {}
            async with self.lock:
                for k, v in self.reports.items():
                    safe_reports[str(k)] = {
                        "warning_id": v["warning_id"],
                        "suspect_id": v["suspect_id"],
                        "chat_id": v["chat_id"],
                        "reporters": list(v["reporters"]),  # 转换 set 为 list
                        "original_text": v["original_text"],
                        "original_message_id": v.get("original_message_id"),
                        "timestamp": v.get("timestamp", time.time())
                    }
            
            # 原子性写入
            temp_file = f"{REPORTS_FILE}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(safe_reports, f, ensure_ascii=False, indent=2)
            
            if os.path.exists(REPORTS_FILE):
                os.replace(temp_file, REPORTS_FILE)
            else:
                os.rename(temp_file, REPORTS_FILE)
            
            logger.debug(f"举报数据已保存: {len(safe_reports)} 条")
        except Exception as e:
            logger.error(f"保存举报数据失败: {e}")
    
    async def add_report(self, message_id: int, warning_id: int, suspect_id: int,
                        chat_id: int, original_text: str, original_message_id: Optional[int] = None):
        """添加举报记录"""
        async with self.lock:
            self.reports[message_id] = {
                "warning_id": warning_id,
                "suspect_id": suspect_id,
                "chat_id": chat_id,
                "reporters": set(),
                "original_text": original_text,
                "original_message_id": original_message_id,
                "timestamp": time.time()
            }
        await self.save()
    
    async def add_reporter(self, message_id: int, reporter_id: int) -> bool:
        """添加举报者"""
        async with self.lock:
            if message_id not in self.reports:
                return False
            self.reports[message_id]["reporters"].add(reporter_id)
        await self.save()
        return True
    
    async def get_report(self, message_id: int) -> Optional[Dict[str, Any]]:
        """获取举报记录"""
        async with self.lock:
            return self.reports.get(message_id)
    
    async def get_report_count(self, message_id: int) -> int:
        """获取举报人数"""
        async with self.lock:
            report = self.reports.get(message_id)
            return len(report["reporters"]) if report else 0
    
    async def has_reported(self, message_id: int, reporter_id: int) -> bool:
        """检查是否已举报"""
        async with self.lock:
            report = self.reports.get(message_id)
            return reporter_id in report["reporters"] if report else False
    
    async def remove_report(self, message_id: int):
        """删除举报记录"""
        async with self.lock:
            self.reports.pop(message_id, None)
        await self.save()
    
    async def get_expired_reports(self, expiry_time: int) -> List[int]:
        """获取已过期的举报"""
        async with self.lock:
            now = time.time()
            expired = [
                mid for mid, data in self.reports.items()
                if now - data.get("timestamp", 0) > expiry_time
            ]
            return expired
    
    async def cleanup_expired(self, expiry_time: int):
        """清理过期举报"""
        expired = await self.get_expired_reports(expiry_time)
        if expired:
            async with self.lock:
                for mid in expired:
                    self.reports.pop(mid, None)
            await self.save()
            logger.info(f"清理了 {len(expired)} 条过期举报")
    
    async def get_count(self) -> int:
        """获取总举报数"""
        async with self.lock:
            return len(self.reports)
    
    @staticmethod
    def _backup_corrupted_file(filepath: str):
        """备份损坏的文件"""
        try:
            backup_file = f"{filepath}.corrupted_{int(time.time())}"
            os.rename(filepath, backup_file)
            logger.info(f"损坏文件已备份: {backup_file}")
        except:
            pass


class KeywordDataManager:
    """关键词数据管理器"""
    
    def __init__(self):
        self.bio_keywords: List[str] = []
        self.lock = asyncio.Lock()
        self._load()
    
    def _load(self):
        """从文件加载关键词"""
        try:
            if os.path.exists(BIO_KEYWORDS_FILE):
                with open(BIO_KEYWORDS_FILE, "r", encoding="utf-8") as f:
                    self.bio_keywords = json.load(f)
                logger.info(f"✅ 已加载 {len(self.bio_keywords)} 条简介关键词")
            else:
                # 使用默认关键词
                self.bio_keywords = self._get_default_keywords()
                self._save()
        except json.JSONDecodeError as e:
            logger.error(f"关键词文件损坏: {e}，使用默认关键词")
            self.bio_keywords = self._get_default_keywords()
            self._save()
        except Exception as e:
            logger.error(f"加载关键词异常: {e}")
            self.bio_keywords = self._get_default_keywords()
    
    @staticmethod
    def _get_default_keywords() -> List[str]:
        """获取默认关键词列表"""
        return [
            "qq:", "qq：", "qq号", "加qq", "扣扣",
            "微信", "wx:", "weixin", "加我微信", "wxid_",
            "幼女", "萝莉", "少妇", "人妻", "福利", "约炮",
            "onlyfans", "小红书", "抖音", "纸飞机", "机场",
            "http", "https", "t.me/", "@"
        ]
    
    def _save(self):
        """保存关键词到文件"""
        try:
            os.makedirs(os.path.dirname(BIO_KEYWORDS_FILE), exist_ok=True)
            with open(BIO_KEYWORDS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.bio_keywords, f, ensure_ascii=False, indent=2)
            logger.debug(f"关键词已保存: {len(self.bio_keywords)} 条")
        except Exception as e:
            logger.error(f"保存关键词失败: {e}")
    
    async def save(self):
        """异步保存关键词"""
        async with self.lock:
            self._save()
    
    async def add_keyword(self, keyword: str) -> bool:
        """添加关键词"""
        keyword = keyword.strip().lower()
        if not keyword or len(keyword) > 100:
            return False
        
        async with self.lock:
            if keyword not in self.bio_keywords:
                self.bio_keywords.append(keyword)
                self._save()
                return True
            return False
    
    async def remove_keyword(self, keyword: str) -> bool:
        """删除关键词"""
        keyword = keyword.strip().lower()
        async with self.lock:
            if keyword in self.bio_keywords:
                self.bio_keywords.remove(keyword)
                self._save()
                return True
            return False
    
    async def get_keywords(self) -> List[str]:
        """获取所有关键词"""
        async with self.lock:
            return self.bio_keywords.copy()
    
    async def get_count(self) -> int:
        """获取关键词总数"""
        async with self.lock:
            return len(self.bio_keywords)
    
    def contains_keyword(self, text: str) -> Optional[str]:
        """检查文本是否包含关键词，返回匹配的关键词"""
        text = text.lower()
        for keyword in self.bio_keywords:
            if keyword in text:
                return keyword
        return None


class BlacklistDataManager:
    """黑名单数据管理器"""
    
    def __init__(self):
        self.blacklist_config: Dict[str, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self._load()
    
    def _load(self):
        """从文件加载黑名单配置"""
        try:
            if os.path.exists(BLACKLIST_CONFIG_FILE):
                with open(BLACKLIST_CONFIG_FILE, "r", encoding="utf-8") as f:
                    self.blacklist_config = json.load(f)
                logger.info(f"✅ 已加载黑名单配置: {len(self.blacklist_config)} 个群组")
            else:
                self.blacklist_config = {}
                logger.info("没有找到黑名单配置，初始化新配置")
        except json.JSONDecodeError as e:
            logger.error(f"黑名单配置文件损坏: {e}，重新初始化")
            self.blacklist_config = {}
        except Exception as e:
            logger.error(f"加载黑名单配置异常: {e}")
            self.blacklist_config = {}
    
    def _save(self):
        """保存黑名单配置到文件"""
        try:
            os.makedirs(os.path.dirname(BLACKLIST_CONFIG_FILE), exist_ok=True)
            with open(BLACKLIST_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.blacklist_config, f, ensure_ascii=False, indent=2)
            logger.debug(f"黑名单配置已保存")
        except Exception as e:
            logger.error(f"保存黑名单配置失败: {e}")
    
    async def save(self):
        """异步保存黑名单配置"""
        async with self.lock:
            self._save()
    
    async def set_blacklist_config(self, group_id: str, enabled: bool, duration: int):
        """设置黑名单配置"""
        async with self.lock:
            self.blacklist_config[group_id] = {
                "enabled": enabled,
                "duration": duration  # 秒，0 = 永久
            }
            self._save()
    
    async def get_blacklist_config(self, group_id: str) -> Optional[Dict[str, Any]]:
        """获取黑名单配置"""
        async with self.lock:
            return self.blacklist_config.get(group_id)
    
    async def is_blacklist_enabled(self, group_id: str) -> bool:
        """检查黑名单是否启用"""
        config = await self.get_blacklist_config(group_id)
        return config.get("enabled", False) if config else False
    
    async def get_config_count(self) -> int:
        """获取配置的黑名单群组数"""
        async with self.lock:
            return len(self.blacklist_config)


# ==================== 全局数据管理器实例 ====================
report_manager = ReportDataManager()
keyword_manager = KeywordDataManager()
blacklist_manager = BlacklistDataManager()

# ==================== 便捷函数 ====================
async def save_all_data():
    """保存所有数据"""
    await report_manager.save()
    await keyword_manager.save()
    await blacklist_manager.save()
    logger.info("✅ 所有数据已保存")

async def load_all_data():
    """加载所有数据（初始化时调用）"""
    logger.info("📂 开始加载所有数据...")
    # 数据管理器的 __init__ 已经自动加载了
    report_count = await report_manager.get_count()
    keyword_count = await keyword_manager.get_count()
    blacklist_count = await blacklist_manager.get_config_count()
    
    logger.info(f"✅ 数据加载完成: {report_count} 举报, {keyword_count} 关键词, {blacklist_count} 黑名单配置")
