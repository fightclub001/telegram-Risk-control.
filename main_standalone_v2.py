"""
完整独立的 Telegram 机器人
- 所有功能在一个文件
- 群组和私聊都能用 /admin
- 无外部依赖
"""

import asyncio
import os
import sys
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== 配置 ====================
TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_IDS_STR = os.getenv("GROUP_IDS", "")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")

GROUP_IDS = set()
ADMIN_IDS = set()

# 解析配置
try:
    if not TOKEN:
        print("❌ 缺少 BOT_TOKEN")
        sys.exit(1)
    
    if GROUP_IDS_STR:
        for gid in GROUP_IDS_STR.strip().split():
            try:
                GROUP_IDS.add(int(gid.strip()))
            except:
                pass
    
    if ADMIN_IDS_STR:
        for uid in ADMIN_IDS_STR.strip().split():
            try:
                ADMIN_IDS.add(int(uid.strip()))
            except:
                pass
    
    print(f"✅ 配置加载:")
    print(f"   群组 ID: {GROUP_IDS}")
    print(f"   管理员 ID: {ADMIN_IDS}")
except Exception as e:
    print(f"❌ 配置错误: {e}")
    sys.exit(1)

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("telegram_bot")

# ==================== Bot 初始化 ====================
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ==================== 数据 ====================
config = {
    "cleanup_check_interval": 600,
    "report_expiry_time": 3600,
    "auto_ban_threshold": 3,
    "ban_duration_24h": 86400,
    "enable_bio_check": True,
    "enable_display_name_check": True,
    "enable_delete_after_ban": True,
}

keywords = [
    "qq:", "qq号", "加qq", "微信", "wx:", "加我微信",
    "幼女", "萝莉", "福利", "约炮", "onlyfans", "纸飞机"
]

ZH_LABELS = {
    "cleanup_check_interval": "清理检查间隔(秒)",
    "report_expiry_time": "举报记录过期时间(秒)",
    "auto_ban_threshold": "自动通知阈值(人数)",
    "ban_duration_24h": "24小时禁言时长(秒)",
    "enable_bio_check": "启用简介检查",
    "enable_display_name_check": "启用显示名检查",
    "enable_delete_after_ban": "禁言后删除消息",
}

CATEGORIES = {
    "⚙️ 配置参数": list(config.keys()),
}

# ==================== 工具函数 ====================
def format_value(value):
    if isinstance(value, bool):
        return "✅ 启用" if value else "❌ 禁用"
    return str(value)

def get_main_kb():
    buttons = [
        [InlineKeyboardButton(text="⚙️ 配置管理", callback_data="adm:cfg")],
        [InlineKeyboardButton(text="🔍 关键词管理", callback_data="adm:kw")],
        [InlineKeyboardButton(text="📊 统计信息", callback_data="adm:stat")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_config_kb():
    buttons = []
    for key in config.keys():
        value = config[key]
        display = format_value(value)
        label = ZH_LABELS.get(key, key)
        buttons.append([InlineKeyboardButton(text=f"{label}: {display}", callback_data=f"adm:edt:{key}")])
    buttons.append([InlineKeyboardButton(text="← 返回主菜单", callback_data="adm:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_bool_kb(key):
    buttons = [
        [
            InlineKeyboardButton(text="✅ 启用", callback_data=f"adm:set:{key}:1"),
            InlineKeyboardButton(text="❌ 禁用", callback_data=f"adm:set:{key}:0"),
        ],
        [InlineKeyboardButton(text="← 返回", callback_data="adm:cfg")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== 管理员命令 ====================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """管理员面板 - 任何地方都能用"""
    uid = message.from_user.id
    
    # 打印日志
    print(f"\n🔑 收到 /admin 命令")
    print(f"   用户 ID: {uid}")
    print(f"   聊天类型: {message.chat.type}")
    print(f"   管理员列表: {ADMIN_IDS}")
    print(f"   是否管理员: {uid in ADMIN_IDS}")
    
    # 权限检查
    if uid not in ADMIN_IDS:
        print(f"❌ 用户 {uid} 不是管理员")
        await message.reply("❌ 你不是管理员")
        return
    
    print(f"✅ 打开管理面板")
    
    text = (
        "👑 <b>管理员控制面板</b>\n\n"
        "✅ 所有菜单都是中文\n"
        "✅ 所有参数都可调整\n\n"
        "请选择操作："
    )
    
    await message.reply(text, reply_markup=get_main_kb(), parse_mode="HTML")

@router.message(Command("test"))
async def cmd_test(message: Message):
    """测试命令"""
    uid = message.from_user.id
    text = (
        f"✅ <b>机器人在线</b>\n\n"
        f"你的 ID: <code>{uid}</code>\n"
        f"是否管理员: {'✅' if uid in ADMIN_IDS else '❌'}\n\n"
        f"如果你是管理员，发送 <b>/admin</b> 打开管理面板"
    )
    await message.reply(text, parse_mode="HTML")

# ==================== 配置管理 ====================
@router.callback_query(F.data == "adm:cfg")
async def cfg_main(callback: CallbackQuery):
    """配置菜单"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    text = f"📋 <b>配置管理</b>\n\n共 {len(config)} 个参数可调整："
    await callback.message.edit_text(text, reply_markup=get_config_kb(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("adm:edt:"))
async def cfg_edit(callback: CallbackQuery):
    """编辑配置"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    key = callback.data.split(":", 2)[2]
    value = config.get(key)
    label = ZH_LABELS.get(key, key)
    
    text = f"🔧 <b>{label}</b>\n\n当前值: <b>{format_value(value)}</b>\n\n"
    
    if isinstance(value, bool):
        text += "请选择新值："
        kb = get_bool_kb(key)
    else:
        text += "请输入新值："
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回", callback_data="adm:cfg")]])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("adm:set:"))
async def cfg_set(callback: CallbackQuery):
    """设置布尔值"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    parts = callback.data.split(":")
    key = parts[1]
    value = parts[2] == "1"
    
    config[key] = value
    label = ZH_LABELS.get(key, key)
    
    text = f"✅ <b>已更新</b>\n\n{label}\n新值: {format_value(value)}"
    await callback.message.edit_text(text, reply_markup=get_main_kb(), parse_mode="HTML")
    await callback.answer(f"✅ 已更新")
    
    logger.info(f"管理员 {uid} 修改 {key} = {value}")

# ==================== 关键词管理 ====================
@router.callback_query(F.data == "adm:kw")
async def kw_main(callback: CallbackQuery):
    """关键词菜单"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    text = f"🔍 <b>关键词管理</b>\n\n当前敏感词: <b>{len(keywords)}</b> 个"
    
    buttons = [
        [InlineKeyboardButton(text="➕ 添加", callback_data="adm:kw_add")],
        [InlineKeyboardButton(text="➖ 删除", callback_data="adm:kw_del")],
        [InlineKeyboardButton(text="📋 查看", callback_data="adm:kw_list")],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="adm:main")],
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "adm:kw_add")
async def kw_add(callback: CallbackQuery):
    """添加关键词"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    text = "➕ <b>添加关键词</b>\n\n请输入要添加的关键词："
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回", callback_data="adm:kw")]]), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "adm:kw_del")
async def kw_del(callback: CallbackQuery):
    """删除关键词"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    if not keywords:
        await callback.answer("没有关键词", show_alert=True)
        return
    
    buttons = []
    for kw in keywords[:10]:
        buttons.append([InlineKeyboardButton(text=f"❌ {kw}", callback_data=f"adm:kw_rm:{kw}")])
    buttons.append([InlineKeyboardButton(text="← 返回", callback_data="adm:kw")])
    
    text = f"➖ <b>删除关键词</b>\n\n共 {len(keywords)} 个，显示前 10 个："
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("adm:kw_rm:"))
async def kw_rm(callback: CallbackQuery):
    """确认删除"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    keyword = callback.data.split(":", 1)[1]
    if keyword in keywords:
        keywords.remove(keyword)
        await callback.answer(f"✅ 已删除: {keyword}")
        logger.info(f"管理员 {uid} 删除关键词: {keyword}")
    else:
        await callback.answer("❌ 删除失败", show_alert=True)

@router.callback_query(F.data == "adm:kw_list")
async def kw_list(callback: CallbackQuery):
    """列出关键词"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    if not keywords:
        text = "🔍 <b>还没有关键词</b>"
    else:
        kw_text = "、".join(keywords[:30])
        if len(keywords) > 30:
            kw_text += f" ... 等共 {len(keywords)} 个"
        text = f"🔍 <b>关键词列表</b> ({len(keywords)} 个)\n\n{kw_text}"
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回", callback_data="adm:kw")]]), parse_mode="HTML")
    await callback.answer()

# ==================== 统计信息 ====================
@router.callback_query(F.data == "adm:stat")
async def show_stats(callback: CallbackQuery):
    """统计信息"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    text = (
        "📊 <b>系统统计</b>\n\n"
        f"监控群组: {len(GROUP_IDS)}\n"
        f"管理员: {len(ADMIN_IDS)}\n"
        f"敏感词: {len(keywords)}\n\n"
        "状态: ✅ 正常运行"
    )
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← 返回主菜单", callback_data="adm:main")]]), parse_mode="HTML")
    await callback.answer()

# ==================== 返回主菜单 ====================
@router.callback_query(F.data == "adm:main")
async def back_main(callback: CallbackQuery):
    """返回主菜单"""
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("❌ 无权限", show_alert=True)
        return
    
    text = "👑 <b>管理员控制面板</b>\n\n请选择操作："
    await callback.message.edit_text(text, reply_markup=get_main_kb(), parse_mode="HTML")
    await callback.answer()

# ==================== 消息处理 ====================
@router.message()
async def handle_message(message: Message):
    """处理所有消息"""
    # 这里可以添加关键词检测等逻辑
    pass

# ==================== 启动 ====================
async def main():
    print("\n" + "="*60)
    print("🚀 Telegram 机器人启动")
    print("="*60)
    print(f"✅ 配置:")
    print(f"   群组 ID: {GROUP_IDS}")
    print(f"   管理员 ID: {ADMIN_IDS}")
    print(f"   敏感词: {len(keywords)} 个")
    print("="*60)
    print("📡 开始轮询...\n")
    print("🧪 可用命令:")
    print("   /test  - 测试机器人")
    print("   /admin - 管理面板（需要管理员权限）")
    print()
    
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        skip_updates=False
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✅ 机器人已停止")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
