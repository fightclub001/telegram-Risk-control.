import asyncio
import json
import os
import time
import hashlib
from collections import deque
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, ReplyKeyboardRemove
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== 环境配置 ====================
GROUP_IDS = set()
ADMIN_IDS = set()

try:
    for gid in os.getenv("GROUP_IDS", "").strip().split():
        if gid.strip(): 
            GROUP_IDS.add(int(gid.strip()))
    for uid in os.getenv("ADMIN_IDS", "").strip().split():
        if uid.strip(): 
            ADMIN_IDS.add(int(uid.strip()))
    if not GROUP_IDS or not ADMIN_IDS:
        raise ValueError("GROUP_IDS 或 ADMIN_IDS 为空")
except Exception as e:
    raise ValueError(f"❌ 环境变量错误: {e}")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ 请设置 BOT_TOKEN")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ==================== 数据文件 ====================
os.makedirs("/data", exist_ok=True)
DATA_FILE = "/data/reports.json"
CONFIG_FILE = "/data/config.json"
USER_VIOLATIONS_FILE = "/data/user_violations.json"

reports = {}
lock = asyncio.Lock()
user_violations = {}
config = {}

# ==================== 配置函数 ====================
async def load_config():
    global config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        else:
            config = {"groups": {}}
            await save_config()
    except Exception as e:
        print(f"配置加载失败: {e}")
        config = {"groups": {}}

async def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"配置保存失败: {e}")

async def load_user_violations():
    global user_violations
    try:
        if os.path.exists(USER_VIOLATIONS_FILE):
            with open(USER_VIOLATIONS_FILE, "r", encoding="utf-8") as f:
                user_violations = json.load(f)
    except Exception as e:
        print(f"违规记录加载失败: {e}")

async def save_user_violations():
    try:
        with open(USER_VIOLATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_violations, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"违规记录保存失败: {e}")

def get_group_config(group_id: int):
    gid = str(group_id)
    if gid not in config["groups"]:
        config["groups"][gid] = {
            "enabled": True,
            "check_bio_link": True,
            "bio_keywords": ["qq:", "qq：", "qq号", "加qq", "扣扣", "微信", "wx:", "weixin", "加我微信", "wxid_", "幼女", "萝莉", "少妇", "人妻", "福利", "约炮", "onlyfans", "小红书", "抖音", "纸飞机", "机场", "t.me/", "@"],
            "check_bio_keywords": True,
            "display_keywords": ["加v", "加微信", "加qq", "加扣", "福利加", "约", "约炮", "资源私聊", "私我", "私聊我", "飞机", "纸飞机", "福利", "外围", "反差", "嫩模", "学生妹", "空姐", "人妻", "熟女", "onlyfans", "of", "leak", "nudes", "十八+", "av"],
            "check_display_keywords": True,
            "message_keywords": ["qq:", "qq号", "微信", "wx:", "幼女", "萝莉", "福利", "约炮", "onlyfans"],
            "check_message_keywords": True,
            "short_msg_detection": True,
            "short_msg_threshold": 3,
            "min_consecutive_count": 2,
            "time_window_seconds": 60,
            "fill_garbage_detection": True,
            "fill_garbage_min_raw_len": 12,
            "fill_garbage_max_clean_len": 8,
            "fill_space_ratio": 0.30,
            "violation_mute_hours": 1,
            "reported_message_threshold": 2,
            "autoreply": {
                "enabled": False,
                "keywords": [],
                "reply_text": "",
                "buttons": [],
                "delete_user_sec": 0,
                "delete_bot_sec": 0
            },
            "exempt_users": {}
        }
    return config["groups"][gid]

# ==================== FSM 状态 ====================
class AdminStates(StatesGroup):
    MainMenu = State()
    ChooseGroup = State()
    GroupMenu = State()
    EditBioKeywords = State()
    EditDisplayKeywords = State()
    EditMessageKeywords = State()
    EditAutoreplyKeywords = State()
    EditAutoreplyText = State()
    EditAutoreplyButtons = State()
    EditAutoreplyDeleteTime = State()
    EditShortMsgThreshold = State()
    EditConsecutiveCount = State()
    EditTimeWindow = State()
    EditFillGarbageMinRaw = State()
    EditFillGarbageMaxClean = State()
    EditFillSpaceRatio = State()
    EditMuteHours = State()
    EditReportedThreshold = State()

# ==================== UI 键盘 ====================
def get_main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton(text="⚙️ 群组管理", callback_data="choose_group")],
        [InlineKeyboardButton(text="📊 状态查看", callback_data="view_status")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_group_list_keyboard():
    buttons = []
    for gid in sorted(GROUP_IDS):
        buttons.append([InlineKeyboardButton(text=f"👥 {gid}", callback_data=f"select_group:{gid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ 返回", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_group_menu_keyboard(group_id: int):
    buttons = [
        [InlineKeyboardButton(text="🔍 简介检测", callback_data=f"submenu_bio:{group_id}")],
        [InlineKeyboardButton(text="👤 名称检测", callback_data=f"submenu_display:{group_id}")],
        [InlineKeyboardButton(text="💬 消息检测", callback_data=f"submenu_message:{group_id}")],
        [InlineKeyboardButton(text="⏱️ 短消息/垃圾", callback_data=f"submenu_short:{group_id}")],
        [InlineKeyboardButton(text="⚠️ 违规处理", callback_data=f"submenu_violation:{group_id}")],
        [InlineKeyboardButton(text="🤖 自动回复", callback_data=f"submenu_autoreply:{group_id}")],
        [InlineKeyboardButton(text="🎛️ 基础设置", callback_data=f"submenu_basic:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data="back_choose_group")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_bio_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    link_status = "✅" if cfg.get("check_bio_link") else "❌"
    kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"链接 {link_status}", callback_data=f"toggle_bio_link:{group_id}")],
        [InlineKeyboardButton(text=f"敏感词 {kw_status}", callback_data=f"toggle_bio_keywords:{group_id}")],
        [InlineKeyboardButton(text="📋 编辑词汇", callback_data=f"edit_bio_kw:{group_id}")],
        [InlineKeyboardButton(text="👀 查看", callback_data=f"view_bio_kw:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_display_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    status = "✅" if cfg.get("check_display_keywords") else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"启用 {status}", callback_data=f"toggle_display:{group_id}")],
        [InlineKeyboardButton(text="📋 编辑词汇", callback_data=f"edit_display_kw:{group_id}")],
        [InlineKeyboardButton(text="👀 查看", callback_data=f"view_display_kw:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_message_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    status = "✅" if cfg.get("check_message_keywords") else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"启用 {status}", callback_data=f"toggle_message:{group_id}")],
        [InlineKeyboardButton(text="📋 编辑词汇", callback_data=f"edit_message_kw:{group_id}")],
        [InlineKeyboardButton(text="👀 查看", callback_data=f"view_message_kw:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_short_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    short_enabled = "✅" if cfg.get("short_msg_detection") else "❌"
    fill_enabled = "✅" if cfg.get("fill_garbage_detection") else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"短消息 {short_enabled}", callback_data=f"toggle_short:{group_id}")],
        [InlineKeyboardButton(text=f"字数: {cfg.get('short_msg_threshold')}", callback_data=f"edit_threshold:{group_id}")],
        [InlineKeyboardButton(text=f"连续: {cfg.get('min_consecutive_count')}", callback_data=f"edit_consecutive:{group_id}")],
        [InlineKeyboardButton(text=f"窗口: {cfg.get('time_window_seconds')}s", callback_data=f"edit_window:{group_id}")],
        [InlineKeyboardButton(text=f"垃圾 {fill_enabled}", callback_data=f"toggle_fill:{group_id}")],
        [InlineKeyboardButton(text=f"最小: {cfg.get('fill_garbage_min_raw_len')}", callback_data=f"edit_fill_min:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_violation_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    buttons = [
        [InlineKeyboardButton(text=f"禁言: {cfg.get('violation_mute_hours')}h", callback_data=f"edit_mute:{group_id}")],
        [InlineKeyboardButton(text=f"阈值: {cfg.get('reported_message_threshold')}", callback_data=f"edit_report_threshold:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_autoreply_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    ar = cfg.get("autoreply", {})
    enabled = "✅" if ar.get("enabled") else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"启用 {enabled}", callback_data=f"toggle_ar:{group_id}")],
        [InlineKeyboardButton(text="🔑 关键词", callback_data=f"edit_ar_kw:{group_id}")],
        [InlineKeyboardButton(text="📝 文本", callback_data=f"edit_ar_text:{group_id}")],
        [InlineKeyboardButton(text="🔘 按钮", callback_data=f"edit_ar_btn:{group_id}")],
        [InlineKeyboardButton(text="⏱️ 延时", callback_data=f"edit_ar_del:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_basic_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    enabled = "✅" if cfg.get("enabled") else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"状态: {enabled}", callback_data=f"toggle_group:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== 管理员命令 ====================
@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_panel(message: Message, state: FSMContext):
    text = "👮 管理员面板"
    kb = get_main_menu_keyboard()
    await message.reply(text, reply_markup=kb)
    await state.set_state(AdminStates.MainMenu)

# ==================== 回调处理 ====================
@router.callback_query(F.data == "choose_group", F.from_user.id.in_(ADMIN_IDS))
async def choose_group_callback(callback: CallbackQuery, state: FSMContext):
    text = "选择群组"
    kb = get_group_list_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.ChooseGroup)
    await callback.answer()

@router.callback_query(F.data.startswith("select_group:"), F.from_user.id.in_(ADMIN_IDS))
async def select_group(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        text = f"群组 {group_id}"
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data == "back_main", F.from_user.id.in_(ADMIN_IDS))
async def back_main(callback: CallbackQuery, state: FSMContext):
    text = "👮 管理员面板"
    kb = get_main_menu_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.MainMenu)
    await callback.answer()

@router.callback_query(F.data == "back_choose_group", F.from_user.id.in_(ADMIN_IDS))
async def back_choose_group(callback: CallbackQuery, state: FSMContext):
    text = "选择群组"
    kb = get_group_list_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.ChooseGroup)
    await callback.answer()

@router.callback_query(F.data.startswith("group_menu:"), F.from_user.id.in_(ADMIN_IDS))
async def group_menu(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        text = f"群组 {group_id}"
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 简介检测 ====================
@router.callback_query(F.data.startswith("submenu_bio:"), F.from_user.id.in_(ADMIN_IDS))
async def bio_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        link_status = "✅" if cfg.get("check_bio_link") else "❌"
        kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
        text = f"简介检测\n链接: {link_status}\n敏感词: {kw_status}"
        kb = get_bio_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_bio_link:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_bio_link(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_bio_link"] = not cfg.get("check_bio_link", True)
        await save_config()
        status = "✅" if cfg["check_bio_link"] else "❌"
        await callback.answer(f"链接检测: {status}", show_alert=True)
        kb = get_bio_menu_keyboard(group_id)
        link_status = "✅" if cfg.get("check_bio_link") else "❌"
        kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
        text = f"简介检测\n链接: {link_status}\n敏感词: {kw_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_bio_keywords:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_bio_keywords(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_bio_keywords"] = not cfg.get("check_bio_keywords", True)
        await save_config()
        status = "✅" if cfg["check_bio_keywords"] else "❌"
        await callback.answer(f"敏感词检测: {status}", show_alert=True)
        kb = get_bio_menu_keyboard(group_id)
        link_status = "✅" if cfg.get("check_bio_link") else "❌"
        kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
        text = f"简介检测\n链接: {link_status}\n敏感词: {kw_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_bio_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_bio_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("bio_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"编辑简介敏感词\n\n{kw_text}\n\n发送新词汇（一行一个）或 /clear 清空"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditBioKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditBioKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_bio_keywords(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        
        if message.text.strip() == "/clear":
            cfg["bio_keywords"] = []
        else:
            cfg["bio_keywords"] = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
        
        await save_config()
        kb = get_bio_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新（{len(cfg['bio_keywords'])}个）", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("view_bio_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def view_bio_keywords(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("bio_keywords", [])
        kw_text = "\n".join(keywords) if keywords else "（无）"
        text = f"简介敏感词（{len(keywords)}个）\n\n{kw_text}"
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 显示名称检测 ====================
@router.callback_query(F.data.startswith("submenu_display:"), F.from_user.id.in_(ADMIN_IDS))
async def display_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        status = "✅" if cfg.get("check_display_keywords") else "❌"
        text = f"名称检测: {status}"
        kb = get_display_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_display:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_display(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_display_keywords"] = not cfg.get("check_display_keywords", True)
        await save_config()
        status = "✅" if cfg["check_display_keywords"] else "❌"
        await callback.answer(f"名称检测: {status}", show_alert=True)
        kb = get_display_menu_keyboard(group_id)
        status_display = "✅" if cfg.get("check_display_keywords") else "❌"
        text = f"名称检测: {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_display_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_display_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("display_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"编辑名称敏感词\n\n{kw_text}\n\n发送新词汇（一行一个）或 /clear 清空"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditDisplayKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditDisplayKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_display_keywords(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        
        if message.text.strip() == "/clear":
            cfg["display_keywords"] = []
        else:
            cfg["display_keywords"] = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
        
        await save_config()
        kb = get_display_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新（{len(cfg['display_keywords'])}个）", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("view_display_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def view_display_keywords(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("display_keywords", [])
        kw_text = "\n".join(keywords) if keywords else "（无）"
        text = f"名称敏感词（{len(keywords)}个）\n\n{kw_text}"
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 消息检测 ====================
@router.callback_query(F.data.startswith("submenu_message:"), F.from_user.id.in_(ADMIN_IDS))
async def message_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        status = "✅" if cfg.get("check_message_keywords") else "❌"
        text = f"消息检测: {status}"
        kb = get_message_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_message:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_message(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_message_keywords"] = not cfg.get("check_message_keywords", True)
        await save_config()
        status = "✅" if cfg["check_message_keywords"] else "❌"
        await callback.answer(f"消息检测: {status}", show_alert=True)
        kb = get_message_menu_keyboard(group_id)
        status_display = "✅" if cfg.get("check_message_keywords") else "❌"
        text = f"消息检测: {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_message_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_message_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("message_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"编辑消息敏感词\n\n{kw_text}\n\n发送新词汇（一行一个）或 /clear 清空"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMessageKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMessageKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_message_keywords(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        
        if message.text.strip() == "/clear":
            cfg["message_keywords"] = []
        else:
            cfg["message_keywords"] = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
        
        await save_config()
        kb = get_message_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新（{len(cfg['message_keywords'])}个）", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("view_message_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def view_message_keywords(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("message_keywords", [])
        kw_text = "\n".join(keywords) if keywords else "（无）"
        text = f"消息敏感词（{len(keywords)}个）\n\n{kw_text}"
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 短消息和垃圾检测 ====================
@router.callback_query(F.data.startswith("submenu_short:"), F.from_user.id.in_(ADMIN_IDS))
async def short_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        short_enabled = "✅" if cfg.get("short_msg_detection") else "❌"
        fill_enabled = "✅" if cfg.get("fill_garbage_detection") else "❌"
        text = f"短消息: {short_enabled}\n垃圾: {fill_enabled}"
        kb = get_short_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_short:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_short_msg(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["short_msg_detection"] = not cfg.get("short_msg_detection", True)
        await save_config()
        status = "✅" if cfg["short_msg_detection"] else "❌"
        await callback.answer(f"短消息: {status}", show_alert=True)
        kb = get_short_menu_keyboard(group_id)
        short_enabled = "✅" if cfg.get("short_msg_detection") else "❌"
        fill_enabled = "✅" if cfg.get("fill_garbage_detection") else "❌"
        text = f"短消息: {short_enabled}\n垃圾: {fill_enabled}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_threshold:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_threshold(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("short_msg_threshold", 3)
        text = f"字数阈值（当前: {current}）："
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditShortMsgThreshold)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditShortMsgThreshold), F.from_user.id.in_(ADMIN_IDS))
async def process_threshold(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = int(message.text.strip())
        cfg["short_msg_threshold"] = value
        await save_config()
        kb = get_short_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_consecutive:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_consecutive(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("min_consecutive_count", 2)
        text = f"连续条数（当前: {current}）："
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditConsecutiveCount)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditConsecutiveCount), F.from_user.id.in_(ADMIN_IDS))
async def process_consecutive(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = int(message.text.strip())
        cfg["min_consecutive_count"] = value
        await save_config()
        kb = get_short_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_window:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_window(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("time_window_seconds", 60)
        text = f"时间窗口秒数（当前: {current}）："
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditTimeWindow)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditTimeWindow), F.from_user.id.in_(ADMIN_IDS))
async def process_window(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = int(message.text.strip())
        cfg["time_window_seconds"] = value
        await save_config()
        kb = get_short_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {value}s", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("toggle_fill:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_fill(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["fill_garbage_detection"] = not cfg.get("fill_garbage_detection", True)
        await save_config()
        status = "✅" if cfg["fill_garbage_detection"] else "❌"
        await callback.answer(f"垃圾检测: {status}", show_alert=True)
        kb = get_short_menu_keyboard(group_id)
        short_enabled = "✅" if cfg.get("short_msg_detection") else "❌"
        fill_enabled = "✅" if cfg.get("fill_garbage_detection") else "❌"
        text = f"短消息: {short_enabled}\n垃圾: {fill_enabled}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_fill_min:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_fill_min(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("fill_garbage_min_raw_len", 12)
        text = f"最小原始长度（当前: {current}）："
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditFillGarbageMinRaw)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditFillGarbageMinRaw), F.from_user.id.in_(ADMIN_IDS))
async def process_fill_min(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = int(message.text.strip())
        cfg["fill_garbage_min_raw_len"] = value
        await save_config()
        kb = get_short_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

# ==================== 违规处理 ====================
@router.callback_query(F.data.startswith("submenu_violation:"), F.from_user.id.in_(ADMIN_IDS))
async def violation_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        mute_hours = cfg.get("violation_mute_hours", 1)
        threshold = cfg.get("reported_message_threshold", 2)
        text = f"违规处理\n禁言: {mute_hours}h\n触发: {threshold}条举报"
        kb = get_violation_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_mute:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_mute_hours(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("violation_mute_hours", 1)
        text = f"禁言时长小时数（当前: {current}）："
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMuteHours)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMuteHours), F.from_user.id.in_(ADMIN_IDS))
async def process_mute_hours(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = int(message.text.strip())
        cfg["violation_mute_hours"] = value
        await save_config()
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {value}h", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_report_threshold:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_report_threshold(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("reported_message_threshold", 2)
        text = f"触发禁言的举报数（当前: {current}）："
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditReportedThreshold)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditReportedThreshold), F.from_user.id.in_(ADMIN_IDS))
async def process_report_threshold(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = int(message.text.strip())
        cfg["reported_message_threshold"] = value
        await save_config()
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

# ==================== 自动回复 ====================
@router.callback_query(F.data.startswith("submenu_autoreply:"), F.from_user.id.in_(ADMIN_IDS))
async def autoreply_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        enabled = "✅" if ar.get("enabled") else "❌"
        kw_count = len(ar.get("keywords", []))
        text = f"自动回复\n状态: {enabled}\n关键词: {kw_count}个"
        kb = get_autoreply_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_ar:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_autoreply(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        ar["enabled"] = not ar.get("enabled", False)
        await save_config()
        status = "✅" if ar["enabled"] else "❌"
        await callback.answer(f"自动回复: {status}", show_alert=True)
        kb = get_autoreply_menu_keyboard(group_id)
        enabled = "✅" if ar.get("enabled") else "❌"
        kw_count = len(ar.get("keywords", []))
        text = f"自动回复\n状态: {enabled}\n关键词: {kw_count}个"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_ar_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        keywords = ar.get("keywords", [])
        kw_text = "\n".join(keywords)
        text = f"自动回复关键词\n\n{kw_text if kw_text else '（无）'}\n\n发送新词汇（一行一个）或 /clear 清空"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditAutoreplyKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_keywords(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        
        if message.text.strip() == "/clear":
            ar["keywords"] = []
        else:
            ar["keywords"] = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
        
        await save_config()
        kb = get_autoreply_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新（{len(ar['keywords'])}个）", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_ar_text:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_text(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        current = ar.get("reply_text", "")
        text = f"自动回复文本\n\n{current if current else '（无）'}\n\n发送新文本"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditAutoreplyText)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyText), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_text(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        ar["reply_text"] = message.text.strip()
        
        await save_config()
        kb = get_autoreply_menu_keyboard(group_id)
        await message.reply("✅ 已更新", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_ar_btn:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_buttons(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        buttons = ar.get("buttons", [])
        btn_text = "\n".join(buttons)
        text = f"自动回复按钮\n\n{btn_text if btn_text else '（无）'}\n\n发送新按钮（一行一个）或 /clear 清空"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditAutoreplyButtons)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyButtons), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_buttons(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        
        if message.text.strip() == "/clear":
            ar["buttons"] = []
        else:
            ar["buttons"] = [x.strip() for x in message.text.strip().split("\n") if x.strip()]
        
        await save_config()
        kb = get_autoreply_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新（{len(ar['buttons'])}个）", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_ar_del:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_delete(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        user_sec = ar.get("delete_user_sec", 0)
        bot_sec = ar.get("delete_bot_sec", 0)
        text = f"删除延时（秒）\n用户消息: {user_sec}\n机器人消息: {bot_sec}\n\n输入两个数字（用空格隔开）"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditAutoreplyDeleteTime)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyDeleteTime), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_delete(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.reply("❌ 输入两个数字")
            return
        
        user_sec = int(parts[0])
        bot_sec = int(parts[1])
        ar["delete_user_sec"] = user_sec
        ar["delete_bot_sec"] = bot_sec
        
        await save_config()
        kb = get_autoreply_menu_keyboard(group_id)
        await message.reply(f"✅ 已设为 {user_sec}s 和 {bot_sec}s", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入两个数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

# ==================== 基础设置 ====================
@router.callback_query(F.data.startswith("submenu_basic:"), F.from_user.id.in_(ADMIN_IDS))
async def basic_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        status = "✅" if cfg.get("enabled") else "❌"
        text = f"基础设置\n群组: {group_id}\n状态: {status}"
        kb = get_basic_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_group:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_group(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["enabled"] = not cfg.get("enabled", True)
        await save_config()
        status = "✅" if cfg["enabled"] else "❌"
        await callback.answer(f"群组状态: {status}", show_alert=True)
        kb = get_basic_menu_keyboard(group_id)
        status_display = "✅" if cfg.get("enabled") else "❌"
        text = f"基础设置\n群组: {group_id}\n状态: {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 状态查看 ====================
@router.callback_query(F.data == "view_status", F.from_user.id.in_(ADMIN_IDS))
async def view_status(callback: CallbackQuery):
    try:
        group_count = len(GROUP_IDS)
        admin_count = len(ADMIN_IDS)
        async with lock:
            report_count = len(reports)
        
        text = f"📊 状态\n✅ 运行正常\n管理员: {admin_count}\n群组: {group_count}\n举报: {report_count}"
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 检测和回复核心逻辑 ====================
FILL_CHARS = set(r" .,，。！？*\\\~`-_=+[]{}()\"'\\|\n\t\r　")

user_short_msg_history = {}

async def load_data():
    global reports
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    v["reporters"] = set(v.get("reporters", []))
                    reports[int(k)] = v
    except Exception as e:
        print("数据加载失败（首次正常）:", e)

async def save_data():
    async with lock:
        try:
            data_to_save = {str(k): {**v, "reporters": list(v["reporters"])} for k, v in reports.items()}
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("保存失败:", e)

def count_user_reported_messages(user_id: int, group_id: int) -> int:
    key = f"{group_id}_{user_id}"
    user_vio = user_violations.get(key, {})
    reported_count = sum(1 for v in user_vio.values() if v.get("reported"))
    return reported_count

def build_warning_buttons(msg_id: int, report_count: int):
    """构建警告消息按钮
    report_count=0: 只显示举报+豁免
    report_count>0: 显示举报+豁免 + 封禁24h+永封
    """
    buttons = [
        [
            InlineKeyboardButton(text="举报", callback_data=f"report:{msg_id}"),
            InlineKeyboardButton(text="误判👮‍♂️", callback_data=f"exempt:{msg_id}")
        ]
    ]
    
    if report_count > 0:
        buttons.append([
            InlineKeyboardButton(text="禁24h👮‍♂️", callback_data=f"ban24h:{msg_id}"),
            InlineKeyboardButton(text="永封👮‍♂️", callback_data=f"banperm:{msg_id}")
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(F.chat.id.in_(GROUP_IDS), F.text)
async def detect_and_warn(message: Message):
    """发言时检测并发送警告"""
    if not message.from_user or message.from_user.is_bot:
        return
    
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        return
    
    user_id = message.from_user.id
    group_id = message.chat.id
    
    # 检查举报禁言
    reported_count = count_user_reported_messages(user_id, group_id)
    threshold = cfg.get("reported_message_threshold", 2)
    
    if reported_count >= threshold:
        try:
            mute_hours = cfg.get("violation_mute_hours", 1)
            until_date = int(time.time()) + (mute_hours * 3600)
            await bot.restrict_chat_member(
                chat_id=group_id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            notice = f"账户异常（{reported_count}条举报），已禁言{mute_hours}h。异议联系管理员。"
            await message.reply(notice)
            return
        except Exception as e:
            print(f"禁言失败: {e}")
    
    # 5层检测
    triggers = []
    
    # 1. 简介链接
    try:
        if cfg.get("check_bio_link", True):
            chat_info = await bot.get_chat(user_id)
            bio = (chat_info.bio or "").lower()
            if any(x in bio for x in ["http://", "https://", "t.me/", "@"]):
                triggers.append("简介链接")
    except Exception:
        pass
    
    # 2. 简介敏感词
    try:
        if cfg.get("check_bio_keywords", True):
            chat_info = await bot.get_chat(user_id)
            bio = (chat_info.bio or "").lower()
            if any(kw.lower() in bio for kw in cfg.get("bio_keywords", [])):
                triggers.append("简介词汇")
    except Exception:
        pass
    
    # 3. 名称敏感词
    if cfg.get("check_display_keywords", True):
        display_name = (message.from_user.full_name or "").lower()
        if any(kw.lower() in display_name for kw in cfg.get("display_keywords", [])):
            triggers.append("昵称词汇")
    
    # 4. 消息敏感词
    if cfg.get("check_message_keywords", True):
        text_lower = message.text.lower()
        for kw in cfg.get("message_keywords", []):
            if kw.lower() in text_lower:
                triggers.append("内容词汇")
                break
    
    # 5. 连续极短消息
    if cfg.get("short_msg_detection", True):
        text_len = len(message.text)
        if text_len <= cfg.get("short_msg_threshold", 3):
            if user_id not in user_short_msg_history:
                user_short_msg_history[user_id] = deque(maxlen=15)
            
            history = user_short_msg_history[user_id]
            now = time.time()
            while history and now - history[0][0] > cfg.get("time_window_seconds", 60):
                history.popleft()
            history.append((now, message.text))
            
            recent = list(history)[-cfg.get("min_consecutive_count", 2):]
            if len(recent) >= cfg.get("min_consecutive_count", 2):
                if all(len(t.strip()) <= cfg.get("short_msg_threshold", 3) for _, t in recent):
                    triggers.append("连续短消息")
    
    # 6. 垃圾填充
    if cfg.get("fill_garbage_detection", True):
        text_len = len(message.text)
        if text_len >= cfg.get("fill_garbage_min_raw_len", 12):
            cleaned = ''.join(c for c in message.text if c not in FILL_CHARS).strip()
            clean_len = len(cleaned)
            space_ratio = (message.text.count(" ") + message.text.count("　")) / text_len if text_len > 0 else 0
            if (clean_len <= cfg.get("fill_garbage_max_clean_len", 8)) or (space_ratio >= cfg.get("fill_space_ratio", 0.30)):
                triggers.append("垃圾填充")
    
    # 第四步：有触发时发送警告（无论几层）
    if len(triggers) > 0:
        try:
            reason = "+".join(triggers)
            warning_text = f"⚠️ ID: {user_id}\n原因: {reason}"
            kb = build_warning_buttons(message.message_id, 0)
            warning = await message.reply(warning_text, reply_markup=kb)
            
            async with lock:
                reports[message.message_id] = {
                    "warning_id": warning.message_id,
                    "suspect_id": user_id,
                    "chat_id": group_id,
                    "reporters": set(),
                    "reason": reason,
                    "trigger_count": len(triggers)
                }
            await save_data()
        except Exception as e:
            print(f"发送警告失败: {e}")
    
    # 第五步：根据触发层数处理
    if len(triggers) >= 3:
        # 自动封禁
        try:
            await bot.restrict_chat_member(
                chat_id=group_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False
                )
            )
            reason = "+".join(triggers)
            final_text = f"⚠️ ID: {user_id}\n原因: {reason}\n结果: 已自动封禁。异议联系管理员。"
            try:
                async with lock:
                    if message.message_id in reports:
                        warning_id = reports[message.message_id]["warning_id"]
                        await bot.edit_message_text(
                            chat_id=group_id,
                            message_id=warning_id,
                            text=final_text,
                            reply_markup=None
                        )
            except:
                pass
            try:
                await message.delete()
            except:
                pass
        except Exception as e:
            print(f"自动封禁失败: {e}")
    
    elif len(triggers) == 2:
        # 私信管理员
        try:
            reason = "+".join(triggers)
            admin_msg = f"⚠️ 用户 {user_id}\n触发: {reason}\n内容: {message.text[:60]}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 立即处理", callback_data=f"admin_ban:{group_id}:{user_id}:{message.message_id}")]
            ])
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, admin_msg, reply_markup=kb)
                except:
                    pass
        except Exception as e:
            print(f"通知管理员失败: {e}")

@router.callback_query(F.data.startswith("admin_ban:"))
async def handle_admin_ban(callback: CallbackQuery):
    """管理员一键封禁"""
    try:
        parts = callback.data.split(":")
        group_id = int(parts[1])
        user_id = int(parts[2])
        
        if callback.from_user.id not in ADMIN_IDS:
            await callback.answer("仅管理员操作", show_alert=True)
            return
        
        await bot.restrict_chat_member(
            chat_id=group_id,
            user_id=user_id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False
            )
        )
        await callback.answer("✅ 已处理", show_alert=True)
    except Exception as e:
        print(f"管理员封禁失败: {e}")
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    """举报处理 - 关键修正：检查举报数，决定是否显示封禁按钮"""
    try:
        msg_id = int(callback.data.split(":", 1)[1])
        reporter_id = callback.from_user.id
        
        async with lock:
            if msg_id not in reports:
                await callback.answer("已过期", show_alert=True)
                return
            data = reports[msg_id]
            if reporter_id in data["reporters"]:
                await callback.answer("已举报过", show_alert=True)
                return
            data["reporters"].add(reporter_id)
            count = len(data["reporters"])
            user_id = data["suspect_id"]
            group_id = data["chat_id"]
            warning_id = data["warning_id"]
            reason = data["reason"]
        
        # 更新违规记录
        key = f"{group_id}_{user_id}"
        if key not in user_violations:
            user_violations[key] = {}
        user_violations[key][str(msg_id)] = {"reported": True, "time": time.time()}
        await save_user_violations()
        
        # 修改警告消息 - 关键：显示举报数 + 根据举报数决定按钮
        updated_text = f"⚠️ ID: {user_id}\n原因: {reason}\n举报: {count}人"
        kb = build_warning_buttons(msg_id, count)  # count > 0 时会添加封禁按钮
        
        try:
            await bot.edit_message_text(
                chat_id=group_id,
                message_id=warning_id,
                text=updated_text,
                reply_markup=kb
            )
        except:
            pass
        
        await callback.answer(f"✅ 举报({count}人)", show_alert=True)
        await save_data()
    except Exception as e:
        print("举报异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith(("ban24h:", "banperm:")))
async def handle_ban(callback: CallbackQuery):
    """封禁处理"""
    try:
        action, msg_id_str = callback.data.split(":", 1)
        msg_id = int(msg_id_str)
        caller_id = callback.from_user.id
        
        async with lock:
            if msg_id not in reports:
                await callback.answer("已过期", show_alert=True)
                return
            data = reports[msg_id]
            user_id = data["suspect_id"]
            group_id = data["chat_id"]
            warning_id = data["warning_id"]
            reason = data["reason"]
        
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员操作", show_alert=True)
            return
        
        # 执行封禁
        until_date = int(time.time()) + 86400 if action == "ban24h" else None
        await bot.restrict_chat_member(
            chat_id=group_id,
            user_id=user_id,
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
        
        # 修改警告消息为最终状态
        ban_type = "禁24h" if action == "ban24h" else "永封"
        report_count = len(data.get("reporters", set()))
        final_text = f"⚠️ ID: {user_id}\n原因: {reason}\n举报: {report_count}人\n结果: {ban_type}"
        
        try:
            await bot.edit_message_text(
                chat_id=group_id,
                message_id=warning_id,
                text=final_text,
                reply_markup=None
            )
        except:
            pass
        
        # 延迟删除警告消息
        async def delayed_delete():
            await asyncio.sleep(10)
            try:
                await bot.delete_message(group_id, warning_id)
            except:
                pass
        
        asyncio.create_task(delayed_delete())
        
        await callback.answer(f"✅ {ban_type}", show_alert=True)
        
        async with lock:
            reports.pop(msg_id, None)
        await save_data()
    
    except TelegramBadRequest:
        await callback.answer("❌ 失败", show_alert=True)
    except Exception as e:
        print("封禁异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("exempt:"))
async def handle_exempt(callback: CallbackQuery):
    """豁免用户"""
    try:
        msg_id = int(callback.data.split(":", 1)[1])
        caller_id = callback.from_user.id
        
        async with lock:
            if msg_id not in reports:
                await callback.answer("已过期", show_alert=True)
                return
            data = reports[msg_id]
            group_id = data["chat_id"]
            warning_id = data["warning_id"]
        
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员操作", show_alert=True)
            return
        
        # 删除警告消息
        try:
            await bot.delete_message(group_id, warning_id)
        except:
            pass
        
        await callback.answer("✅ 已豁免", show_alert=True)
        
        async with lock:
            reports.pop(msg_id, None)
        await save_data()
    
    except Exception as e:
        print("豁免异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

async def cleanup_deleted_messages():
    """清理已删除的消息记录"""
    while True:
        await asyncio.sleep(300)
        to_remove = []
        async with lock:
            check_list = list(reports.items())
        for msg_id, data in check_list:
            try:
                test_msg = await bot.forward_message(
                    chat_id=list(ADMIN_IDS)[0],
                    from_chat_id=data["chat_id"],
                    message_id=msg_id
                )
                await bot.delete_message(list(ADMIN_IDS)[0], test_msg.message_id)
            except TelegramBadRequest:
                try:
                    await bot.delete_message(data["chat_id"], data["warning_id"])
                    to_remove.append(msg_id)
                except Exception:
                    pass
        if to_remove:
            async with lock:
                for oid in to_remove:
                    reports.pop(oid, None)
            await save_data()
        await asyncio.sleep(1)

async def main():
    print("🚀 机器人启动")
    await load_config()
    await load_data()
    await load_user_violations()
    asyncio.create_task(cleanup_deleted_messages())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
