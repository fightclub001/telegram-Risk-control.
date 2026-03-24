import asyncio
import json
import os
import re
import time
import hashlib
from copy import deepcopy
from collections import deque
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, ReplyKeyboardRemove, BufferedInputFile
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
FORWARD_MATCH_FILE = os.path.join(DATA_DIR, "forward_match_memory.json")
RECENT_MESSAGES_FILE = os.path.join(DATA_DIR, "recent_messages.json")
REPORT_ACTIONS_FILE = os.path.join(DATA_DIR, "report_actions.json")
AD_DELETE_SUMMARY_FILE = os.path.join(DATA_DIR, "ad_delete_summaries.json")

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
forward_match_memory = {}  # normalized_text -> {"group_id": int, "user_id": int, "updated_at": int}
report_action_state = {}  # key: "gid_uid" -> {"last_trigger_count": int, "last_trigger_at": int}
pending_ad_delete_summaries = []  # recent AD delete events waiting for 10-item admin summary
ad_delete_summary_lock = asyncio.Lock()
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
MISJUDGE_BOT_MENTION = "如有误封，请直接联系本群管理员处理。"
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

# ==================== 监听决策日志（仅保留最近10条） ====================
listen_decision_logs = deque(maxlen=10)  # newest appended to right


def _clip_text(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _push_listen_log(
    *,
    group_id: int | None,
    user_id: int | None,
    msg_id: int | None,
    text: str,
    verdict: str,
    details: str = "",
) -> None:
    """
    记录一次“收到消息→决策路径→结果”的摘要。
    verdict 示例：SKIP / PASS / AD_DELETE / RULE_DELETE / RULE_BAN / ERROR 等
    """
    try:
        ts = int(time.time())
        listen_decision_logs.append(
            {
                "ts": ts,
                "group_id": group_id,
                "user_id": user_id,
                "msg_id": msg_id,
                "text": _clip_text(text, 120),
                "verdict": verdict,
                "details": _clip_text(details, 300),
            }
        )
    except Exception:
        pass

# ==================== 配置函数 ====================
def _default_group_config():
    """单群默认配置（关键词等会随管理员编辑持久化到 CONFIG_FILE）"""
    return {
        "enabled": True,
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
        "report_history_threshold": 3,
        "report_history_mute_hours": 24,
        "report_history_whitelist": [],
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
                for obsolete_key in (
                    "check_bio_link",
                    "bio_keywords",
                    "check_bio_keywords",
                    "check_message_link",
                    "violation_mute_hours",
                    "reported_message_threshold",
                    "autoreply",
                ):
                    saved.pop(obsolete_key, None)
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
        if GROUP_IDS:
            primary_gid = get_primary_group_id()
            primary_cfg = deepcopy(get_group_config(primary_gid))
            if "groups" not in config:
                config["groups"] = {}
            for gid in GROUP_IDS:
                config["groups"][str(gid)] = deepcopy(primary_cfg)
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

async def load_forward_match_memory():
    global forward_match_memory
    try:
        if os.path.exists(FORWARD_MATCH_FILE):
            with open(FORWARD_MATCH_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            forward_match_memory = raw if isinstance(raw, dict) else {}
        else:
            forward_match_memory = {}
    except Exception as e:
        print(f"forward match memory load failed: {e}")
        forward_match_memory = {}

async def save_forward_match_memory():
    try:
        now = int(time.time())
        cutoff = now - USER_MSG_24H_SEC
        stale_keys = [
            key for key, value in forward_match_memory.items()
            if not isinstance(value, dict) or int(value.get("updated_at", 0)) < cutoff
        ]
        for key in stale_keys:
            forward_match_memory.pop(key, None)
        with open(FORWARD_MATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(forward_match_memory, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"forward match memory save failed: {e}")

async def load_report_action_state():
    global report_action_state
    try:
        if os.path.exists(REPORT_ACTIONS_FILE):
            with open(REPORT_ACTIONS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            report_action_state = raw if isinstance(raw, dict) else {}
        else:
            report_action_state = {}
    except Exception as e:
        print(f"report action state load failed: {e}")
        report_action_state = {}

async def save_report_action_state():
    try:
        with open(REPORT_ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(report_action_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"report action state save failed: {e}")

async def load_ad_delete_summaries():
    global pending_ad_delete_summaries
    try:
        if os.path.exists(AD_DELETE_SUMMARY_FILE):
            with open(AD_DELETE_SUMMARY_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            pending_ad_delete_summaries = raw if isinstance(raw, list) else []
        else:
            pending_ad_delete_summaries = []
    except Exception as e:
        print(f"ad delete summary load failed: {e}")
        pending_ad_delete_summaries = []

async def save_ad_delete_summaries():
    try:
        with open(AD_DELETE_SUMMARY_FILE, "w", encoding="utf-8") as f:
            json.dump(pending_ad_delete_summaries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"ad delete summary save failed: {e}")

def _prune_recent_messages_cache():
    cutoff = time.time() - USER_MSG_24H_SEC
    for key in list(user_recent_message_ids.keys()):
        msgs = user_recent_message_ids.get(key)
        if not msgs:
            user_recent_message_ids.pop(key, None)
            continue
        kept = [item for item in msgs if len(item) == 3 and item[1] >= cutoff]
        if kept:
            user_recent_message_ids[key] = deque(kept[-USER_MSG_TRACK_MAXLEN:], maxlen=USER_MSG_TRACK_MAXLEN)
        else:
            user_recent_message_ids.pop(key, None)

async def load_recent_messages_cache():
    global user_recent_message_ids
    try:
        if not os.path.exists(RECENT_MESSAGES_FILE):
            user_recent_message_ids = {}
            return
        with open(RECENT_MESSAGES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        loaded = {}
        for key, items in (raw or {}).items():
            try:
                gid_str, uid_str = key.split("_", 1)
                gid = int(gid_str)
                uid = int(uid_str)
            except Exception:
                continue
            if not isinstance(items, list):
                continue
            cleaned = []
            for item in items:
                if not isinstance(item, list) or len(item) != 3:
                    continue
                try:
                    cleaned.append((int(item[0]), float(item[1]), str(item[2] or "")))
                except Exception:
                    continue
            if cleaned:
                loaded[(gid, uid)] = deque(cleaned[-USER_MSG_TRACK_MAXLEN:], maxlen=USER_MSG_TRACK_MAXLEN)
        user_recent_message_ids = loaded
        _prune_recent_messages_cache()
    except Exception as e:
        print(f"recent messages cache load failed: {e}")
        user_recent_message_ids = {}

async def save_recent_messages_cache():
    try:
        _prune_recent_messages_cache()
        data = {}
        for (gid, uid), msgs in user_recent_message_ids.items():
            data[f"{gid}_{uid}"] = [[msg_id, ts, text] for msg_id, ts, text in list(msgs)]
        with open(RECENT_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"recent messages cache save failed: {e}")

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

def get_primary_group_id() -> int:
    if not GROUP_IDS:
        raise ValueError("GROUP_IDS is empty")
    return sorted(GROUP_IDS)[0]

def get_global_config():
    return get_group_config(get_primary_group_id())

def apply_global_config_value(key: str, value):
    for gid in GROUP_IDS:
        cfg = get_group_config(gid)
        cfg[key] = deepcopy(value)

def apply_global_config_updates(updates: dict):
    for gid in GROUP_IDS:
        cfg = get_group_config(gid)
        for key, value in updates.items():
            cfg[key] = deepcopy(value)


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
    EditReportHistoryWhitelist = State()
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
    """保留旧入口，统一跳到全局控制台。"""
    buttons = [
        [InlineKeyboardButton(text="⚙️ 进入全局控制台", callback_data="group_menu_single")],
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
        [InlineKeyboardButton(text="🧠 AD机器学习", callback_data=f"submenu_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="⏱️ 短消息/垃圾", callback_data=f"submenu_short:{group_id}")],
        [InlineKeyboardButton(text="⚠️ 举报处罚", callback_data=f"submenu_violation:{group_id}")],
        [InlineKeyboardButton(text="🔁 重复发言", callback_data=f"submenu_repeat:{group_id}")],
        [InlineKeyboardButton(text="📎 媒体权限", callback_data=f"submenu_media_perm:{group_id}")],
        [InlineKeyboardButton(text="📣 媒体举报", callback_data=f"submenu_media_report:{group_id}")],
        [InlineKeyboardButton(text="🎛️ 基础设置", callback_data=f"submenu_basic:{group_id}")],
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
    mute_h = cfg.get("report_history_mute_hours", 24)
    mute_sec = mute_h * 3600
    whitelist = cfg.get("report_history_whitelist", []) or []
    whitelist_count = len(whitelist) if isinstance(whitelist, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"🔇 禁言时长: {fmt_duration(mute_sec)}", callback_data=f"edit_mute:{group_id}")],
        [InlineKeyboardButton(text=f"阈值: {cfg.get('report_history_threshold', 3)}", callback_data=f"edit_report_threshold:{group_id}")],
        [InlineKeyboardButton(text=f"豁免白名单: {whitelist_count}", callback_data=f"edit_report_whitelist:{group_id}")],
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
        [InlineKeyboardButton(text="📄 导出监听日志（近10条）", callback_data=f"export_listen_log:{group_id}")],
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
    """进入全局控制台，配置同步应用到全部受控群组。"""
    if not GROUP_IDS:
        await message.reply("当前未配置任何受控群组（GROUP_IDS 为空）。")
        return
    group_id = get_primary_group_id()
    await state.update_data(group_id=group_id)
    cfg = get_group_config(group_id)
    status = "✅ 运行中" if cfg.get("enabled", True) else "❌ 已停用"
    text = (
        "👮 管理员面板\n\n"
        f"状态: {status}\n"
        f"受控群组数: {len(GROUP_IDS)}\n"
        f"主配置群ID: <code>{group_id}</code>\n\n"
        "所有参数都会同步应用到全部群组。"
    )
    kb = get_group_menu_keyboard(group_id)
    await message.reply(text, reply_markup=kb)
    await state.set_state(AdminStates.GroupMenu)

# ==================== 回调处理 ====================
@router.callback_query(F.data == "choose_group", F.from_user.id.in_(ADMIN_IDS))
async def choose_group_callback(callback: CallbackQuery, state: FSMContext):
    """兼容旧入口：统一跳到全局控制台。"""
    if not GROUP_IDS:
        await callback.answer("未配置受控群组。", show_alert=True)
        return
    group_id = get_primary_group_id()
    await state.update_data(group_id=group_id)
    cfg = get_group_config(group_id)
    status = "✅ 运行中" if cfg.get("enabled", True) else "❌ 已停用"
    text = (
        "👮 管理员面板\n\n"
        f"状态: {status}\n"
        f"受控群组数: {len(GROUP_IDS)}\n"
        f"主配置群ID: <code>{group_id}</code>\n\n"
        "所有参数都会同步应用到全部群组。"
    )
    kb = get_group_menu_keyboard(group_id)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.GroupMenu)
    await callback.answer()

@router.callback_query(F.data == "group_menu_single", F.from_user.id.in_(ADMIN_IDS))
async def group_menu_single(callback: CallbackQuery, state: FSMContext):
    await choose_group_callback(callback, state)

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
    """单群模式下，返回即回到本群控制台。"""
    await choose_group_callback(callback, state)

@router.callback_query(F.data.startswith("group_menu:"), F.from_user.id.in_(ADMIN_IDS))
async def group_menu(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        cfg = get_group_config(group_id)
        status = "✅ 运行中" if cfg.get("enabled", True) else "❌ 已停用"
        text = (
            "👮 管理员面板\n\n"
            f"状态: {status}\n"
            f"受控群组数: {len(GROUP_IDS)}\n"
            f"主配置群ID: <code>{group_id}</code>\n\n"
            "所有参数都会同步应用到全部群组。"
        )
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("submenu_multi_rules:"), F.from_user.id.in_(ADMIN_IDS))
async def multi_rules_submenu(callback: CallbackQuery):
    """多功能叠加规则：展示当前各模块的触发顺序与优先级。"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        text = (
            f"<b>{title}</b> › 多功能叠加规则\n\n"
            "当前触发顺序（从上到下，前者优先）：\n"
            "1️⃣ AD 语义广告检测：命中后直接删除该条消息，不再执行后续检测。\n"
            "2️⃣ 举报阈值禁言：被非管理员举报消息数 ≥ 阈值 "
            f"（当前: {cfg.get('reported_message_threshold', 3)}）时按次数封禁并删除消息。\n"
            "3️⃣ 多层风控检测：简介/昵称/链接等命中形成多层触发，3 层及以上直接封禁并清理 24 小时内消息。\n"
            "4️⃣ 轻度触发累计：1～2 层触发计入轻度警告，达到 3 次时仅通知管理员，可在 AD 面板中豁免轻度。\n"
            "5️⃣ 重复发言检测：在配置窗口内多次重复同一内容，按违规等级禁言并清理近期消息。\n\n"
            "当前策略为：\n"
            "- AD 命中后，不再进入举报、多层、重复检测（避免多次处罚）。\n"
            "- 当多层风控和重复发言同时满足时，以多层风控结果为准（更重的处罚覆盖较轻处罚）。\n\n"
            "后续如果需要，可以在这里增加可调策略，例如切换优先级或是否让 AD 命中也计入其它统计。"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")]
            ]
        )
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

# ==================== 简介检测 ====================
@router.callback_query(F.data.startswith("submenu_bio:"), F.from_user.id.in_(ADMIN_IDS))
async def bio_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        text = "该功能已下线。\n当前不再检测简介链接或简介敏感词。"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")]]
        )
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
        # 只要成功学习到样本，就自动开启该群的 AD 语义检测，避免“只收录不生效”
        if added_ids and group_id:
            cfg = get_group_config(group_id)
            if not cfg.get("semantic_ad_enabled", False):
                cfg["semantic_ad_enabled"] = True
                await save_config()
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
        # 解析页码（默认第 0 页 = 最新一页）
        parts = callback.data.split(":", 1)
        page = 0
        if len(parts) == 2:
            try:
                page = int(parts[1])
            except ValueError:
                page = 0
        if page < 0:
            page = 0

        samples = semantic_ad_detector.list_samples()
        if not samples:
            await callback.answer("当前广告语义库为空。", show_alert=False)
            return

        PAGE_SIZE = 20
        total = len(samples)
        # 按时间排序后，最新在最后一条；分页时从最新往前翻
        samples_sorted = samples
        max_page = (total - 1) // PAGE_SIZE
        if page > max_page:
            page = max_page

        start = total - (page + 1) * PAGE_SIZE
        end = total - page * PAGE_SIZE
        if start < 0:
            start = 0
        page_items = samples_sorted[start:end]

        lines = [f"{s.id}: {s.text}" for s in page_items]
        header = f"广告语义库（共 {total} 条，当前第 {page + 1}/{max_page + 1} 页，ID: 文本）\n"
        text = header + "\n".join(lines)

        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"view_semantic_ad:{page-1}"))
        if page < max_page:
            buttons.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=f"view_semantic_ad:{page+1}"))
        rows = []
        if buttons:
            rows.append(buttons)
        # 返回 AD 菜单
        rows.append([InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_semantic_ad:{callback.message.chat.id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"❌ 查看失败: {e}", show_alert=False)


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
        text = f"<b>{breadcrumb}</b> › 多层风控检测\n\n简介链接: {link_status}\n简介敏感词: {kw_status}"
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
        text = f"<b>{breadcrumb}</b> › 多层风控检测\n\n简介链接: {link_status}\n简介敏感词: {kw_status}"
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
        mute_hours = cfg.get("report_history_mute_hours", 24)
        mute_sec = mute_hours * 3600
        threshold = cfg.get("report_history_threshold", 3)
        whitelist = cfg.get("report_history_whitelist", []) or []
        whitelist_count = len(whitelist) if isinstance(whitelist, list) else 0
        text = (
            f"<b>{title}</b> › 举报处罚\n\n"
            f"🔇 禁言: {fmt_duration(mute_sec)}\n"
            f"触发: {threshold} 条历史举报\n"
            f"📋 豁免白名单: {whitelist_count} 人"
        )
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
        current = cfg.get("report_history_mute_hours", 24)
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
        old_h = cfg.get("report_history_mute_hours", 24)
        value = int(message.text.strip())
        apply_global_config_value("report_history_mute_hours", value)
        await save_config()
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新全局举报禁言时长: {old_h}h → {value}h", reply_markup=kb)
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
        current = cfg.get("report_history_threshold", 3)
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
        value = int(message.text.strip())
        apply_global_config_value("report_history_threshold", value)
        await save_config()
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"✅ 已更新全局举报触发阈值为 {value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("❌ 请输入数字")
    except Exception as e:
        await message.reply(f"❌ {str(e)}")

@router.callback_query(F.data.startswith("edit_report_whitelist:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_report_whitelist(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        whitelist = cfg.get("report_history_whitelist", []) or []
        if not isinstance(whitelist, list):
            whitelist = []
        current = "\n".join(str(item) for item in whitelist) if whitelist else "（空）"
        text = (
            "编辑举报处罚豁免白名单\n"
            "每行一个用户ID或用户名，发送 /clear 可清空。\n\n"
            f"当前列表：\n{current}"
        )
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditReportHistoryWhitelist)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditReportHistoryWhitelist), F.from_user.id.in_(ADMIN_IDS))
async def process_report_history_whitelist(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        if (message.text or "").strip() == "/clear":
            apply_global_config_value("report_history_whitelist", [])
            await save_config()
            await message.reply("✅ 已清空举报处罚豁免白名单", reply_markup=get_violation_menu_keyboard(group_id))
        else:
            current = get_global_config().get("report_history_whitelist", []) or []
            if not isinstance(current, list):
                current = []
            additions = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
            merged = list(current)
            for item in additions:
                if item not in merged:
                    merged.append(item)
            apply_global_config_value("report_history_whitelist", merged)
            await save_config()
            await message.reply(
                f"✅ 已更新举报处罚豁免白名单，当前 {len(merged)} 项",
                reply_markup=get_violation_menu_keyboard(group_id),
            )
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
        text = "自动回复功能已下线。"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")]]
        )
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


@router.callback_query(F.data.startswith("export_listen_log:"), F.from_user.id.in_(ADMIN_IDS))
async def export_listen_log(callback: CallbackQuery):
    """导出最近 10 条监听决策日志（用于定位：是否收到群消息、为何未触发 AD/规则）。"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        rows = list(listen_decision_logs)
        if not rows:
            text = (
                f"<b>{title}</b> › 监听日志（近10条）\n\n"
                "当前没有任何监听记录。\n\n"
                "这通常意味着：机器人没有收到群消息更新。\n"
                "请优先检查：\n"
                "1) BotFather 隐私模式（/setprivacy）是否关闭\n"
                "2) 机器人是否是群管理员 & 有读取/删除权限\n"
                "3) 环境变量 GROUP_IDS 是否包含该群真实 chat.id（常见为 -100…）"
            )
            await callback.message.reply(text)
            await callback.answer()
            return

        # 最新在后，导出时按“新→旧”
        lines = [f"{title} 监听日志（近10条，新→旧）", f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}", ""]
        for it in reversed(rows):
            gid = it.get("group_id")
            uid = it.get("user_id")
            mid = it.get("msg_id")
            ts = it.get("ts", 0)
            tstr = time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "??"
            verdict = it.get("verdict", "")
            txt = it.get("text", "")
            details = it.get("details", "")
            lines.append(f"[{tstr}] gid={gid} uid={uid} mid={mid} => {verdict}")
            if txt:
                lines.append(f"  msg: {txt}")
            if details:
                lines.append(f"  why: {details}")
            lines.append("")
        out = "\n".join(lines).strip()

        # 1) 先发一份文本（便于快速看）
        await callback.message.reply(f"<pre>{out}</pre>")

        # 2) 再发一份 txt 作为“导出”
        buf = out.encode("utf-8")
        filename = f"listen_log_{int(time.time())}.txt"
        await callback.message.reply_document(BufferedInputFile(buf, filename=filename))
        await callback.answer("✅ 已导出")
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

def _remember_forward_match(group_id: int, user_id: int, text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    forward_match_memory[norm] = {
        "group_id": group_id,
        "user_id": user_id,
        "updated_at": int(time.time()),
    }
    return True

def _get_remembered_user_id_by_text(group_id: int, text: str) -> int | None:
    norm = _normalize_text(text)
    if not norm:
        return None
    data = forward_match_memory.get(norm)
    if not isinstance(data, dict):
        return None
    if int(data.get("group_id", 0)) != int(group_id):
        return None
    if int(time.time()) - int(data.get("updated_at", 0)) > USER_MSG_24H_SEC:
        return None
    try:
        return int(data.get("user_id"))
    except Exception:
        return None

def _remember_recent_user_texts(group_id: int, user_id: int) -> bool:
    key = (group_id, user_id)
    msgs = user_recent_message_ids.get(key) or []
    cutoff = time.time() - USER_MSG_24H_SEC
    changed = False
    for _msg_id, ts, txt in list(msgs):
        if ts < cutoff or not txt:
            continue
        changed = _remember_forward_match(group_id, user_id, txt) or changed
    return changed


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
            await _delete_original_and_linked_reply(group_id, message.message_id)
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

def _report_action_key(group_id: int, user_id: int) -> str:
    return f"{group_id}_{user_id}"

def get_report_history_action_count(group_id: int, user_id: int) -> int:
    data = report_action_state.get(_report_action_key(group_id, user_id), {})
    if not isinstance(data, dict):
        return 0
    return int(data.get("last_trigger_count", 0) or 0)

def build_report_history_exempt_keyboard(group_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="举报处罚豁免👮‍♂️", callback_data=f"report_history_exempt:{group_id}:{user_id}")]
        ]
    )

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
    memory_changed = False
    if key in user_recent_message_ids:
        for msg_id, t, txt in list(user_recent_message_ids[key]):
            if t >= cutoff:
                # 被机器人删除的消息也作为广告样本学习
                if txt:
                    try:
                        semantic_ad_detector.add_ad_sample(txt)
                        memory_changed = _remember_forward_match(group_id, user_id, txt) or memory_changed
                    except Exception as e:
                        print(f"删除用户消息时学习样本失败: {e}")
                await _delete_original_and_linked_reply(group_id, msg_id)
    if memory_changed:
        asyncio.create_task(save_forward_match_memory())
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
        await _delete_original_and_linked_reply(group_id, orig_msg_id)
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

@router.message(
    F.chat.id.in_(GROUP_IDS),
    F.photo | F.video | F.voice | F.video_note | F.document | F.animation | F.audio,
)
async def on_media_message(message: Message):
    """媒体消息统一入口：先跑广告匹配，再做媒体权限拦截，最后挂举报/点赞按钮。"""
    if not message.from_user or message.from_user.is_bot:
        return
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    now = time.time()
    _track_user_message(group_id, user_id, message.message_id, message.caption or "")
    semantic_text = (message.caption or "").strip()
    if semantic_text:
        if await _check_and_delete_semantic_ad_message(message, semantic_text, group_id=group_id, user_id=user_id):
            return
    username = message.from_user.username if message.from_user else None
    await _refresh_user_boosts(group_id, user_id)
    if not _can_send_media(group_id, user_id, username):
        await _delete_original_and_linked_reply(group_id, message.message_id)
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
    asyncio.create_task(save_recent_messages_cache())


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


async def _delete_linked_bot_replies(group_id: int, original_msg_id: int | None):
    """删除引用了某条原消息的机器人回复，避免原消息删除后群里残留机器人的告警。"""
    if not original_msg_id:
        return
    linked = [
        (bot_msg_id, created_ts)
        for (gid, bot_msg_id), (orig_msg_id, created_ts) in list(bot_reply_links.items())
        if gid == group_id and orig_msg_id == original_msg_id
    ]
    for bot_msg_id, _ in linked:
        try:
            await bot.delete_message(group_id, bot_msg_id)
        except Exception:
            pass
        finally:
            bot_reply_links.pop((group_id, bot_msg_id), None)


async def _delete_original_and_linked_reply(group_id: int, original_msg_id: int | None):
    """删除原消息，并同步删除机器人对该消息的引用回复。"""
    if not original_msg_id:
        return
    try:
        await bot.delete_message(group_id, original_msg_id)
    except Exception:
        pass
    await _delete_linked_bot_replies(group_id, original_msg_id)


def _semantic_detection_enabled_for_group(group_id: int) -> bool:
    cfg = get_group_config(group_id)
    return bool(cfg.get("semantic_ad_enabled", False))


async def _enable_semantic_detection_for_group(group_id: int) -> bool:
    """学习到广告样本后，确保对应群组开启语义广告检测。"""
    cfg = get_group_config(group_id)
    if cfg.get("semantic_ad_enabled", False):
        return False
    cfg["semantic_ad_enabled"] = True
    await save_config()
    return True

async def _flush_ad_delete_summary_batch() -> None:
    async with ad_delete_summary_lock:
        if len(pending_ad_delete_summaries) < 10:
            return
        batch = pending_ad_delete_summaries[:10]
        del pending_ad_delete_summaries[:10]
        await save_ad_delete_summaries()

    lines = []
    learned_count = 0
    for idx, item in enumerate(batch, start=1):
        if item.get("learned"):
            learned_count += 1
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(int(item.get("ts", time.time()))))
        lines.append(
            f"{idx}. [{ts}] gid={item.get('group_id')} uid={item.get('user_id')} "
            f"score={float(item.get('score', 0.0)):.3f} learned={'Y' if item.get('learned') else 'N'}\n"
            f"   {item.get('text', '')}"
        )

    summary_text = (
        "AD 删除汇总（10条）\n\n"
        f"总数: 10\n"
        f"新增学习入库: {learned_count}\n\n"
        + "\n".join(lines)
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, summary_text)
        except Exception as e:
            print(f"send AD delete summary failed for admin {admin_id}: {e}")

async def _record_semantic_ad_deletion(group_id: int, user_id: int, message_id: int, text: str, score: float) -> None:
    learned = False
    try:
        sample = semantic_ad_detector.add_ad_sample(text)
        learned = sample is not None
    except Exception as e:
        print(f"learn ad sample on delete failed: {e}")

    event = {
        "ts": int(time.time()),
        "group_id": group_id,
        "user_id": user_id,
        "message_id": message_id,
        "text": _clip_text(text, 120),
        "score": round(float(score), 3),
        "learned": learned,
    }
    async with ad_delete_summary_lock:
        pending_ad_delete_summaries.append(event)
        await save_ad_delete_summaries()
    await _flush_ad_delete_summary_batch()


async def _check_and_delete_semantic_ad_message(message: Message, text: str, *, group_id: int, user_id: int) -> bool:
    """
    用已学习的广告库主动匹配当前消息。
    命中后直接删除原消息和相关机器人回复。
    """
    if not _semantic_detection_enabled_for_group(group_id):
        return False
    if len((text or "").strip()) < 4:
        return False

    cfg = get_group_config(group_id)
    exempt_users = cfg.get("exempt_users") or []
    if isinstance(exempt_users, list) and str(user_id) in exempt_users:
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="PASS",
            details="进入AD检测但命中豁免用户 exempt_users，跳过AD匹配",
        )
        return False

    is_semantic_ad, sim, _ = semantic_ad_detector.check_text(text)
    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message.message_id,
        text=text,
        verdict="AD_HIT" if is_semantic_ad else "AD_MISS",
        details=f"AD匹配结果: is_ad={is_semantic_ad}, score={sim:.3f}",
    )
    if not is_semantic_ad:
        return False

    await _record_semantic_ad_deletion(group_id, user_id, message.message_id, text, sim)
    await _delete_original_and_linked_reply(group_id, message.message_id)
    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message.message_id,
        text=text,
        verdict="AD_DELETE",
        details="命中AD语义库，已执行删除",
    )
    return True


def _get_only_group_id() -> int | None:
    """仅配置了一个受控群时，返回该群 ID，便于单群模式兜底。"""
    if len(GROUP_IDS) != 1:
        return None
    return next(iter(GROUP_IDS))


def _find_recent_user_ids_by_text(group_id: int, text: str, *, limit: int = 3) -> list[int]:
    """
    在最近缓存里按文案反查用户。
    单群转发学习时，Telegram 经常不给原始 user/chat 信息，这里做本地兜底。
    """
    now = time.time()
    cutoff = now - USER_MSG_24H_SEC
    raw = (text or "").strip()
    norm = _normalize_text(text)
    if not raw and not norm:
        return []

    scored: list[tuple[float, int]] = []
    for (gid, uid), msgs in user_recent_message_ids.items():
        if gid != group_id:
            continue
        best_score = 0.0
        best_ts = 0.0
        for _, ts, msg_text in msgs:
            if ts < cutoff or not msg_text:
                continue
            raw_msg = (msg_text or "").strip()
            norm_msg = _normalize_text(msg_text)
            score = 0.0
            if raw and raw_msg == raw:
                score = 1.0
            elif norm and norm_msg == norm:
                score = 0.95
            elif raw and raw_msg and (raw in raw_msg or raw_msg in raw):
                score = 0.80
            elif norm and norm_msg and (norm in norm_msg or norm_msg in norm):
                score = 0.75
            if score > best_score or (score == best_score and ts > best_ts):
                best_score = score
                best_ts = ts
        if best_score > 0:
            scored.append((best_score + min(best_ts / 10**12, 0.001), uid))

    scored.sort(reverse=True)
    out: list[int] = []
    for _, uid in scored:
        if uid not in out:
            out.append(uid)
        if len(out) >= limit:
            break
    return out


async def _delete_recent_messages_by_text(group_id: int, text: str) -> int:
    """
    当拿不到 user_id 时，退化为按同文案删除最近消息，并清掉对应机器人警告。
    返回删除的原消息条数。
    """
    now = time.time()
    cutoff = now - USER_MSG_24H_SEC
    raw = (text or "").strip()
    norm = _normalize_text(text)
    if not raw and not norm:
        return 0

    deleted = 0
    seen: set[int] = set()
    for (gid, _uid), msgs in list(user_recent_message_ids.items()):
        if gid != group_id:
            continue
        for msg_id, ts, msg_text in list(msgs):
            if ts < cutoff or not msg_text or msg_id in seen:
                continue
            raw_msg = (msg_text or "").strip()
            norm_msg = _normalize_text(msg_text)
            if raw and raw_msg == raw or norm and norm_msg == norm:
                await _delete_original_and_linked_reply(group_id, msg_id)
                seen.add(msg_id)
                deleted += 1
    return deleted


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
    """文本引流：包含链接 或 @外部用户（任意 @xxx 均视为外部）。"""
    if not text:
        return False
    has_link = any(x in text for x in ["http://", "https://", "t.me/"])
    mentions = re.findall(r"@(\w+)", text)
    has_external_at = bool(mentions)
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
        _push_listen_log(
            group_id=getattr(message.chat, "id", None),
            user_id=getattr(getattr(message, "from_user", None), "id", None),
            msg_id=getattr(message, "message_id", None),
            text=(message.text or ""),
            verdict="SKIP",
            details="from_user 为空或消息来自机器人",
        )
        return
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        _push_listen_log(
            group_id=message.chat.id,
            user_id=message.from_user.id,
            msg_id=message.message_id,
            text=(message.text or ""),
            verdict="SKIP",
            details="群组总开关 enabled=false，未执行任何检测",
        )
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    text = message.text or ""
    _track_user_message(group_id, user_id, message.message_id, text)

    # 语义广告检测（优先级最高；命中后直接删除不做提醒）
    if cfg.get("semantic_ad_enabled", False) and len((message.text or "").strip()) >= 4:
        if await _check_and_delete_semantic_ad_message(message, text, group_id=group_id, user_id=user_id):
            return
    else:
        # 记录为什么没有进入 AD 检测（方便排查“优先级最高但不执行”）
        reason = []
        if not cfg.get("semantic_ad_enabled", False):
            reason.append("semantic_ad_enabled=false")
        if len((message.text or "").strip()) < 4:
            reason.append("文本长度<4")
        if reason:
            _push_listen_log(
                group_id=group_id,
                user_id=user_id,
                msg_id=message.message_id,
                text=text,
                verdict="PASS",
                details="未进入AD检测: " + "，".join(reason),
            )

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

    report_history_whitelist = cfg.get("report_history_whitelist", []) or []
    report_history_exempt = (
        isinstance(report_history_whitelist, list)
        and (
            str(user_id) in report_history_whitelist
            or (
                message.from_user.username
                and any(
                    isinstance(item, str)
                    and item.strip().lstrip("@").lower() == message.from_user.username.lower()
                    for item in report_history_whitelist
                )
            )
        )
    )
    reported_count = count_user_reported_messages(user_id, group_id)
    threshold = cfg.get("report_history_threshold", 3)
    last_action_count = get_report_history_action_count(group_id, user_id)
    if (
        not report_history_exempt
        and reported_count >= threshold
        and reported_count > last_action_count
    ):
        try:
            mute_hours = min(cfg.get("report_history_mute_hours", 24), REPORT_BAN_HOURS_CAP)
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
            await _delete_original_and_linked_reply(group_id, message.message_id)
            report_action_state[_report_action_key(group_id, user_id)] = {
                "last_trigger_count": reported_count,
                "last_trigger_at": int(time.time()),
            }
            await save_report_action_state()
            display_name = _get_display_name_from_message(message, user_id)
            warning = await bot.send_message(
                group_id,
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：历史被举报消息累计 {reported_count} 条。\n"
                f"🔒 处理结果：已被限制发言 {mute_hours} 小时。\n"
                "如需取消这条历史举报处罚，管理员可点下方豁免。",
                reply_markup=build_report_history_exempt_keyboard(group_id, user_id),
            )
            _track_bot_message(group_id, warning.message_id)
            return
        except Exception as e:
            print(f"历史举报处罚失败: {e}")

    # 多层内容检测：当前仅保留昵称敏感词
    triggers = []
    if not misjudge_exempt:
        if cfg.get("check_display_keywords", True):
            display_name = (message.from_user.full_name or "").lower()
            if any(kw.lower() in display_name for kw in cfg.get("display_keywords", [])):
                triggers.append("昵称词汇")

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
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="RULE_ACTION",
            details="重复发言检测已触发并执行处罚/提醒（详见重复发言模块）",
        )
        return

    # 走到这里说明没有触发任何处罚型规则
    if triggers:
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="PASS",
            details="多层监听触发项: " + "、".join(triggers),
        )
    else:
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="PASS",
            details="未命中AD语义库；多层监听无触发项",
        )


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
                await _enable_semantic_detection_for_group(group_id)
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


@router.message(F.from_user.id.in_(ADMIN_IDS))
async def on_forward_learn_ad(message: Message):
    """
    管理员转发用户消息给机器人：
    1) 学习该条广告文本
    2) 根据原始群ID和用户ID，删除其最近24小时内的全部消息和警告，并学习这些文本
    """
    try:
        f_user = getattr(message, "forward_from", None)
        f_chat = getattr(message, "forward_from_chat", None)
        f_origin = getattr(message, "forward_origin", None)

        # 兼容新版 forward_origin（user/chat/channel）
        if not f_user and f_origin is not None:
            f_user = getattr(f_origin, "sender_user", None)
            if f_chat is None:
                f_chat = getattr(f_origin, "sender_chat", None)

        text = message.text or message.caption or ""
        learned = False
        memory_changed = False
        if text:
            try:
                semantic_ad_detector.add_ad_sample(text)
                learned = True
            except Exception as e:
                print(f"转发学习广告样本失败: {e}")
        # 单群模式兜底：即使 Telegram 转发里不带原群信息，也直接假定唯一受控群
        group_id = f_chat.id if f_chat else _get_only_group_id()
        if not group_id:
            if learned:
                await message.reply("✅ 已学习该转发消息内容，但当前不是单群模式，且转发里没有原群信息，无法精准回群删除。")
            return
        if group_id not in GROUP_IDS:
            only_gid = _get_only_group_id()
            if only_gid is None:
                if learned:
                    await message.reply("✅ 已学习该转发消息内容，但转发来源群不在受控群列表，未执行回群删除。")
                return
            group_id = only_gid

        # 学习成功则自动开启该群的 AD 语义检测
        if learned:
            await _enable_semantic_detection_for_group(group_id)

        # 1) 优先使用 Telegram 直接给出的 uid
        user_id = None
        if f_user:
            user_id = f_user.id
        else:
            user_id = _get_remembered_user_id_by_text(group_id, text)
        if not user_id:
            # 2) 无 uid 时，在该群最近消息里按相同文案反查用户
            matched_user_ids = _find_recent_user_ids_by_text(group_id, text, limit=3)
            if len(matched_user_ids) == 1:
                user_id = matched_user_ids[0]
            elif len(matched_user_ids) > 1:
                # 有多个命中时，优先删最近最像的用户，但同时保留后续按文案删消息的兜底
                user_id = matched_user_ids[0]
            else:
                matched_user_ids = []

        deleted_by_user = False
        if user_id:
            try:
                memory_changed = _remember_forward_match(group_id, user_id, text) or memory_changed
                memory_changed = _remember_recent_user_texts(group_id, user_id) or memory_changed
                await _delete_user_recent_and_warnings(group_id, user_id, orig_msg_id=None)
                deleted_by_user = True
            except Exception as e:
                print(f"转发学习时删除用户消息失败: {e}")

        # 3) 再做一层按同文案删最近消息的兜底，解决 Telegram 不回传 uid/chat 信息的问题
        deleted_by_text = 0
        try:
            deleted_by_text = await _delete_recent_messages_by_text(group_id, text)
        except Exception as e:
            print(f"按文案回群删除失败: {e}")

        if memory_changed:
            asyncio.create_task(save_forward_match_memory())

        if learned:
            if deleted_by_user or deleted_by_text:
                scope = f"群 {group_id}"
                if user_id:
                    await message.reply(f"✅ 已学习广告内容，并已在 {scope} 清理该用户近期发言；同文案兜底删除 {deleted_by_text} 条。")
                else:
                    await message.reply(f"✅ 已学习广告内容；Telegram 未返回原用户信息，已在 {scope} 按同文案兜底删除 {deleted_by_text} 条。")
            else:
                await message.reply(f"✅ 已学习广告内容，但在群 {group_id} 的最近消息缓存里没找到可删除的同文案记录。")
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
            await _delete_original_and_linked_reply(group_id, msg_id)
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

@router.callback_query(F.data.startswith("report_history_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def handle_report_history_exempt(callback: CallbackQuery):
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("已过期", show_alert=True)
            return
        group_id = int(parts[1])
        user_id = int(parts[2])
        cfg = get_group_config(group_id)
        whitelist = cfg.get("report_history_whitelist", []) or []
        if not isinstance(whitelist, list):
            whitelist = []
        user_key = str(user_id)
        if user_key not in whitelist:
            whitelist.append(user_key)
            apply_global_config_value("report_history_whitelist", whitelist)
            await save_config()
        current_count = count_user_reported_messages(user_id, group_id)
        report_action_state[_report_action_key(group_id, user_id)] = {
            "last_trigger_count": max(current_count, get_report_history_action_count(group_id, user_id)),
            "last_trigger_at": int(time.time()),
            "whitelisted_by": callback.from_user.id,
        }
        await save_report_action_state()
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
            pass
        await callback.message.edit_text(
            f"✅ 已将用户 {user_id} 加入举报处罚豁免白名单。\n后续只有出现新的举报增量，才会再次进入处罚判断。",
            reply_markup=None,
        )
        await callback.answer("已豁免")
    except Exception as e:
        print("历史举报豁免异常:", e)
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
                await _enable_semantic_detection_for_group(group_id)
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
            await _delete_original_and_linked_reply(chat_id, media_msg_id)
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
    await load_report_action_state()
    await load_ad_delete_summaries()
    await load_recent_messages_cache()
    await load_forward_match_memory()
    load_repeat_levels()
    load_link_ref_levels()
    await load_media_stats()
    asyncio.create_task(cleanup_deleted_messages())
    asyncio.create_task(cleanup_orphan_replies())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
