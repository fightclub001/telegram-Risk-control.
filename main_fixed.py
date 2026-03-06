"""
生产级 Telegram 群组监控机器人
群组内容监控、举报系统、管理员控制面板
保留原始代码的所有中文菜单和功能
"""

import asyncio
import json
import os
import time
import hashlib
from collections import deque, defaultdict
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, ReplyKeyboardRemove, ChatMemberUpdated
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== 配置 ====================
GROUP_IDS = set()
ADMIN_IDS = set()

try:
    for gid in os.getenv("GROUP_IDS", "").strip().split():
        if gid.strip(): GROUP_IDS.add(int(gid.strip()))
    for uid in os.getenv("ADMIN_IDS", "").strip().split():
        if uid.strip(): ADMIN_IDS.add(int(uid.strip()))
    if not GROUP_IDS or not ADMIN_IDS:
        raise ValueError("GROUP_IDS 或 ADMIN_IDS 为空")
except Exception as e:
    raise ValueError(f"❌ 环境变量错误: {e}")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ 请设置 BOT_TOKEN")

print(f"✅ 配置加载成功：{len(GROUP_IDS)} 个群组，{len(ADMIN_IDS)} 个管理员")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

DATA_FILE = "/data/reports.json"
BIO_KEYWORDS_FILE = "/data/bio_keywords.json"
BLACKLIST_CONFIG_FILE = "/data/blacklist_config.json"
reports = {}
lock = asyncio.Lock()

exempt_users = {}
blacklist_config = {}

# ==================== 可配置参数 ====================
CONFIG = {
    "cleanup_check_interval": 600,
    "report_expiry_time": 3600,
    "auto_ban_threshold": 3,
    "ban_duration_24h": 86400,
    "enable_bio_check": True,
    "enable_display_name_check": True,
    "enable_delete_after_ban": True,
    "delete_warning_timeout": 10,
}

async def load_blacklist_config():
    global blacklist_config
    try:
        os.makedirs(os.path.dirname(BLACKLIST_CONFIG_FILE), exist_ok=True)
        if os.path.exists(BLACKLIST_CONFIG_FILE):
            with open(BLACKLIST_CONFIG_FILE, "r", encoding="utf-8") as f:
                blacklist_config = json.load(f)
        else:
            blacklist_config = {}
    except Exception as e:
        print("加载黑名单配置失败:", e)

async def save_blacklist_config():
    try:
        os.makedirs(os.path.dirname(BLACKLIST_CONFIG_FILE), exist_ok=True)
        with open(BLACKLIST_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(blacklist_config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存黑名单配置失败:", e)

async def load_bio_keywords():
    try:
        os.makedirs(os.path.dirname(BIO_KEYWORDS_FILE), exist_ok=True)
        if os.path.exists(BIO_KEYWORDS_FILE):
            with open(BIO_KEYWORDS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        default = ["qq:", "qq：", "qq号", "加qq", "扣扣", "微信", "wx:", "weixin", "加我微信", "wxid_", "幼女", "萝莉", "少妇", "人妻", "福利", "约炮", "onlyfans", "小红书", "抖音", "纸飞机", "机场", "http", "https", "t.me/", "@"]
        with open(BIO_KEYWORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    except Exception as e:
        print("加载 bio 关键词失败:", e)
        return ["qq:", "微信", "幼女", "福利", "t.me/"]

async def save_data():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        safe_reports = {}
        async with lock:
            for k, v in reports.items():
                safe_reports[str(k)] = {
                    "warning_id": v["warning_id"],
                    "suspect_id": v["suspect_id"],
                    "chat_id": v["chat_id"],
                    "reporters": list(v["reporters"]),
                    "original_text": v["original_text"],
                    "original_message_id": v.get("original_message_id"),
                    "timestamp": v.get("timestamp", time.time())
                }
        
        temp_file = f"{DATA_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(safe_reports, f, ensure_ascii=False, indent=2)
        
        if os.path.exists(DATA_FILE):
            os.replace(temp_file, DATA_FILE)
        else:
            os.rename(temp_file, DATA_FILE)
    except Exception as e:
        print(f"保存数据失败: {e}")

async def load_data():
    global reports
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                reports = {}
                for k, v in data.items():
                    try:
                        message_id = int(k)
                        reports[message_id] = {
                            "warning_id": v["warning_id"],
                            "suspect_id": v["suspect_id"],
                            "chat_id": v["chat_id"],
                            "reporters": set(v.get("reporters", [])),
                            "original_text": v.get("original_text", ""),
                            "original_message_id": v.get("original_message_id"),
                            "timestamp": v.get("timestamp", time.time())
                        }
                    except (ValueError, KeyError) as e:
                        print(f"跳过无效的报告 {k}: {e}")
        print(f"✅ 已加载 {len(reports)} 条举报记录")
    except Exception as e:
        print(f"加载数据失败: {e}")
        reports = {}

BIO_KEYWORDS = []

DISPLAY_NAME_KEYWORDS = [
    "加v", "加微信", "加qq", "加扣", "福利加", "约", "约炮", "资源私聊", "私我", "私聊我",
    "飞机", "纸飞机", "福利", "外围", "反差", "嫩模", "学生妹", "空姐", "人妻", "熟女",
    "onlyfans", "of", "leak", "nudes", "十八+", "av"
]

async def load_all():
    global BIO_KEYWORDS
    BIO_KEYWORDS = await load_bio_keywords()
    print(f"bio 关键词加载完成: {len(BIO_KEYWORDS)} 个")
    print(f"显示名称关键词: {len(DISPLAY_NAME_KEYWORDS)} 个")
    await load_blacklist_config()
    await load_data()

# ==================== FSM 状态 ====================
class AdminStates(StatesGroup):
    ChoosingGroup = State()
    InGroupMenu = State()
    AddingBioKw = State()
    DeletingBioKw = State()
    AddingDispKw = State()
    DeletingDispKw = State()
    SettingBlacklist = State()

# ==================== 键盘生成器 ====================
def get_group_selection_keyboard():
    buttons = []
    row = []
    for gid in sorted(GROUP_IDS):
        row.append(InlineKeyboardButton(text=f"群 {gid}", callback_data=f"group:{gid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="← 返回主菜单", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_group_menu_keyboard(group_id: int):
    buttons = [
        [InlineKeyboardButton(text="➕ 添加简介敏感词", callback_data=f"add_bio:{group_id}")],
        [InlineKeyboardButton(text="➖ 删除简介敏感词", callback_data=f"del_bio:{group_id}")],
        [InlineKeyboardButton(text="📋 查看简介敏感词", callback_data=f"list_bio:{group_id}")],
        [InlineKeyboardButton(text="➕ 添加显示名敏感词", callback_data=f"add_disp:{group_id}")],
        [InlineKeyboardButton(text="➖ 删除显示名敏感词", callback_data=f"del_disp:{group_id}")],
        [InlineKeyboardButton(text="📋 查看显示名敏感词", callback_data=f"list_disp:{group_id}")],
        [InlineKeyboardButton(text="🚫 退群自动拉黑", callback_data=f"blacklist:{group_id}")],
        [InlineKeyboardButton(text="← 返回主菜单", callback_data="back_to_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_blacklist_duration_keyboard(group_id: int):
    buttons = [
        [InlineKeyboardButton(text="开启 - 1小时", callback_data=f"blacklist_set:{group_id}:3600")],
        [InlineKeyboardButton(text="开启 - 1周", callback_data=f"blacklist_set:{group_id}:604800")],
        [InlineKeyboardButton(text="开启 - 永久", callback_data=f"blacklist_set:{group_id}:0")],
        [InlineKeyboardButton(text="关闭自动拉黑", callback_data=f"blacklist_set:{group_id}:off")],
        [InlineKeyboardButton(text="← 返回子菜单", callback_data=f"group:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== 管理员命令 ====================
@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message, state: FSMContext):
    """管理员命令 - 显示主菜单"""
    try:
        print(f"✅ 管理员 {message.from_user.id} 打开管理面板")
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙️ 管理群组设置", callback_data="select_group")
        ]])
        await message.reply("👑 <b>管理员控制面板</b>\n\n请选择操作：", reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        print(f"❌ 打开管理面板失败: {e}")
        await message.reply(f"操作失败：{str(e)}")

@router.callback_query(F.data == "select_group", F.from_user.id.in_(ADMIN_IDS))
async def select_group(callback: CallbackQuery, state: FSMContext):
    """选择群组"""
    try:
        if not GROUP_IDS:
            await callback.answer("无监控群组", show_alert=True)
            return
        kb = get_group_selection_keyboard()
        await callback.message.edit_text("请选择群组：", reply_markup=kb)
        await state.set_state(AdminStates.ChoosingGroup)
        await callback.answer()
    except Exception as e:
        print(f"❌ 选择群组失败: {e}")
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("group:"), F.from_user.id.in_(ADMIN_IDS))
async def enter_group_menu(callback: CallbackQuery, state: FSMContext):
    """进入群组菜单"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        if group_id not in GROUP_IDS:
            await callback.answer("无效群组", show_alert=True)
            return
        await state.update_data(group_id=group_id)
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(f"管理群 {group_id}：", reply_markup=kb)
        await state.set_state(AdminStates.InGroupMenu)
        await callback.answer()
        print(f"✅ 进入群组 {group_id} 的管理菜单")
    except Exception as e:
        print(f"❌ 进入群组菜单失败: {e}")
        await callback.answer(f"失败：{str(e)}", show_alert=True)

# ==================== 简介关键词处理 ====================
@router.callback_query(F.data.startswith("add_bio:"), F.from_user.id.in_(ADMIN_IDS))
async def add_bio_keyword(callback: CallbackQuery, state: FSMContext):
    """添加简介关键词"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(group_id=group_id)
        await callback.message.edit_text("请输入要添加的简介敏感词：")
        await state.set_state(AdminStates.AddingBioKw)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.message(AdminStates.AddingBioKw, F.from_user.id.in_(ADMIN_IDS))
async def process_add_bio_keyword(message: Message, state: FSMContext):
    """处理添加的关键词"""
    try:
        keyword = message.text.strip().lower()
        if not keyword or len(keyword) > 100:
            await message.reply("❌ 关键词长度必须在 1-100 个字符之间")
            return
        
        if keyword not in BIO_KEYWORDS:
            BIO_KEYWORDS.append(keyword)
            # 保存到文件
            os.makedirs(os.path.dirname(BIO_KEYWORDS_FILE), exist_ok=True)
            with open(BIO_KEYWORDS_FILE, "w", encoding="utf-8") as f:
                json.dump(BIO_KEYWORDS, f, ensure_ascii=False, indent=2)
            await message.reply(f"✅ 已添加关键词：<b>{keyword}</b>", parse_mode="HTML")
            print(f"✅ 添加简介关键词：{keyword}")
        else:
            await message.reply(f"⚠️ 关键词 '{keyword}' 已存在")
        
        data = await state.get_data()
        group_id = data.get("group_id")
        kb = get_group_menu_keyboard(group_id)
        await message.reply("回到群组菜单", reply_markup=kb)
        await state.set_state(AdminStates.InGroupMenu)
    except Exception as e:
        print(f"❌ 处理关键词失败: {e}")
        await message.reply(f"操作失败：{str(e)}")

@router.callback_query(F.data.startswith("del_bio:"), F.from_user.id.in_(ADMIN_IDS))
async def del_bio_keyword(callback: CallbackQuery, state: FSMContext):
    """删除简介关键词"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        if not BIO_KEYWORDS:
            await callback.answer("没有可删除的关键词", show_alert=True)
            return
        
        buttons = []
        for kw in BIO_KEYWORDS[:10]:
            buttons.append([InlineKeyboardButton(text=f"❌ {kw}", callback_data=f"del_bio_confirm:{kw}")])
        buttons.append([InlineKeyboardButton(text="← 返回", callback_data=f"group:{group_id}")])
        
        await callback.message.edit_text("选择要删除的关键词：", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("del_bio_confirm:"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_del_bio_keyword(callback: CallbackQuery):
    """确认删除关键词"""
    try:
        keyword = callback.data.split(":", 1)[1]
        if keyword in BIO_KEYWORDS:
            BIO_KEYWORDS.remove(keyword)
            os.makedirs(os.path.dirname(BIO_KEYWORDS_FILE), exist_ok=True)
            with open(BIO_KEYWORDS_FILE, "w", encoding="utf-8") as f:
                json.dump(BIO_KEYWORDS, f, ensure_ascii=False, indent=2)
            await callback.answer(f"✅ 已删除关键词：{keyword}")
            print(f"✅ 删除简介关键词：{keyword}")
        else:
            await callback.answer("关键词不存在", show_alert=True)
    except Exception as e:
        print(f"❌ 删除关键词失败: {e}")
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("list_bio:"), F.from_user.id.in_(ADMIN_IDS))
async def list_bio_keywords(callback: CallbackQuery):
    """列出所有简介关键词"""
    try:
        if not BIO_KEYWORDS:
            text = "🔍 当前没有简介敏感词"
        else:
            keyword_text = "、".join(BIO_KEYWORDS[:50])
            if len(BIO_KEYWORDS) > 50:
                keyword_text += f" ... 等共 {len(BIO_KEYWORDS)} 个"
            text = f"🔍 <b>简介敏感词列表</b>（共 {len(BIO_KEYWORDS)} 个）\n\n{keyword_text}"
        
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

# ==================== 显示名关键词处理 ====================
@router.callback_query(F.data.startswith("add_disp:"), F.from_user.id.in_(ADMIN_IDS))
async def add_disp_keyword(callback: CallbackQuery, state: FSMContext):
    """添加显示名关键词"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(group_id=group_id)
        await callback.message.edit_text("请输入要添加的显示名敏感词：")
        await state.set_state(AdminStates.AddingDispKw)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.message(AdminStates.AddingDispKw, F.from_user.id.in_(ADMIN_IDS))
async def process_add_disp_keyword(message: Message, state: FSMContext):
    """处理添加的显示名关键词"""
    try:
        keyword = message.text.strip().lower()
        if not keyword or len(keyword) > 100:
            await message.reply("❌ 关键词长度必须在 1-100 个字符之间")
            return
        
        if keyword not in DISPLAY_NAME_KEYWORDS:
            DISPLAY_NAME_KEYWORDS.append(keyword)
            await message.reply(f"✅ 已添加显示名关键词：<b>{keyword}</b>", parse_mode="HTML")
            print(f"✅ 添加显示名关键词：{keyword}")
        else:
            await message.reply(f"⚠️ 关键词 '{keyword}' 已存在")
        
        data = await state.get_data()
        group_id = data.get("group_id")
        kb = get_group_menu_keyboard(group_id)
        await message.reply("回到群组菜单", reply_markup=kb)
        await state.set_state(AdminStates.InGroupMenu)
    except Exception as e:
        print(f"❌ 处理关键词失败: {e}")
        await message.reply(f"操作失败：{str(e)}")

@router.callback_query(F.data.startswith("del_disp:"), F.from_user.id.in_(ADMIN_IDS))
async def del_disp_keyword(callback: CallbackQuery, state: FSMContext):
    """删除显示名关键词"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        if not DISPLAY_NAME_KEYWORDS:
            await callback.answer("没有可删除的关键词", show_alert=True)
            return
        
        buttons = []
        for kw in DISPLAY_NAME_KEYWORDS[:10]:
            buttons.append([InlineKeyboardButton(text=f"❌ {kw}", callback_data=f"del_disp_confirm:{kw}")])
        buttons.append([InlineKeyboardButton(text="← 返回", callback_data=f"group:{group_id}")])
        
        await callback.message.edit_text("选择要删除的关键词：", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("del_disp_confirm:"), F.from_user.id.in_(ADMIN_IDS))
async def confirm_del_disp_keyword(callback: CallbackQuery):
    """确认删除显示名关键词"""
    try:
        keyword = callback.data.split(":", 1)[1]
        if keyword in DISPLAY_NAME_KEYWORDS:
            DISPLAY_NAME_KEYWORDS.remove(keyword)
            await callback.answer(f"✅ 已删除关键词：{keyword}")
            print(f"✅ 删除显示名关键词：{keyword}")
        else:
            await callback.answer("关键词不存在", show_alert=True)
    except Exception as e:
        print(f"❌ 删除关键词失败: {e}")
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("list_disp:"), F.from_user.id.in_(ADMIN_IDS))
async def list_disp_keywords(callback: CallbackQuery):
    """列出所有显示名关键词"""
    try:
        if not DISPLAY_NAME_KEYWORDS:
            text = "🔍 当前没有显示名敏感词"
        else:
            keyword_text = "、".join(DISPLAY_NAME_KEYWORDS[:50])
            if len(DISPLAY_NAME_KEYWORDS) > 50:
                keyword_text += f" ... 等共 {len(DISPLAY_NAME_KEYWORDS)} 个"
            text = f"🔍 <b>显示名敏感词列表</b>（共 {len(DISPLAY_NAME_KEYWORDS)} 个）\n\n{keyword_text}"
        
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

# ==================== 黑名单处理 ====================
@router.callback_query(F.data.startswith("blacklist:"), F.from_user.id.in_(ADMIN_IDS))
async def set_blacklist(callback: CallbackQuery):
    """设置黑名单"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        kb = get_blacklist_duration_keyboard(group_id)
        await callback.message.edit_text("选择黑名单时长：", reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("blacklist_set:"), F.from_user.id.in_(ADMIN_IDS))
async def set_blacklist_duration(callback: CallbackQuery):
    """设置黑名单时长"""
    try:
        parts = callback.data.split(":")
        group_id = parts[1]
        duration = parts[2]
        
        if duration == "off":
            blacklist_config.pop(group_id, None)
            await callback.answer("✅ 已关闭自动拉黑")
        else:
            blacklist_config[group_id] = {
                "enabled": True,
                "duration": int(duration)
            }
            await callback.answer(f"✅ 已设置黑名单")
        
        await save_blacklist_config()
        print(f"✅ 群 {group_id} 黑名单配置已更新")
    except Exception as e:
        print(f"❌ 设置黑名单失败: {e}")
        await callback.answer(f"失败：{str(e)}", show_alert=True)

@router.callback_query(F.data == "back_to_main", F.from_user.id.in_(ADMIN_IDS))
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    """返回主菜单"""
    try:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚙️ 管理群组设置", callback_data="select_group")
        ]])
        await callback.message.edit_text("👑 <b>管理员控制面板</b>", reply_markup=kb, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        await callback.answer(f"失败：{str(e)}", show_alert=True)

# ==================== 群组消息监控 ====================
@router.message(F.chat.id.in_(GROUP_IDS))
async def handle_group_message(message: Message):
    """处理群组消息"""
    try:
        if not message.from_user:
            return
        
        # 检查是否是敏感用户
        is_sensitive = False
        
        if CONFIG.get("enable_display_name_check", True):
            first_name = (message.from_user.first_name or "").lower()
            last_name = (message.from_user.last_name or "").lower()
            display_name = f"{first_name} {last_name}".lower()
            
            for keyword in DISPLAY_NAME_KEYWORDS:
                if keyword in display_name:
                    is_sensitive = True
                    break
        
        if not is_sensitive and CONFIG.get("enable_bio_check", True):
            for keyword in BIO_KEYWORDS:
                if keyword in (message.from_user.username or "").lower():
                    is_sensitive = True
                    break
        
        if not is_sensitive:
            return
        
        # 发送警告
        warning_text = (
            f"⚠️ <b>检测到疑似广告引流规避行为</b>\n\n"
            f"<b>用户ID:</b> {message.from_user.id}\n"
            f"<b>昵称:</b> {message.from_user.first_name or 'N/A'}\n"
            f"<b>用户名:</b> @{message.from_user.username or 'N/A'}\n\n"
            f"<b>举报人数:</b> 0"
        )
        
        try:
            warning_message = await message.reply(
                warning_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="👮 举报此用户", callback_data=f"report:{message.message_id}")
                ]]),
                parse_mode="HTML"
            )
            
            async with lock:
                reports[message.message_id] = {
                    "warning_id": warning_message.message_id,
                    "suspect_id": message.from_user.id,
                    "chat_id": message.chat.id,
                    "reporters": set(),
                    "original_text": warning_text,
                    "original_message_id": message.message_id,
                    "timestamp": time.time()
                }
            
            await save_data()
            print(f"✅ 检测到敏感用户 {message.from_user.id} 在群 {message.chat.id}")
        except Exception as e:
            print(f"❌ 发送警告失败: {e}")
    except Exception as e:
        print(f"❌ 处理消息异常: {e}")

# ==================== 举报处理 ====================
@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    """处理举报"""
    try:
        original_id = int(callback.data.split(":", 1)[1])
        reporter_id = callback.from_user.id
        
        async with lock:
            if original_id not in reports:
                await callback.answer("该举报已过期", show_alert=True)
                return
            
            data = reports[original_id]
            if reporter_id in data["reporters"]:
                await callback.answer("您已经举报过了", show_alert=True)
                return
            
            data["reporters"].add(reporter_id)
            count = len(data["reporters"])
            suspect_id = data["suspect_id"]
            warning_id = data["warning_id"]
            chat_id = data["chat_id"]
            original_text = data.get("original_text", "⚠️ 检测到疑似广告引流规避行为")
        
        # 更新举报数
        threshold = CONFIG.get("auto_ban_threshold", 3)
        if count >= threshold:
            status = f"🚨 超 {threshold} 人举报 已通知管理员\n\n举报人数: {count}"
        else:
            status = f"🚨 已有人举报\n\n举报人数: {count}"
        
        lines = original_text.splitlines()
        prefix = "\n".join(lines[:2]) if len(lines) >= 2 else original_text
        new_text = f"{prefix}\n{status}"
        
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=warning_id,
                text=new_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔨 禁言24小时", callback_data=f"ban24h:{original_id}"),
                    InlineKeyboardButton(text="🚫 永久禁言", callback_data=f"banperm:{original_id}"),
                ]])
            )
        except Exception as e:
            print(f"❌ 更新消息失败: {e}")
        
        await save_data()
        await callback.answer(f"✅ 举报成功！当前 {count} 人")
        print(f"✅ 用户 {reporter_id} 举报了用户 {suspect_id}")
    except Exception as e:
        print(f"❌ 举报处理异常: {e}")
        await callback.answer("操作失败", show_alert=True)

# ==================== 禁言处理 ====================
@router.callback_query(F.data.startswith(("ban24h:", "banperm:")))
async def handle_ban(callback: CallbackQuery):
    """处理禁言"""
    try:
        action, original_id_str = callback.data.split(":", 1)
        original_id = int(original_id_str)
        caller_id = callback.from_user.id
        
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员可操作", show_alert=True)
            return
        
        async with lock:
            if original_id not in reports:
                await callback.answer("记录已过期", show_alert=True)
                return
            
            data = reports[original_id]
            suspect_id = data["suspect_id"]
            warning_id = data["warning_id"]
            chat_id = data["chat_id"]
            original_message_id = data.get("original_message_id")
            original_text = data.get("original_text", "⚠️ 检测到疑似广告引流规避行为")
        
        # 执行禁言
        try:
            until_date = int(time.time()) + CONFIG.get("ban_duration_24h", 86400) if action == "ban24h" else None
            
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
        
        # 更新消息
        ban_type = "禁言24小时" if action == "ban24h" else "永久限制"
        lines = original_text.splitlines()
        prefix = "\n".join(lines[:2]) if len(lines) >= 2 else original_text
        new_text = f"{prefix}\n🚨 已由管理员{ban_type}"
        
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=warning_id,
                text=new_text,
                reply_markup=None
            )
        except:
            pass
        
        # 延迟删除
        if CONFIG.get("enable_delete_after_ban", True):
            async def delayed_delete():
                await asyncio.sleep(CONFIG.get("delete_warning_timeout", 10))
                try:
                    await bot.delete_message(chat_id, warning_id)
                except:
                    pass
                try:
                    if original_message_id:
                        await bot.delete_message(chat_id, original_message_id)
                except:
                    pass
            
            asyncio.create_task(delayed_delete())
        
        async with lock:
            reports.pop(original_id, None)
        await save_data()
        
        await callback.answer(f"✅ 已{ban_type}")
        print(f"✅ 管理员 {caller_id} 对用户 {suspect_id} 执行{ban_type}")
    except Exception as e:
        print(f"❌ 禁言异常: {e}")
        await callback.answer("操作失败", show_alert=True)

# ==================== 清理任务 ====================
async def cleanup_deleted_messages():
    """清理过期举报"""
    print("✅ 清理任务已启动")
    while True:
        try:
            await asyncio.sleep(CONFIG.get("cleanup_check_interval", 600))
            
            expiry_time = CONFIG.get("report_expiry_time", 3600)
            async with lock:
                now = time.time()
                to_remove = [
                    mid for mid, data in reports.items()
                    if now - data.get("timestamp", 0) > expiry_time
                ]
            
            if to_remove:
                async with lock:
                    for mid in to_remove:
                        reports.pop(mid, None)
                await save_data()
                print(f"✅ 清理了 {len(to_remove)} 条过期举报")
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
    
    await load_all()
    
    print(f"✅ 配置加载完成")
    print(f"📊 监控群组: {len(GROUP_IDS)}")
    print(f"👮 管理员数: {len(ADMIN_IDS)}")
    print(f"🔑 关键词数: {len(BIO_KEYWORDS) + len(DISPLAY_NAME_KEYWORDS)}")
    print("="*60)
    print("📡 开始轮询...")
    
    # 启动清理任务
    cleanup_task = asyncio.create_task(cleanup_deleted_messages())
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await save_data()
        cleanup_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✅ 机器人已停止")
