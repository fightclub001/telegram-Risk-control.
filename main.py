"""
生产级 Telegram 机器人
群组内容监控、举报系统、管理员控制面板

符合规范：
- Telegram Bot API 标准
- aiogram 3.14.0+ 
- Railway 部署规范
- Python 环境规范
"""

import asyncio
import os
import sys
import time
import signal
from datetime import datetime
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatPermissions
)
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

# 导入自定义模块
from bot_logging import logger, setup_logging
from bot_config import (
    IMMUTABLE_CONFIG, config_manager, validate_immutable_config,
    get_config, update_config
)
from bot_data import (
    report_manager, keyword_manager, blacklist_manager, save_all_data, load_all_data
)
from bot_admin import router as admin_router

# ==================== 全局配置 ====================
validate_immutable_config()

TOKEN = IMMUTABLE_CONFIG["BOT_TOKEN"]
GROUP_IDS = IMMUTABLE_CONFIG["GROUP_IDS"]
ADMIN_IDS = IMMUTABLE_CONFIG["ADMIN_IDS"]

# 初始化 Bot 和 Dispatcher
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# 注册路由
dp.include_router(admin_router)
dp.include_router(router)

# 速率限制
user_rate_limits = defaultdict(lambda: {"last_action": None, "count": 0})

# ==================== 工具函数 ====================
async def check_rate_limit(user_id: int, action: str = "report", limit_per_hour: int = 5) -> tuple[bool, str]:
    """
    检查用户是否超过速率限制
    
    参数:
        user_id: 用户 ID
        action: 操作类型
        limit_per_hour: 每小时限制次数
    
    返回:
        (是否通过, 错误消息)
    """
    now = time.time()
    rate_limit_window = get_config("rate_limit_window", 3600)
    
    key = f"{user_id}:{action}"
    user_data = user_rate_limits[key]
    
    # 重置计数器（超过时间窗口）
    if user_data["last_action"] is None or (now - user_data["last_action"]) > rate_limit_window:
        user_rate_limits[key] = {"last_action": now, "count": 0}
        return True, ""
    
    # 检查是否超限
    if user_data["count"] >= limit_per_hour:
        remaining = rate_limit_window - (now - user_data["last_action"])
        return False, f"操作过于频繁，请在 {int(remaining)} 秒后重试"
    
    # 更新计数
    user_rate_limits[key]["count"] += 1
    return True, ""

async def notify_admins(title: str, details: str, urgent: bool = False):
    """
    通知所有管理员
    
    参数:
        title: 标题
        details: 详细内容
        urgent: 是否紧急（会不禁用通知）
    """
    if not ADMIN_IDS:
        logger.warning("没有管理员，无法发送通知")
        return
    
    message = f"<b>{title}</b>\n\n{details}"
    
    failed_count = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                message,
                parse_mode=ParseMode.HTML,
                disable_notification=not urgent
            )
            logger.debug(f"已通知管理员: {admin_id}")
        except Exception as e:
            logger.error(f"通知管理员 {admin_id} 失败: {e}")
            failed_count += 1
    
    if failed_count > 0:
        logger.warning(f"有 {failed_count} 个管理员未成功通知")

def is_sensitive_user(user) -> bool:
    """
    检查用户是否为敏感用户
    
    参数:
        user: Telegram 用户对象
    
    返回:
        是否为敏感用户
    """
    # 显示名检测
    if not get_config("enable_display_name_check", True):
        return False
    
    display_name_keywords = [
        "加v", "加微信", "加qq", "加扣", "福利加", "约", "约炮", "资源私聊", "私我", "私聊我",
        "飞机", "纸飞机", "福利", "外围", "反差", "嫩模", "学生妹", "空姐", "人妻", "熟女",
        "onlyfans", "of", "leak", "nudes", "十八+", "av"
    ]
    
    first_name = (user.first_name or "").lower()
    last_name = (user.last_name or "").lower()
    username = (user.username or "").lower()
    
    display_name = f"{first_name} {last_name}".lower()
    
    for keyword in display_name_keywords:
        if keyword in display_name or keyword in username:
            return True
    
    return False

async def check_bio_keywords(user_id: int) -> bool:
    """
    检查用户简介是否包含敏感词
    
    参数:
        user_id: 用户 ID
    
    返回:
        是否包含敏感词
    """
    if not get_config("enable_bio_check", True):
        return False
    
    try:
        # 获取用户信息（如果可用）
        user_profile = await bot.get_user_profile_photos(user_id, limit=1)
        # 注：Telegram Bot API 不直接支持获取用户简介
        # 实际应用中需要通过群组成员信息或其他方式获取
        return False
    except Exception as e:
        logger.debug(f"检查用户简介失败: {e}")
        return False

# ==================== 消息处理器 ====================
@router.message(F.chat.id.in_(GROUP_IDS))
async def handle_group_message(message: Message):
    """处理群组消息"""
    try:
        # 检查发送者
        if message.from_user is None:
            return
        
        # 检查敏感用户
        is_sensitive = is_sensitive_user(message.from_user)
        bio_sensitive = await check_bio_keywords(message.from_user.id)
        
        if not (is_sensitive or bio_sensitive):
            return  # 不敏感，返回
        
        # 检查速率限制
        allowed, msg = await check_rate_limit(
            message.from_user.id,
            "warn",
            get_config("max_reports_per_hour", 5)
        )
        
        if not allowed:
            logger.debug(f"用户 {message.from_user.id} 触发速率限制")
            return
        
        # 生成警告信息
        warning_text = (
            f"⚠️ <b>检测到疑似广告引流规避行为</b>\n\n"
            f"<b>用户ID:</b> {message.from_user.id}\n"
            f"<b>昵称:</b> {message.from_user.first_name or 'N/A'} {message.from_user.last_name or ''}\n"
            f"<b>用户名:</b> @{message.from_user.username or 'N/A'}\n"
            f"<b>原消息:</b> <code>{message.text[:100] if message.text else 'N/A'}</code>\n\n"
            f"<b>举报人数:</b> 0\n"
            f"点击下方按钮举报此用户"
        )
        
        # 发送警告消息
        try:
            warning_message = await message.reply(
                warning_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👮 举报此用户", callback_data=f"report:{message.message_id}")
                ]])
            )
            
            # 保存举报记录
            await report_manager.add_report(
                message_id=message.message_id,
                warning_id=warning_message.message_id,
                suspect_id=message.from_user.id,
                chat_id=message.chat.id,
                original_text=warning_text,
                original_message_id=message.message_id
            )
            
            logger.info(
                f"检测敏感用户 {message.from_user.id} 在群 {message.chat.id}，"
                f"原因: {'显示名' if is_sensitive else '简介'}"
            )
        except TelegramBadRequest as e:
            logger.error(f"发送警告失败: {e}")
    
    except Exception as e:
        logger.error(f"处理群组消息异常: {e}", exc_info=True)

@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    """处理举报回调"""
    try:
        original_id = int(callback.data.split(":", 1)[1])
        reporter_id = callback.from_user.id
        
        # 获取举报记录
        report = await report_manager.get_report(original_id)
        if not report:
            await callback.answer("该举报已过期", show_alert=True)
            return
        
        # 检查是否已举报
        has_reported = await report_manager.has_reported(original_id, reporter_id)
        if has_reported:
            await callback.answer("您已经举报过了", show_alert=True)
            return
        
        # 添加举报者
        await report_manager.add_reporter(original_id, reporter_id)
        count = await report_manager.get_report_count(original_id)
        
        # 更新消息
        auto_ban_threshold = get_config("auto_ban_threshold", 3)
        if count >= auto_ban_threshold:
            status = f"🚨 超 {auto_ban_threshold} 人举报 已通知管理员\n\n举报人数: {count}"
            await notify_admins(
                "🚨 多人举报警告",
                f"用户 {report['suspect_id']}\n群组 {report['chat_id']}\n举报人数: {count}",
                urgent=True
            )
        else:
            status = f"🚨 已有人举报\n\n举报人数: {count}"
        
        # 更新警告消息
        lines = report["original_text"].splitlines()
        prefix = "\n".join(lines[:3]) if len(lines) >= 3 else report["original_text"]
        new_text = f"{prefix}\n{status}"
        
        try:
            await bot.edit_message_text(
                chat_id=report["chat_id"],
                message_id=report["warning_id"],
                text=new_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👮 举报此用户", callback_data=f"report:{original_id}"),
                    InlineKeyboardButton(text="🔨 禁言24小时", callback_data=f"ban24h:{original_id}"),
                    InlineKeyboardButton(text="🚫 永久禁言", callback_data=f"banperm:{original_id}"),
                ]])
            )
        except TelegramBadRequest:
            logger.debug(f"更新举报消息失败（可能已删除）: {original_id}")
        
        await callback.answer(f"✅ 举报成功！当前 {count} 人")
        logger.info(f"用户 {reporter_id} 举报了用户 {report['suspect_id']}")
    
    except Exception as e:
        logger.error(f"处理举报异常: {e}")
        await callback.answer("操作失败", show_alert=True)

@router.callback_query(F.data.startswith(("ban24h:", "banperm:")))
async def handle_ban(callback: CallbackQuery):
    """处理禁言操作"""
    try:
        action, original_id_str = callback.data.split(":", 1)
        original_id = int(original_id_str)
        caller_id = callback.from_user.id
        
        # 权限检查
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员可操作", show_alert=True)
            return
        
        # 获取举报记录
        report = await report_manager.get_report(original_id)
        if not report:
            await callback.answer("记录已过期", show_alert=True)
            return
        
        suspect_id = report["suspect_id"]
        warning_id = report["warning_id"]
        chat_id = report["chat_id"]
        original_message_id = report.get("original_message_id")
        
        # 执行禁言
        try:
            until_date = int(time.time()) + get_config("ban_duration_24h", 86400) if action == "ban24h" else None
            
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=suspect_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                ),
                until_date=until_date
            )
        except TelegramBadRequest as e:
            error_msg = str(e).lower()
            if "user_not_participant" in error_msg:
                await callback.answer("用户不在群组", show_alert=True)
            elif "not enough rights" in error_msg:
                await callback.answer("机器人缺少权限", show_alert=True)
            else:
                await callback.answer(f"操作失败: {str(e)}", show_alert=True)
            return
        
        # 更新警告消息
        ban_type = "禁言24小时" if action == "ban24h" else "永久限制"
        lines = report["original_text"].splitlines()
        prefix = "\n".join(lines[:2]) if len(lines) >= 2 else report["original_text"]
        new_text = f"{prefix}\n🚨 已由管理员 {ban_type}"
        
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=warning_id,
                text=new_text,
                reply_markup=None
            )
        except TelegramBadRequest:
            logger.debug(f"更新警告消息失败: {warning_id}")
        
        # 延迟删除
        if get_config("enable_delete_after_ban", True):
            async def delayed_delete():
                await asyncio.sleep(get_config("delete_warning_timeout", 10))
                try:
                    await bot.delete_message(chat_id, warning_id)
                    logger.debug(f"删除警告消息: {warning_id}")
                except:
                    pass
                try:
                    if original_message_id:
                        await bot.delete_message(chat_id, original_message_id)
                        logger.debug(f"删除用户消息: {original_message_id}")
                except:
                    pass
            
            asyncio.create_task(delayed_delete())
        
        # 删除举报记录
        await report_manager.remove_report(original_id)
        
        await callback.answer(f"✅ 已{ban_type}")
        logger.info(f"管理员 {caller_id} 对用户 {suspect_id} 执行 {ban_type}")
    
    except Exception as e:
        logger.error(f"禁言操作异常: {e}")
        await callback.answer("操作失败", show_alert=True)

# ==================== 清理任务 ====================
async def cleanup_deleted_messages():
    """清理已删除的消息和过期的举报"""
    logger.info("✅ 清理任务已启动")
    
    while True:
        try:
            await asyncio.sleep(get_config("cleanup_check_interval", 600))
            
            # 清理过期举报
            expiry_time = get_config("report_expiry_time", 3600)
            await report_manager.cleanup_expired(expiry_time)
            
        except asyncio.CancelledError:
            logger.info("清理任务被取消")
            break
        except Exception as e:
            logger.error(f"清理任务异常: {e}", exc_info=True)
            await asyncio.sleep(60)

# ==================== 启动和关闭 ====================
async def on_startup():
    """启动钩子"""
    logger.info("=" * 60)
    logger.info("🚀 Telegram 机器人启动中...")
    logger.info("=" * 60)
    
    # 加载数据
    await load_all_data()
    
    # 初始化配置
    config_manager._load_config()
    setup_logging(log_level=config_manager.get("log_level", "INFO"))
    
    logger.info(f"✅ 配置加载完成")
    logger.info(f"📊 监控群组: {len(GROUP_IDS)}")
    logger.info(f"👮 管理员数: {len(ADMIN_IDS)}")
    logger.info("=" * 60)

async def on_shutdown():
    """关闭钩子"""
    logger.info("=" * 60)
    logger.info("🛑 机器人关闭中...")
    
    # 保存所有数据
    await save_all_data()
    
    # 关闭连接
    try:
        await dp.storage.close()
    except:
        pass
    
    try:
        await bot.session.close()
    except:
        pass
    
    logger.info("✅ 机器人已安全关闭")
    logger.info("=" * 60)

async def main():
    """主函数"""
    try:
        await on_startup()
        
        # 创建清理任务
        cleanup_task = asyncio.create_task(cleanup_deleted_messages())
        
        # 启动轮询
        logger.info("📡 开始轮询 Telegram 服务器...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            skip_updates=False
        )
    
    except KeyboardInterrupt:
        logger.info("收到键盘中断信号")
    except Exception as e:
        logger.error(f"运行时异常: {e}", exc_info=True)
        raise
    finally:
        await on_shutdown()

# ==================== 信号处理 ====================
def handle_signal(signum, frame):
    """处理系统信号"""
    logger.info(f"收到信号 {signum}，正在关闭...")
    if sys.platform != "win32":
        raise KeyboardInterrupt

if __name__ == "__main__":
    # Railway 运行环境下的信号处理
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("机器人已终止")
    except Exception as e:
        logger.error(f"致命错误: {e}", exc_info=True)
        sys.exit(1)
