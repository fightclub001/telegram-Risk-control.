import asyncio
import json
import os
import re
import time
import hashlib
from collections import deque
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, ReplyKeyboardRemove
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from semantic_ads import SemanticAdDetector

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
try:
    from bot_admin import router as admin_router
    dp.include_router(admin_router)
except ImportError:
    admin_router = None

# ==================== 数据文件 ====================
# 使用环境变量 DATA_DIR；Railway 需将 Volume 挂载到该路径（如 /data），重新部署后配置与名单才不丢失
# 以下数据均持久化，重启不丢失：CONFIG_FILE（豁免名单 exempt_users、媒体白名单 media_unlock_whitelist、
# 重复发言豁免词 repeat_exempt_keywords、各群关键词与开关等）；DATA_FILE 举报记录；MEDIA_STATS_FILE 合规数/助力/解锁
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "reports.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USER_VIOLATIONS_FILE = os.path.join(DATA_DIR, "user_violations.json")
MEDIA_STATS_FILE = os.path.join(DATA_DIR, "media_stats.json")
REPEAT_LEVEL_FILE = os.path.join(DATA_DIR, "repeat_levels.json")
LINK_REF_LEVELS_FILE = os.path.join(DATA_DIR, "link_ref_levels.json")

reports = {}  # key: (group_id, message_id)
lock = asyncio.Lock()
user_violations = {}  # key: "gid_uid" -> { msg_id: { "time", "reporters": [] } }
user_recent_message_ids = {}  # (group_id, user_id) -> deque of (msg_id, time, text), for 24h delete & learning
mild_trigger_entries = {}  # (group_id, user_id) -> list of (orig_msg_id, warning_msg_id), max 3
repeat_warning_msg_id = {}  # (group_id, user_id) -> msg_id of "2次" repeat warning, delete if orig deleted
# 外部引用 / 消息链接：0=未触发过，1=已触发一次（下次永封）
external_ref_level = {}  # (group_id, user_id) -> 0|1
message_link_level = {}  # (group_id, user_id) -> 0|1
config = {}
# 媒体权限统计：合规消息数、同条超过10次不计数、已解锁名单、助力数（持久化到 MEDIA_STATS_FILE，重新部署须保留 DATA_DIR 卷）
media_stats = {"message_counts": {}, "text_counts": {}, "unlocked": {}, "boosts": {}}
media_stats_loaded = False
# 媒体消息举报/点赞（内存即可，按消息维度）
media_reports = {}
media_reports_lock = asyncio.Lock()
media_report_last = {}  # (uid,) -> (msg_id, time) 最近一次举报的媒体
media_report_day_count = {}  # (uid, date_str) -> count
SEMANTIC_AD_DATA_DIR = os.path.join(DATA_DIR, "semantic_ads")
semantic_ad_detector = SemanticAdDetector(SEMANTIC_AD_DATA_DIR)
# 召唤代发：未解锁用户发「召唤」后下一次媒体由机器人代发（避免炸群）
summon_pending = {}  # (group_id, user_id) -> timestamp
SUMMON_TIMEOUT_SEC = 300
# 无权限发媒体警告：同用户删上一条；(group_id, user_id) -> 上一条机器人警告 message_id
last_media_no_perm_msg = {}
MEDIA_NO_PERM_DELETE_AFTER_SEC = 60  # 不同用户的警告 1 分钟后自动删除
media_no_perm_strikes = {}  # (group_id, user_id) -> (count, last_time) 连续无权限发媒体计数
MEDIA_NO_PERM_STRIKE_RESET_SEC = 300  # 超过此时间未再触发则视为重新计算连续次数
# 举报按钮规则：管理员未点击封禁/误判豁免前，按钮永不过期（不因原消息被删而移除）。
# (1) 机器人自动封禁 (2) 管理员点击封禁：移除按钮并保留/更新文案；(3) 管理员点击误判豁免：删除警告消息。
# 超过此时长仍未处理：仅隐藏按钮、保留消息文本，并从内存移除记录。
REPORT_BUTTON_HIDE_AFTER_SEC = 24 * 3600
REPORT_BAN_HOURS_CAP = 72
last_ban_warning_msg = {}  # group_id -> warning_id：上一条已处理的封禁警告，下次封禁时 15 秒后删除
MISJUDGE_BOT_MENTION = "误封联系管理员 @trump2028_bot"
USER_MSG_TRACK_MAXLEN = 500
USER_MSG_24H_SEC = 24 * 3600
BOT_MSG_AUTO_DELETE_SEC = 24 * 3600  # 机器人消息24小时后自动删除

# 机器人消息跟踪：(group_id, msg_id) -> timestamp
bot_sent_messages = {}
# 机器人在群里的“引用回复”跟踪：(group_id, bot_reply_msg_id) -> (original_msg_id, created_ts)
bot_reply_links = {}
# 同用户连续触发警告防刷屏：(group_id, user_id) -> (last_warning_time, last_warning_msg_id)
user_last_warning = {}
USER_WARNING_COOLDOWN_SEC = 60  # 同用户60秒内只发一条警告
# 已封禁警告消息列表：group_id -> list of warning_msg_id（用于一次性删除所有已封禁警告）
banned_warning_messages = {}

# ==================== 配置函数 ====================
def _default_group_config():
    """单群默认配置（关键词等会随管理员编辑持久化到 CONFIG_FILE）"""
    return {
        "enabled": True,
        "check_bio_link": True,
        "bio_keywords": ["qq:", "qq：", "qq号", "加qq", "扣扣", "微信", "wx:", "weixin", "加我微信", "wxid_", "幼女", "福利", "约炮", "onlyfans", "小红书", "抖音", "纸飞机", "机场", "1v1", "看片", "集中营", "门槛"],
        "check_bio_keywords": True,
        "check_message_link": True,  # 消息内链接/@引流（归属消息检测，与简介/名称同级）
        "display_keywords": ["加v", "加微信", "加qq", "加扣", "福利加", "约", "约炮", "资源私聊", "私我", "私聊我", "飞机", "纸飞机", "福利", "外围", "反差", "嫩模", "学生妹", "空姐", "人妻", "熟女", "onlyfans", "of", "leak", "nudes", "十八+", "av"],
        "check_display_keywords": True,
        "message_keywords": ["qq:", "qq号", "微信", "wx:", "幼女", "萝莉", "福利", "约炮", "onlyfans"],
        "check_message_keywords": True,
        "message_keyword_normalize": True,  # 防拼字规避：忽略空格标点后匹配（如 A  bc，D 命中 abcd）
        "short_msg_detection": True,
        "short_msg_threshold": 3,
        "min_consecutive_count": 2,
        "time_window_seconds": 60,
        "fill_garbage_detection": True,
        "fill_garbage_min_raw_len": 12,
        "fill_garbage_max_clean_len": 8,
        "fill_space_ratio": 0.30,
        "violation_mute_hours": 1,
        "reported_message_threshold": 3,
        "autoreply": {
            "enabled": False,
            "keywords": [],
            "reply_text": "",
            "buttons": [],
            "delete_user_sec": 0,
            "delete_bot_sec": 0
        },
        "exempt_users": [],  # 管理员手动维护的豁免（与发图权限无关）
        "misjudge_whitelist": [],  # 仅管理员点击「误判」后加入，豁免多层内容检测
        "mild_exempt_whitelist": [],  # 轻度触发豁免名单（管理员通过私聊按钮设置）
        "repeat_window_seconds": 2 * 3600,
        "repeat_max_count": 3,
        "repeat_ban_seconds": 86400,
        "repeat_exempt_keywords": [],  # 含任一词的消息不触发重复发言检测（白名单词）
        "media_unlock_msg_count": 50,
        "media_unlock_boosts": 4,
        "media_unlock_whitelist": [],
        "media_report_cooldown_sec": 20 * 60,
        "media_report_max_per_day": 3,
        "media_report_delete_threshold": 3,
        "media_rules_broadcast": True,
        "media_rules_broadcast_interval_minutes": 120,
        "semantic_ad_enabled": False,
    }

async def load_config():
    """从 CONFIG_FILE 加载配置；已保存的豁免名单、白名单、豁免词等全部保留，仅对缺失项补默认值"""
    global config
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
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
    """保存配置到 CONFIG_FILE，豁免名单/白名单/豁免词等所有名单均在此持久化，重启不丢失"""
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
                raw = json.load(f)
            user_violations = {}
            for key, entries in raw.items():
                user_violations[key] = {}
                for msg_id, v in (entries or {}).items():
                    vv = dict(v)
                    if "reporters" in vv and isinstance(vv["reporters"], list):
                        vv["reporters"] = set(vv["reporters"])
                    user_violations[key][msg_id] = vv
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
        out = {}
        for k, v in items[:50]:
            vv = dict(v)
            if "reporters" in vv and isinstance(vv["reporters"], set):
                vv["reporters"] = list(vv["reporters"])
            out[k] = vv
        user_violations[key] = out

async def save_user_violations():
    try:
        _prune_user_violations()
        to_save = {}
        for key, entries in user_violations.items():
            to_save[key] = {}
            for msg_id, v in entries.items():
                vv = dict(v)
                if "reporters" in vv and isinstance(vv["reporters"], set):
                    vv["reporters"] = list(vv["reporters"])
                to_save[key][msg_id] = vv
        with open(USER_VIOLATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
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


def load_link_ref_levels():
    global external_ref_level, message_link_level
    try:
        if os.path.exists(LINK_REF_LEVELS_FILE):
            with open(LINK_REF_LEVELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            external_ref_level = {}
            for k, v in (data.get("external_ref") or {}).items():
                parts = k.split("_", 1)
                if len(parts) == 2:
                    try:
                        external_ref_level[(int(parts[0]), int(parts[1]))] = int(v)
                    except ValueError:
                        pass
            message_link_level = {}
            for k, v in (data.get("message_link") or {}).items():
                parts = k.split("_", 1)
                if len(parts) == 2:
                    try:
                        message_link_level[(int(parts[0]), int(parts[1]))] = int(v)
                    except ValueError:
                        pass
    except Exception as e:
        print(f"链接/引用级别加载失败: {e}")


async def save_link_ref_levels():
    try:
        data = {
            "external_ref": {f"{g}_{u}": v for (g, u), v in external_ref_level.items()},
            "message_link": {f"{g}_{u}": v for (g, u), v in message_link_level.items()},
        }
        with open(LINK_REF_LEVELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"链接/引用级别保存失败: {e}")


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

        # 如果助力数已达到解锁条件，则标记为已解锁并尝试恢复其发媒体权限
        cfg = get_group_config(group_id)
        need_boosts = cfg.get("media_unlock_boosts", 4)
        if count >= need_boosts and not media_stats["unlocked"].get(key):
            media_stats["unlocked"][key] = True
            await save_media_stats()
            try:
                await bot.restrict_chat_member(
                    chat_id=group_id,
                    user_id=user_id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                        can_change_info=False,
                        can_invite_users=True,
                        can_pin_messages=False,
                    ),
                )
            except Exception:
                # 恢复权限失败不会影响后续解锁判断
                pass
    except Exception:
        pass

def _can_send_media(group_id: int, user_id: int, username: str | None = None) -> bool:
    """是否已解锁发媒体：仅看本处媒体解锁白名单 / 合规消息数 / 助力次数（与豁免检测 exempt_users 无关）。"""
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
    """合规消息计数（同一条超过 10 次不计数）。已解锁=满50条/白名单/助力的用户不再统计（不含一发图就被删的用户，避免逻辑循环）。返回是否因本次达到阈值而刚解锁。"""
    cfg = get_group_config(group_id)
    need_count = cfg.get("media_unlock_msg_count", 50)
    key = _media_key(group_id, user_id)
    if media_stats["unlocked"].get(key):  # 已能发媒体，不再统计
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


async def _try_count_media_and_notify(message: Message, group_id: int, user_id: int, cfg: dict) -> None:
    """合规消息计入并可能发送解锁贺信。仅对「未解锁」用户统计（已解锁=满50条/白名单/助力，不包含一发图就被删的用户，避免逻辑循环）。"""
    media_key = _media_key(group_id, user_id)
    if media_stats["unlocked"].get(media_key):
        return
    try:
        norm = _normalize_text(message.text or "")
        if not norm:
            return
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
            # 若该用户此前因多次违规被关闭媒体权限，则在达到解锁条件后自动恢复其发媒体权限
            try:
                await bot.restrict_chat_member(
                    chat_id=group_id,
                    user_id=user_id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                        can_change_info=False,
                        can_invite_users=True,
                        can_pin_messages=False,
                    ),
                )
            except Exception:
                # 如果恢复权限失败，不影响解锁逻辑本身
                pass
    except Exception as e:
        print(f"媒体计数失败: {e}")


def get_group_config(group_id: int):
    gid = str(group_id)
    if gid not in config["groups"]:
        config["groups"][gid] = _default_group_config()
    return config["groups"][gid]


def fmt_duration(seconds: int) -> str:
    """将秒数格式化为人类可读时长"""
    if seconds == 0:
        return "永久"
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    if seconds < 86400:
        return f"{seconds // 3600}小时"
    if seconds < 604800:
        return f"{seconds // 86400}天"
    return f"{seconds // 604800}周"


async def get_chat_title_safe(bot, chat_id: int) -> str:
    """获取群组/聊天标题，失败时返回 ID"""
    try:
        chat = await bot.get_chat(chat_id)
        return (chat.title or "").strip() or f"ID {chat_id}"
    except Exception:
        return str(chat_id)


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
    EditRepeatExemptKeywords = State()
    EditMediaUnlockMsg = State()
    EditMediaUnlockBoosts = State()
    EditMediaReportCooldown = State()
    EditMediaReportMaxDay = State()
    EditMediaWhitelistAdd = State()
    EditMediaWhitelistRemove = State()
    EditExemptUsers = State()
    EditMediaDeleteThreshold = State()
    EditMediaBroadcastInterval = State()
    EditSemanticAdAdd = State()
    EditSemanticAdRemove = State()

# ==================== UI 键盘 ====================
def get_main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton(text="⚙️ 群组管理", callback_data="choose_group")],
        [InlineKeyboardButton(text="📊 状态查看", callback_data="view_status")],
        [InlineKeyboardButton(text="🔧 全局系统配置", callback_data="admin:main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def get_group_list_keyboard(bot):
    """异步生成群组列表键盘，显示群名 + ID"""
    buttons = []
    for gid in sorted(GROUP_IDS):
        title = await get_chat_title_safe(bot, gid)
        label = f"👥 {title}" if title != str(gid) else f"👥 {gid}"
        # 标题过长时截断，保留 ID 信息
        if len(label) > 35:
            label = label[:32] + "..."
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"select_group:{gid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ 返回", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_group_menu_keyboard(group_id: int):
    buttons = [
        [InlineKeyboardButton(text="🔍 简介检测", callback_data=f"submenu_bio:{group_id}")],
        [InlineKeyboardButton(text="👤 名称检测", callback_data=f"submenu_display:{group_id}")],
        [InlineKeyboardButton(text="💬 消息检测", callback_data=f"submenu_message:{group_id}")],
        [InlineKeyboardButton(text="⏱️ 短消息/垃圾", callback_data=f"submenu_short:{group_id}")],
        [InlineKeyboardButton(text="🧠 AD机器学习", callback_data=f"submenu_semantic_ad:{group_id}")],
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
    msg_link_status = "✅" if cfg.get("check_message_link", True) else "❌"
    norm_status = "✅" if cfg.get("message_keyword_normalize", True) else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"敏感词 {status}", callback_data=f"toggle_message:{group_id}")],
        [InlineKeyboardButton(text=f"链接/@引流 {msg_link_status}", callback_data=f"toggle_message_link:{group_id}")],
        [InlineKeyboardButton(text=f"防拼字规避 {norm_status}", callback_data=f"toggle_message_normalize:{group_id}")],
        [InlineKeyboardButton(text="📋 编辑词汇", callback_data=f"edit_message_kw:{group_id}")],
        [InlineKeyboardButton(text="👀 查看", callback_data=f"view_message_kw:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_short_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    short_enabled = "✅" if cfg.get("short_msg_detection") else "❌"
    fill_enabled = "✅" if cfg.get("fill_garbage_detection") else "❌"
    window_sec = cfg.get("time_window_seconds", 60)
    buttons = [
        [InlineKeyboardButton(text=f"短消息 {short_enabled}", callback_data=f"toggle_short:{group_id}")],
        [InlineKeyboardButton(text=f"字数: {cfg.get('short_msg_threshold')}", callback_data=f"edit_threshold:{group_id}")],
        [InlineKeyboardButton(text=f"连续: {cfg.get('min_consecutive_count')}", callback_data=f"edit_consecutive:{group_id}")],
        [InlineKeyboardButton(text=f"窗口: {fmt_duration(window_sec)}", callback_data=f"edit_window:{group_id}")],
        [InlineKeyboardButton(text=f"垃圾 {fill_enabled}", callback_data=f"toggle_fill:{group_id}")],
        [InlineKeyboardButton(text=f"最小: {cfg.get('fill_garbage_min_raw_len')}", callback_data=f"edit_fill_min:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_semantic_ad_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    enabled = "✅" if cfg.get("semantic_ad_enabled", False) else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"开关 {enabled}", callback_data=f"toggle_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="➕ 增加广告语句", callback_data=f"add_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="➖ 减少广告语句", callback_data=f"remove_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="📂 广告词库展示", callback_data=f"view_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_violation_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    mute_h = cfg.get("violation_mute_hours", 1)
    mute_sec = mute_h * 3600
    buttons = [
        [InlineKeyboardButton(text=f"🔇 禁言时长: {fmt_duration(mute_sec)}", callback_data=f"edit_mute:{group_id}")],
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
    kw = cfg.get("repeat_exempt_keywords", []) or []
    n_kw = len(kw) if isinstance(kw, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"⏱ 时间窗口: {fmt_duration(w)}", callback_data=f"edit_repeat_window:{group_id}")],
        [InlineKeyboardButton(text=f"触发次数: {m}次", callback_data=f"edit_repeat_max:{group_id}")],
        [InlineKeyboardButton(text=f"🔇 首次禁言: {fmt_duration(b)}", callback_data=f"edit_repeat_ban:{group_id}")],
        [InlineKeyboardButton(text=f"📋 豁免词(白名单) ({n_kw})", callback_data=f"edit_repeat_exempt:{group_id}")],
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
        [InlineKeyboardButton(text=f"⏱ 连续举报冷却: {fmt_duration(cooldown)}", callback_data=f"edit_media_cooldown:{group_id}")],
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
    text = "📋 选择要管理的群组（名称 + ID）："
    kb = await get_group_list_keyboard(callback.bot)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.ChooseGroup)
    await callback.answer()

@router.callback_query(F.data.startswith("select_group:"), F.from_user.id.in_(ADMIN_IDS))
async def select_group(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "✅ 运行中" if cfg.get("enabled", True) else "❌ 已停用"
        text = (
            f"👥 <b>{title}</b>\n"
            f"<code>ID: {group_id}</code>  |  状态: {status}\n\n"
            "选择要管理的功能："
        )
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
    text = "📋 选择要管理的群组（名称 + ID）："
    kb = await get_group_list_keyboard(callback.bot)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.ChooseGroup)
    await callback.answer()

@router.callback_query(F.data.startswith("group_menu:"), F.from_user.id.in_(ADMIN_IDS))
async def group_menu(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "✅ 运行中" if cfg.get("enabled", True) else "❌ 已停用"
        text = (
            f"👥 <b>{title}</b>\n"
            f"<code>ID: {group_id}</code>  |  状态: {status}\n\n"
            "选择要管理的功能："
        )
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        link_status = "✅" if cfg.get("check_bio_link") else "❌"
        kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
        text = f"<b>{title}</b> › 简介检测\n\n链接: {link_status}\n敏感词: {kw_status}"
        kb = get_bio_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("submenu_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def semantic_ad_submenu(callback: CallbackQuery):
    """AD机器学习子菜单."""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        enabled = "✅" if cfg.get("semantic_ad_enabled", False) else "❌"
        text = f"<b>{title}</b> › AD机器学习\n\n当前状态: {enabled}"
        kb = get_semantic_ad_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("toggle_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_semantic_ad(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("semantic_ad_enabled", False)
        cfg["semantic_ad_enabled"] = not current
        await save_config()
        enabled = "✅" if cfg["semantic_ad_enabled"] else "❌"
        await callback.answer(f"AD机器学习: {enabled}", show_alert=True)
        kb = get_semantic_ad_menu_keyboard(group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{title}</b> › AD机器学习\n\n当前状态: {enabled}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("add_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def add_semantic_ad_callback(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        text = (
            f"<b>{title}</b> › AD机器学习 › 增加广告语句\n\n"
            "请发送一条广告样本文本（仅内容部分），我会将其加入广告语义库。\n"
            "发送 /cancel 取消。"
        )
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditSemanticAdAdd)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.message(StateFilter(AdminStates.EditSemanticAdAdd), F.from_user.id.in_(ADMIN_IDS))
async def process_semantic_ad_add(message: Message, state: FSMContext):
    try:
        if not message.text:
            await message.reply("❌ 请输入文本。发送 /cancel 取消。")
            return
        if message.text.strip() == "/cancel":
            data = await state.get_data()
            group_id = data.get("group_id")
            kb = get_semantic_ad_menu_keyboard(group_id)
            await message.reply("已取消。", reply_markup=kb)
            await state.set_state(AdminStates.GroupMenu)
            return
        data = await state.get_data()
        group_id = data.get("group_id")
        kb = get_semantic_ad_menu_keyboard(group_id)
        lines = [ln.strip() for ln in message.text.split("\n") if ln.strip()]
        added_ids = []
        skipped = 0
        for ln in lines:
            sample = semantic_ad_detector.add_ad_sample(ln)
            if sample is None:
                skipped += 1
            else:
                added_ids.append(sample.id)
        if added_ids:
            await message.reply(f"✅ 已添加 {len(added_ids)} 条广告样本，ID: {', '.join(map(str, added_ids))}。", reply_markup=kb)
        if skipped and not added_ids:
            await message.reply("✅ 所有行与现有样本高度相似，已视为重复，未新增。", reply_markup=kb)
        elif skipped:
            await message.reply(f"ℹ️ 其中 {skipped} 行与现有样本高度相似，已跳过。", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ 添加失败: {e}")


@router.callback_query(F.data.startswith("view_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def view_semantic_ad(callback: CallbackQuery):
    try:
        samples = semantic_ad_detector.list_samples()
        if not samples:
            await callback.answer("当前广告语义库为空。", show_alert=True)
            return
        # 仅展示前 20 条，避免过长
        head = samples[-20:]
        lines = [f"{s.id}: {s.text}" for s in head]
        text = "广告语义库（最近 20 条，ID: 文本）：\n\n" + "\n".join(lines)
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ 查看失败: {e}", show_alert=True)


@router.callback_query(F.data.startswith("remove_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def remove_semantic_ad_callback(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        text = (
            f"<b>{title}</b> › AD机器学习 › 减少广告语句\n\n"
            "请发送要删除的广告样本 ID（数字）。可以先点击「广告词库展示」查看 ID。\n"
            "发送 /cancel 取消。"
        )
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditSemanticAdRemove)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.message(StateFilter(AdminStates.EditSemanticAdRemove), F.from_user.id.in_(ADMIN_IDS))
async def process_semantic_ad_remove(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        if not message.text:
            await message.reply("❌ 请输入要删除的样本 ID（数字）。发送 /cancel 取消。")
            return
        if message.text.strip() == "/cancel":
            kb = get_semantic_ad_menu_keyboard(group_id)
            await message.reply("已取消。", reply_markup=kb)
            await state.set_state(AdminStates.GroupMenu)
            return
        kb = get_semantic_ad_menu_keyboard(group_id)
        lines = [ln.strip() for ln in message.text.split("\n") if ln.strip()]
        removed = []
        not_found = []
        invalid = 0
        for ln in lines:
            try:
                sid = int(ln)
            except ValueError:
                invalid += 1
                continue
            ok = semantic_ad_detector.remove_sample(sid)
            if ok:
                removed.append(sid)
            else:
                not_found.append(sid)
        if removed:
            await message.reply(f"✅ 已删除广告样本 ID: {', '.join(map(str, removed))}", reply_markup=kb)
        if not_found:
            await message.reply(f"ℹ️ 未找到样本 ID: {', '.join(map(str, not_found))}", reply_markup=kb)
        if invalid and not removed and not not_found:
            await message.reply("❌ 请输入有效的数字 ID（每行一个）。发送 /cancel 取消。", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ 删除失败: {e}")

@router.callback_query(F.data.startswith("toggle_bio_link:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_bio_link(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_bio_link"] = not cfg.get("check_bio_link", True)
        await save_config()
        status = "✅" if cfg["check_bio_link"] else "❌"
        await callback.answer(f"链接: {status}", show_alert=True)
        kb = get_bio_menu_keyboard(group_id)
        link_status = "✅" if cfg.get("check_bio_link") else "❌"
        kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 简介检测\n\n链接: {link_status}\n敏感词: {kw_status}"
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
        await callback.answer(f"敏感词: {status}", show_alert=True)
        kb = get_bio_menu_keyboard(group_id)
        link_status = "✅" if cfg.get("check_bio_link") else "❌"
        kw_status = "✅" if cfg.get("check_bio_keywords") else "❌"
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 简介检测\n\n链接: {link_status}\n敏感词: {kw_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_bio_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_bio_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        keywords = cfg.get("bio_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"<b>{title}</b> › 编辑简介敏感词\n\n当前列表：\n" + (kw_text if kw_text else "（空）") + "\n\n发送新词（一行一个）会追加到列表，/clear 清空全部"
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
            await save_config()
            kb = get_bio_menu_keyboard(group_id)
            await message.reply("✅ 已清空简介敏感词列表", reply_markup=kb)
        else:
            existing = cfg.get("bio_keywords", []) or []
            if not isinstance(existing, list):
                existing = []
            new_words = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
            added = [w for w in new_words if w not in existing]
            existing.extend(added)
            cfg["bio_keywords"] = existing
            await save_config()
            kb = get_bio_menu_keyboard(group_id)
            await message.reply(f"✅ 已追加 {len(added)} 个词，当前共 {len(existing)} 个", reply_markup=kb)
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "✅" if cfg.get("check_display_keywords") else "❌"
        text = f"<b>{title}</b> › 名称检测: {status}"
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
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 名称检测: {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_display_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_display_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        keywords = cfg.get("display_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"<b>{title}</b> › 编辑名称敏感词\n\n当前列表：\n" + (kw_text if kw_text else "（空）") + "\n\n发送新词（一行一个）会追加到列表，/clear 清空全部"
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
            await save_config()
            kb = get_display_menu_keyboard(group_id)
            await message.reply("✅ 已清空名称敏感词列表", reply_markup=kb)
        else:
            existing = cfg.get("display_keywords", []) or []
            if not isinstance(existing, list):
                existing = []
            new_words = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
            added = [w for w in new_words if w not in existing]
            existing.extend(added)
            cfg["display_keywords"] = existing
            await save_config()
            kb = get_display_menu_keyboard(group_id)
            await message.reply(f"✅ 已追加 {len(added)} 个词，当前共 {len(existing)} 个", reply_markup=kb)
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "✅" if cfg.get("check_message_keywords") else "❌"
        msg_link = "✅" if cfg.get("check_message_link", True) else "❌"
        norm_status = "✅" if cfg.get("message_keyword_normalize", True) else "❌"
        text = f"<b>{title}</b> › 消息检测\n\n敏感词: {status}\n链接/@引流: {msg_link}\n防拼字规避: {norm_status}"
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
        msg_link = "✅" if cfg.get("check_message_link", True) else "❌"
        norm_status = "✅" if cfg.get("message_keyword_normalize", True) else "❌"
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 消息检测\n\n敏感词: {status_display}\n链接/@引流: {msg_link}\n防拼字规避: {norm_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_message_link:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_message_link(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_message_link"] = not cfg.get("check_message_link", True)
        await save_config()
        status = "✅" if cfg["check_message_link"] else "❌"
        await callback.answer(f"链接/@引流: {status}", show_alert=True)
        kb = get_message_menu_keyboard(group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        status_display = "✅" if cfg.get("check_message_keywords") else "❌"
        msg_link = "✅" if cfg.get("check_message_link", True) else "❌"
        norm_status = "✅" if cfg.get("message_keyword_normalize", True) else "❌"
        text = f"<b>{title}</b> › 消息检测\n\n敏感词: {status_display}\n链接/@引流: {msg_link}\n防拼字规避: {norm_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_message_normalize:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_message_normalize(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["message_keyword_normalize"] = not cfg.get("message_keyword_normalize", True)
        await save_config()
        status = "✅" if cfg["message_keyword_normalize"] else "❌"
        await callback.answer(f"防拼字规避: {status}", show_alert=True)
        kb = get_message_menu_keyboard(group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        status_display = "✅" if cfg.get("check_message_keywords") else "❌"
        msg_link = "✅" if cfg.get("check_message_link", True) else "❌"
        norm_status = "✅" if cfg.get("message_keyword_normalize", True) else "❌"
        text = f"<b>{title}</b> › 消息检测\n\n敏感词: {status_display}\n链接/@引流: {msg_link}\n防拼字规避: {norm_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_message_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_message_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        keywords = cfg.get("message_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"<b>{title}</b> › 编辑消息敏感词\n\n当前列表：\n" + (kw_text if kw_text else "（空）") + "\n\n发送新词（一行一个）会追加到列表，/clear 清空全部"
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
            await save_config()
            kb = get_message_menu_keyboard(group_id)
            await message.reply("✅ 已清空消息敏感词列表", reply_markup=kb)
        else:
            existing = cfg.get("message_keywords", []) or []
            if not isinstance(existing, list):
                existing = []
            new_words = [x.strip().lower() for x in message.text.strip().split("\n") if x.strip()]
            added = [w for w in new_words if w not in existing]
            existing.extend(added)
            cfg["message_keywords"] = existing
            await save_config()
            kb = get_message_menu_keyboard(group_id)
            await message.reply(f"✅ 已追加 {len(added)} 个词，当前共 {len(existing)} 个", reply_markup=kb)
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        short_enabled = "✅" if cfg.get("short_msg_detection") else "❌"
        fill_enabled = "✅" if cfg.get("fill_garbage_detection") else "❌"
        th = cfg.get("short_msg_threshold", 3)
        n = cfg.get("min_consecutive_count", 2)
        w = cfg.get("time_window_seconds", 60)
        rule = f"连续{n}条字数≤{th}在{w}秒内即触发（防「点」「我」「头」「像」式连发）"
        text = f"<b>{title}</b> › 短消息/垃圾\n\n短消息: {short_enabled}\n规则: {rule}\n\n垃圾: {fill_enabled}"
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
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 短消息/垃圾\n\n短消息: {short_enabled}\n垃圾: {fill_enabled}"
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
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 短消息/垃圾\n\n短消息: {short_enabled}\n垃圾: {fill_enabled}"
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        mute_hours = cfg.get("violation_mute_hours", 1)
        mute_sec = mute_hours * 3600
        threshold = cfg.get("reported_message_threshold", 2)
        text = f"<b>{title}</b> › 违规处理\n\n🔇 禁言: {fmt_duration(mute_sec)}\n触发: {threshold} 条举报"
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
        old_h = cfg.get("violation_mute_hours", 1)
        value = int(message.text.strip())
        cfg["violation_mute_hours"] = value
        await save_config()
        title = await get_chat_title_safe(message.bot, group_id)
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新: <b>{title}</b> › 违规处理\n禁言时长: {old_h}h → {value}h", reply_markup=kb)
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        w = cfg.get("repeat_window_seconds", 7200)
        m = cfg.get("repeat_max_count", 3)
        b = cfg.get("repeat_ban_seconds", 86400)
        kw = cfg.get("repeat_exempt_keywords", []) or []
        n_kw = len(kw) if isinstance(kw, list) else 0
        text = f"<b>{title}</b> › 重复发言\n\n⏱ 窗口: {fmt_duration(w)}\n触发: {m} 次\n🔇 首次禁言: {fmt_duration(b)}\n📋 豁免词: {n_kw} 个"
        kb = get_repeat_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_repeat_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_exempt(callback: CallbackQuery, state: FSMContext):
    """编辑重复发言豁免词（含任一词的消息不触发重复检测）"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        kw = cfg.get("repeat_exempt_keywords", []) or []
        if not isinstance(kw, list):
            kw = []
        text = "编辑重复发言豁免词（白名单）\n含任一词的消息不触发重复检测。\n\n当前列表：\n" + ("\n".join(kw) if kw else "（空）") + "\n\n发送新词（一行一个）会追加到列表，/clear 清空全部"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatExemptKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatExemptKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_exempt(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        if message.text and message.text.strip() == "/clear":
            cfg["repeat_exempt_keywords"] = []
            await save_config()
            await message.reply("✅ 已清空豁免词列表", reply_markup=get_repeat_menu_keyboard(group_id))
        else:
            existing = cfg.get("repeat_exempt_keywords", []) or []
            if not isinstance(existing, list):
                existing = []
            new_words = [x.strip() for x in (message.text or "").strip().splitlines() if x.strip()]
            added = [w for w in new_words if w not in existing]
            existing.extend(added)
            cfg["repeat_exempt_keywords"] = existing
            await save_config()
            await message.reply(f"✅ 已追加 {len(added)} 个词，当前共 {len(existing)} 个豁免词", reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        msg = cfg.get("media_unlock_msg_count", 50)
        boost = cfg.get("media_unlock_boosts", 4)
        text = f"<b>{title}</b> › 媒体权限\n\n解锁所需消息: {msg}\n解锁所需助力: {boost}"
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
        title = await get_chat_title_safe(callback.bot, group_id)
        msg = cfg.get("media_unlock_msg_count", 50)
        boost = cfg.get("media_unlock_boosts", 4)
        text = f"<b>{title}</b> › 媒体权限\n\n解锁所需消息: {msg}\n解锁所需助力: {boost}"
        await callback.message.edit_text(text, reply_markup=get_media_perm_menu_keyboard(group_id))
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        wl = cfg.get("media_unlock_whitelist", [])
        if not isinstance(wl, list):
            wl = []
        text = f"<b>{title}</b> › 媒体解锁白名单\n\n用户ID或用户名，满足即无需消息/助力可发媒体。\n当前：" + (", ".join(str(x) for x in wl) if wl else "（空）")
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
        title = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{title}</b> › 媒体解锁白名单\n\n当前：" + (", ".join(str(x) for x in wl) if wl else "（空）")
        await callback.message.edit_text(text, reply_markup=get_media_whitelist_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 媒体举报 ====================
@router.callback_query(F.data.startswith("submenu_media_report:"), F.from_user.id.in_(ADMIN_IDS))
async def media_report_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
        max_day = cfg.get("media_report_max_per_day", 3)
        del_th = cfg.get("media_report_delete_threshold", 3)
        text = f"<b>{title}</b> › 媒体举报\n\n⏱ 连续举报冷却: {fmt_duration(cooldown)}\n每日上限: {max_day} 次\n举报达 {del_th} 人删媒体"
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        ar = cfg.get("autoreply", {})
        enabled = "✅" if ar.get("enabled") else "❌"
        kw_count = len(ar.get("keywords", []))
        text = f"<b>{title}</b> › 自动回复\n\n状态: {enabled}\n关键词: {kw_count} 个"
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
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> › 自动回复\n\n状态: {enabled}\n关键词: {kw_count} 个"
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
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "✅ 运行中" if cfg.get("enabled") else "❌ 已停用"
        text = f"<b>{title}</b> › 基础设置\n\n<code>ID: {group_id}</code>\n状态: {status}"
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
        status_display = "✅ 运行中" if cfg.get("enabled") else "❌ 已停用"
        title = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{title}</b> › 基础设置\n\n<code>ID: {group_id}</code>\n状态: {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("submenu_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def exempt_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        exempt = cfg.get("exempt_users") or []
        if isinstance(exempt, dict):
            exempt = list(exempt.keys())
        text = f"<b>{title}</b> › 豁免检测（简介/昵称等，与发图白名单无关）\n\n当前: " + (", ".join(str(x) for x in exempt) if exempt else "（无）")
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
        text = f"编辑豁免检测用户（用户ID，一行一个；豁免简介/昵称等检测，发图另有白名单）\n\n当前列表：\n" + ("\n".join(str(x) for x in exempt) if exempt else "（空）") + "\n\n发送新用户ID（一行一个）会追加到列表，/clear 清空全部"
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
            await save_config()
            await message.reply("✅ 已清空豁免用户列表", reply_markup=get_exempt_menu_keyboard(group_id))
        else:
            existing = cfg.get("exempt_users", []) or []
            if not isinstance(existing, list):
                existing = []
            new_users = [x.strip() for x in message.text.strip().splitlines() if x.strip()]
            added = [u for u in new_users if u not in existing]
            existing.extend(added)
            cfg["exempt_users"] = existing
            await save_config()
            await message.reply(f"✅ 已追加 {len(added)} 人，当前共 {len(existing)} 人", reply_markup=get_exempt_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

# ==================== 状态查看 ====================
async def _build_status_text(bot) -> str:
    """生成状态页完整文案"""
    admin_count = len(ADMIN_IDS)
    async with lock:
        report_count = len(reports)
    lines = [
        "📊 <b>系统状态</b>\n",
        "<b>群组概览</b>:",
    ]
    for gid in sorted(GROUP_IDS):
        title = await get_chat_title_safe(bot, gid)
        cfg = get_group_config(gid)
        status = "✅ 运行中" if cfg.get("enabled", True) else "❌ 已停用"
        lines.append(f"├ {title} (<code>{gid}</code>)  {status}")
    lines.append("")
    lines.append("<b>数据统计</b>:")
    lines.append(f"├ 进行中举报: {report_count} 条")
    try:
        uv_total = len(user_violations) if user_violations else 0
        lines.append(f"├ 违规用户记录: {uv_total} 条")
    except Exception:
        lines.append("├ 违规用户记录: —")
    lines.append("")
    lines.append("<b>系统</b>: ✅ 运行正常  |  管理员: %d" % admin_count)
    return "\n".join(lines)


@router.callback_query(F.data == "view_status", F.from_user.id.in_(ADMIN_IDS))
async def view_status(callback: CallbackQuery):
    try:
        text = await _build_status_text(callback.bot)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 刷新", callback_data="view_status")],
            [InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="back_main")],
        ])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 检测和回复核心逻辑 ====================
FILL_CHARS = set(r" .,，。！？*\\\~`-_=+[]{}()\"'\\|\n\t\r　")
# 防拼字规避：去掉空格、常见标点后匹配敏感词（如 A  bc，D 命中 abcd）
KEYWORD_NORMALIZE_CHARS = set(" .,，。！？、；：\"'（）【】\n\t\r　*_~`-+=|")

def _normalize_for_keyword(text: str) -> str:
    """去掉空格和常见标点、转小写，用于防拼字规避匹配"""
    if not text:
        return ""
    return "".join(c for c in text.lower() if c not in KEYWORD_NORMALIZE_CHARS)

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
    exempt_kw = cfg.get("repeat_exempt_keywords", []) or []
    if isinstance(exempt_kw, list) and exempt_kw:
        text_lower = (message.text or "").lower()
        if any((k or "").strip().lower() in text_lower for k in exempt_kw if k):
            return False
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
            f"⚠️ 检测到你在 {window_sec // 3600} 小时内重复发送相同内容（2/{max_count}），请调整文字内容。"
        )
        try:
            w = await message.reply(warn_text)
            repeat_warning_msg_id[(group_id, user_id)] = w.message_id
            _track_group_reply(message, w)
        except Exception:
            pass
        return False

    if count >= max_count:
        level_key = (group_id, user_id)
        current_level = repeat_violation_level.get(level_key, 0)
        display_name = _get_display_name_from_message(message, user_id)
        try:
            await message.delete()
        except TelegramBadRequest:
            wid = repeat_warning_msg_id.pop((group_id, user_id), None)
            if wid:
                try:
                    await bot.delete_message(group_id, wid)
                except Exception:
                    pass

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
            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：在配置时间窗口内多次重复发送相同内容（{max_count}/{max_count}）。\n"
                f"🔒 处理结果：因刷屏已被本群禁言 1 天。\n{MISJUDGE_BOT_MENTION}"
            )
            try:
                await bot.send_message(group_id, notice)
            except Exception:
                pass
            return True

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
            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：多次在 2 小时内重复发送相同内容，且在被解禁后仍然继续违规。\n"
                f"🔒 处理结果：已被本群永久禁止发言。{MISJUDGE_BOT_MENTION}"
            )
            try:
                await bot.send_message(group_id, notice)
            except Exception:
                pass
            return True

    return False

def _report_key(gid: int, mid: int) -> tuple:
    return (gid, mid)

def _report_key_str(gid: int, mid: int) -> str:
    return f"{gid}_{mid}"

async def load_data():
    global reports
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.items():
                    v["reporters"] = set(v.get("reporters", []))
                    if "timestamp" not in v:
                        v["timestamp"] = time.time()
                    parts = k.split("_", 1)
                    if len(parts) == 2:
                        try:
                            reports[(int(parts[0]), int(parts[1]))] = v
                        except ValueError:
                            pass
    except Exception as e:
        print("数据加载失败（首次正常）:", e)

async def save_data():
    async with lock:
        try:
            data_to_save = {
                _report_key_str(k[0], k[1]): {**v, "reporters": list(v["reporters"]), "timestamp": v.get("timestamp", time.time())}
                for k, v in reports.items()
            }
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("保存失败:", e)

def count_user_reported_messages(user_id: int, group_id: int) -> int:
    """仅统计被非管理员举报的消息条数"""
    key = f"{group_id}_{user_id}"
    user_vio = user_violations.get(key, {})
    count = 0
    for v in user_vio.values():
        reporters = v.get("reporters") or []
        if isinstance(reporters, set):
            reporters = list(reporters)
        if reporters and any(r not in ADMIN_IDS for r in reporters):
            count += 1
        elif not reporters and v.get("reported"):
            count += 1
    return count

def build_warning_buttons(group_id: int, msg_id: int, report_count: int):
    """构建警告消息按钮；callback 带 group_id 避免多群串案；举报按钮显示当前人数"""
    report_text = f"举报 ({report_count}人)" if report_count > 0 else "举报"
    buttons = [
        [
            InlineKeyboardButton(text=report_text, callback_data=f"report:{group_id}:{msg_id}"),
            InlineKeyboardButton(text="误判👮‍♂️", callback_data=f"exempt:{group_id}:{msg_id}")
        ]
    ]
    if report_count > 0:
        buttons.append([
            InlineKeyboardButton(text="禁24h👮‍♂️", callback_data=f"ban24h:{group_id}:{msg_id}"),
            InlineKeyboardButton(text="永封👮‍♂️", callback_data=f"banperm:{group_id}:{msg_id}")
        ])
    # 管理员标记广告并删除：学习广告样本 + 删除该用户近期全部消息
    buttons.append([
        InlineKeyboardButton(text="标记广告并删除👮‍♂️", callback_data=f"markad:{group_id}:{msg_id}")
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

async def _delete_user_recent_and_warnings(group_id: int, user_id: int, orig_msg_id: int | None, keep_one_text: str = "", auto_delete_sec: int = 0):
    """删除该用户最近 24 小时内消息、机器人对其的警告，仅保留一条最终公告（带误封联系）。
    auto_delete_sec > 0 时，公告消息在指定秒数后自动删除。"""
    key = (group_id, user_id)
    now = time.time()
    cutoff = now - USER_MSG_24H_SEC
    if key in user_recent_message_ids:
        for msg_id, t, txt in list(user_recent_message_ids[key]):
            if t >= cutoff:
                # 被机器人删除的消息也作为广告样本学习
                if txt:
                    try:
                        semantic_ad_detector.add_ad_sample(txt)
                    except Exception as e:
                        print(f"删除用户消息时学习样本失败: {e}")
                try:
                    await bot.delete_message(group_id, msg_id)
                except Exception:
                    pass
    to_remove = []
    async with lock:
        for (gid, mid), data in list(reports.items()):
            if gid == group_id and data.get("suspect_id") == user_id:
                try:
                    await bot.delete_message(group_id, data["warning_id"])
                except Exception:
                    pass
                to_remove.append((gid, mid))
        for k in to_remove:
            reports.pop(k, None)
    await save_data()
    if orig_msg_id:
        try:
            await bot.delete_message(group_id, orig_msg_id)
        except Exception:
            pass
    if keep_one_text:
        try:
            sent = await bot.send_message(group_id, keep_one_text)
            if auto_delete_sec > 0:
                async def _auto_del(cid: int, mid: int):
                    await asyncio.sleep(auto_delete_sec)
                    try:
                        await bot.delete_message(cid, mid)
                    except Exception:
                        pass
                asyncio.create_task(_auto_del(group_id, sent.message_id))
        except Exception:
            pass

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
    """媒体消息：先检外部引用；无权限则删除并提示；有权限则回复举报/点赞按钮"""
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
        try:
            await message.delete()
        except Exception:
            pass
        need_msg = cfg.get("media_unlock_msg_count", 50)
        need_boosts = cfg.get("media_unlock_boosts", 4)
        key = _media_key(group_id, user_id)
        count = media_stats["message_counts"].get(key, 0)
        boosts = media_stats["boosts"].get(key, 0)
        name = _get_display_name_from_message(message, user_id)
        sk = (group_id, user_id)
        # 计算连续无权限发媒体次数（超过一定时间未再触发则重置）
        strike_count, last_ts = media_no_perm_strikes.get(sk, (0, 0))
        if now - last_ts > MEDIA_NO_PERM_STRIKE_RESET_SEC:
            strike_count = 0
        strike_count += 1
        media_no_perm_strikes[sk] = (strike_count, now)

        if strike_count >= 2:
            # 连续两次以上触发无权限发媒体，直接关闭其发媒体权限，防止继续刷屏
            prev_msg_id = last_media_no_perm_msg.pop(sk, None)
            if prev_msg_id is not None:
                try:
                    await bot.delete_message(group_id, prev_msg_id)
                except Exception:
                    pass
            try:
                await bot.restrict_chat_member(
                    chat_id=group_id,
                    user_id=user_id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=False,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True,
                        can_change_info=False,
                        can_invite_users=True,
                        can_pin_messages=False,
                    ),
                )
            except Exception as e:
                print(f"关闭媒体权限失败: {e}")
            return

        prev_msg_id = last_media_no_perm_msg.get(sk)
        if prev_msg_id is not None:
            try:
                await bot.delete_message(group_id, prev_msg_id)
            except Exception:
                pass
        sent = await bot.send_message(
            group_id,
            f"⚠️ {name} 尚未解锁发媒体。\n"
            f"📊 您的进度：发送合规消息 {count}/{need_msg}，助力 {boosts}/{need_boosts}（满其一即可解锁）。\n"
            f"输入「权限」查进度，输入「召唤」使用机器人代发图。"
        )
        last_media_no_perm_msg[sk] = sent.message_id
        if prev_msg_id is None:
            async def _delete_after():
                await asyncio.sleep(MEDIA_NO_PERM_DELETE_AFTER_SEC)
                try:
                    await bot.delete_message(group_id, sent.message_id)
                except Exception:
                    pass
                if last_media_no_perm_msg.get(sk) == sent.message_id:
                    last_media_no_perm_msg.pop(sk, None)
            asyncio.create_task(_delete_after())
        return
    reply = await message.reply("📎 媒体消息", reply_markup=_media_reply_buttons(group_id, message.message_id, 0, 0))
    _track_group_reply(message, reply)
    async with media_reports_lock:
        media_reports[(group_id, message.message_id)] = {
            "chat_id": group_id,
            "media_msg_id": message.message_id,
            "reply_msg_id": reply.message_id,
            "reporters": set(),
            "likes": set(),
            "deleted": False,
        }

def _track_user_message(group_id: int, user_id: int, msg_id: int, text: str = ""):
    """记录用户消息 id 和文本，用于 24 小时内可删并可学习"""
    key = (group_id, user_id)
    if key not in user_recent_message_ids:
        user_recent_message_ids[key] = deque(maxlen=USER_MSG_TRACK_MAXLEN)
    user_recent_message_ids[key].append((msg_id, time.time(), text or ""))


def _track_bot_message(group_id: int, msg_id: int, auto_delete_sec: int = BOT_MSG_AUTO_DELETE_SEC):
    """跟踪机器人发送的消息，安排自动删除"""
    bot_sent_messages[(group_id, msg_id)] = time.time()
    
    async def _auto_delete():
        await asyncio.sleep(auto_delete_sec)
        try:
            await bot.delete_message(group_id, msg_id)
        except Exception:
            pass
        bot_sent_messages.pop((group_id, msg_id), None)
    
    asyncio.create_task(_auto_delete())


def _track_group_reply(message: Message, reply: Message):
    """仅记录在目标群里的引用回复，后续做补偿删除"""
    try:
        chat = message.chat
        if not chat or chat.id not in GROUP_IDS:
            return
        bot_reply_links[(chat.id, reply.message_id)] = (message.message_id, time.time())
    except Exception:
        pass


def _should_send_warning(group_id: int, user_id: int) -> bool:
    """检查是否应该为该用户发送新警告（防止刷屏）"""
    key = (group_id, user_id)
    now = time.time()
    last = user_last_warning.get(key)
    if last:
        last_time, last_msg_id = last
        if now - last_time < USER_WARNING_COOLDOWN_SEC:
            return False
    return True


def _record_warning_sent(group_id: int, user_id: int, msg_id: int):
    """记录已发送的警告"""
    user_last_warning[(group_id, user_id)] = (time.time(), msg_id)


def _add_banned_warning(group_id: int, warning_msg_id: int):
    """添加已封禁警告消息到列表"""
    if group_id not in banned_warning_messages:
        banned_warning_messages[group_id] = []
    if warning_msg_id not in banned_warning_messages[group_id]:
        banned_warning_messages[group_id].append(warning_msg_id)


async def _delete_all_banned_warnings(group_id: int):
    """删除该群所有已封禁的警告消息"""
    if group_id not in banned_warning_messages:
        return
    for msg_id in banned_warning_messages[group_id]:
        try:
            await bot.delete_message(group_id, msg_id)
        except Exception:
            pass
    banned_warning_messages[group_id] = []

def _message_has_link_or_external_at(text: str) -> bool:
    """文本引流：包含链接 或 @外部用户（@ 且非仅 @trump2028_bot）"""
    if not text:
        return False
    has_link = any(x in text for x in ["http://", "https://", "t.me/"])
    mentions = re.findall(r"@(\w+)", text)
    has_external_at = bool(mentions) and any(m.lower() != "trump2028_bot" for m in mentions)
    return has_link or has_external_at


def _has_external_reference(message: Message) -> bool:
    """外部引用：A. 消息为转发 或 B. 回复了转发消息"""
    if getattr(message, "forward_origin", None) is not None:
        return True
    if getattr(message, "forward_from", None) is not None:
        return True
    if getattr(message, "forward_from_chat", None) is not None:
        return True
    if getattr(message, "forward_sender_name", None) is not None:
        return True
    reply = getattr(message, "reply_to_message", None)
    if not reply:
        return False
    if getattr(reply, "forward_origin", None) is not None:
        return True
    if getattr(reply, "forward_from", None) is not None:
        return True
    if getattr(reply, "forward_from_chat", None) is not None:
        return True
    if getattr(reply, "forward_sender_name", None) is not None:
        return True
    return False

@router.message(F.chat.id.in_(GROUP_IDS), F.text)
async def detect_and_warn(message: Message):
    """发言时检测并发送警告。顺序：豁免 -> 召唤(无操作) -> 权限 -> 举报阈值 -> 多层 -> 5.1 -> 5.3 -> 重复；合规仅当 triggers<=1 且无处罚。"""
    if not message.from_user or message.from_user.is_bot:
        return
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    text = message.text or ""
    _track_user_message(group_id, user_id, message.message_id, text)

    # 语义广告检测（优先级最高；仅文本消息，白名单用户/词汇跳过；命中后直接删除不做提醒）
    if cfg.get("semantic_ad_enabled", False) and len((message.text or "").strip()) >= 4:
        exempt_users = cfg.get("exempt_users") or []
        if isinstance(exempt_users, list) and str(user_id) in exempt_users:
            is_semantic_ad = False
        else:
            wl_words = cfg.get("repeat_exempt_keywords") or []
            if any(w and w in text for w in wl_words):
                is_semantic_ad = False
            else:
                is_semantic_ad, sim, _ = semantic_ad_detector.check_text(text)
        if is_semantic_ad:
            try:
                await message.delete()
            except Exception:
                pass
            return

    # 误判豁免：仅不做多层内容检测，举报阈值/外部引用/5.1/重复等检测仍执行
    misjudge_wl = cfg.get("misjudge_whitelist") or []
    misjudge_exempt = isinstance(misjudge_wl, list) and str(user_id) in misjudge_wl

    mild_wl = cfg.get("mild_exempt_whitelist") or []
    mild_exempt = isinstance(mild_wl, list) and str(user_id) in mild_wl

    # 「召唤」：本机器人不做任何动作，由群内其他机器人处理
    if message.text and message.text.strip() == "召唤":
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
            await message.reply(f"✅ 已解锁发媒体（发送合规消息已满 {need_msg} 条）。")
            return
        if boosts >= need_boosts:
            await message.reply(f"✅ 已解锁发媒体（已助力 {boosts} 次）。")
            return
        await message.reply(
            f"📊 发媒体进度\n"
            f"· 发送合规消息：{count}/{need_msg}\n"
            f"· 群组助力：{boosts}/{need_boosts}\n"
            f"（刷屏/重复/短消息不计入）"
        )
        return

    # 举报阈值禁言（在多层检测前）：仅统计非管理员举报，N 条则禁言 min(N,72) 小时
    reported_count = count_user_reported_messages(user_id, group_id)
    threshold = cfg.get("reported_message_threshold", 3)
    if reported_count >= threshold:
        try:
            mute_hours = min(reported_count, REPORT_BAN_HOURS_CAP)
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
            try:
                await message.delete()
            except Exception:
                pass
            display_name = _get_display_name_from_message(message, user_id)
            await bot.send_message(
                group_id,
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：在本群多次垃圾消息被其他成员举报（累计 {reported_count} 条）。\n"
                f"🔒 处理结果：已被限制发言 {mute_hours} 小时。\n"
                f"{MISJUDGE_BOT_MENTION}"
            )
            return
        except Exception as e:
            print(f"举报阈值禁言失败: {e}")

    # 多层内容检测：误判豁免仅跳过「简介链接、简介词汇、昵称词汇、主页频道」，其余项所有人均检
    triggers = []
    if not misjudge_exempt:
        bio_exempt = False
        try:
            if cfg.get("check_bio_link", True) or cfg.get("check_bio_keywords", True):
                chat_info = await bot.get_chat(user_id)
                bio = (chat_info.bio or "").strip()
                bio_lower = bio.lower()
                # 局部豁免：如果简介包含 t.me/fast_telegram，则本用户本次消息跳过「简介链接」和「简介词汇」检测
                fast_telegram_bio_exempt = "t.me/fast_telegram" in bio_lower
                # 统计所有引流链接形式（http/https/t.me/@），数量>=2 时不适用「bot+双向」豁免
                bio_ref_count = len(re.findall(r"https?://[^\s]+|t\.me/[^\s]+|@\w+", bio_lower))
                if "双向" in bio and "bot" in bio_lower and bio_ref_count < 2:
                    bio_exempt = True
                if not bio_exempt:
                    if (
                        not fast_telegram_bio_exempt
                        and cfg.get("check_bio_link", True)
                        and any(x in bio_lower for x in ["http://", "https://", "t.me/", "@"])
                    ):
                        triggers.append("简介链接")
                    if (
                        not fast_telegram_bio_exempt
                        and cfg.get("check_bio_keywords", True)
                        and any(kw.lower() in bio_lower for kw in cfg.get("bio_keywords", []))
                    ):
                        triggers.append("简介词汇")
                    # 检测用户主页是否有频道（personal_chat 属性）
                    personal_chat = getattr(chat_info, "personal_chat", None)
                    if personal_chat:
                        # 用户主页设置了个人频道，视为潜在引流
                        triggers.append("主页频道")
        except Exception:
            pass

        # 3. 名称敏感词（bio_exempt 时一并跳过）
        if not bio_exempt and cfg.get("check_display_keywords", True):
            display_name = (message.from_user.full_name or "").lower()
            if any(kw.lower() in display_name for kw in cfg.get("display_keywords", [])):
                triggers.append("昵称词汇")
    
    # 4~6 功能（消息敏感词 / 连续短消息 / 垃圾填充）暂时下线，不再参与触发
    
    # 消息链接/@引流：误判白名单也检，以便 5.1/5.3 仍执行
    if cfg.get("check_message_link", True) and message.text and _message_has_link_or_external_at(message.text):
        triggers.append("消息链接/@引流")
    
    # 5.1 消息链接/@引流即时动作：首次禁言1小时，第二次永封；封禁消息10秒后自动删除
    if "消息链接/@引流" in triggers:
        display_name = _get_display_name_from_message(message, user_id)
        try:
            await message.delete()
        except Exception:
            pass
        # 发送引流提示（10秒后自动删除）
        try:
            sent = await bot.send_message(group_id, f"用户 {display_name} 违规引流。")
            _track_bot_message(group_id, sent.message_id, 10)
        except Exception:
            pass
        level_key = (group_id, user_id)
        level = message_link_level.get(level_key, 0)
        try:
            if level == 0:
                until_date = int(time.time()) + 3600
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
                message_link_level[level_key] = 1
            else:
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
                await _delete_user_recent_and_warnings(group_id, user_id, None, keep_one_text=
                    f"🚫 用户 {display_name}\n📌 触发原因：{'+'.join(triggers)}\n🔒 处理结果：已被本群永久限制发言。\n{MISJUDGE_BOT_MENTION}",
                    auto_delete_sec=10)
            await save_link_ref_levels()
        except Exception as e:
            print(f"消息链接处罚失败: {e}")
        return

    # 5.2 统一警告（非仅引流时）- 同用户连续触发只发一条警告（防刷屏）
    if len(triggers) > 0:
        reason = "+".join(triggers)
        display_name = _get_display_name_from_message(message, user_id)
        
        # 检查是否应该发送警告（同用户60秒内只发一条）
        should_warn = _should_send_warning(group_id, user_id)
        
        if should_warn:
            warning_text = (
                f"🚨 检测到 👤 用户 {display_name} 疑似广告，包含 {reason} 内容。\n"
                f"⚠️ 警惕该用户，可点举报或由管理员标记。"
            )
            try:
                kb = build_warning_buttons(group_id, message.message_id, 0)
                warning = await message.reply(warning_text, reply_markup=kb)
                _track_group_reply(message, warning)
                rk = _report_key(group_id, message.message_id)
                async with lock:
                    reports[rk] = {
                        "warning_id": warning.message_id,
                        "suspect_id": user_id,
                        "chat_id": group_id,
                        "reporters": set(),
                        "reason": reason,
                        "trigger_count": len(triggers),
                        "suspect_name": display_name,
                        "original_message_id": message.message_id,
                        "original_text": message.text or "",
                        "timestamp": time.time(),
                    }
                await save_data()
                _record_warning_sent(group_id, user_id, warning.message_id)
                # 机器人警告消息24小时后自动删除
                _track_bot_message(group_id, warning.message_id)
            except Exception as e:
                print(f"发送警告失败: {e}")
        else:
            # 同用户连续触发，不发新警告，但仍记录到reports
            rk = _report_key(group_id, message.message_id)
            async with lock:
                reports[rk] = {
                    "warning_id": 0,  # 无警告消息
                    "suspect_id": user_id,
                    "chat_id": group_id,
                    "reporters": set(),
                    "reason": reason,
                    "trigger_count": len(triggers),
                    "suspect_name": display_name,
                    "original_message_id": message.message_id,
                    "original_text": message.text or "",
                    "timestamp": time.time(),
                }
            await save_data()

    # 5.3 按触发层数处理
    if len(triggers) >= 3:
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
            await _delete_user_recent_and_warnings(group_id, user_id, message.message_id, keep_one_text=
                f"🚫 用户 {display_name}\n📌 触发原因：{reason}\n🔒 处理结果：已被本群永久限制发言。\n{MISJUDGE_BOT_MENTION}",
                auto_delete_sec=10)
        except Exception as e:
            print(f"自动封禁失败: {e}")
    elif len(triggers) <= 2 and len(triggers) > 0 and not mild_exempt:
        mild_key = (group_id, user_id)
        entries = mild_trigger_entries.get(mild_key, [])
        rk = _report_key(group_id, message.message_id)
        warning_id = reports.get(rk, {}).get("warning_id") if rk in reports else None
        if warning_id:
            entries = (entries + [(message.message_id, warning_id)])[-3:]
        mild_trigger_entries[mild_key] = entries
        if len(entries) >= 3:
            link = _message_link(group_id, entries[2][0])
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        f"⚠️ 用户 {user_id} 已第三次触发轻度警告。\n定位: {link}",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[
                                InlineKeyboardButton(text="定位到消息", url=link),
                                InlineKeyboardButton(text="豁免轻度", callback_data=f"mild_exempt:{group_id}:{user_id}")
                            ]]
                        ),
                    )
                except Exception:
                    pass
            mild_trigger_entries[mild_key] = [entries[2]]

    # 重复发言检测（多层之后执行）
    if await handle_repeat_message(message):
        return


@router.message(Command(commands=["ad", "AD", "Ad"]), F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def cmd_mark_ad(message: Message):
    """管理员命令：/ad，回复一条广告消息，学习并删除该用户最近消息。"""
    try:
        target = message.reply_to_message
        if not target or not target.from_user or target.from_user.is_bot:
            await message.reply("请回复真实用户的广告消息使用 /ad。")
            return
        group_id = message.chat.id
        user_id = target.from_user.id
        text = target.text or target.caption or ""
        if text:
            try:
                semantic_ad_detector.add_ad_sample(text)
            except Exception as e:
                print(f"/ad 学习广告样本失败: {e}")
        try:
            await _delete_user_recent_and_warnings(group_id, user_id, target.message_id)
        except Exception as e:
            print(f"/ad 删除用户消息失败: {e}")
        await message.reply("✅ 已学习并删除该用户近期发言。")
    except Exception as e:
        print("/ad 命令异常:", e)
        await message.reply("❌ 失败", reply_markup=ReplyKeyboardRemove())


@router.message((F.forward_from | F.forward_from_chat), F.from_user.id.in_(ADMIN_IDS))
async def on_forward_learn_ad(message: Message):
    """
    管理员转发用户消息给机器人：
    1) 学习该条广告文本
    2) 根据原始群ID和用户ID，删除其最近24小时内的全部消息和警告，并学习这些文本
    """
    try:
        f_user = getattr(message, "forward_from", None)
        f_chat = getattr(message, "forward_from_chat", None)
        if not f_user or not f_chat:
            # 无法同时获取原用户与原群，放弃处理，避免误删
            return

        group_id = f_chat.id
        user_id = f_user.id
        # 仅处理配置中的受控群
        if group_id not in GROUP_IDS:
            return

        text = message.text or message.caption or ""
        if text:
            try:
                semantic_ad_detector.add_ad_sample(text)
            except Exception as e:
                print(f"转发学习广告样本失败: {e}")

        try:
            await _delete_user_recent_and_warnings(group_id, user_id, orig_msg_id=None)
        except Exception as e:
            print(f"转发学习时删除用户消息失败: {e}")
    except Exception as e:
        print("转发学习命令异常:", e)


@router.message(F.chat.id.in_(GROUP_IDS), F.left_chat_member)
async def on_member_left(message: Message):
    """成员退群：删除其在本群的最近消息和全部警告"""
    try:
        if not message.left_chat_member or message.left_chat_member.is_bot:
            return
        group_id = message.chat.id
        user_id = message.left_chat_member.id
        # 利用已有工具函数：删除最近24小时内消息 + 所有警告记录
        await _delete_user_recent_and_warnings(group_id, user_id, orig_msg_id=None)
    except Exception as e:
        print(f"处理退群用户消息清理失败: {e}")

    # 合规消息：仅当 triggers<=1 且本条未受任何处罚时计入
    if len(triggers) <= 1:
        await _try_count_media_and_notify(message, group_id, user_id, cfg)

# 其他内容类型（贴纸/文件/动画等）：仅做外部引用检测，与文本/媒体一致处理
_OTHER_CONTENT = {
    ContentType.STICKER,
    ContentType.DOCUMENT,
    ContentType.ANIMATION,
    ContentType.AUDIO,
    ContentType.LOCATION,
    ContentType.CONTACT,
    ContentType.DICE,
    ContentType.POLL,
    ContentType.VENUE,
    ContentType.GAME,
}


# 外部引用检测已移除，交由其他机器人处理


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
    """举报处理；仅非管理员举报计入历史阈值；优化响应速度"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("已过期")
            return
        group_id = int(parts[1])
        msg_id = int(parts[2])
        reporter_id = callback.from_user.id
        rk = _report_key(group_id, msg_id)
        async with lock:
            if rk not in reports:
                await callback.answer("已过期")
                return
            data = reports[rk]
            if reporter_id in data["reporters"]:
                await callback.answer("已举报过")
                return
            data["reporters"].add(reporter_id)
            count = len(data["reporters"])
            user_id = data["suspect_id"]
            warning_id = data["warning_id"]
            reason = data["reason"]
        
        # 尽早返回响应，后续操作不阻塞用户
        await callback.answer(f"✅ 举报({count}人)")
        
        # 后台保存
        key = f"{group_id}_{user_id}"
        if key not in user_violations:
            user_violations[key] = {}
        if str(msg_id) not in user_violations[key]:
            user_violations[key][str(msg_id)] = {"time": time.time(), "reporters": set()}
        user_violations[key][str(msg_id)]["reporters"].add(reporter_id)
        asyncio.create_task(save_user_violations())
        
        # 修改警告消息 - 关键：显示举报数 + 根据举报数决定按钮
        display_name = data.get("suspect_name") or f"ID {user_id}"
        updated_text = (
            "🚨 已收到群成员的举报\n\n"
            f"👤 用户：{display_name}（ID: {user_id}）📌 触发原因：{reason}\n"
            f"📣 当前举报人数：{count} 人\n\n"
            "⚠️ 疑似广告，请勿私信该用户，可继续点举报。"
        )
        kb = build_warning_buttons(group_id, msg_id, count)
        try:
            if warning_id:  # 只有有警告消息时才更新
                await bot.edit_message_text(
                    chat_id=group_id,
                    message_id=warning_id,
                    text=updated_text,
                    reply_markup=kb
                )
        except Exception:
            pass
        trigger_count = data.get("trigger_count", 0)
        # 触发2层检测+2人举报=永封
        if count >= 2 and trigger_count == 2:
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
                    until_date=None
                )
                # 永封时删除用户全部消息和全部警告
                await _delete_user_recent_and_warnings(group_id, user_id, msg_id)
                await _delete_all_banned_warnings(group_id)
                # 发送封禁通知（10秒后自动删除）
                final_text = (
                    f"🚫 用户 {display_name}\n"
                    f"📌 触发原因：{reason}（已被 {count} 位成员举报）\n"
                    f"🔒 处理结果：永久禁止在本群发言。\n{MISJUDGE_BOT_MENTION}"
                )
                try:
                    sent = await bot.send_message(group_id, final_text)
                    _track_bot_message(group_id, sent.message_id, 10)
                except Exception:
                    pass
                async with lock:
                    reports.pop(rk, None)
                asyncio.create_task(save_data())
                return
            except Exception as e:
                print("2层2举报永封失败:", e)
        asyncio.create_task(save_data())
    except Exception as e:
        print("举报异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith(("ban24h:", "banperm:")))
async def handle_ban(callback: CallbackQuery):
    """封禁处理"""
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("已过期")
            return
        action, group_id_str, msg_id_str = parts[0], parts[1], parts[2]
        group_id = int(group_id_str)
        msg_id = int(msg_id_str)
        caller_id = callback.from_user.id
        rk = _report_key(group_id, msg_id)
        async with lock:
            if rk not in reports:
                await callback.answer("已过期")
                return
            data = reports[rk]
            user_id = data["suspect_id"]
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
        
        ban_type = "禁言 24 小时" if action == "ban24h" else "永久禁止在本群发言"
        report_count = len(data.get("reporters", set()))
        display_name = data.get("suspect_name") or f"ID {user_id}"
        
        # 永封时删除用户全部消息和全部警告
        if action == "banperm":
            await _delete_user_recent_and_warnings(group_id, user_id, msg_id)
            # 删除所有已封禁的警告消息
            await _delete_all_banned_warnings(group_id)
            # 删除当前警告消息
            if warning_id:
                try:
                    await bot.delete_message(group_id, warning_id)
                except Exception:
                    pass
            # 发送封禁通知（10秒后自动删除）
            final_text = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：{reason}（已被 {report_count} 位成员举报）\n"
                f"🔒 处理结果：{ban_type}。\n{MISJUDGE_BOT_MENTION}"
            )
            try:
                sent = await bot.send_message(group_id, final_text)
                _track_bot_message(group_id, sent.message_id, 10)  # 10秒后删除
            except Exception:
                pass
        else:
            # 24小时禁言：删除源消息，更新警告
            try:
                await bot.delete_message(group_id, msg_id)
            except Exception:
                pass
            final_text = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：{reason}（已被 {report_count} 位成员举报）\n"
                f"🔒 处理结果：{ban_type}。\n{MISJUDGE_BOT_MENTION}"
            )
            if warning_id:
                try:
                    await bot.edit_message_text(
                        chat_id=group_id,
                        message_id=warning_id,
                        text=final_text,
                        reply_markup=None
                    )
                    # 添加到已封禁警告列表
                    _add_banned_warning(group_id, warning_id)
                except Exception:
                    pass

        # 删除所有已封禁的警告消息（替换原来的只删上一条）
        await _delete_all_banned_warnings(group_id)

        await callback.answer(f"✅ {ban_type}")
        async with lock:
            reports.pop(rk, None)
        await save_data()
    
    except TelegramBadRequest:
        await callback.answer("❌ 失败", show_alert=True)
    except Exception as e:
        print("封禁异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("exempt:"))
async def handle_exempt(callback: CallbackQuery):
    """误判豁免：删除警告、移除报告，并将该用户加入多层检测白名单"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("已过期")
            return
        group_id = int(parts[1])
        msg_id = int(parts[2])
        caller_id = callback.from_user.id
        rk = _report_key(group_id, msg_id)
        async with lock:
            if rk not in reports:
                await callback.answer("已过期")
                return
            data = reports[rk]
            warning_id = data["warning_id"]
            suspect_id = data["suspect_id"]
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员操作", show_alert=True)
            return
        try:
            await bot.delete_message(group_id, warning_id)
        except Exception:
            pass
        cfg = get_group_config(group_id)
        wl = cfg.get("misjudge_whitelist") or []
        if not isinstance(wl, list):
            wl = []
        sid = str(suspect_id)
        if sid not in wl:
            wl.append(sid)
            cfg["misjudge_whitelist"] = wl
            await save_config()
        await callback.answer("✅ 已豁免")
        async with lock:
            reports.pop(rk, None)
        await save_data()
    except Exception as e:
        print("豁免异常:", e)
        await callback.answer("❌ 失败", show_alert=True)


@router.callback_query(F.data.startswith("markad:"), F.from_user.id.in_(ADMIN_IDS))
async def handle_mark_ad(callback: CallbackQuery):
    """标记广告并删除：学习广告样本 + 删除该用户最近消息和全部警告"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("已过期", show_alert=True)
            return
        group_id = int(parts[1])
        msg_id = int(parts[2])
        rk = _report_key(group_id, msg_id)
        async with lock:
            data = reports.get(rk)
        if not data:
            await callback.answer("记录已过期", show_alert=True)
            return
        suspect_id = data.get("suspect_id")
        orig_msg_id = data.get("original_message_id")
        orig_text = data.get("original_text") or ""

        # 学习广告样本（仅使用当前触发的原始文本）
        if orig_text:
            try:
                semantic_ad_detector.add_ad_sample(orig_text)
            except Exception as e:
                print(f"学习广告样本失败: {e}")

        # 删除该用户最近消息和全部警告
        try:
            await _delete_user_recent_and_warnings(group_id, suspect_id, orig_msg_id)
        except Exception as e:
            print(f"标记广告时删除消息失败: {e}")

        async with lock:
            reports.pop(rk, None)
        await save_data()
        # 不弹窗，仅静默确认
        await callback.answer()
    except Exception as e:
        print("标记广告异常:", e)
        await callback.answer("❌ 失败", show_alert=True)


@router.callback_query(F.data.startswith("mild_exempt:"))
async def handle_mild_exempt(callback: CallbackQuery):
    """轻度触发豁免：仅关闭该用户的轻度检测，不影响其他检测"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("已过期")
            return
        group_id = int(parts[1])
        user_id = int(parts[2])
        caller_id = callback.from_user.id
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员操作", show_alert=True)
            return

        cfg = get_group_config(group_id)
        wl = cfg.get("mild_exempt_whitelist") or []
        if not isinstance(wl, list):
            wl = []
        sid = str(user_id)
        if sid not in wl:
            wl.append(sid)
            cfg["mild_exempt_whitelist"] = wl
            await save_config()

        await callback.answer("✅ 已豁免该用户的轻度检测")
    except Exception as e:
        print("轻度豁免异常:", e)
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

async def cleanup_deleted_messages():
    """每 10 分钟检查：举报记录超过 24 小时未处理则隐藏按钮并从内存移除。"""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        to_remove = []
        async with lock:
            check_list = list(reports.items())
        for rk, data in check_list:
            age = now - data.get("timestamp", 0)
            if age < REPORT_BUTTON_HIDE_AFTER_SEC:
                continue
            group_id = data["chat_id"]
            warning_id = data["warning_id"]
            try:
                await bot.edit_message_reply_markup(
                    chat_id=group_id,
                    message_id=warning_id,
                    reply_markup=None
                )
            except TelegramBadRequest:
                pass
            to_remove.append(rk)
        if to_remove:
            async with lock:
                for oid in to_remove:
                    reports.pop(oid, None)
            await save_data()
        await asyncio.sleep(1)


async def cleanup_orphan_replies():
    """每小时清理一次机器人在群里的引用回复，避免漏杀"""
    while True:
        await asyncio.sleep(3600)
        # 拷贝一份当前列表，避免遍历时修改
        items = list(bot_reply_links.items())
        if not items:
            continue
        for (group_id, bot_msg_id), (orig_msg_id, created_ts) in items:
            try:
                await bot.delete_message(group_id, bot_msg_id)
            except TelegramBadRequest:
                # 已经被删就忽略
                pass
            except Exception:
                # 其他错误也不影响继续
                pass
            finally:
                bot_reply_links.pop((group_id, bot_msg_id), None)

async def main():
    print("🚀 机器人启动")
    if admin_router is not None:
        try:
            from bot_config import validate_immutable_config
            validate_immutable_config()
        except Exception as e:
            print(f"⚠️ 全局配置未加载（全局系统配置面板不可用）: {e}")
    await load_config()
    for gid in GROUP_IDS:
        get_group_config(gid)
    await save_config()
    await load_data()
    await load_user_violations()
    load_repeat_levels()
    load_link_ref_levels()
    await load_media_stats()
    asyncio.create_task(cleanup_deleted_messages())
    asyncio.create_task(cleanup_orphan_replies())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
