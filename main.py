"""
生产级 Telegram 机器人 - 测试版
带完整的调试日志，帮助排查 /admin 命令问题
"""

import asyncio
import os
import sys
import time
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, ChatPermissions
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

from bot_logging import logger, setup_logging
from bot_config import (
    IMMUTABLE_CONFIG, config_manager, validate_immutable_config,
    get_config
)
from bot_data import (
    report_manager, keyword_manager, blacklist_manager, save_all_data, load_all_data
)
from bot_admin import router as admin_router

# ==================== 配置验证 ====================
try:
    validate_immutable_config()
    TOKEN = IMMUTABLE_CONFIG["BOT_TOKEN"]
    GROUP_IDS = IMMUTABLE_CONFIG["GROUP_IDS"]
    ADMIN_IDS = IMMUTABLE_CONFIG["ADMIN_IDS"]
    print(f"✅ 配置验证成功")
    print(f"   - BOT_TOKEN: {TOKEN[:10]}...（已隐藏）")
    print(f"   - GROUP_IDS: {GROUP_IDS}")
    print(f"   - ADMIN_IDS: {ADMIN_IDS}")
except Exception as e:
    print(f"❌ 配置验证失败: {e}")
    sys.exit(1)

# ==================== 初始化 Bot 和 Dispatcher ====================
print("初始化 Bot 和 Dispatcher...")
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# 注册路由 - admin_router 必须先注册
print("注册路由...")
dp.include_router(admin_router)
dp.include_router(router)

# ==================== 测试命令 ====================
@router.message(Command("test"))
async def cmd_test(message: Message):
    """测试命令 - 检查机器人是否正常工作"""
    print(f"📝 /test 命令被触发")
    print(f"   用户 ID: {message.from_user.id}")
    print(f"   用户名: {message.from_user.username}")
    print(f"   聊天类型: {message.chat.type}")
    print(f"   聊天 ID: {message.chat.id}")
    
    text = (
        f"✅ <b>测试命令成功</b>\n\n"
        f"用户 ID: {message.from_user.id}\n"
        f"用户名: @{message.from_user.username or '未设置'}\n"
        f"聊天类型: {message.chat.type}\n\n"
        f"如果你是管理员，试试发送 /admin"
    )
    await message.reply(text, parse_mode="HTML")

@router.message(Command("admin"))
async def cmd_admin_test(message: Message):
    """测试 admin 命令 - 检查权限"""
    user_id = message.from_user.id
    print(f"🔑 /admin 命令被触发")
    print(f"   用户 ID: {user_id}")
    print(f"   管理员列表: {ADMIN_IDS}")
    print(f"   是否为管理员: {user_id in ADMIN_IDS}")
    print(f"   聊天类型: {message.chat.type}")
    
    if user_id not in ADMIN_IDS:
        await message.reply("❌ 你不是管理员，无法访问管理面板")
        print(f"❌ 用户 {user_id} 不在管理员列表中")
        return
    
    if message.chat.type != "private":
        await message.reply("❌ 请在私聊中使用 /admin 命令")
        print(f"❌ /admin 命令在非私聊中被调用")
        return
    
    print(f"✅ 用户 {user_id} 权限检查通过，打开管理面板")
    await message.reply("✅ 管理面板已打开\n\n等待 admin_router 处理...")

@router.message(Command("status"))
async def cmd_status(message: Message):
    """查看机器人状态"""
    print(f"📊 /status 命令被触发")
    
    try:
        report_count = await report_manager.get_count()
        keyword_count = await keyword_manager.get_count()
        
        text = (
            f"✅ <b>机器人状态</b>\n\n"
            f"<b>配置:</b>\n"
            f"├ 监控群组: {len(GROUP_IDS)}\n"
            f"├ 管理员: {len(ADMIN_IDS)}\n"
            f"├ 你的ID: {message.from_user.id}\n"
            f"├ 是否管理员: {'✅' if message.from_user.id in ADMIN_IDS else '❌'}\n\n"
            f"<b>数据:</b>\n"
            f"├ 举报记录: {report_count}\n"
            f"├ 敏感词: {keyword_count}\n\n"
            f"<b>提示:</b>\n"
            f"如果你是管理员，在<b>私聊</b>中发送 /admin"
        )
        
        await message.reply(text, parse_mode="HTML")
        print(f"✅ 状态查询完成")
    except Exception as e:
        print(f"❌ 查询状态失败: {e}")
        await message.reply(f"❌ 查询失败: {e}")

# ==================== 群组消息监控 ====================
@router.message(F.chat.id.in_(GROUP_IDS))
async def handle_group_message(message: Message):
    """处理群组消息"""
    if not message.from_user:
        return
    
    print(f"📨 群组消息: {message.chat.id} - {message.from_user.id}")
    
    # 这里可以添加你的监控逻辑
    # 暂时空着

# ==================== 清理任务 ====================
async def cleanup_task():
    """清理过期举报"""
    print("🧹 清理任务已启动")
    while True:
        try:
            await asyncio.sleep(get_config("cleanup_check_interval", 600))
            
            expiry_time = get_config("report_expiry_time", 3600)
            expired = await report_manager.get_expired_reports(expiry_time)
            
            if expired:
                await report_manager.cleanup_expired(expiry_time)
                print(f"🧹 清理了 {len(expired)} 条过期举报")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"❌ 清理任务异常: {e}")
            await asyncio.sleep(60)

# ==================== 启动 ====================
async def main():
    print("="*60)
    print("🚀 Telegram 机器人启动中...")
    print("="*60)
    print()
    
    # 初始化日志
    setup_logging(get_config("log_level", "INFO"))
    logger.info("日志系统已初始化")
    
    # 加载数据
    print("📂 加载数据...")
    await load_all_data()
    print("✅ 数据加载完成")
    
    # 初始化配置
    config_manager._load_config()
    
    print()
    print("="*60)
    print(f"✅ 配置信息:")
    print(f"   监控群组: {len(GROUP_IDS)} 个 → {GROUP_IDS}")
    print(f"   管理员: {len(ADMIN_IDS)} 个 → {ADMIN_IDS}")
    print(f"   关键词: {await keyword_manager.get_count()} 个")
    print(f"   举报: {await report_manager.get_count()} 条")
    print()
    print("="*60)
    print("📡 开始轮询...")
    print("="*60)
    print()
    print("🔧 <b>测试命令</b>:")
    print("   /test   - 测试机器人是否工作")
    print("   /status - 查看机器人状态")
    print("   /admin  - 打开管理面板（需要在私聊，需要是管理员）")
    print()
    
    # 启动清理任务
    cleanup = asyncio.create_task(cleanup_task())
    
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            skip_updates=False
        )
    finally:
        print("\n关闭中...")
        await save_all_data()
        cleanup.cancel()
        print("✅ 已安全关闭")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✅ 机器人已停止")
    except Exception as e:
        print(f"\n❌ 致命错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
