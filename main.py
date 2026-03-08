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
# 使用环境变量 DATA_DIR；Railway 需将 Volume 挂载到该路径（如 /data），重新部署后配置与关键词才不丢失
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "reports.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USER_VIOLATIONS_FILE = os.path.join(DATA_DIR, "user_violations.json")
MEDIA_STATS_FILE = os.path.join(DATA_DIR, "media_stats.json")
REPEAT_LEVEL_FILE = os.path.join(DATA_DIR, "repeat_levels.json")

reports = {}
lock = asyncio.Lock()
user_violations = {}
config = {}
# 媒体权限统计：合规消息数、同条超过10次不计数、已解锁名单、助力数（持久化到 MEDIA_STATS_FILE，重新部署须保留 DATA_DIR 卷）
media_stats = {"message_counts": {}, "text_counts": {}, "unlocked": {}, "boosts": {}}
media_stats_loaded = False
# 媒体消息举报/点赞（内存即可，按消息维度）
media_reports = {}
media_reports_lock = asyncio.Lock()
media_report_last = {}  # (uid,) -> (msg_id, time) 最近一次举报的媒体
media_report_day_count = {}  # (uid, date_str) -> count
# 召唤代发：未解锁用户发「召唤」后下一次媒体由机器人代发（避免炸群）
summon_pending = {}  # (group_id, user_id) -> timestamp
SUMMON_TIMEOUT_SEC = 300

# ==================== 配置函数 ====================
def _default_group_config():
    """单群默认配置（关键词等会随管理员编辑持久化到 CONFIG_FILE）"""
    return {
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
        "exempt_users": [],  # 用户ID列表，豁免后不触发警告
        "repeat_window_seconds": 2 * 3600,
        "repeat_max_count": 3,
        "repeat_ban_seconds": 86400,
        "media_unlock_msg_count": 50,
        "media_unlock_boosts": 4,
        "media_unlock_whitelist": [],
        "media_report_cooldown_sec": 20 * 60,
        "media_report_max_per_day": 3,
        "media_report_delete_threshold": 3,
        "media_rules_broadcast": True,
        "media_rules_broadcast_interval_minutes": 120,
    }

async def load_config():
    global config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            # 合并默认值，保证新增配置项有默认值且已保存的关键词等不丢失
            if "groups" not in config:
                config["groups"] = {}
            for gid, saved in list(config["groups"].items()):
                default = _default_group_config()
                for k, v in default.items():
                    if k not in saved:
                        saved[k] = v
                config["groups"][gid] = saved
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

def _prune_user_violations():
    """保留每用户最近 50 条且 30 天内的举报记录，避免文件无限增长"""
    now = time.time()
    cutoff = now - 30 * 86400
    for key in list(user_violations.keys()):
        entries = user_violations.get(key, {})
        if not isinstance(entries, dict):
            continue
        items = [(k, v) for k, v in entries.items() if (v.get("time") or 0) >= cutoff]
        items.sort(key=lambda x: x[1].get("time", 0), reverse=True)
        user_violations[key] = dict(items[:50])

async def save_user_violations():
    try:
        _prune_user_violations()
        with open(USER_VIOLATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_violations, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"违规记录保存失败: {e}")

async def load_media_stats():
    global media_stats, media_stats_loaded
    media_stats_loaded = False
    try:
        if os.path.exists(MEDIA_STATS_FILE):
            with open(MEDIA_STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            media_stats = {
                "message_counts": data.get("message_counts", {}),
                "text_counts": data.get("text_counts", {}),
                "unlocked": data.get("unlocked", {}),
                "boosts": data.get("boosts", {}),
            }
        media_stats_loaded = True
    except Exception as e:
        print(f"媒体统计加载失败: {e}（本次运行不写入，避免覆盖磁盘原有数据）")

async def save_media_stats():
    global media_stats_loaded
    if not media_stats_loaded:
        return
    try:
        with open(MEDIA_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(media_stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"媒体统计保存失败: {e}")

def load_repeat_levels():
    global repeat_violation_level
    try:
        if os.path.exists(REPEAT_LEVEL_FILE):
            with open(REPEAT_LEVEL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            repeat_violation_level = {}
            for k, v in data.items():
                parts = k.split("_", 1)
                if len(parts) == 2:
                    try:
                        repeat_violation_level[(int(parts[0]), int(parts[1]))] = int(v)
                    except ValueError:
                        pass
    except Exception as e:
        print(f"重复违规级别加载失败: {e}")

async def save_repeat_levels():
    try:
        data = {f"{g}_{u}": v for (g, u), v in repeat_violation_level.items()}
        with open(REPEAT_LEVEL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"重复违规级别保存失败: {e}")

def _media_key(group_id: int, user_id: int) -> str:
    return f"{group_id}_{user_id}"

async def _refresh_user_boosts(group_id: int, user_id: int) -> None:
    """用 Telegram API 拉取用户对本群的助力数并写回 media_stats（仅会员可助力）"""
    if not media_stats_loaded:
        return
    try:
        res = await bot.get_user_chat_boosts(chat_id=group_id, user_id=user_id)
        count = len(getattr(res, "boosts", []) or [])
        key = _media_key(group_id, user_id)
        media_stats["boosts"][key] = count
        await save_media_stats()
    except Exception:
        pass

def _can_send_media(group_id: int, user_id: int, username: str | None = None) -> bool:
    """是否已解锁发媒体：白名单 / 合规消息数 / 助力次数（助力需 Telegram 会员，仅会员可为群组助力）。"""
    cfg = get_group_config(group_id)
    wl = cfg.get("media_unlock_whitelist", [])
    if not isinstance(wl, list):
        wl = []
    sid = str(user_id)
    if sid in wl:
        return True
    if username:
        un = (username or "").strip().lstrip("@").lower()
        if un and any(un == (x.strip().lstrip("@").lower()) for x in wl if isinstance(x, str) and not x.isdigit()):
            return True
    key = _media_key(group_id, user_id)
    if media_stats["unlocked"].get(key):
        return True
    need_boosts = cfg.get("media_unlock_boosts", 4)
    if media_stats["boosts"].get(key, 0) >= need_boosts:
        return True
    return False

async def _increment_media_count(group_id: int, user_id: int, normalized_text: str) -> bool:
    """合规消息计数（同一条超过 10 次不计数）。返回是否因本次达到阈值而刚解锁。"""
    cfg = get_group_config(group_id)
    need_count = cfg.get("media_unlock_msg_count", 50)
    key = _media_key(group_id, user_id)
    if media_stats["unlocked"].get(key):
        return False
    tc = media_stats["text_counts"].setdefault(key, {})
    if tc.get(normalized_text, 0) >= 10:
        return False
    tc[normalized_text] = tc.get(normalized_text, 0) + 1
    count = media_stats["message_counts"].get(key, 0) + 1
    media_stats["message_counts"][key] = count
    if count >= need_count:
        media_stats["unlocked"][key] = True
        await save_media_stats()
        return True
    await save_media_stats()
    return False

def get_group_config(group_id: int):
    gid = str(group_id)
    if gid not in config["groups"]:
        config["groups"][gid] = _default_group_config()
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
    EditRepeatWindow = State()
    EditRepeatMaxCount = State()
    EditRepeatBanSec = State()
    EditMediaUnlockMsg = State()
    EditMediaUnlockBoosts = State()
    EditMediaReportCooldown = State()
    EditMediaReportMaxDay = State()
    EditMediaWhitelistAdd = State()
    EditMediaWhitelistRemove = State()
    EditExemptUsers = State()
    EditMediaDeleteThreshold = State()
    EditMediaBroadcastInterval = State()

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
        [InlineKeyboardButton(text="🔁 重复发言", callback_data=f"submenu_repeat:{group_id}")],
        [InlineKeyboardButton(text="📎 媒体权限", callback_data=f"submenu_media_perm:{group_id}")],
        [InlineKeyboardButton(text="📣 媒体举报", callback_data=f"submenu_media_report:{group_id}")],
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
    exempt = cfg.get("exempt_users") or []
    n = len(exempt) if isinstance(exempt, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"状态: {enabled}", callback_data=f"toggle_group:{group_id}")],
        [InlineKeyboardButton(text=f"🛡️ 豁免用户 ({n})", callback_data=f"submenu_exempt:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_exempt_menu_keyboard(group_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 编辑", callback_data=f"edit_exempt:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_basic:{group_id}")],
    ])

def get_repeat_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    w = cfg.get("repeat_window_seconds", 7200)
    m = cfg.get("repeat_max_count", 3)
    b = cfg.get("repeat_ban_seconds", 86400)
    buttons = [
        [InlineKeyboardButton(text=f"时间窗口: {w // 3600}h", callback_data=f"edit_repeat_window:{group_id}")],
        [InlineKeyboardButton(text=f"触发次数: {m}次", callback_data=f"edit_repeat_max:{group_id}")],
        [InlineKeyboardButton(text=f"首次禁言: {b // 3600}h", callback_data=f"edit_repeat_ban:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_perm_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    msg = cfg.get("media_unlock_msg_count", 50)
    boost = cfg.get("media_unlock_boosts", 4)
    wl = cfg.get("media_unlock_whitelist", [])
    n = len(wl) if isinstance(wl, list) else 0
    broadcast_on = "✅" if cfg.get("media_rules_broadcast", True) else "❌"
    interval = cfg.get("media_rules_broadcast_interval_minutes", 120)
    buttons = [
        [InlineKeyboardButton(text=f"解锁所需消息数: {msg}", callback_data=f"edit_media_msg:{group_id}")],
        [InlineKeyboardButton(text=f"解锁所需助力: {boost}", callback_data=f"edit_media_boosts:{group_id}")],
        [InlineKeyboardButton(text=f"📋 媒体解锁白名单 ({n})", callback_data=f"submenu_media_whitelist:{group_id}")],
        [InlineKeyboardButton(text=f"规则广播: {broadcast_on}", callback_data=f"toggle_media_broadcast:{group_id}")],
        [InlineKeyboardButton(text=f"广播间隔: {interval}分钟", callback_data=f"edit_media_broadcast_interval:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_whitelist_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    wl = cfg.get("media_unlock_whitelist", [])
    if not isinstance(wl, list):
        wl = []
    buttons = []
    for i, v in enumerate(wl[:25]):
        s = str(v)[:30]
        buttons.append([InlineKeyboardButton(text=f"❌ {s}", callback_data=f"remove_mw:{group_id}:{i}")])
    buttons.append([InlineKeyboardButton(text="➕ 添加", callback_data=f"add_media_whitelist:{group_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_media_perm:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_report_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
    max_day = cfg.get("media_report_max_per_day", 3)
    del_th = cfg.get("media_report_delete_threshold", 3)
    buttons = [
        [InlineKeyboardButton(text=f"连续举报冷却: {cooldown // 60}分钟", callback_data=f"edit_media_cooldown:{group_id}")],
        [InlineKeyboardButton(text=f"每日举报上限: {max_day}次", callback_data=f"edit_media_maxday:{group_id}")],
        [InlineKeyboardButton(text=f"举报达多少人删媒体: {del_th}", callback_data=f"edit_media_delete_threshold:{group_id}")],
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

# ==================== 重复发言 ====================
@router.callback_query(F.data.startswith("submenu_repeat:"), F.from_user.id.in_(ADMIN_IDS))
async def repeat_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        w = cfg.get("repeat_window_seconds", 7200)
        m = cfg.get("repeat_max_count", 3)
        b = cfg.get("repeat_ban_seconds", 86400)
        text = f"重复发言\n窗口: {w // 3600}h\n触发: {m}次\n首次禁言: {b // 3600}h"
        kb = get_repeat_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_repeat_window:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_window(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_window_seconds", 7200)
        await callback.message.edit_text(f"重复检测时间窗口（小时）（当前: {current // 3600}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatWindow)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatWindow), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_window(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_window_seconds"] = int(message.text.strip()) * 3600
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("edit_repeat_max:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_max(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_max_count", 3)
        await callback.message.edit_text(f"重复几次触发（当前: {current}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatMaxCount)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatMaxCount), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_max(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_max_count"] = int(message.text.strip())
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("edit_repeat_ban:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_ban(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_ban_seconds", 86400)
        await callback.message.edit_text(f"首次重复违规禁言时长（小时）（当前: {current // 3600}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatBanSec)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatBanSec), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_ban(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_ban_seconds"] = int(message.text.strip()) * 3600
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

# ==================== 媒体权限 ====================
@router.callback_query(F.data.startswith("submenu_media_perm:"), F.from_user.id.in_(ADMIN_IDS))
async def media_perm_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        msg = cfg.get("media_unlock_msg_count", 50)
        boost = cfg.get("media_unlock_boosts", 4)
        text = f"媒体权限\n解锁所需消息: {msg}\n解锁所需助力: {boost}"
        kb = get_media_perm_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_media_msg:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_msg(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_unlock_msg_count", 50)
        await callback.message.edit_text(f"解锁发媒体所需合规消息数（当前: {current}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaUnlockMsg)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaUnlockMsg), F.from_user.id.in_(ADMIN_IDS))
async def process_media_msg(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_unlock_msg_count"] = int(message.text.strip())
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_media_perm_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("edit_media_boosts:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_boosts(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_unlock_boosts", 4)
        await callback.message.edit_text(f"解锁发媒体所需助力次数（当前: {current}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaUnlockBoosts)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaUnlockBoosts), F.from_user.id.in_(ADMIN_IDS))
async def process_media_boosts(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_unlock_boosts"] = int(message.text.strip())
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_media_perm_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("toggle_media_broadcast:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_media_broadcast(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["media_rules_broadcast"] = not cfg.get("media_rules_broadcast", True)
        await save_config()
        on = "✅" if cfg["media_rules_broadcast"] else "❌"
        await callback.answer(f"规则广播: {on}", show_alert=True)
        await callback.message.edit_text(callback.message.text or "媒体权限", reply_markup=get_media_perm_menu_keyboard(group_id))
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_media_broadcast_interval:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_broadcast_interval(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_rules_broadcast_interval_minutes", 120)
        await callback.message.edit_text(f"规则广播间隔（分钟）（当前: {current}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaBroadcastInterval)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaBroadcastInterval), F.from_user.id.in_(ADMIN_IDS))
async def process_media_broadcast_interval(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_rules_broadcast_interval_minutes"] = max(1, int(message.text.strip()))
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_media_perm_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("submenu_media_whitelist:"), F.from_user.id.in_(ADMIN_IDS))
async def media_whitelist_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        wl = cfg.get("media_unlock_whitelist", [])
        if not isinstance(wl, list):
            wl = []
        text = "媒体解锁白名单（用户ID或用户名，满足即无需消息/助力可发媒体）\n当前：" + (", ".join(str(x) for x in wl) if wl else "（空）")
        await callback.message.edit_text(text, reply_markup=get_media_whitelist_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("add_media_whitelist:"), F.from_user.id.in_(ADMIN_IDS))
async def add_media_whitelist(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await callback.message.edit_text("输入要添加的用户ID或用户名（一行一个，支持多行）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaWhitelistAdd)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaWhitelistAdd), F.from_user.id.in_(ADMIN_IDS))
async def process_media_whitelist_add(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        wl = cfg.get("media_unlock_whitelist", [])
        if not isinstance(wl, list):
            wl = []
        for line in message.text.strip().splitlines():
            s = line.strip().lstrip("@")
            if s and s not in wl:
                wl.append(s)
        cfg["media_unlock_whitelist"] = wl
        await save_config()
        await message.reply(f"✅ 已添加，当前共 {len(wl)} 项", reply_markup=get_media_whitelist_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("remove_mw:"), F.from_user.id.in_(ADMIN_IDS))
async def remove_media_whitelist(callback: CallbackQuery):
    try:
        parts = callback.data.split(":", 2)
        group_id = int(parts[1])
        idx = int(parts[2])
        cfg = get_group_config(group_id)
        wl = cfg.get("media_unlock_whitelist", [])
        if not isinstance(wl, list):
            wl = []
        if 0 <= idx < len(wl):
            wl.pop(idx)
            cfg["media_unlock_whitelist"] = wl
            await save_config()
        text = "媒体解锁白名单\n当前：" + (", ".join(str(x) for x in wl) if wl else "（空）")
        await callback.message.edit_text(text, reply_markup=get_media_whitelist_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 媒体举报 ====================
@router.callback_query(F.data.startswith("submenu_media_report:"), F.from_user.id.in_(ADMIN_IDS))
async def media_report_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
        max_day = cfg.get("media_report_max_per_day", 3)
        del_th = cfg.get("media_report_delete_threshold", 3)
        text = f"媒体举报\n连续举报冷却: {cooldown // 60}分钟\n每日上限: {max_day}次\n举报达{del_th}人删媒体"
        kb = get_media_report_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_media_cooldown:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_cooldown(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_report_cooldown_sec", 20 * 60)
        await callback.message.edit_text(f"连续举报冷却（分钟）（当前: {current // 60}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaReportCooldown)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaReportCooldown), F.from_user.id.in_(ADMIN_IDS))
async def process_media_cooldown(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_report_cooldown_sec"] = int(message.text.strip()) * 60
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_media_report_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("edit_media_maxday:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_maxday(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_report_max_per_day", 3)
        await callback.message.edit_text(f"每日举报次数上限（当前: {current}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaReportMaxDay)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaReportMaxDay), F.from_user.id.in_(ADMIN_IDS))
async def process_media_maxday(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_report_max_per_day"] = int(message.text.strip())
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_media_report_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

@router.callback_query(F.data.startswith("edit_media_delete_threshold:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_delete_threshold(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_report_delete_threshold", 3)
        await callback.message.edit_text(f"举报达多少人删除媒体（当前: {current}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaDeleteThreshold)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaDeleteThreshold), F.from_user.id.in_(ADMIN_IDS))
async def process_media_delete_threshold(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_report_delete_threshold"] = max(1, int(message.text.strip()))
        await save_config()
        await message.reply("✅ 已更新", reply_markup=get_media_report_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"❌ 请输入数字: {e}")

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

@router.callback_query(F.data.startswith("submenu_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def exempt_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        exempt = cfg.get("exempt_users") or []
        if isinstance(exempt, dict):
            exempt = list(exempt.keys())
        text = f"豁免用户（不触发警告）\n当前: " + (", ".join(str(x) for x in exempt) if exempt else "（无）")
        await callback.message.edit_text(text, reply_markup=get_exempt_menu_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_exempt(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        exempt = cfg.get("exempt_users") or []
        if isinstance(exempt, dict):
            exempt = list(exempt.keys())
        text = f"编辑豁免用户（用户ID，一行一个）\n\n" + "\n".join(str(x) for x in exempt) + "\n\n发送新列表或 /clear 清空"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditExemptUsers)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditExemptUsers), F.from_user.id.in_(ADMIN_IDS))
async def process_exempt(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        if message.text.strip() == "/clear":
            cfg["exempt_users"] = []
        else:
            cfg["exempt_users"] = [x.strip() for x in message.text.strip().splitlines() if x.strip()]
        await save_config()
        await message.reply(f"✅ 已更新（{len(cfg['exempt_users'])} 人）", reply_markup=get_exempt_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

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

# key: (group_id, user_id, normalized_text) -> deque[timestamp]；key 数量上限 REPEAT_HISTORY_MAX_KEYS，超则淘汰最久未用
repeat_message_history = {}
repeat_message_history_last = {}  # key -> last_activity_time，用于淘汰
REPEAT_HISTORY_MAX_KEYS = 20000
# key: (group_id, user_id) -> int（0/1/2）；持久化到 REPEAT_LEVEL_FILE）
repeat_violation_level = {}
MEDIA_REPORT_LAST_MAX = 5000


def _normalize_text(text: str) -> str:
    """统一文本格式用于重复检测"""
    return " ".join((text or "").strip().split()).lower()


def _get_display_name_from_message(message: Message, user_id: int) -> str:
    """从消息中获取用于展示的用户名"""
    name = None
    if message and message.from_user and message.from_user.id == user_id:
        name = message.from_user.full_name or message.from_user.username
    if not name:
        name = f"ID {user_id}"
    return name


async def handle_repeat_message(message: Message) -> bool:
    """
    检测用户是否在配置时间窗口内重复发送相同内容
    返回 True 表示已经进行了处罚/提醒并且本次消息后续逻辑应中止
    """
    if not message.text:
        return False

    user_id = message.from_user.id
    group_id = message.chat.id
    cfg = get_group_config(group_id)
    window_sec = cfg.get("repeat_window_seconds", 2 * 3600)
    max_count = cfg.get("repeat_max_count", 3)
    ban_sec = cfg.get("repeat_ban_seconds", 86400)

    norm_text = _normalize_text(message.text)
    if not norm_text:
        return False

    key = (group_id, user_id, norm_text)
    now = time.time()

    if key not in repeat_message_history:
        if len(repeat_message_history) >= REPEAT_HISTORY_MAX_KEYS:
            for k in sorted(repeat_message_history_last.keys(), key=lambda k: repeat_message_history_last.get(k, 0))[:5000]:
                repeat_message_history.pop(k, None)
                repeat_message_history_last.pop(k, None)
        repeat_message_history[key] = deque(maxlen=10)
    history = repeat_message_history[key]
    repeat_message_history_last[key] = now

    while history and now - history[0] > window_sec:
        history.popleft()

    history.append(now)
    count = len(history)

    if count == 2:
        warn_text = (
            f"⚠️ 检测到你在 {window_sec // 3600} 小时内重复发送相同内容（2/{max_count}），请停止刷屏。"
        )
        try:
            await message.reply(warn_text)
        except Exception:
            pass
        return False

    if count >= max_count:
        level_key = (group_id, user_id)
        current_level = repeat_violation_level.get(level_key, 0)
        display_name = _get_display_name_from_message(message, user_id)

        if current_level == 0:
            until_date = int(now) + ban_sec
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
                    ),
                    until_date=until_date
                )
            except Exception as e:
                print(f"重复发言禁言失败: {e}")
                return False

            repeat_violation_level[level_key] = 1
            await save_repeat_levels()

            try:
                await message.delete()
            except Exception:
                pass

            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：在配置时间窗口内多次重复发送相同内容（{max_count}/{max_count}）。\n"
                f"🔒 处理结果：已被本群禁言 1 天。\n"
                f"⚠️ 疑似刷屏/引流，请谨慎。"
            )
            try:
                await bot.send_message(group_id, notice)
            except Exception:
                pass

            return True

        # 解封后再次在 2 小时内重复达到 3/3：永封
        else:
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
            except Exception as e:
                print(f"重复发言永封失败: {e}")
                return False

            repeat_violation_level[level_key] = 2
            await save_repeat_levels()

            try:
                await message.delete()
            except Exception:
                pass

            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：多次在 2 小时内重复发送相同内容，且在被解禁后仍然继续违规。\n"
                f"🔒 处理结果：已被本群永久禁止发言。\n"
                f"⚠️ 疑似严重刷屏/引流行为，请群友提高警惕。"
            )
            try:
                await bot.send_message(group_id, notice)
            except Exception:
                pass

            return True

    return False

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

def _media_reply_buttons(chat_id: int, media_msg_id: int, report_count: int, like_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"举报儿童色情⚠️ {report_count}人", callback_data=f"mr:{chat_id}:{media_msg_id}"),
            InlineKeyboardButton(text=f"点赞👍 {like_count}人", callback_data=f"ml:{chat_id}:{media_msg_id}"),
        ]
    ])

def _message_link(chat_id: int, msg_id: int) -> str:
    """群内消息链接，便于管理员定位"""
    cid = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{cid}/{msg_id}"

@router.message(Command("setboost"), F.chat.id.in_(GROUP_IDS), F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def cmd_set_boost(message: Message):
    """管理员在群内回复某条消息并发送 /setboost 4，将该用户的群组助力次数设为 4（用于解锁发媒体）"""
    try:
        text = (message.text or "").strip().split()
        if len(text) != 2:
            await message.reply("用法：回复要设置的用户的消息，发送 /setboost 数字（如 /setboost 4）")
            return
        count = int(text[1])
        if count < 0 or count > 100:
            await message.reply("助力次数请填 0～100")
            return
        target = message.reply_to_message.from_user
        if not target or target.is_bot:
            await message.reply("请回复真实用户的消息")
            return
        key = _media_key(message.chat.id, target.id)
        media_stats["boosts"][key] = count
        await save_media_stats()
        name = target.full_name or target.username or target.id
        await message.reply(f"已将该用户在本群的助力次数设为 {count}。{name} 现可发媒体。")
    except ValueError:
        await message.reply("请发送数字，如 /setboost 4")
    except Exception as e:
        await message.reply(f"设置失败: {e}")

@router.message(F.chat.id.in_(GROUP_IDS), F.photo | F.video | F.voice | F.video_note)
async def on_media_message(message: Message):
    """媒体消息：无权限则删除并提示或召唤代发；有权限则回复举报/点赞按钮"""
    if not message.from_user or message.from_user.is_bot:
        return
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    now = time.time()
    username = message.from_user.username if message.from_user else None
    await _refresh_user_boosts(group_id, user_id)
    if not _can_send_media(group_id, user_id, username):
        # 召唤代发：用户已发「召唤」则本次由机器人代发
        sk = (group_id, user_id)
        if sk in summon_pending and (now - summon_pending[sk]) <= SUMMON_TIMEOUT_SEC:
            try:
                await message.delete()
            except Exception:
                pass
            try:
                if message.photo:
                    await bot.send_photo(group_id, message.photo[-1].file_id, caption=message.caption)
                elif message.video:
                    await bot.send_video(group_id, message.video.file_id, caption=message.caption)
                elif message.voice:
                    await bot.send_voice(group_id, message.voice.file_id, caption=message.caption)
                elif message.video_note:
                    await bot.send_video_note(group_id, message.video_note.file_id)
            except Exception as e:
                print(f"召唤代发失败: {e}")
            summon_pending.pop(sk, None)
        else:
            try:
                await message.delete()
            except Exception:
                pass
            need_msg = cfg.get("media_unlock_msg_count", 50)
            need_boosts = cfg.get("media_unlock_boosts", 4)
            name = _get_display_name_from_message(message, user_id)
            await bot.send_message(
                group_id,
                f"⚠️ {name} 尚未解锁发媒体权限。\n"
                f"发送「权限」可查看进度；满 {need_msg} 条合规消息即可解锁，或为群组助力 {need_boosts} 次即可解锁（仅 Telegram 会员可为群组助力）。\n"
                f"现阶段为避免炸群，不满足条件的用户可输入「召唤」后发送图片/视频/语音，由机器人代发。"
            )
        return
    reply = await message.reply("📎 媒体消息", reply_markup=_media_reply_buttons(group_id, message.message_id, 0, 0))
    async with media_reports_lock:
        media_reports[(group_id, message.message_id)] = {
            "chat_id": group_id,
            "media_msg_id": message.message_id,
            "reply_msg_id": reply.message_id,
            "reporters": set(),
            "likes": set(),
            "deleted": False,
        }

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

    # 豁免用户不触发任何警告与检测（名单存 config，持久化）
    exempt = cfg.get("exempt_users") or []
    if isinstance(exempt, dict):
        exempt = list(exempt.keys())
    if str(user_id) in exempt:
        return

    # 「召唤」：未解锁用户可让机器人代发下一次媒体（为避免炸群）
    if message.text and message.text.strip() == "召唤":
        uname = message.from_user.username if message.from_user else None
        if not _can_send_media(group_id, user_id, uname):
            summon_pending[(group_id, user_id)] = time.time()
            await message.reply("请直接发送你要发布的图片/视频/语音，我将代你发出（为避免炸群）。")
        return

    # 「权限」查询发媒体进度（拉取最新助力数）
    if message.text and message.text.strip() == "权限":
        await _refresh_user_boosts(group_id, user_id)
        key = _media_key(group_id, user_id)
        count = media_stats["message_counts"].get(key, 0)
        unlocked = media_stats["unlocked"].get(key, False)
        boosts = media_stats["boosts"].get(key, 0)
        need_msg = cfg.get("media_unlock_msg_count", 50)
        need_boosts = cfg.get("media_unlock_boosts", 4)
        if unlocked:
            await message.reply(f"✅ 你已解锁在本群直接发送图片/视频/语音（合规消息已满 {need_msg} 条）。")
            return
        if boosts >= need_boosts:
            await message.reply(f"✅ 你已解锁发媒体权限（已为群组助力 {boosts} 次）。")
            return
        await message.reply(
            f"📊 发媒体权限进度\n"
            f"· 合规消息：{count}/{need_msg}（满 {need_msg} 条可解锁）\n"
            f"· 群组助力：{boosts}/{need_boosts}（满 {need_boosts} 次可解锁，仅 Telegram 会员可为群组助力）\n"
            f"（刷屏、重复发言、短消息等不计入合规消息）"
        )
        return
    
    # 重复发言检测（优先执行）
    if await handle_repeat_message(message):
        return
    
    # 检查举报禁言（被多人举报的集中处理）
    reported_count = count_user_reported_messages(user_id, group_id)
    threshold = cfg.get("reported_message_threshold", 2)
    
    if reported_count >= threshold:
        try:
            mute_hours = cfg.get("violation_mute_hours", 1)
            until_date = int(time.time()) + (mute_hours * 3600)
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

            # 删除当前触发的源消息
            try:
                await message.delete()
            except Exception:
                pass

            display_name = _get_display_name_from_message(message, user_id)
            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：在本群多次被成员举报（累计 {reported_count} 条）。\n"
                f"🔒 处理结果：已被限制发言 {mute_hours} 小时。\n"
                f"⚠️ 疑似引流/广告，请谨慎，可继续使用“举报”按钮。"
            )
            await bot.send_message(group_id, notice)
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
    
    # 5. 连续极短消息（按群+用户统计，避免跨群误判）
    if cfg.get("short_msg_detection", True):
        text_len = len(message.text)
        if text_len <= cfg.get("short_msg_threshold", 3):
            short_key = (group_id, user_id)
            if short_key not in user_short_msg_history:
                user_short_msg_history[short_key] = deque(maxlen=15)
            
            history = user_short_msg_history[short_key]
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
            display_name = _get_display_name_from_message(message, user_id)
            warning_text = (
                "🚨 检测到疑似违规内容\n\n"
                f"👤 用户：{display_name}（ID: {user_id}）\n"
                f"📌 触发原因：{reason}\n\n"
                "⚠️ 疑似引流/广告，请谨慎，可点下方按钮举报或标记误判。"
            )
            kb = build_warning_buttons(message.message_id, 0)
            warning = await message.reply(warning_text, reply_markup=kb)
            
            async with lock:
                reports[message.message_id] = {
                    "warning_id": warning.message_id,
                    "suspect_id": user_id,
                    "chat_id": group_id,
                    "reporters": set(),
                    "reason": reason,
                    "trigger_count": len(triggers),
                    "suspect_name": display_name,
                    "original_message_id": message.message_id
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
            display_name = _get_display_name_from_message(message, user_id)
            final_text = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：{reason}\n"
                "🔒 处理结果：因同时触发多项高危规则，已被本群永久限制发言。\n"
                "⚠️ 疑似高危引流/广告内容，请所有群友提高警惕。"
            )
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
            except Exception:
                pass
            try:
                await message.delete()
            except Exception:
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
    else:
        # 合规消息：计入媒体权限进度（同一条超过 10 次不计数）
        try:
            norm = _normalize_text(message.text or "")
            if norm:
                just_unlocked = await _increment_media_count(group_id, user_id, norm)
                if just_unlocked:
                    name = _get_display_name_from_message(message, user_id)
                    need_msg = cfg.get("media_unlock_msg_count", 50)
                    try:
                        await bot.send_message(
                            group_id,
                            f"🎉 {name} 已在本群发送合规消息满 {need_msg} 条，解锁直接发送图片/视频/语音的权限。"
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"媒体计数失败: {e}")

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
        await callback.answer("✅ 已处理")
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
                await callback.answer("已过期")
                return
            data = reports[msg_id]
            if reporter_id in data["reporters"]:
                await callback.answer("已举报过")
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
        display_name = data.get("suspect_name") or f"ID {user_id}"
        updated_text = (
            "🚨 已收到群成员的举报\n\n"
            f"👤 用户：{display_name}（ID: {user_id}）\n"
            f"📌 触发原因：{reason}\n"
            f"📣 当前举报人数：{count} 人\n\n"
            "⚠️ 疑似引流/广告消息，请谨慎，可继续补充举报，由管理员统一处理。"
        )
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
        
        await callback.answer(f"✅ 举报({count}人)")
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
                await callback.answer("已过期")
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
        
        # 删除被封禁用户的源消息（避免继续在群内展示）
        try:
            await bot.delete_message(group_id, msg_id)
        except Exception:
            pass

        # 修改警告消息为最终状态并给出完整说明
        ban_type = "禁言 24 小时" if action == "ban24h" else "永久禁止在本群发言"
        report_count = len(data.get("reporters", set()))
        display_name = data.get("suspect_name") or f"ID {user_id}"
        final_text = (
            f"🚫 用户 {display_name}\n"
            f"📌 触发原因：{reason}（已被 {report_count} 位成员举报）\n"
            f"🔒 处理结果：{ban_type}。\n"
            "⚠️ 疑似引流/广告账号，请谨慎，不要随意添加或私信。"
        )
        
        try:
            await bot.edit_message_text(
                chat_id=group_id,
                message_id=warning_id,
                text=final_text,
                reply_markup=None
            )
        except:
            pass
        
        await callback.answer(f"✅ {ban_type}")
        
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
                await callback.answer("已过期")
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
        
        await callback.answer("✅ 已豁免")
        
        async with lock:
            reports.pop(msg_id, None)
        await save_data()
    
    except Exception as e:
        print("豁免异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("mr:"))
async def handle_media_report(callback: CallbackQuery):
    """举报儿童色情：限流（连续两条 cooldown、一天上限）从群配置读"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("无效", show_alert=True)
            return
        chat_id = int(parts[1])
        media_msg_id = int(parts[2])
        cfg = get_group_config(chat_id)
        cooldown_sec = cfg.get("media_report_cooldown_sec", 20 * 60)
        max_per_day = cfg.get("media_report_max_per_day", 3)
        uid = callback.from_user.id
        now = time.time()
        today_str = time.strftime("%Y-%m-%d", time.localtime(now))
        for k in list(media_report_day_count.keys()):
            if k[1] != today_str:
                media_report_day_count.pop(k, None)
        day_key = (uid, today_str)

        day_count = media_report_day_count.get(day_key, 0)
        if day_count >= max_per_day:
            await callback.answer("今日举报次数已达上限，如有问题请直接联系管理员。", show_alert=True)
            return

        if len(media_report_last) >= MEDIA_REPORT_LAST_MAX:
            items = sorted(media_report_last.items(), key=lambda x: x[1][1])[:1000]
            for u, _ in items:
                media_report_last.pop(u, None)
        last = media_report_last.get(uid)
        if last:
            last_mid, last_ts = last
            if last_mid != media_msg_id and (now - last_ts) < cooldown_sec:
                await callback.answer(f"请勿在 {cooldown_sec // 60} 分钟内对多条媒体连续举报，请稍后再试。", show_alert=True)
                return
        media_report_last[uid] = (media_msg_id, now)
        media_report_day_count[day_key] = day_count + 1

        async with media_reports_lock:
            key = (chat_id, media_msg_id)
            if key not in media_reports:
                await callback.answer("已过期")
                return
            data = media_reports[key]
            if data["deleted"]:
                await callback.answer("该媒体已被删除")
                return
            if uid in data["reporters"]:
                await callback.answer("已举报过")
                return
            data["reporters"].add(uid)
            report_count = len(data["reporters"])
            like_count = len(data["likes"])
            reply_id = data["reply_msg_id"]

        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=reply_id,
                reply_markup=_media_reply_buttons(chat_id, media_msg_id, report_count, like_count)
            )
        except Exception:
            pass
        await callback.answer()

        delete_threshold = cfg.get("media_report_delete_threshold", 3)
        if report_count >= delete_threshold:
            try:
                await bot.delete_message(chat_id, media_msg_id)
            except Exception:
                pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=reply_id,
                    text="⚠️ 多人举报，已删除该媒体。",
                    reply_markup=None
                )
            except Exception:
                pass
            async with media_reports_lock:
                if key in media_reports:
                    media_reports[key]["deleted"] = True
        elif report_count == 2:
            link = _message_link(chat_id, media_msg_id)
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ 群内媒体被举报（儿童色情相关）\n群: {chat_id}\n消息: {link}\n当前举报人数: 2 人",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="定位到消息", url=link)]
                        ])
                    )
                except Exception:
                    pass
    except Exception as e:
        print("媒体举报异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("ml:"))
async def handle_media_like(callback: CallbackQuery):
    """点赞"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("无效", show_alert=True)
            return
        chat_id = int(parts[1])
        media_msg_id = int(parts[2])
        uid = callback.from_user.id
        async with media_reports_lock:
            key = (chat_id, media_msg_id)
            if key not in media_reports:
                await callback.answer("已过期")
                return
            data = media_reports[key]
            if uid in data["likes"]:
                await callback.answer("已点赞过")
                return
            data["likes"].add(uid)
            like_count = len(data["likes"])
            reply_id = data["reply_msg_id"]
            report_count = len(data["reporters"])
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=reply_id,
                reply_markup=_media_reply_buttons(chat_id, media_msg_id, report_count, like_count)
            )
        except Exception:
            pass
        await callback.answer()
    except Exception as e:
        print("媒体点赞异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

def _media_rules_text(group_id: int) -> str:
    cfg = get_group_config(group_id)
    need_msg = cfg.get("media_unlock_msg_count", 50)
    need_boosts = cfg.get("media_unlock_boosts", 4)
    return (
        "📋 本群发媒体（图片/视频/语音）规则\n\n"
        f"· 合规消息满 {need_msg} 条可解锁发媒体\n"
        f"· 为群组助力 {need_boosts} 次可解锁（仅 Telegram 会员可为群组助力）\n"
        "· 刷屏、重复发言、短消息等不计入合规条数\n"
        "· 未解锁可发「召唤」后发图，由机器人代发\n\n"
        "发送「权限」可随时查询自己的进度。"
    )

async def broadcast_media_rules_every_2h():
    """按群配置间隔向各群发送媒体权限规则（可关、可调间隔）"""
    while True:
        interval_min = 120
        for gid in GROUP_IDS:
            m = get_group_config(gid).get("media_rules_broadcast_interval_minutes", 120)
            interval_min = min(interval_min, max(1, m))
        await asyncio.sleep(interval_min * 60)
        for gid in GROUP_IDS:
            try:
                cfg = get_group_config(gid)
                if not cfg.get("enabled", True) or not cfg.get("media_rules_broadcast", True):
                    continue
                await bot.send_message(gid, _media_rules_text(gid))
            except Exception as e:
                print(f"广播媒体规则失败 {gid}: {e}")

async def cleanup_deleted_messages():
    """清理已删除的消息记录（每 10 分钟，降低 API 调用）"""
    while True:
        await asyncio.sleep(600)
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
                # 仅从内存移除记录，不删除机器人警告消息，保证群内警告始终保留
                to_remove.append(msg_id)
        if to_remove:
            async with lock:
                for oid in to_remove:
                    reports.pop(oid, None)
            await save_data()
        await asyncio.sleep(1)

async def main():
    print("🚀 机器人启动")
    await load_config()
    for gid in GROUP_IDS:
        get_group_config(gid)
    await save_config()
    await load_data()
    await load_user_violations()
    load_repeat_levels()
    await load_media_stats()
    asyncio.create_task(cleanup_deleted_messages())
    asyncio.create_task(broadcast_media_rules_every_2h())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
