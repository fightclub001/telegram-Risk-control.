import asyncio
import concurrent.futures
import heapq
import html
import io
import json
import os
import re
import sqlite3
import time
import hashlib
import zipfile
from copy import deepcopy
from collections import deque
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from urllib import error as urllib_error, request as urllib_request
from urllib.parse import unquote
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ContentType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions, ReplyKeyboardRemove, BufferedInputFile, ChatJoinRequest, ChatMemberUpdated
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== 环境配置 ====================
GROUP_IDS = set()
ADMIN_IDS = set()
KNOWN_GROUP_IDS = set()
SHARED_GROUP_CONFIG_KEY = "__shared__"
GROUP_GUARD_CACHE_TTL_SEC = max(10, int((os.getenv("GROUP_GUARD_CACHE_TTL_SECONDS") or "60").strip()))
group_guard_cache = {}
BOT_SELF_ID: int | None = None
BOT_TIMEZONE = (os.getenv("BOT_TIMEZONE") or os.getenv("TZ") or "Asia/Shanghai").strip()

try:
    for gid in os.getenv("GROUP_IDS", "").strip().split():
        if gid.strip(): 
            GROUP_IDS.add(int(gid.strip()))
    for uid in os.getenv("ADMIN_IDS", "").strip().split():
        if uid.strip(): 
            ADMIN_IDS.add(int(uid.strip()))
    KNOWN_GROUP_IDS.update(GROUP_IDS)
    if not ADMIN_IDS:
        raise ValueError("ADMIN_IDS 为空")
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
# 使用环境变量 DATA_DIR；兼容旧环境变量 CONFIG_DIR，并在常见挂载目录中自动识别已有持久化数据。
# 以下数据均持久化，重启不丢失：CONFIG_FILE（核心面板配置）、DATA_FILE（进行中举报记录）、
# MEDIA_STATS_FILE / REPEAT_LEVEL_FILE（仅用于旧 JSON 迁移）、RECENT_MESSAGES_DB_FILE（最近24小时消息缓存 + 轻量运行时状态）。
_RUNTIME_STATE_FILES = (
    "config.json",
    "reports.json",
    "media_stats.json",
    "repeat_levels.json",
    "forward_match_memory.json",
    "recent_messages.json",
    "recent_messages.db",
    "image_fuzzy_blocks.json",
    "semantic_ads/semantic_ads.db",
)


def _has_runtime_state(path: str) -> bool:
    if not path:
        return False
    try:
        return any(os.path.exists(os.path.join(path, filename)) for filename in _RUNTIME_STATE_FILES)
    except Exception:
        return False


def _resolve_data_dir() -> tuple[str, str]:
    explicit_data_dir = (os.getenv("DATA_DIR") or "").strip()
    legacy_config_dir = (os.getenv("CONFIG_DIR") or "").strip()

    candidates = []
    for value in (
        explicit_data_dir,
        legacy_config_dir,
        "/opt/telegram-risk-control/data",
        "/data",
        "/app/data",
    ):
        if value and value not in candidates:
            candidates.append(value)

    for candidate in candidates:
        if not _has_runtime_state(candidate):
            continue
        if explicit_data_dir and candidate == explicit_data_dir:
            return candidate, "DATA_DIR(existing)"
        if legacy_config_dir and candidate == legacy_config_dir:
            return candidate, "CONFIG_DIR(existing)"
        return candidate, "auto-detected(existing)"

    if explicit_data_dir:
        return explicit_data_dir, "DATA_DIR"
    if legacy_config_dir:
        return legacy_config_dir, "CONFIG_DIR(legacy)"
    return "/opt/telegram-risk-control/data", "default"


DATA_DIR, DATA_DIR_SOURCE = _resolve_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
print(f"[startup] runtime data dir: {DATA_DIR} (source: {DATA_DIR_SOURCE})")
DATA_FILE = os.path.join(DATA_DIR, "reports.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
CONFIG_BACKUP_DIR = os.path.join(DATA_DIR, "backups", "config")
CONFIG_BACKUP_KEEP = max(10, int((os.getenv("CONFIG_BACKUP_KEEP") or "120").strip()))
CONFIG_GUARD_ALLOW_EMPTY_REPEAT_EXEMPT = (
    (os.getenv("CONFIG_GUARD_ALLOW_EMPTY_REPEAT_EXEMPT") or "false").strip().lower()
    in {"1", "true", "yes", "on"}
)
MEDIA_STATS_FILE = os.path.join(DATA_DIR, "media_stats.json")
REPEAT_LEVEL_FILE = os.path.join(DATA_DIR, "repeat_levels.json")
FORWARD_MATCH_FILE = os.path.join(DATA_DIR, "forward_match_memory.json")
RECENT_MESSAGES_FILE = os.path.join(DATA_DIR, "recent_messages.json")
RECENT_MESSAGES_DB_FILE = os.path.join(DATA_DIR, "recent_messages.db")
JOIN_REVIEW_LOG_FILE = os.path.join(DATA_DIR, "join_review_logs.json")
MOD_ACTION_LOG_FILE = os.path.join(DATA_DIR, "moderation_logs.json")
CONFIG_SYNC_URL = (os.getenv("CONFIG_SYNC_URL") or "").strip()
CONFIG_SYNC_TOKEN = (os.getenv("CONFIG_SYNC_TOKEN") or "").strip()
CONFIG_SYNC_TIMEOUT_SEC = max(2, int((os.getenv("CONFIG_SYNC_TIMEOUT_SEC") or "5").strip()))
CONFIG_SYNC_PULL_ON_START = (os.getenv("CONFIG_SYNC_PULL_ON_START") or "true").strip().lower() in {"1", "true", "yes", "on"}
CONFIG_SYNC_PUSH_ON_SAVE = (os.getenv("CONFIG_SYNC_PUSH_ON_SAVE") or "true").strip().lower() in {"1", "true", "yes", "on"}
CONFIG_SYNC_ENABLED = bool(CONFIG_SYNC_URL and CONFIG_SYNC_TOKEN)
IMAGE_FUZZY_SYNC_URL = (os.getenv("IMAGE_FUZZY_SYNC_URL") or "").strip()
if not IMAGE_FUZZY_SYNC_URL and CONFIG_SYNC_URL:
    if CONFIG_SYNC_URL.endswith("/config"):
        IMAGE_FUZZY_SYNC_URL = f"{CONFIG_SYNC_URL[:-7]}/image-fuzzy-blocks"
    else:
        IMAGE_FUZZY_SYNC_URL = f"{CONFIG_SYNC_URL.rstrip('/')}/image-fuzzy-blocks"
IMAGE_FUZZY_SYNC_ENABLED = bool(IMAGE_FUZZY_SYNC_URL and CONFIG_SYNC_TOKEN)
STATE_SYNC_MANIFEST_URL = (os.getenv("STATE_SYNC_MANIFEST_URL") or "").strip()
STATE_SYNC_BUNDLE_URL = (os.getenv("STATE_SYNC_BUNDLE_URL") or "").strip()
if CONFIG_SYNC_URL:
    if CONFIG_SYNC_URL.endswith("/config"):
        _state_sync_base_url = CONFIG_SYNC_URL[:-7]
    else:
        _state_sync_base_url = CONFIG_SYNC_URL.rstrip("/")
    if not STATE_SYNC_MANIFEST_URL:
        STATE_SYNC_MANIFEST_URL = f"{_state_sync_base_url}/state-manifest"
    if not STATE_SYNC_BUNDLE_URL:
        STATE_SYNC_BUNDLE_URL = f"{_state_sync_base_url}/state-bundle"
else:
    _state_sync_base_url = ""
STATE_SYNC_ENABLED = bool(STATE_SYNC_MANIFEST_URL and STATE_SYNC_BUNDLE_URL and CONFIG_SYNC_TOKEN)

reports = {}  # key: (group_id, message_id)
reports_dirty = False
lock = asyncio.Lock()
repeat_warning_msg_id = {}  # (group_id, user_id) -> msg_id of "2次" repeat warning, delete if orig deleted
config = {}
_group_config_default_template: dict[str, Any] | None = None
forward_match_memory = {}  # normalized_text -> {"group_id": int, "user_id": int, "updated_at": int}
forward_match_memory_dirty = False
# 媒体消息举报/点赞（内存即可，按消息维度）
media_reports = {}
media_reports_lock = asyncio.Lock()
media_group_report_index = {}  # (chat_id, media_group_id) -> primary media_msg_id
media_report_last = {}  # (uid,) -> (msg_id, time) 最近一次举报的媒体
media_report_day_count = {}  # (uid, date_str) -> count
pending_media_groups = {}  # (chat_id, media_group_id) -> {"message_ids": [], "caption": str, "first_message_id": int, "user_id": int, "display_name": str, "last_update_ts": float, "repeat_signatures": set[str]}
MEDIA_GROUP_SETTLE_SEC = 2.5
MEDIA_GROUP_STALE_SEC = 5 * 60
MEDIA_REPORT_ENTRY_TTL_SEC = 2 * 3600
MEDIA_REPORT_DELETED_TTL_SEC = 15 * 60
MEDIA_REPORT_LAST_TTL_SEC = 6 * 3600
BIO_WATCH_DELAY_SEC = 2.0
BIO_WATCH_CACHE_HIT_TTL_SEC = max(300, int((os.getenv("BIO_WATCH_HIT_TTL_SECONDS") or "7200").strip()))
BIO_WATCH_CACHE_MISS_TTL_SEC = max(60, int((os.getenv("BIO_WATCH_MISS_TTL_SECONDS") or "1800").strip()))
BIO_WATCH_CACHE_FAIL_TTL_SEC = max(30, int((os.getenv("BIO_WATCH_FAIL_TTL_SECONDS") or "300").strip()))
BIO_WATCH_CACHE_MAX = max(128, int((os.getenv("BIO_WATCH_CACHE_MAX") or "2048").strip()))
BIO_WATCH_WORKER_IDLE_SEC = 0.5
_BIO_WATCH_DEFAULT_CHANNEL_IDS = "-1003816108283"
_BIO_WATCH_DEFAULT_INVITES = "https://t.me/+1byYJLskCfAyMGZk"
BIO_WATCH_PENDING_MAX = max(0, int((os.getenv("BIO_WATCH_PENDING_MAX") or "1000").strip()))
BIO_WATCH_PENDING_PER_USER_MAX = max(1, int((os.getenv("BIO_WATCH_PENDING_PER_USER_MAX") or "3").strip()))
BIO_WATCH_CHECKED_USERS_MAX = max(1000, int((os.getenv("BIO_WATCH_CHECKED_USERS_MAX") or "20000").strip()))
WORKER_THREAD_MAX = max(1, int((os.getenv("WORKER_THREAD_MAX") or "2").strip()))
SEMANTIC_AD_DATA_DIR = os.path.join(DATA_DIR, "semantic_ads")
IMAGE_FUZZY_BLOCK_FILE = os.path.join(DATA_DIR, "image_fuzzy_blocks.json")
SEMANTIC_AD_DB_FILE = os.path.join(SEMANTIC_AD_DATA_DIR, "semantic_ads.db")
ADMIN_STATE_FILE_PATHS = {
    "config.json": CONFIG_FILE,
    "image_fuzzy_blocks.json": IMAGE_FUZZY_BLOCK_FILE,
    "semantic_ads/semantic_ads.db": SEMANTIC_AD_DB_FILE,
}
semantic_ad_detector: Any | None = None
join_approval_avatar_ocr: Any | None = None
join_approval_risk_matcher: Any | None = None
image_fuzzy_blocker: Any | None = None
admin_state_sync_task: asyncio.Task | None = None
admin_state_sync_requested = False
JOIN_APPROVAL_OCR_CACHE_TTL_SECONDS = max(60, int((os.getenv("OCR_CACHE_TTL_SECONDS") or "10800").strip()))
JOIN_APPROVAL_OCR_CACHE_MAX = max(8, int((os.getenv("OCR_CACHE_MAX") or "24").strip()))
JOIN_APPROVAL_DECLINE_AND_BAN = (os.getenv("DECLINE_AND_BAN") or "false").strip().lower() in {"1", "true", "yes", "on"}
JOIN_APPROVAL_REQUEST_TIMEOUT = 10
join_approval_avatar_cache = {}  # file_unique_id -> {ocr_text, normalized_text, is_text_avatar, chinese_char_count, total_char_count, matched_term, timestamp}
join_review_logs = deque(maxlen=200)
moderation_logs = deque(maxlen=200)
join_review_logs_dirty = False
moderation_logs_dirty = False
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
MISJUDGE_BOT_MENTION = "如有误封，请直接联系本群管理员处理。"
USER_MSG_TRACK_MAXLEN = 500
USER_MSG_24H_SEC = 24 * 3600
BOT_MSG_AUTO_DELETE_SEC = 24 * 3600  # 机器人消息24小时后自动删除
BOT_MESSAGE_SWEEP_SEC = 60
RECENT_MESSAGES_FLUSH_SEC = 2
RECENT_MESSAGES_PRUNE_SEC = 10 * 60

# 机器人消息跟踪：(group_id, msg_id) -> expire_at
bot_sent_messages = {}
# 机器人在群里的“引用回复”跟踪：(group_id, bot_reply_msg_id) -> (original_msg_id, created_ts)
bot_reply_links = {}
BOT_REPLY_ORPHAN_MAX_AGE_SEC = 15 * 60
# 同用户连续触发警告防刷屏：(group_id, user_id) -> (last_warning_time, last_warning_msg_id)
user_last_warning = {}
USER_WARNING_COOLDOWN_SEC = 60  # 同用户60秒内只发一条警告
# 已封禁警告消息列表：group_id -> list of warning_msg_id（用于一次性删除所有已封禁警告）
banned_warning_messages = {}
bio_watch_cache = {}  # (group_id, user_id) -> (expires_at, is_match, reason)
bio_watch_checked_users = {}  # (group_id, user_id) -> last_checked_ts，同一用户在同一群内只查一次 bio
bio_watch_pending_heap = []  # (due_ts, seq, group_id, user_id, message_id)
bio_watch_pending_keys = set()  # {(group_id, message_id)}
bio_watch_pending_user_counts = {}  # (group_id, user_id) -> pending count
bio_watch_seq = 0

# ==================== 监听决策日志（仅保留最近10条） ====================
listen_decision_logs = deque(maxlen=10)  # newest appended to right
recent_messages_conn: sqlite3.Connection | None = None
recent_messages_pending_writes = deque()
recent_messages_lock = asyncio.Lock()
recent_messages_last_prune_ts = 0.0
RECENT_MESSAGES_PENDING_MAX = max(1000, int((os.getenv("RECENT_MESSAGES_PENDING_MAX") or "5000").strip()))
SQLITE_INT64_MAX = (1 << 63) - 1
SQLITE_UINT64_MOD = 1 << 64


def _encode_sqlite_u64(value: int) -> int:
    normalized = int(value or 0) % SQLITE_UINT64_MOD
    if normalized > SQLITE_INT64_MAX:
        return normalized - SQLITE_UINT64_MOD
    return normalized


def _decode_sqlite_u64(value: int) -> int:
    normalized = int(value or 0)
    if normalized < 0:
        return normalized + SQLITE_UINT64_MOD
    return normalized


def _normalize_image_hashes_for_storage(image_hashes: dict[str, int] | None) -> tuple[int, int, int]:
    hashes = image_hashes or {}
    return (
        _encode_sqlite_u64(hashes.get("ahash", 0)),
        _encode_sqlite_u64(hashes.get("dhash", 0)),
        _encode_sqlite_u64(hashes.get("phash", 0)),
    )
_BIO_URL_SPACE_RE = re.compile(r"[\s\u200b-\u200f\u2060\ufeff]+")
_BIO_TELEGRAM_HOST_MARKERS = ("t.me/", "telegram.me/", "telegram.dog/", "tg://")


def _ensure_recent_messages_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"]): str(row["type"] or "")
        for row in conn.execute("PRAGMA table_info(recent_messages)").fetchall()
    }
    if "has_image" not in columns:
        conn.execute("ALTER TABLE recent_messages ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0")
    if "image_ahash" not in columns:
        conn.execute("ALTER TABLE recent_messages ADD COLUMN image_ahash INTEGER NOT NULL DEFAULT 0")
    if "image_dhash" not in columns:
        conn.execute("ALTER TABLE recent_messages ADD COLUMN image_dhash INTEGER NOT NULL DEFAULT 0")
    if "image_phash" not in columns:
        conn.execute("ALTER TABLE recent_messages ADD COLUMN image_phash INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recent_messages_image_ts "
        "ON recent_messages(group_id, has_image, ts DESC)"
    )


def _parse_env_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for item in (raw or "").replace(",", " ").split():
        try:
            values.add(int(item.strip()))
        except Exception:
            continue
    return values


def _parse_env_text_set(raw: str) -> set[str]:
    values: set[str] = set()
    for item in (raw or "").replace(",", " ").split():
        cleaned = item.strip()
        if cleaned:
            values.add(cleaned)
    return values


def _remember_group(group_id: int | None) -> None:
    try:
        gid = int(group_id or 0)
    except Exception:
        return
    if gid < 0:
        KNOWN_GROUP_IDS.add(gid)


def _get_managed_group_ids() -> set[int]:
    return set(KNOWN_GROUP_IDS) if KNOWN_GROUP_IDS else set(GROUP_IDS)


def _is_group_chat_message(message: Message) -> bool:
    chat = getattr(message, "chat", None)
    return bool(chat and getattr(chat, "type", None) in {"group", "supergroup"})


def _is_member_present(status: ChatMemberStatus | str | None) -> bool:
    return status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.RESTRICTED,
    }


def _is_bot_admin(member) -> bool:
    status = getattr(member, "status", None)
    return status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


def _has_bot_moderation_capability(member) -> bool:
    if getattr(member, "status", None) == ChatMemberStatus.CREATOR:
        return True
    return bool(
        getattr(member, "can_restrict_members", False)
        or getattr(member, "can_delete_messages", False)
    )


async def _get_bot_self_id() -> int:
    global BOT_SELF_ID
    if BOT_SELF_ID is None:
        BOT_SELF_ID = int((await bot.get_me()).id)
    return BOT_SELF_ID


async def _check_group_guard(group_id: int) -> tuple[bool, str]:
    gid = int(group_id)
    _remember_group(gid)
    try:
        bot_self_id = await _get_bot_self_id()
        me = await bot.get_chat_member(gid, bot_self_id)
    except Exception as e:
        return False, f"bot_member_check_failed:{type(e).__name__}"
    if not _is_bot_admin(me):
        return False, "bot_not_admin"
    if not _has_bot_moderation_capability(me):
        return False, "bot_admin_no_mod_permission"
    for admin_id in sorted(ADMIN_IDS):
        try:
            member = await bot.get_chat_member(gid, int(admin_id))
        except Exception:
            continue
        if _is_member_present(getattr(member, "status", None)):
            return True, f"admin_present:{admin_id}"
    return False, "admin_absent"


async def _is_group_eligible(group_id: int, *, force_refresh: bool = False) -> tuple[bool, str]:
    gid = int(group_id)
    now = time.time()
    if not force_refresh:
        cached = group_guard_cache.get(gid)
        if cached and now < float(cached[0]):
            return bool(cached[1]), str(cached[2])
    ok, reason = await _check_group_guard(gid)
    group_guard_cache[gid] = (now + GROUP_GUARD_CACHE_TTL_SEC, ok, reason)
    return ok, reason


def _invalidate_group_guard_cache(group_id: int | None) -> None:
    try:
        gid = int(group_id or 0)
    except Exception:
        return
    group_guard_cache.pop(gid, None)


def _select_admin_group_id(current_chat_id: int | None = None) -> int | None:
    if current_chat_id is not None:
        try:
            gid = int(current_chat_id)
            if gid < 0:
                _remember_group(gid)
                return gid
        except Exception:
            pass
    managed = sorted(_get_managed_group_ids())
    if not managed:
        return None
    return managed[0]


def _format_ts(ts: int | float, fmt: str = "%m-%d %H:%M:%S") -> str:
    try:
        timestamp = float(ts)
    except Exception:
        timestamp = 0.0
    try:
        if ZoneInfo:
            return datetime.fromtimestamp(timestamp, ZoneInfo(BOT_TIMEZONE)).strftime(fmt)
    except Exception:
        pass
    return time.strftime(fmt, time.localtime(timestamp))


async def _resolve_group_titles(group_ids: list[int]) -> dict[int, str]:
    titles: dict[int, str] = {}
    for gid in group_ids:
        try:
            chat = await bot.get_chat(int(gid))
            title = (getattr(chat, "title", None) or "").strip()
            if not title:
                title = (getattr(chat, "full_name", None) or "").strip()
            titles[int(gid)] = title or "未命名群"
        except Exception:
            titles[int(gid)] = "未命名群"
    return titles


BIO_WATCH_TARGET_CHANNEL_IDS = _parse_env_int_set(os.getenv("BIO_WATCH_CHANNEL_IDS", _BIO_WATCH_DEFAULT_CHANNEL_IDS))
BIO_WATCH_TARGET_CHANNEL_FULL_IDS = {str(item) for item in BIO_WATCH_TARGET_CHANNEL_IDS}
BIO_WATCH_TARGET_CHANNEL_SHORT_IDS = {
    str(item).replace("-100", "", 1)
    for item in BIO_WATCH_TARGET_CHANNEL_IDS
}
BIO_WATCH_DEFAULT_LINKS = [item for item in _parse_env_text_set(os.getenv("BIO_WATCH_INVITE_LINKS", _BIO_WATCH_DEFAULT_INVITES))]


def _normalize_bio_watch_text(text: str) -> str:
    compact = html.unescape((text or "").strip().lower())
    compact = compact.replace("\\/", "/")
    for _ in range(2):
        decoded = unquote(compact)
        if decoded == compact:
            break
        compact = decoded
    return _BIO_URL_SPACE_RE.sub("", compact)


def _extract_bio_watch_invite_tokens(links: list[str]) -> set[str]:
    tokens: set[str] = set()
    for link in links:
        compact = _normalize_bio_watch_text(link)
        for marker in ("t.me/+", "telegram.me/+", "joinchat/", "domain=+", "invite="):
            idx = compact.find(marker)
            if idx >= 0:
                token = compact[idx + len(marker):].split("&", 1)[0].split("/", 1)[0].split("#", 1)[0].strip()
                if token:
                    tokens.add(token)
    return tokens


def _get_bio_watch_links(group_id: int) -> list[str]:
    cfg = get_group_config(group_id)
    raw = cfg.get("bio_watch_blacklist_links")
    if isinstance(raw, list):
        cleaned = [str(item).strip() for item in raw if str(item).strip()]
        if cleaned:
            return cleaned
    return list(BIO_WATCH_DEFAULT_LINKS)


def _match_bio_watch_target(group_id: int, bio_text: str) -> tuple[bool, str]:
    compact = _normalize_bio_watch_text(bio_text)
    if not compact:
        return False, "bio_empty"
    if not any(marker in compact for marker in _BIO_TELEGRAM_HOST_MARKERS):
        return False, "bio_no_tg_link"

    for token in _extract_bio_watch_invite_tokens(_get_bio_watch_links(group_id)):
        if not token:
            continue
        if (
            f"t.me/+{token}" in compact
            or f"telegram.me/+{token}" in compact
            or f"joinchat/{token}" in compact
            or f"domain=+{token}" in compact
            or f"invite={token}" in compact
        ):
            return True, f"invite:{token}"

    for short_id in BIO_WATCH_TARGET_CHANNEL_SHORT_IDS:
        if not short_id:
            continue
        if (
            f"/c/{short_id}" in compact
            or f"domain=c/{short_id}" in compact
            or f"channel={short_id}" in compact
            or f"channel=-100{short_id}" in compact
            or f"chatid=-100{short_id}" in compact
            or f"chat_id=-100{short_id}" in compact
            or f"peer=-100{short_id}" in compact
            or f"startchannel=-100{short_id}" in compact
        ):
            return True, f"channel:{short_id}"

    for full_id in BIO_WATCH_TARGET_CHANNEL_FULL_IDS:
        if not full_id:
            continue
        if (
            f"channel={full_id}" in compact
            or f"chatid={full_id}" in compact
            or f"chat_id={full_id}" in compact
            or f"peer={full_id}" in compact
        ):
            return True, f"channel:{full_id}"

    return False, "bio_other_tg_link"


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


def _join_approval_timeout_kwargs() -> dict:
    timeout = JOIN_APPROVAL_REQUEST_TIMEOUT
    return {
        "request_timeout": timeout,
    }


class _DeferredReplyMessageProxy:
    """只保留 reply 所需最小字段，避免相册聚合阶段把完整 Message 常驻在内存里。"""

    def __init__(self, chat_id: int, message_id: int, user_id: int, display_name: str, caption: str = "") -> None:
        self.chat = SimpleNamespace(id=int(chat_id))
        self.from_user = SimpleNamespace(id=int(user_id), full_name=str(display_name or f"ID {user_id}"), username=None)
        self.message_id = int(message_id)
        self.caption = str(caption or "")

    async def reply(self, text: str, **kwargs):
        kwargs.setdefault("reply_to_message_id", self.message_id)
        kwargs.setdefault("allow_sending_without_reply", True)
        return await bot.send_message(self.chat.id, text, **kwargs)


def get_semantic_ad_detector() -> Any:
    global semantic_ad_detector
    if semantic_ad_detector is None:
        from semantic_ads import SemanticAdDetector

        semantic_ad_detector = SemanticAdDetector(SEMANTIC_AD_DATA_DIR)
    return semantic_ad_detector


def _add_semantic_ad_sample(raw_text: str) -> Any:
    sample = get_semantic_ad_detector().add_ad_sample(raw_text)
    if sample is not None:
        _schedule_admin_state_sync("semantic-ad-add")
    return sample


def _remove_semantic_ad_sample(sample_id: int) -> bool:
    removed = bool(get_semantic_ad_detector().remove_sample(sample_id))
    if removed:
        _schedule_admin_state_sync("semantic-ad-remove")
    return removed


def get_join_approval_avatar_ocr() -> Any:
    global join_approval_avatar_ocr
    if join_approval_avatar_ocr is None:
        from join_approval_avatar_ocr import JoinApprovalAvatarOCR

        join_approval_avatar_ocr = JoinApprovalAvatarOCR()
    return join_approval_avatar_ocr


def get_join_approval_risk_matcher() -> Any:
    global join_approval_risk_matcher
    if join_approval_risk_matcher is None:
        from join_approval_risk_terms import JoinApprovalRiskMatcher

        join_approval_risk_matcher = JoinApprovalRiskMatcher(os.path.dirname(os.path.abspath(__file__)))
    return join_approval_risk_matcher


def get_image_fuzzy_blocker() -> Any:
    global image_fuzzy_blocker
    if image_fuzzy_blocker is None:
        from image_fuzzy_blocker import ImageFuzzyBlocker

        image_fuzzy_blocker = ImageFuzzyBlocker(IMAGE_FUZZY_BLOCK_FILE)
    return image_fuzzy_blocker


def _get_join_approval_terms(group_id: int) -> list[str]:
    try:
        from join_approval_risk_terms import DEFAULT_RISK_TERMS

        cfg = get_group_config(group_id)
        terms = cfg.get("join_approval_avatar_terms")
        if isinstance(terms, list) and terms:
            return [str(item).strip() for item in terms if str(item).strip()]
        return list(DEFAULT_RISK_TERMS)
    except Exception:
        return []


def _match_join_approval_risk_term(group_id: int, text: str) -> str | None:
    try:
        from join_approval_risk_terms import match_terms

        return match_terms(text, _get_join_approval_terms(group_id))
    except Exception:
        return None


def _image_hash_enabled(group_id: int) -> bool:
    cfg = get_group_config(group_id)
    return bool(cfg.get("image_fuzzy_block_enabled", True))


def _image_hash_max_distance(group_id: int) -> int:
    cfg = get_group_config(group_id)
    return max(0, int(cfg.get("image_fuzzy_block_distance", 10) or 10))


def _image_hash_max_single_distance(group_id: int) -> int:
    return max(8, min(16, _image_hash_max_distance(group_id) + 2))


def _image_hash_should_ban(group_id: int) -> bool:
    cfg = get_group_config(group_id)
    return bool(cfg.get("image_fuzzy_ban_on_match", True))


def _prune_join_approval_avatar_cache() -> None:
    now = time.time()
    expired = [
        key
        for key, value in join_approval_avatar_cache.items()
        if now - float(value.get("timestamp", 0)) > JOIN_APPROVAL_OCR_CACHE_TTL_SECONDS
    ]
    for key in expired:
        join_approval_avatar_cache.pop(key, None)
    if len(join_approval_avatar_cache) <= JOIN_APPROVAL_OCR_CACHE_MAX:
        return
    overflow = sorted(
        join_approval_avatar_cache.items(),
        key=lambda item: float(item[1].get("timestamp", 0)),
    )[: len(join_approval_avatar_cache) - JOIN_APPROVAL_OCR_CACHE_MAX]
    for key, _value in overflow:
        join_approval_avatar_cache.pop(key, None)


def _get_join_approval_avatar_cache(file_unique_id: str) -> Any | None:
    entry = join_approval_avatar_cache.get(file_unique_id)
    if not entry:
        return None
    if time.time() - float(entry.get("timestamp", 0)) > JOIN_APPROVAL_OCR_CACHE_TTL_SECONDS:
        join_approval_avatar_cache.pop(file_unique_id, None)
        return None
    return SimpleNamespace(
        extracted_text=str(entry.get("ocr_text", "")),
        normalized_text=str(entry.get("normalized_text", "")),
        is_text_avatar=bool(entry.get("is_text_avatar", False)),
        chinese_char_count=int(entry.get("chinese_char_count", 0)),
        total_char_count=int(entry.get("total_char_count", 0)),
        matched_term=entry.get("matched_term"),
    )


def _set_join_approval_avatar_cache(file_unique_id: str, result: Any) -> None:
    join_approval_avatar_cache[file_unique_id] = {
        "ocr_text": result.extracted_text,
        "normalized_text": result.normalized_text,
        "is_text_avatar": result.is_text_avatar,
        "chinese_char_count": result.chinese_char_count,
        "total_char_count": result.total_char_count,
        "matched_term": result.matched_term,
        "timestamp": time.time(),
    }
    _prune_join_approval_avatar_cache()


def _log_join_review(*, user_id: int, chat_id: int, final_decision: str, reason: str) -> None:
    print(
        "join_review "
        f"user_id={user_id} chat_id={chat_id} "
        f"decision={final_decision} reason={reason}"
    )


def _prune_bio_watch_cache() -> None:
    now = time.time()
    expired = [
        user_id
        for user_id, (expires_at, _is_match, _reason) in list(bio_watch_cache.items())
        if now >= float(expires_at)
    ]
    for user_id in expired:
        bio_watch_cache.pop(user_id, None)
    if len(bio_watch_cache) <= BIO_WATCH_CACHE_MAX:
        return
    overflow = sorted(
        bio_watch_cache.items(),
        key=lambda item: float(item[1][0]),
    )[: len(bio_watch_cache) - BIO_WATCH_CACHE_MAX]
    for user_id, _value in overflow:
        bio_watch_cache.pop(user_id, None)


def _mark_bio_watch_checked(group_id: int, user_id: int) -> None:
    key = (int(group_id), int(user_id))
    bio_watch_checked_users[key] = time.time()
    if len(bio_watch_checked_users) <= BIO_WATCH_CHECKED_USERS_MAX:
        return
    overflow = sorted(
        bio_watch_checked_users,
        key=lambda item: float(bio_watch_checked_users.get(item, 0.0)),
    )[: len(bio_watch_checked_users) - BIO_WATCH_CHECKED_USERS_MAX]
    for item in overflow:
        bio_watch_checked_users.pop(item, None)


def _get_bio_watch_cached(group_id: int, user_id: int) -> tuple[bool, str] | None:
    cache_key = (int(group_id), int(user_id))
    entry = bio_watch_cache.get(cache_key)
    if not entry:
        return None
    expires_at, is_match, reason = entry
    if time.time() >= float(expires_at):
        bio_watch_cache.pop(cache_key, None)
        return None
    return bool(is_match), str(reason or "")


async def _refresh_bio_watch_cache(group_id: int, user_id: int) -> tuple[bool, str]:
    reason = "bio_fetch_failed"
    is_match = False
    ttl = BIO_WATCH_CACHE_FAIL_TTL_SEC
    try:
        chat = await bot.get_chat(int(user_id))
        bio_text = (getattr(chat, "bio", None) or getattr(chat, "description", None) or "").strip()
        is_match, reason = _match_bio_watch_target(group_id, bio_text)
        ttl = BIO_WATCH_CACHE_HIT_TTL_SEC if is_match else BIO_WATCH_CACHE_MISS_TTL_SEC
    except Exception as e:
        reason = f"bio_fetch_failed:{type(e).__name__}"
    bio_watch_cache[(int(group_id), int(user_id))] = (time.time() + max(30, int(ttl)), bool(is_match), str(reason))
    _prune_bio_watch_cache()
    return bool(is_match), str(reason)


def _schedule_bio_watch_check(message: Message) -> None:
    global bio_watch_seq
    if not BIO_WATCH_TARGET_CHANNEL_IDS:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    if message.from_user.id in ADMIN_IDS:
        return
    if not _is_group_chat_message(message):
        return
    _remember_group(message.chat.id)
    group_id = int(message.chat.id)
    user_id = int(message.from_user.id)
    user_key = (group_id, user_id)
    if user_key in bio_watch_checked_users:
        return
    cached = _get_bio_watch_cached(group_id, user_id)
    if cached is not None:
        if not cached[0]:
            _mark_bio_watch_checked(group_id, user_id)
        return
    if bio_watch_pending_user_counts.get(user_key, 0) >= BIO_WATCH_PENDING_PER_USER_MAX:
        return
    if BIO_WATCH_PENDING_MAX and len(bio_watch_pending_heap) >= BIO_WATCH_PENDING_MAX:
        print(
            "bio_watch pending queue full; skip check "
            f"group_id={group_id} user_id={user_id} pending={len(bio_watch_pending_heap)}"
        )
        return
    key = (int(message.chat.id), int(message.message_id))
    if key in bio_watch_pending_keys:
        return
    bio_watch_pending_keys.add(key)
    bio_watch_pending_user_counts[user_key] = bio_watch_pending_user_counts.get(user_key, 0) + 1
    bio_watch_seq += 1
    heapq.heappush(
        bio_watch_pending_heap,
        (time.time() + BIO_WATCH_DELAY_SEC, bio_watch_seq, group_id, user_id, int(message.message_id)),
    )


async def _enforce_bio_watch_message(group_id: int, user_id: int, message_id: int) -> None:
    cached = _get_bio_watch_cached(group_id, user_id)
    if cached is None:
        is_match, reason = await _refresh_bio_watch_cache(group_id, user_id)
    else:
        is_match, reason = cached
    if not is_match:
        if str(reason).startswith("bio_fetch_failed"):
            _push_listen_log(
                group_id=group_id,
                user_id=user_id,
                msg_id=message_id,
                text="",
                verdict="SKIP",
                details=f"简介频道检查未执行成功: {reason}",
            )
        return

    deleted = False
    try:
        await bot.delete_message(group_id, message_id)
        deleted = True
    except TelegramBadRequest:
        return
    except Exception:
        return

    if not deleted:
        return

    _forget_tracked_user_message(group_id, message_id)
    await _delete_linked_bot_replies(group_id, message_id)
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
                can_pin_messages=False,
            ),
            until_date=None,
        )
    except Exception as e:
        print(f"bio watch restrict failed group_id={group_id} user_id={user_id}: {e}")
    await _record_moderation_log(
        group_id=group_id,
        user_id=user_id,
        user_label=f"ID {user_id}",
        action="简介引流封禁",
        reason=f"简介命中目标频道，来源={reason}",
    )
    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message_id,
        text="",
        verdict="RULE_ACTION",
        details=f"简介命中目标频道，消息存活超过 {int(BIO_WATCH_DELAY_SEC)} 秒后已删除并封禁",
    )


async def bio_watch_enforcement_worker() -> None:
    while True:
        if not bio_watch_pending_heap:
            await asyncio.sleep(BIO_WATCH_WORKER_IDLE_SEC)
            continue
        due_ts, _seq, group_id, user_id, message_id = bio_watch_pending_heap[0]
        now = time.time()
        if now < float(due_ts):
            await asyncio.sleep(min(BIO_WATCH_WORKER_IDLE_SEC, max(0.05, float(due_ts) - now)))
            continue
        heapq.heappop(bio_watch_pending_heap)
        bio_watch_pending_keys.discard((int(group_id), int(message_id)))
        user_key = (int(group_id), int(user_id))
        pending_count = bio_watch_pending_user_counts.get(user_key, 0) - 1
        if pending_count > 0:
            bio_watch_pending_user_counts[user_key] = pending_count
        else:
            bio_watch_pending_user_counts.pop(user_key, None)
        try:
            await _enforce_bio_watch_message(int(group_id), int(user_id), int(message_id))
        except Exception as e:
            print(
                "bio watch enforcement failed "
                f"group_id={group_id} user_id={user_id} message_id={message_id}: {e}"
            )
        finally:
            _mark_bio_watch_checked(int(group_id), int(user_id))


def _join_review_reason_text(reason: str) -> str:
    mapping = {
        "avatar_fuzzy_hash_match": "头像命中关键图相似哈希，拒绝入群",
        "avatar_text_risk_term": "文字头像命中敏感词，拒绝入群",
        "avatar_ocr_disabled": "头像OCR未启用",
        "no_avatar": "没有头像",
        "avatar_not_text": "头像不是文字头像",
        "avatar_text_no_risk_term": "文字头像未命中敏感词，允许入群",
        "avatar_fetch_or_ocr_failed": "头像下载或OCR失败",
        "avatar_profile_check_failed": "头像资料检查失败",
        "no_rule_hit": "未触发任何规则",
    }
    return mapping.get(reason, reason or "未知原因")


def _format_join_review_user(user) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    full_name = getattr(user, "full_name", None) or getattr(user, "first_name", None) or "无用户名用户"
    safe_name = str(full_name).replace("\n", " ").strip()
    return f"{safe_name} (ID {getattr(user, 'id', '-')})"


async def load_join_review_logs() -> None:
    global join_review_logs, join_review_logs_dirty
    try:
        if not os.path.exists(JOIN_REVIEW_LOG_FILE):
            join_review_logs = deque(maxlen=200)
            join_review_logs_dirty = False
            return
        with open(JOIN_REVIEW_LOG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raw = []
        join_review_logs = deque(raw[-200:], maxlen=200)
        join_review_logs_dirty = False
    except Exception as e:
        print(f"join review logs load failed: {e}")
        join_review_logs = deque(maxlen=200)
        join_review_logs_dirty = False


async def save_join_review_logs(force: bool = False) -> None:
    global join_review_logs_dirty
    if not force and not join_review_logs_dirty:
        return
    try:
        with open(JOIN_REVIEW_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(list(join_review_logs), f, ensure_ascii=False, indent=2)
        join_review_logs_dirty = False
    except Exception as e:
        print(f"join review logs save failed: {e}")


async def load_moderation_logs() -> None:
    global moderation_logs, moderation_logs_dirty
    try:
        if not os.path.exists(MOD_ACTION_LOG_FILE):
            moderation_logs = deque(maxlen=200)
            moderation_logs_dirty = False
            return
        with open(MOD_ACTION_LOG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raw = []
        moderation_logs = deque(raw[-200:], maxlen=200)
        moderation_logs_dirty = False
    except Exception as e:
        print(f"moderation logs load failed: {e}")
        moderation_logs = deque(maxlen=200)
        moderation_logs_dirty = False


async def save_moderation_logs(force: bool = False) -> None:
    global moderation_logs_dirty
    if not force and not moderation_logs_dirty:
        return
    try:
        with open(MOD_ACTION_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(list(moderation_logs), f, ensure_ascii=False, indent=2)
        moderation_logs_dirty = False
    except Exception as e:
        print(f"moderation logs save failed: {e}")


async def _record_join_review_log(
    *,
    user,
    chat_id: int,
    final_decision: str,
    reason: str,
) -> None:
    global join_review_logs_dirty
    join_review_logs.append(
        {
            "ts": int(time.time()),
            "chat_id": chat_id,
            "user_label": _format_join_review_user(user),
            "decision_label": "拒绝" if final_decision == "decline" else "通过",
            "reason_label": _join_review_reason_text(reason),
        }
    )
    join_review_logs_dirty = True


async def _record_moderation_log(
    *,
    group_id: int,
    user_id: int,
    user_label: str,
    action: str,
    reason: str,
) -> None:
    global moderation_logs_dirty
    moderation_logs.append(
        {
            "ts": int(time.time()),
            "group_id": group_id,
            "user_id": user_id,
            "user_label": user_label,
            "action": action,
            "reason": reason,
        }
    )
    moderation_logs_dirty = True
    await save_moderation_logs(force=True)


@router.chat_join_request()
async def handle_chat_join_request(join_request: ChatJoinRequest):
    """Join approval: no avatar pass; non-text avatar pass; text avatar only declines on sensitive terms."""
    user = join_request.from_user
    user_id = user.id
    chat_id = join_request.chat.id
    _remember_group(chat_id)
    active, reason_guard = await _is_group_eligible(chat_id)
    if not active:
        print(f"join request skipped chat_id={chat_id}: {reason_guard}")
        return
    final_decision = "approve"
    reason = "no_rule_hit"
    approval_ocr = get_join_approval_avatar_ocr()

    if not approval_ocr.ocr_enabled:
        reason = "avatar_ocr_disabled"
    else:
        try:
            photos = await bot.get_user_profile_photos(
                user_id=user_id,
                limit=1,
                **_join_approval_timeout_kwargs(),
            )
            if photos.total_count <= 0 or not photos.photos or not photos.photos[0]:
                reason = "no_avatar"
            else:
                largest = photos.photos[0][-1]
                avatar_result = None
                try:
                    tg_file = await bot.get_file(
                        largest.file_id,
                        **_join_approval_timeout_kwargs(),
                    )
                    if not tg_file.file_path:
                        raise ValueError("empty file_path")
                    downloaded = await bot.download_file(
                        tg_file.file_path,
                        timeout=JOIN_APPROVAL_REQUEST_TIMEOUT,
                    )
                    if downloaded is None:
                        raise ValueError("avatar download returned None")
                    image_bytes = downloaded.getvalue() if isinstance(downloaded, io.BytesIO) else downloaded.read()
                    image_match = await _match_fuzzy_blocked_image(chat_id, image_bytes)
                    if image_match is not None:
                        final_decision = "decline"
                        reason = "avatar_fuzzy_hash_match"
                    else:
                        avatar_result = _get_join_approval_avatar_cache(largest.file_unique_id)
                        if avatar_result is None:
                            avatar_result = await asyncio.to_thread(
                                approval_ocr.analyze_avatar,
                                image_bytes,
                            )
                            _set_join_approval_avatar_cache(largest.file_unique_id, avatar_result)
                except Exception as e:
                    reason = "avatar_fetch_or_ocr_failed"
                    print(f"join avatar fetch/ocr failed user_id={user_id}: {e}")
                    avatar_result = None
                if avatar_result:
                    matched_term = _match_join_approval_risk_term(chat_id, avatar_result.normalized_text or avatar_result.extracted_text)
                    print(
                        "join_avatar_ocr "
                        f"user_id={user_id} chat_id={chat_id} "
                        f"text={avatar_result.extracted_text!r} "
                        f"normalized={avatar_result.normalized_text!r} "
                        f"is_text_avatar={avatar_result.is_text_avatar} "
                        f"matched_term={matched_term!r}"
                    )
                    if avatar_result.is_text_avatar:
                        if matched_term:
                            final_decision = "decline"
                            reason = "avatar_text_risk_term"
                        else:
                            reason = "avatar_text_no_risk_term"
                    else:
                        reason = "avatar_not_text"
        except Exception as e:
            reason = "avatar_profile_check_failed"
            print(f"join profile photo check failed user_id={user_id}: {e}")

    try:
        if final_decision == "decline":
            await bot.decline_chat_join_request(
                chat_id=chat_id,
                user_id=user_id,
                **_join_approval_timeout_kwargs(),
            )
            if JOIN_APPROVAL_DECLINE_AND_BAN:
                try:
                    await bot.ban_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        **_join_approval_timeout_kwargs(),
                    )
                except Exception as e:
                    print(f"join decline ban failed user_id={user_id}: {e}")
        else:
            await bot.approve_chat_join_request(
                chat_id=chat_id,
                user_id=user_id,
                **_join_approval_timeout_kwargs(),
            )
    finally:
        _log_join_review(
            user_id=user_id,
            chat_id=chat_id,
            final_decision=final_decision,
            reason=reason,
        )
        await _record_join_review_log(
            user=user,
            chat_id=chat_id,
            final_decision=final_decision,
            reason=reason,
        )
        await save_join_review_logs(force=True)


@router.my_chat_member()
async def on_my_chat_member_update(event: ChatMemberUpdated):
    """Bot 在群内权限变化时刷新群资格缓存。"""
    chat = getattr(event, "chat", None)
    if not chat:
        return
    _remember_group(chat.id)
    _invalidate_group_guard_cache(chat.id)


@router.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated):
    """管理员进出群时刷新群资格缓存。"""
    chat = getattr(event, "chat", None)
    member = getattr(event, "new_chat_member", None)
    user = getattr(member, "user", None)
    if not chat:
        return
    if user and int(user.id) in ADMIN_IDS:
        _invalidate_group_guard_cache(chat.id)

# ==================== 配置函数 ====================
def _default_group_config():
    """单群默认配置：仅保留当前生产仍需的核心功能。"""
    global _group_config_default_template
    if _group_config_default_template is not None:
        return deepcopy(_group_config_default_template)
    try:
        from join_approval_risk_terms import DEFAULT_RISK_TERMS
        join_terms = list(DEFAULT_RISK_TERMS)
    except Exception:
        join_terms = []
    _group_config_default_template = {
        "enabled": True,
        "repeat_window_seconds": 2 * 3600,
        "repeat_media_window_seconds": 2 * 3600,
        "repeat_max_count": 3,
        "repeat_ban_seconds": 86400,
        "repeat_exempt_keywords": [],  # 含任一词的消息不触发重复发言检测（白名单词）
        "media_unlock_msg_count": 50,
        "media_report_cooldown_sec": 20 * 60,
        "media_report_max_per_day": 3,
        "media_report_delete_threshold": 2,
        "semantic_ad_enabled": False,
        "join_approval_avatar_terms": join_terms,
        "bio_watch_blacklist_links": list(BIO_WATCH_DEFAULT_LINKS),
        "image_fuzzy_block_enabled": True,
        "image_fuzzy_block_distance": 10,
        "image_fuzzy_ban_on_match": True,
    }
    return deepcopy(_group_config_default_template)


def _ensure_group_config_defaults(group_cfg: dict[str, Any]) -> None:
    global _group_config_default_template
    if _group_config_default_template is None:
        _default_group_config()
    defaults = _group_config_default_template or {}
    for key, value in defaults.items():
        if key not in group_cfg:
            group_cfg[key] = deepcopy(value)


def _collect_group_config_keys(groups: dict[str, Any]) -> list[str]:
    keys: set[str] = set()
    for gid in _get_managed_group_ids():
        try:
            if int(gid) < 0:
                keys.add(str(int(gid)))
        except Exception:
            continue
    for raw_key in groups.keys():
        if raw_key == SHARED_GROUP_CONFIG_KEY:
            continue
        try:
            gid = int(raw_key)
        except Exception:
            continue
        if gid < 0:
            keys.add(str(gid))
    return sorted(keys, key=int)


def _safe_load_json_dict(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        return None
    return None


def _write_json_atomic(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_file = f"{path}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, path)


def _write_bytes_atomic(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_file = f"{path}.tmp"
    with open(temp_file, "wb") as f:
        f.write(payload)
    os.replace(temp_file, path)


def _flush_admin_state_for_sync() -> None:
    detector = semantic_ad_detector
    if detector is not None:
        try:
            detector.checkpoint()
        except Exception as e:
            print(f"[state-sync] semantic ad checkpoint failed: {e}")

    blocker = image_fuzzy_blocker
    if blocker is not None:
        try:
            blocker.save()
        except Exception as e:
            print(f"[state-sync] image sample save failed: {e}")


def _build_admin_state_manifest(*, flush: bool = True) -> dict[str, Any]:
    if flush:
        _flush_admin_state_for_sync()

    files: dict[str, Any] = {}
    for rel_path, abs_path in ADMIN_STATE_FILE_PATHS.items():
        if not os.path.exists(abs_path):
            continue
        try:
            stat = os.stat(abs_path)
            digest = hashlib.sha256()
            with open(abs_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    if not chunk:
                        break
                    digest.update(chunk)
            files[rel_path] = {
                "size": int(stat.st_size),
                "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                "sha256": digest.hexdigest(),
            }
        except Exception as e:
            print(f"[state-sync] manifest skip {rel_path}: {e}")
    return {
        "schema": 1,
        "generated_at_ns": time.time_ns(),
        "files": files,
    }


def _extract_manifest_files(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    files = raw.get("files")
    if not isinstance(files, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for rel_path, meta in files.items():
        if rel_path not in ADMIN_STATE_FILE_PATHS or not isinstance(meta, dict):
            continue
        cleaned[rel_path] = {
            "size": int(meta.get("size", 0) or 0),
            "mtime_ns": int(meta.get("mtime_ns", 0) or 0),
            "sha256": str(meta.get("sha256", "") or ""),
        }
    return cleaned


def _compare_admin_state_manifests(local_manifest: dict[str, Any], remote_manifest: dict[str, Any]) -> tuple[set[str], set[str]]:
    local_files = _extract_manifest_files(local_manifest)
    remote_files = _extract_manifest_files(remote_manifest)
    local_newer: set[str] = set()
    remote_newer: set[str] = set()

    for rel_path in set(local_files) | set(remote_files):
        local_meta = local_files.get(rel_path)
        remote_meta = remote_files.get(rel_path)
        if local_meta and not remote_meta:
            local_newer.add(rel_path)
            continue
        if remote_meta and not local_meta:
            remote_newer.add(rel_path)
            continue
        if not local_meta or not remote_meta:
            continue
        if local_meta.get("sha256") == remote_meta.get("sha256"):
            continue
        local_mtime = int(local_meta.get("mtime_ns", 0) or 0)
        remote_mtime = int(remote_meta.get("mtime_ns", 0) or 0)
        if local_mtime > remote_mtime:
            local_newer.add(rel_path)
        elif remote_mtime > local_mtime:
            remote_newer.add(rel_path)
        else:
            # 相同时间戳但内容不同时，优先保留当前节点内容，避免启动时被旧远端反向覆盖。
            local_newer.add(rel_path)
            print(f"[state-sync] same-mtime conflict on {rel_path}; prefer local")
    return local_newer, remote_newer


def _build_admin_state_bundle_bytes() -> bytes:
    manifest = _build_admin_state_manifest(flush=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel_path in sorted(_extract_manifest_files(manifest).keys()):
            abs_path = ADMIN_STATE_FILE_PATHS[rel_path]
            if os.path.exists(abs_path):
                zf.write(abs_path, arcname=rel_path)
    return buffer.getvalue()


def _apply_admin_state_bundle_bytes(bundle: bytes, *, only_paths: set[str] | None = None) -> list[str]:
    if not bundle:
        return []
    applied: list[str] = []
    with zipfile.ZipFile(io.BytesIO(bundle), "r") as zf:
        try:
            manifest_raw = json.loads(zf.read("_manifest.json").decode("utf-8"))
        except Exception as e:
            raise ValueError(f"invalid_state_bundle_manifest:{e}") from e
        manifest_files = _extract_manifest_files(manifest_raw)
        allowed_paths = set(manifest_files.keys()) if only_paths is None else {path for path in only_paths if path in manifest_files}
        for rel_path in sorted(allowed_paths):
            if rel_path not in zf.namelist():
                continue
            target_path = ADMIN_STATE_FILE_PATHS[rel_path]
            raw = zf.read(rel_path)
            _write_bytes_atomic(target_path, raw)
            mtime_ns = int(manifest_files[rel_path].get("mtime_ns", 0) or 0)
            if mtime_ns > 0:
                try:
                    os.utime(target_path, ns=(mtime_ns, mtime_ns))
                except Exception:
                    pass
            applied.append(rel_path)

    if "image_fuzzy_blocks.json" in applied:
        global image_fuzzy_blocker
        image_fuzzy_blocker = None

    if "semantic_ads/semantic_ads.db" in applied:
        global semantic_ad_detector
        detector = semantic_ad_detector
        if detector is not None:
            try:
                detector.close()
            except Exception:
                pass
        semantic_ad_detector = None

    return applied


def _list_config_backup_files(limit: int = 50) -> list[str]:
    try:
        if not os.path.isdir(CONFIG_BACKUP_DIR):
            return []
        files = [
            os.path.join(CONFIG_BACKUP_DIR, name)
            for name in os.listdir(CONFIG_BACKUP_DIR)
            if name.startswith("config-") and name.endswith(".json")
        ]
        files.sort(reverse=True)
        return files[: max(1, int(limit))]
    except Exception:
        return []


def _create_config_backup(snapshot: dict[str, Any], reason: str) -> None:
    try:
        os.makedirs(CONFIG_BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "-", (reason or "snapshot")).strip("-") or "snapshot"
        backup_path = os.path.join(CONFIG_BACKUP_DIR, f"config-{ts}-{safe_reason}.json")
        _write_json_atomic(backup_path, snapshot)
        files = _list_config_backup_files(limit=CONFIG_BACKUP_KEEP + 20)
        for stale_path in files[CONFIG_BACKUP_KEEP:]:
            try:
                os.remove(stale_path)
            except Exception:
                pass
    except Exception as e:
        print(f"[config-backup] create failed: {e}")


def _load_config_with_backup_fallback() -> dict[str, Any]:
    primary = _safe_load_json_dict(CONFIG_FILE)
    if isinstance(primary, dict):
        return primary
    for backup_path in _list_config_backup_files(limit=50):
        restored = _safe_load_json_dict(backup_path)
        if not isinstance(restored, dict):
            continue
        try:
            _write_json_atomic(CONFIG_FILE, restored)
            print(f"[config-backup] restored config from backup: {backup_path}")
        except Exception as e:
            print(f"[config-backup] restore write failed: {e}")
        return restored
    return {"groups": {}}


def _restore_empty_repeat_exempt_from_previous(snapshot: dict[str, Any]) -> None:
    if CONFIG_GUARD_ALLOW_EMPTY_REPEAT_EXEMPT:
        return
    current_groups = snapshot.setdefault("groups", {})
    if not isinstance(current_groups, dict):
        snapshot["groups"] = {}
        current_groups = snapshot["groups"]
    previous = _safe_load_json_dict(CONFIG_FILE) or {}
    previous_groups = previous.get("groups", {}) if isinstance(previous, dict) else {}
    if not isinstance(previous_groups, dict):
        return
    restored = 0
    for gid, prev_cfg in previous_groups.items():
        if gid == SHARED_GROUP_CONFIG_KEY or not isinstance(prev_cfg, dict):
            continue
        prev_kw = prev_cfg.get("repeat_exempt_keywords") or []
        if not isinstance(prev_kw, list) or not prev_kw:
            continue
        cur_cfg = current_groups.get(gid)
        if not isinstance(cur_cfg, dict):
            continue
        cur_kw = cur_cfg.get("repeat_exempt_keywords") or []
        if isinstance(cur_kw, list) and cur_kw:
            continue
        cur_cfg["repeat_exempt_keywords"] = deepcopy(prev_kw)
        restored += 1
    if restored:
        print(f"[config-guard] restored repeat_exempt_keywords from previous snapshot for {restored} group(s)")


def _restore_empty_repeat_exempt_from_backups(snapshot: dict[str, Any]) -> None:
    if CONFIG_GUARD_ALLOW_EMPTY_REPEAT_EXEMPT:
        return
    groups = snapshot.get("groups", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(groups, dict):
        return
    targets = [
        gid
        for gid, cfg in groups.items()
        if gid != SHARED_GROUP_CONFIG_KEY
        and isinstance(cfg, dict)
        and not (isinstance(cfg.get("repeat_exempt_keywords"), list) and cfg.get("repeat_exempt_keywords"))
    ]
    if not targets:
        return
    restored = 0
    pending = set(targets)
    for backup_path in _list_config_backup_files(limit=80):
        backup_cfg = _safe_load_json_dict(backup_path)
        if not isinstance(backup_cfg, dict):
            continue
        backup_groups = backup_cfg.get("groups", {})
        if not isinstance(backup_groups, dict):
            continue
        hit_this_file = 0
        for gid in list(pending):
            bcfg = backup_groups.get(gid)
            if not isinstance(bcfg, dict):
                continue
            bkw = bcfg.get("repeat_exempt_keywords") or []
            if not isinstance(bkw, list) or not bkw:
                continue
            groups[gid]["repeat_exempt_keywords"] = deepcopy(bkw)
            pending.remove(gid)
            restored += 1
            hit_this_file += 1
        if hit_this_file and not pending:
            break
    if restored:
        print(f"[config-guard] restored repeat_exempt_keywords from backups for {restored} group(s)")


def _build_sync_request(
    url: str,
    method: str,
    payload: bytes | None = None,
    *,
    accept: str = "application/json",
    content_type: str = "application/json; charset=utf-8",
) -> urllib_request.Request:
    request = urllib_request.Request(url, method=method)
    request.add_header("Accept", accept)
    request.add_header("Authorization", f"Bearer {CONFIG_SYNC_TOKEN}")
    if payload is not None:
        request.add_header("Content-Type", content_type)
        request.add_header("Content-Length", str(len(payload)))
    return request


def _pull_remote_state_manifest_once() -> dict[str, Any] | None:
    if not (STATE_SYNC_ENABLED and CONFIG_SYNC_PULL_ON_START):
        return None
    try:
        req = _build_sync_request(STATE_SYNC_MANIFEST_URL, "GET")
        with urllib_request.urlopen(req, timeout=CONFIG_SYNC_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        raw = json.loads(body)
        if isinstance(raw, dict):
            return raw
        print("[state-sync] remote manifest is not a JSON object; ignored")
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[state-sync] manifest pull skipped: {e}")
    except Exception as e:
        print(f"[state-sync] manifest pull failed: {e}")
    return None


def _pull_remote_state_bundle_once() -> bytes | None:
    if not (STATE_SYNC_ENABLED and CONFIG_SYNC_PULL_ON_START):
        return None
    try:
        req = _build_sync_request(STATE_SYNC_BUNDLE_URL, "GET", accept="application/zip")
        with urllib_request.urlopen(req, timeout=max(CONFIG_SYNC_TIMEOUT_SEC, 15)) as resp:
            return resp.read()
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError) as e:
        print(f"[state-sync] bundle pull skipped: {e}")
    except Exception as e:
        print(f"[state-sync] bundle pull failed: {e}")
    return None


def _push_remote_state_bundle_once(bundle: bytes) -> bool:
    if not (STATE_SYNC_ENABLED and CONFIG_SYNC_PUSH_ON_SAVE):
        return False
    if not bundle:
        return False
    try:
        req = _build_sync_request(
            STATE_SYNC_BUNDLE_URL,
            "PUT",
            payload=bundle,
            accept="application/json",
            content_type="application/zip",
        )
        with urllib_request.urlopen(req, data=bundle, timeout=max(CONFIG_SYNC_TIMEOUT_SEC, 15)) as resp:
            _ = resp.read()
        return True
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError) as e:
        print(f"[state-sync] bundle push skipped: {e}")
    except Exception as e:
        print(f"[state-sync] bundle push failed: {e}")
    return False


async def _push_admin_state_bundle_if_enabled(reason: str = "") -> bool:
    if not (STATE_SYNC_ENABLED and CONFIG_SYNC_PUSH_ON_SAVE):
        return False
    bundle = await asyncio.to_thread(_build_admin_state_bundle_bytes)
    pushed = await asyncio.to_thread(_push_remote_state_bundle_once, bundle)
    if pushed:
        label = f" ({reason})" if reason else ""
        print(f"[state-sync] admin state bundle pushed{label}")
    return pushed


async def _run_admin_state_sync_loop() -> None:
    global admin_state_sync_requested
    while admin_state_sync_requested:
        admin_state_sync_requested = False
        await asyncio.sleep(0.2)
        await _push_admin_state_bundle_if_enabled(reason="debounced")


def _schedule_admin_state_sync(reason: str = "") -> None:
    global admin_state_sync_task, admin_state_sync_requested
    if not (STATE_SYNC_ENABLED and CONFIG_SYNC_PUSH_ON_SAVE):
        return
    admin_state_sync_requested = True
    if admin_state_sync_task is None or admin_state_sync_task.done():
        admin_state_sync_task = asyncio.create_task(_run_admin_state_sync_loop())
    if reason:
        print(f"[state-sync] sync scheduled: {reason}")


async def _reconcile_admin_state_with_remote() -> None:
    if not STATE_SYNC_ENABLED:
        return

    local_manifest = await asyncio.to_thread(_build_admin_state_manifest, flush=True)
    remote_manifest = await asyncio.to_thread(_pull_remote_state_manifest_once)

    if not isinstance(remote_manifest, dict):
        if _extract_manifest_files(local_manifest):
            await _push_admin_state_bundle_if_enabled(reason="startup-bootstrap")
        return

    local_newer, remote_newer = _compare_admin_state_manifests(local_manifest, remote_manifest)
    if remote_newer:
        bundle = await asyncio.to_thread(_pull_remote_state_bundle_once)
        if bundle:
            applied = await asyncio.to_thread(_apply_admin_state_bundle_bytes, bundle, only_paths=remote_newer)
            if applied:
                print(f"[state-sync] pulled newer remote files: {', '.join(applied)}")
                local_manifest = await asyncio.to_thread(_build_admin_state_manifest, flush=False)

    post_local_newer, _post_remote_newer = _compare_admin_state_manifests(local_manifest, remote_manifest)
    if post_local_newer:
        await _push_admin_state_bundle_if_enabled(reason="startup-merge")


def _pull_remote_config_once() -> dict[str, Any] | None:
    if not (CONFIG_SYNC_ENABLED and CONFIG_SYNC_PULL_ON_START):
        return None
    try:
        req = _build_sync_request(CONFIG_SYNC_URL, "GET")
        with urllib_request.urlopen(req, timeout=CONFIG_SYNC_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        raw = json.loads(body)
        if isinstance(raw, dict):
            return raw
        print("[config-sync] remote payload is not a JSON object; ignored")
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[config-sync] pull skipped: {e}")
    except Exception as e:
        print(f"[config-sync] pull failed: {e}")
    return None


async def _pull_remote_config_if_enabled() -> bool:
    remote_config = await asyncio.to_thread(_pull_remote_config_once)
    if not isinstance(remote_config, dict):
        return False
    try:
        current_local = _safe_load_json_dict(CONFIG_FILE)
        if isinstance(current_local, dict):
            _create_config_backup(current_local, "before-remote-pull")
        _write_json_atomic(CONFIG_FILE, remote_config)
        print("[config-sync] remote config pulled to local snapshot")
        return True
    except Exception as e:
        print(f"[config-sync] failed to persist pulled config: {e}")
        return False


def _push_remote_config_once(snapshot: dict[str, Any]) -> bool:
    if not (CONFIG_SYNC_ENABLED and CONFIG_SYNC_PUSH_ON_SAVE):
        return False
    try:
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        req = _build_sync_request(CONFIG_SYNC_URL, "PUT", payload=payload)
        with urllib_request.urlopen(req, data=payload, timeout=CONFIG_SYNC_TIMEOUT_SEC) as resp:
            _ = resp.read()
        return True
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError) as e:
        print(f"[config-sync] push skipped: {e}")
    except Exception as e:
        print(f"[config-sync] push failed: {e}")
    return False


async def _push_remote_config_if_enabled(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict):
        return
    pushed = await asyncio.to_thread(_push_remote_config_once, snapshot)
    if pushed:
        print("[config-sync] config pushed to remote volume")


def _pull_remote_image_fuzzy_blocks_once() -> list[dict[str, Any]] | None:
    if not (IMAGE_FUZZY_SYNC_ENABLED and CONFIG_SYNC_PULL_ON_START):
        return None
    try:
        req = _build_sync_request(IMAGE_FUZZY_SYNC_URL, "GET")
        with urllib_request.urlopen(req, timeout=CONFIG_SYNC_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        raw = json.loads(body)
        if not isinstance(raw, list):
            print("[image-sync] remote payload is not a JSON array; ignored")
            return None
        cleaned: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                cleaned.append(item)
        return cleaned
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[image-sync] pull skipped: {e}")
    except Exception as e:
        print(f"[image-sync] pull failed: {e}")
    return None


async def _pull_remote_image_fuzzy_blocks_if_enabled() -> bool:
    remote_items = await asyncio.to_thread(_pull_remote_image_fuzzy_blocks_once)
    if not isinstance(remote_items, list):
        return False
    try:
        os.makedirs(os.path.dirname(IMAGE_FUZZY_BLOCK_FILE), exist_ok=True)
        temp_file = f"{IMAGE_FUZZY_BLOCK_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(remote_items, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, IMAGE_FUZZY_BLOCK_FILE)
        global image_fuzzy_blocker
        image_fuzzy_blocker = None
        print("[image-sync] remote image sample library pulled to local snapshot")
        return True
    except Exception as e:
        print(f"[image-sync] failed to persist pulled samples: {e}")
        return False


def _push_remote_image_fuzzy_blocks_once(snapshot: list[dict[str, Any]]) -> bool:
    if not (IMAGE_FUZZY_SYNC_ENABLED and CONFIG_SYNC_PUSH_ON_SAVE):
        return False
    try:
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        req = _build_sync_request(IMAGE_FUZZY_SYNC_URL, "PUT", payload=payload)
        with urllib_request.urlopen(req, data=payload, timeout=CONFIG_SYNC_TIMEOUT_SEC) as resp:
            _ = resp.read()
        return True
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError) as e:
        print(f"[image-sync] push skipped: {e}")
    except Exception as e:
        print(f"[image-sync] push failed: {e}")
    return False


async def _push_remote_image_fuzzy_blocks_if_enabled() -> None:
    if not (IMAGE_FUZZY_SYNC_ENABLED and CONFIG_SYNC_PUSH_ON_SAVE):
        return
    try:
        if os.path.exists(IMAGE_FUZZY_BLOCK_FILE):
            with open(IMAGE_FUZZY_BLOCK_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raw = []
        else:
            raw = []
        snapshot = [item for item in raw if isinstance(item, dict)]
        pushed = await asyncio.to_thread(_push_remote_image_fuzzy_blocks_once, snapshot)
        if pushed:
            print("[image-sync] image sample library pushed to remote volume")
    except Exception as e:
        print(f"[image-sync] local snapshot read failed: {e}")


async def _add_image_fuzzy_sample_and_sync(*, group_id: int, label: str, image_bytes: bytes) -> dict[str, Any]:
    item = await asyncio.to_thread(
        get_image_fuzzy_blocker().add_sample,
        group_id=group_id,
        label=label,
        image_bytes=image_bytes,
    )
    _schedule_admin_state_sync("image-sample-add")
    return item


async def _remove_image_fuzzy_samples_and_sync(*, group_id: int, sample_ids: list[int]) -> list[int]:
    removed = await asyncio.to_thread(
        get_image_fuzzy_blocker().remove_samples,
        group_id=group_id,
        sample_ids=sample_ids,
    )
    if removed:
        _schedule_admin_state_sync("image-sample-remove")
    return removed

async def load_config():
    """从 CONFIG_FILE 加载配置；每个群组独立保存配置，仅对缺失项补默认值。"""
    global config
    try:
        await _reconcile_admin_state_with_remote()
        default = _default_group_config()
        config = _load_config_with_backup_fallback()
        if "groups" not in config or not isinstance(config["groups"], dict):
            config["groups"] = {}
        groups = config["groups"]
        for gid in list(groups.keys()):
            try:
                _remember_group(int(gid))
            except Exception:
                pass

        groups = config.setdefault("groups", {})
        shared_template = groups.get(SHARED_GROUP_CONFIG_KEY)
        if not isinstance(shared_template, dict):
            shared_template = None

        group_keys = _collect_group_config_keys(groups)

        # 旧版本只有一套 __shared__ 配置；当已经知道群组时再迁移，避免空上下文误清空。
        has_concrete_group_cfg = any(
            key != SHARED_GROUP_CONFIG_KEY and isinstance(value, dict)
            for key, value in groups.items()
        )
        if shared_template is not None and not has_concrete_group_cfg and group_keys:
            for gid in group_keys:
                groups[str(gid)] = deepcopy(shared_template)

        for gid in group_keys:
            groups.setdefault(str(gid), deepcopy(shared_template) if shared_template is not None else {})

        obsolete_keys = (
            "check_bio_link",
            "bio_keywords",
            "check_bio_keywords",
            "check_display_keywords",
            "display_keywords",
            "check_message_keywords",
            "message_keywords",
            "check_message_link",
            "message_keyword_normalize",
            "short_msg_detection",
            "short_msg_threshold",
            "min_consecutive_count",
            "time_window_seconds",
            "fill_garbage_detection",
            "fill_garbage_min_raw_len",
            "fill_garbage_max_clean_len",
            "fill_space_ratio",
            "violation_mute_hours",
            "reported_message_threshold",
            "report_history_mute_hours",
            "report_history_threshold",
            "report_history_whitelist",
            "exempt_users",
            "misjudge_whitelist",
            "mild_exempt_whitelist",
            "media_unlock_boosts",
            "media_unlock_whitelist",
            "media_rules_broadcast",
            "media_rules_broadcast_interval_minutes",
            "autoreply",
        )
        for gid, group_cfg in list(groups.items()):
            if gid == SHARED_GROUP_CONFIG_KEY:
                continue
            if not isinstance(group_cfg, dict):
                groups[gid] = deepcopy(default)
                continue
            _ensure_group_config_defaults(group_cfg)
            for obsolete_key in obsolete_keys:
                group_cfg.pop(obsolete_key, None)
        _restore_empty_repeat_exempt_from_backups(config)
        await save_config(sync_remote=False)
    except Exception as e:
        print(f"配置加载失败: {e}")
        config = {"groups": {}}

async def save_config(sync_remote: bool = True):
    """保存配置到 CONFIG_FILE，按群组独立持久化。"""
    try:
        if "groups" not in config or not isinstance(config["groups"], dict):
            config["groups"] = {}
        groups = config["groups"]
        has_concrete_group_cfg = any(
            key != SHARED_GROUP_CONFIG_KEY and isinstance(value, dict)
            for key, value in groups.items()
        )
        if has_concrete_group_cfg:
            groups.pop(SHARED_GROUP_CONFIG_KEY, None)
        elif not isinstance(groups.get(SHARED_GROUP_CONFIG_KEY), dict):
            groups.pop(SHARED_GROUP_CONFIG_KEY, None)
        _restore_empty_repeat_exempt_from_previous(config)
        snapshot = deepcopy(config)
        _write_json_atomic(CONFIG_FILE, snapshot)
        _create_config_backup(snapshot, "save")
        if sync_remote and CONFIG_SYNC_PUSH_ON_SAVE:
            _schedule_admin_state_sync("config-save")
    except Exception as e:
        print(f"配置保存失败: {e}")

def _mark_forward_match_memory_dirty() -> None:
    global forward_match_memory_dirty
    forward_match_memory_dirty = True


async def load_forward_match_memory():
    global forward_match_memory, forward_match_memory_dirty
    try:
        if os.path.exists(FORWARD_MATCH_FILE):
            with open(FORWARD_MATCH_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                compacted = {}
                for key, value in raw.items():
                    unpacked = _unpack_forward_match_value(value)
                    if unpacked is None:
                        continue
                    compacted[key] = _pack_forward_match_value(*unpacked)
                forward_match_memory = compacted
            else:
                forward_match_memory = {}
            _mark_forward_match_memory_dirty()
            await save_forward_match_memory(force=True)
        else:
            forward_match_memory = {}
        forward_match_memory_dirty = False
    except Exception as e:
        print(f"forward match memory load failed: {e}")
        forward_match_memory = {}
        forward_match_memory_dirty = False

async def save_forward_match_memory(force: bool = False):
    global forward_match_memory_dirty
    if not force and not forward_match_memory_dirty:
        return
    try:
        now = int(time.time())
        cutoff = now - USER_MSG_24H_SEC
        stale_keys = []
        for key, value in list(forward_match_memory.items()):
            unpacked = _unpack_forward_match_value(value)
            if unpacked is None or unpacked[2] < cutoff:
                stale_keys.append(key)
        for key in stale_keys:
            forward_match_memory.pop(key, None)
        max_items = 3000
        if len(forward_match_memory) > max_items:
            trim_count = len(forward_match_memory) - max_items
            oldest_keys = sorted(
                forward_match_memory,
                key=lambda key: (_unpack_forward_match_value(forward_match_memory.get(key)) or (0, 0, 0))[2],
            )[:trim_count]
            for key in oldest_keys:
                forward_match_memory.pop(key, None)
        with open(FORWARD_MATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(forward_match_memory, f, ensure_ascii=False, indent=2)
        forward_match_memory_dirty = False
    except Exception as e:
        print(f"forward match memory save failed: {e}")


async def _forward_match_flush_worker() -> None:
    while True:
        await asyncio.sleep(10)
        try:
            await save_forward_match_memory()
        except Exception as e:
            print(f"forward match flush failed: {e}")

def _get_recent_messages_conn() -> sqlite3.Connection:
    global recent_messages_conn
    if recent_messages_conn is None:
        conn = sqlite3.connect(RECENT_MESSAGES_DB_FILE, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-2048")
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA cache_spill=ON")
        conn.execute("PRAGMA wal_autocheckpoint=200")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_messages (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                text TEXT NOT NULL,
                norm_text TEXT NOT NULL,
                PRIMARY KEY (group_id, message_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_messages_user_ts "
            "ON recent_messages(group_id, user_id, ts DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_messages_norm_ts "
            "ON recent_messages(group_id, norm_text, ts DESC)"
        )
        _ensure_recent_messages_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_unlock_stats (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                unlocked INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_unlock_text_counts (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL,
                PRIMARY KEY (group_id, user_id, fingerprint)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_unlock_updated_ts "
            "ON media_unlock_stats(updated_ts DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repeat_violation_levels (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                updated_ts REAL NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_repeat_levels_updated_ts "
            "ON repeat_violation_levels(updated_ts DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS repeat_first_trigger_state (
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                skipped_once INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_repeat_first_trigger_updated_ts "
            "ON repeat_first_trigger_state(updated_ts DESC)"
        )
        recent_messages_conn = conn
    return recent_messages_conn


async def _prune_recent_messages_db(force: bool = False) -> None:
    global recent_messages_last_prune_ts
    now = time.time()
    if not force and (now - recent_messages_last_prune_ts) < RECENT_MESSAGES_PRUNE_SEC:
        return
    cutoff = now - USER_MSG_24H_SEC
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        conn.execute("DELETE FROM recent_messages WHERE ts < ?", (cutoff,))
        conn.execute(
            """
            DELETE FROM recent_messages
            WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT
                        rowid,
                        ROW_NUMBER() OVER (
                            PARTITION BY group_id, user_id
                            ORDER BY ts DESC, message_id DESC
                        ) AS rn
                    FROM recent_messages
                )
                WHERE rn > ?
            )
            """,
            (USER_MSG_TRACK_MAXLEN,),
        )
        conn.commit()
    recent_messages_last_prune_ts = now


def _queue_recent_message_upsert(
    group_id: int,
    user_id: int,
    msg_id: int,
    ts: float,
    text: str,
    image_hashes: dict[str, int] | None = None,
) -> None:
    while len(recent_messages_pending_writes) >= RECENT_MESSAGES_PENDING_MAX:
        recent_messages_pending_writes.popleft()
    has_image = 1 if image_hashes else 0
    image_ahash, image_dhash, image_phash = _normalize_image_hashes_for_storage(image_hashes)
    recent_messages_pending_writes.append(
        (
            "upsert",
            int(group_id),
            int(user_id),
            int(msg_id),
            float(ts),
            str(text or ""),
            _normalize_text(text),
            has_image,
            image_ahash,
            image_dhash,
            image_phash,
        )
    )


def _queue_recent_message_delete(group_id: int, original_msg_id: int) -> None:
    while len(recent_messages_pending_writes) >= RECENT_MESSAGES_PENDING_MAX:
        recent_messages_pending_writes.popleft()
    recent_messages_pending_writes.append(("delete", int(group_id), int(original_msg_id)))


async def _flush_recent_messages_writes(force: bool = False) -> None:
    if not recent_messages_pending_writes and not force:
        return
    conn = _get_recent_messages_conn()
    pending = []
    while recent_messages_pending_writes:
        pending.append(recent_messages_pending_writes.popleft())
    async with recent_messages_lock:
        if pending:
            conn.execute("BEGIN")
            try:
                for item in pending:
                    op = item[0]
                    if op == "upsert":
                        if len(item) >= 11:
                            _, group_id, user_id, msg_id, ts, text, norm_text, has_image, image_ahash, image_dhash, image_phash = item
                        else:
                            _, group_id, user_id, msg_id, ts, text, norm_text = item
                            has_image = 0
                            image_ahash = 0
                            image_dhash = 0
                            image_phash = 0
                        conn.execute(
                            """
                            INSERT INTO recent_messages (
                                group_id, user_id, message_id, ts, text, norm_text,
                                has_image, image_ahash, image_dhash, image_phash
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(group_id, message_id) DO UPDATE SET
                                user_id=excluded.user_id,
                                ts=excluded.ts,
                                text=excluded.text,
                                norm_text=excluded.norm_text,
                                has_image=excluded.has_image,
                                image_ahash=excluded.image_ahash,
                                image_dhash=excluded.image_dhash,
                                image_phash=excluded.image_phash
                            """,
                            (
                                group_id,
                                user_id,
                                msg_id,
                                ts,
                                text,
                                norm_text,
                                has_image,
                                image_ahash,
                                image_dhash,
                                image_phash,
                            ),
                        )
                    elif op == "delete":
                        _, group_id, msg_id = item
                        conn.execute(
                            "DELETE FROM recent_messages WHERE group_id = ? AND message_id = ?",
                            (group_id, msg_id),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    await _prune_recent_messages_db(force=force)


async def _recent_messages_flush_worker() -> None:
    while True:
        await asyncio.sleep(RECENT_MESSAGES_FLUSH_SEC)
        try:
            await _flush_recent_messages_writes()
        except Exception as e:
            print(f"recent messages flush failed: {e}")


async def _migrate_recent_messages_from_legacy_json() -> None:
    if not os.path.exists(RECENT_MESSAGES_FILE):
        return
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute("SELECT 1 FROM recent_messages LIMIT 1").fetchone()
        if row is not None:
            return
    try:
        with open(RECENT_MESSAGES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"legacy recent messages migration skipped: {e}")
        return
    rows = []
    for key, items in (raw or {}).items():
        try:
            gid_str, uid_str = key.split("_", 1)
            gid = int(gid_str)
            uid = int(uid_str)
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        for item in items[-USER_MSG_TRACK_MAXLEN:]:
            if not isinstance(item, list) or len(item) != 3:
                continue
            try:
                msg_id = int(item[0])
                ts = float(item[1])
                text = str(item[2] or "")
            except Exception:
                continue
            rows.append((gid, uid, msg_id, ts, text, _normalize_text(text)))
    if not rows:
        return
    async with recent_messages_lock:
        conn.executemany(
            """
            INSERT OR REPLACE INTO recent_messages (group_id, user_id, message_id, ts, text, norm_text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    await _prune_recent_messages_db(force=True)


async def load_recent_messages_cache():
    try:
        _get_recent_messages_conn()
        await _migrate_recent_messages_from_legacy_json()
        await _prune_recent_messages_db(force=True)
    except Exception as e:
        print(f"recent messages cache load failed: {e}")


async def save_recent_messages_cache():
    try:
        await _flush_recent_messages_writes(force=True)
    except Exception as e:
        print(f"recent messages cache save failed: {e}")


async def _recent_messages_fetch_by_user(group_id: int, user_id: int) -> list[tuple[int, float, str]]:
    cutoff = time.time() - USER_MSG_24H_SEC
    await _flush_recent_messages_writes()
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        rows = conn.execute(
            """
            SELECT message_id, ts, text
            FROM recent_messages
            WHERE group_id = ? AND user_id = ? AND ts >= ?
            ORDER BY ts DESC, message_id DESC
            """,
            (group_id, user_id, cutoff),
        ).fetchall()
    return [(int(row["message_id"]), float(row["ts"]), str(row["text"] or "")) for row in rows]


async def _recent_message_exists(group_id: int, message_id: int) -> bool:
    cutoff = time.time() - USER_MSG_24H_SEC
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute(
            """
            SELECT 1
            FROM recent_messages
            WHERE group_id = ? AND message_id = ? AND ts >= ?
            LIMIT 1
            """,
            (group_id, message_id, cutoff),
        ).fetchone()
    return row is not None


async def _recent_messages_find_user_ids_by_text(group_id: int, text: str, *, limit: int = 3) -> list[int]:
    norm = _normalize_text(text)
    raw = (text or "").strip()
    if not norm and not raw:
        return []
    cutoff = time.time() - USER_MSG_24H_SEC
    conn = _get_recent_messages_conn()
    candidates: list[tuple[float, int]] = []
    async with recent_messages_lock:
        if norm:
            rows = conn.execute(
                """
                SELECT user_id, MAX(ts) AS last_ts
                FROM recent_messages
                WHERE group_id = ? AND ts >= ? AND norm_text = ?
                GROUP BY user_id
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (group_id, cutoff, norm, limit),
            ).fetchall()
            for row in rows:
                candidates.append((float(row["last_ts"]), int(row["user_id"])))
        if len(candidates) < limit and raw:
            like_value = f"%{raw}%"
            rows = conn.execute(
                """
                SELECT user_id, MAX(ts) AS last_ts
                FROM recent_messages
                WHERE group_id = ? AND ts >= ? AND text LIKE ?
                GROUP BY user_id
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (group_id, cutoff, like_value, limit),
            ).fetchall()
            for row in rows:
                uid = int(row["user_id"])
                if uid not in [item[1] for item in candidates]:
                    candidates.append((float(row["last_ts"]), uid))
    candidates.sort(reverse=True)
    return [uid for _ts, uid in candidates[:limit]]


async def _recent_messages_delete_by_text(group_id: int, text: str) -> list[int]:
    norm = _normalize_text(text)
    raw = (text or "").strip()
    if not norm and not raw:
        return []
    cutoff = time.time() - USER_MSG_24H_SEC
    conn = _get_recent_messages_conn()
    message_ids: list[int] = []
    async with recent_messages_lock:
        if norm:
            rows = conn.execute(
                """
                SELECT message_id
                FROM recent_messages
                WHERE group_id = ? AND ts >= ? AND norm_text = ?
                ORDER BY ts DESC, message_id DESC
                """,
                (group_id, cutoff, norm),
            ).fetchall()
            message_ids.extend(int(row["message_id"]) for row in rows)
        if raw:
            like_value = f"%{raw}%"
            rows = conn.execute(
                """
                SELECT message_id
                FROM recent_messages
                WHERE group_id = ? AND ts >= ? AND text LIKE ?
                ORDER BY ts DESC, message_id DESC
                """,
                (group_id, cutoff, like_value),
            ).fetchall()
            for row in rows:
                msg_id = int(row["message_id"])
                if msg_id not in message_ids:
                    message_ids.append(msg_id)
    return message_ids

def _media_key(group_id: int, user_id: int) -> str:
    return f"{group_id}_{user_id}"


def _parse_media_key(key: str) -> tuple[int, int] | None:
    parts = str(key or "").split("_", 1)
    if len(parts) != 2:
        return None
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return None


async def _migrate_media_stats_from_legacy_json() -> None:
    if not os.path.exists(MEDIA_STATS_FILE):
        return
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute("SELECT 1 FROM media_unlock_stats LIMIT 1").fetchone()
        if row is not None:
            return
    try:
        with open(MEDIA_STATS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"媒体统计迁移失败: {e}")
        return

    message_counts = raw.get("message_counts", {}) if isinstance(raw, dict) else {}
    text_counts = raw.get("text_counts", {}) if isinstance(raw, dict) else {}
    unlocked = raw.get("unlocked", {}) if isinstance(raw, dict) else {}
    now = time.time()
    stats_rows: list[tuple[int, int, int, int, float]] = []
    fp_rows: list[tuple[int, int, str, int, float]] = []
    seen_stats: set[tuple[int, int]] = set()

    for raw_key, raw_count in (message_counts or {}).items():
        parsed = _parse_media_key(raw_key)
        if parsed is None:
            continue
        group_id, user_id = parsed
        stats_rows.append(
            (
                group_id,
                user_id,
                max(0, int(raw_count or 0)),
                1 if unlocked.get(raw_key) else 0,
                now,
            )
        )
        seen_stats.add((group_id, user_id))

    for raw_key, raw_unlocked in (unlocked or {}).items():
        parsed = _parse_media_key(raw_key)
        if parsed is None:
            continue
        if parsed in seen_stats:
            continue
        group_id, user_id = parsed
        stats_rows.append((group_id, user_id, 0, 1 if raw_unlocked else 0, now))
        seen_stats.add((group_id, user_id))

    for raw_key, raw_fps in (text_counts or {}).items():
        parsed = _parse_media_key(raw_key)
        if parsed is None or not isinstance(raw_fps, dict):
            continue
        group_id, user_id = parsed
        if parsed not in seen_stats:
            stats_rows.append((group_id, user_id, 0, 1 if unlocked.get(raw_key) else 0, now))
            seen_stats.add((group_id, user_id))
        for fp, raw_count in raw_fps.items():
            try:
                count = max(0, int(raw_count or 0))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            fp_rows.append((group_id, user_id, str(fp), count, now))

    async with recent_messages_lock:
        conn.execute("BEGIN")
        try:
            if stats_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO media_unlock_stats
                    (group_id, user_id, message_count, unlocked, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    stats_rows,
                )
            if fp_rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO media_unlock_text_counts
                    (group_id, user_id, fingerprint, count, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    fp_rows,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


async def _migrate_repeat_levels_from_legacy_json() -> None:
    if not os.path.exists(REPEAT_LEVEL_FILE):
        return
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute("SELECT 1 FROM repeat_violation_levels LIMIT 1").fetchone()
        if row is not None:
            return
    try:
        with open(REPEAT_LEVEL_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"重复违规级别迁移失败: {e}")
        return

    rows: list[tuple[int, int, int, float]] = []
    now = time.time()
    if isinstance(raw, dict):
        for raw_key, raw_level in raw.items():
            parsed = _parse_media_key(raw_key)
            if parsed is None:
                continue
            try:
                level = max(0, int(raw_level or 0))
            except (TypeError, ValueError):
                continue
            if level <= 0:
                continue
            rows.append((parsed[0], parsed[1], level, now))

    async with recent_messages_lock:
        conn.execute("BEGIN")
        try:
            if rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO repeat_violation_levels
                    (group_id, user_id, level, updated_ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


async def load_media_stats() -> None:
    await _migrate_media_stats_from_legacy_json()


async def load_repeat_levels() -> None:
    await _migrate_repeat_levels_from_legacy_json()


async def _get_media_progress(group_id: int, user_id: int) -> tuple[int, bool]:
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute(
            """
            SELECT message_count, unlocked
            FROM media_unlock_stats
            WHERE group_id = ? AND user_id = ?
            """,
            (int(group_id), int(user_id)),
        ).fetchone()
    if row is None:
        return 0, False
    return int(row["message_count"] or 0), bool(row["unlocked"])


async def _can_send_media(group_id: int, user_id: int, username: str | None = None) -> bool:
    """是否已解锁发媒体：仅看合规文本累计是否达到阈值。"""
    _count, unlocked = await _get_media_progress(group_id, user_id)
    return unlocked


async def _increment_media_count(group_id: int, user_id: int, normalized_text: str) -> bool:
    """合规消息计数（同一条超过 10 次不计数）。达到阈值后解锁媒体权限。"""
    cfg = get_group_config(group_id)
    need_count = cfg.get("media_unlock_msg_count", 50)
    fp = _media_text_fingerprint(normalized_text)
    now = time.time()
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute(
            """
            SELECT message_count, unlocked
            FROM media_unlock_stats
            WHERE group_id = ? AND user_id = ?
            """,
            (int(group_id), int(user_id)),
        ).fetchone()
        message_count = int(row["message_count"] or 0) if row else 0
        unlocked = bool(row["unlocked"]) if row else False
        if unlocked:
            return False

        fp_row = conn.execute(
            """
            SELECT count
            FROM media_unlock_text_counts
            WHERE group_id = ? AND user_id = ? AND fingerprint = ?
            """,
            (int(group_id), int(user_id), fp),
        ).fetchone()
        fp_count = int(fp_row["count"] or 0) if fp_row else 0
        if fp_count >= 10:
            return False

        new_count = message_count + 1
        new_unlocked = 1 if new_count >= need_count else 0
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO media_unlock_stats
                (group_id, user_id, message_count, unlocked, updated_ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    message_count = excluded.message_count,
                    unlocked = excluded.unlocked,
                    updated_ts = excluded.updated_ts
                """,
                (int(group_id), int(user_id), new_count, new_unlocked, now),
            )
            if new_unlocked:
                conn.execute(
                    "DELETE FROM media_unlock_text_counts WHERE group_id = ? AND user_id = ?",
                    (int(group_id), int(user_id)),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO media_unlock_text_counts
                    (group_id, user_id, fingerprint, count, updated_ts)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id, fingerprint) DO UPDATE SET
                        count = excluded.count,
                        updated_ts = excluded.updated_ts
                    """,
                    (int(group_id), int(user_id), fp, fp_count + 1, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return bool(new_unlocked)


async def _get_repeat_violation_level(group_id: int, user_id: int) -> int:
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute(
            """
            SELECT level
            FROM repeat_violation_levels
            WHERE group_id = ? AND user_id = ?
            """,
            (int(group_id), int(user_id)),
        ).fetchone()
    if row is None:
        return 0
    return max(0, int(row["level"] or 0))


async def _set_repeat_violation_level(group_id: int, user_id: int, level: int) -> None:
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        conn.execute(
            """
            INSERT INTO repeat_violation_levels
            (group_id, user_id, level, updated_ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                level = excluded.level,
                updated_ts = excluded.updated_ts
            """,
            (int(group_id), int(user_id), max(0, int(level)), time.time()),
        )
        conn.commit()


async def _has_repeat_first_trigger_skipped(group_id: int, user_id: int) -> bool:
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        row = conn.execute(
            """
            SELECT skipped_once
            FROM repeat_first_trigger_state
            WHERE group_id = ? AND user_id = ?
            """,
            (int(group_id), int(user_id)),
        ).fetchone()
    if row is None:
        return False
    return bool(row["skipped_once"])


async def _mark_repeat_first_trigger_skipped(group_id: int, user_id: int) -> None:
    conn = _get_recent_messages_conn()
    async with recent_messages_lock:
        conn.execute(
            """
            INSERT INTO repeat_first_trigger_state
            (group_id, user_id, skipped_once, updated_ts)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                skipped_once = 1,
                updated_ts = excluded.updated_ts
            """,
            (int(group_id), int(user_id), time.time()),
        )
        conn.commit()


def _pack_forward_match_value(group_id: int, user_id: int, updated_at: int) -> list[int]:
    return [int(group_id), int(user_id), int(updated_at)]


def _unpack_forward_match_value(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, dict):
        try:
            return (
                int(value.get("group_id", 0)),
                int(value.get("user_id", 0)),
                int(value.get("updated_at", 0)),
            )
        except Exception:
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except Exception:
            return None
    return None


def _media_text_fingerprint(normalized_text: str) -> str:
    """用短哈希代替原文存储媒体解锁计数，降低内存和落盘体积。"""
    return hashlib.blake2b(normalized_text.encode("utf-8"), digest_size=8).hexdigest()


async def _try_count_media_and_notify(message: Message, group_id: int, user_id: int, cfg: dict) -> None:
    """合规消息计入媒体解锁进度。达到阈值后静默解锁，不再主动群内提示。"""
    _count, unlocked = await _get_media_progress(group_id, user_id)
    if unlocked:
        return
    try:
        norm = _normalize_text(message.text or "")
        if not norm:
            return
        await _increment_media_count(group_id, user_id, norm)
    except Exception as e:
        print(f"媒体计数失败: {e}")


def get_group_config(group_id: int):
    _remember_group(group_id)
    if "groups" not in config or not isinstance(config["groups"], dict):
        config["groups"] = {}
    gid = str(int(group_id))
    shared_cfg = config["groups"].get(SHARED_GROUP_CONFIG_KEY)
    if gid not in config["groups"] or not isinstance(config["groups"].get(gid), dict):
        if isinstance(shared_cfg, dict):
            config["groups"][gid] = deepcopy(shared_cfg)
        else:
            config["groups"][gid] = _default_group_config()
    group_cfg = config["groups"][gid]
    _ensure_group_config_defaults(group_cfg)
    return group_cfg

def get_primary_group_id() -> int:
    managed = sorted(_get_managed_group_ids())
    if not managed:
        raise ValueError("No managed groups discovered")
    return managed[0]


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
    GroupMenu = State()
    EditJoinApprovalTerms = State()
    EditJoinImageHashDistance = State()
    AddJoinImageHashSample = State()
    RemoveJoinImageHashSample = State()
    EditBioBlacklistLinks = State()
    EditRepeatWindow = State()
    EditRepeatMediaWindow = State()
    EditRepeatMaxCount = State()
    EditRepeatBanSec = State()
    EditRepeatExemptKeywords = State()
    EditMediaUnlockMsg = State()
    EditMediaReportCooldown = State()
    EditMediaReportMaxDay = State()
    EditMediaDeleteThreshold = State()
    EditSemanticAdAdd = State()
    EditSemanticAdRemove = State()

# ==================== UI 键盘 ====================
def get_main_menu_keyboard():
    """保留旧入口，进入群组选择。"""
    buttons = [
        [InlineKeyboardButton(text="👥 选择群组", callback_data="choose_group")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_group_picker_keyboard(group_ids: list[int], group_titles: dict[int, str]) -> InlineKeyboardMarkup:
    buttons = []
    for gid in group_ids[:30]:
        title = (group_titles.get(gid) or str(gid)).strip()
        if len(title) > 24:
            title = title[:24] + "..."
        buttons.append([InlineKeyboardButton(text=f"{title} ({gid})", callback_data=f"select_group:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_group_menu_keyboard(group_id: int):
    join_terms = _get_join_approval_terms(group_id)
    buttons = [
        [
            InlineKeyboardButton(text="🧠 广告风控", callback_data=f"submenu_semantic_ad:{group_id}"),
            InlineKeyboardButton(text=f"🛂 入群风控 ({len(join_terms)})", callback_data=f"submenu_join_approval:{group_id}"),
        ],
        [
            InlineKeyboardButton(text="🔁 重复发言", callback_data=f"submenu_repeat:{group_id}"),
            InlineKeyboardButton(text="📎 媒体权限", callback_data=f"submenu_media_perm:{group_id}"),
        ],
        [
            InlineKeyboardButton(text="📣 媒体举报", callback_data=f"submenu_media_report:{group_id}"),
            InlineKeyboardButton(text="🎛️ 基础设置", callback_data=f"submenu_basic:{group_id}"),
        ],
        [
            InlineKeyboardButton(text="🚪 入群记录", callback_data=f"view_join_logs:{group_id}:0"),
            InlineKeyboardButton(text="📝 处理记录", callback_data=f"view_mod_logs:{group_id}:0"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_semantic_ad_summary(title: str, group_id: int) -> str:
    cfg = get_group_config(group_id)
    enabled = "✅ 已开启" if cfg.get("semantic_ad_enabled", False) else "❌ 已关闭"
    image_samples = len(get_image_fuzzy_blocker().list_group_samples(group_id))
    try:
        detector = get_semantic_ad_detector()
        text_samples = int(detector.stats().get("sample_count", 0) or 0)
    except Exception:
        text_samples = 0
    return (
        f"<b>{title}</b> › 广告风控\n\n"
        f"文本语义识别：{enabled}\n"
        f"广告文本样本：{text_samples} 条\n"
        f"广告图片样本：{image_samples} 张\n\n"
        "说明：广告图片库会同时用于群内发图追溯执法，以及入群头像相似图拦截。"
    )


def get_semantic_ad_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    enabled = "✅" if cfg.get("semantic_ad_enabled", False) else "❌"
    samples = get_image_fuzzy_blocker().list_group_samples(group_id)
    buttons = [
        [InlineKeyboardButton(text=f"开关 {enabled}", callback_data=f"toggle_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="➕ 增加广告语句", callback_data=f"add_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="➖ 减少广告语句", callback_data=f"remove_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="📂 广告词库展示", callback_data=f"view_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text=f"🖼️ 广告图片库 ({len(samples)})", callback_data=f"view_semantic_ad_image:{group_id}")],
        [InlineKeyboardButton(text="➕ 增加广告图片", callback_data=f"add_semantic_ad_image:{group_id}")],
        [InlineKeyboardButton(text="➖ 删除广告图片", callback_data=f"remove_semantic_ad_image:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_join_approval_menu_keyboard(group_id: int):
    terms = _get_join_approval_terms(group_id)
    samples = get_image_fuzzy_blocker().list_group_samples(group_id)
    image_enabled = "✅" if _image_hash_enabled(group_id) else "❌"
    buttons = [
        [InlineKeyboardButton(text=f"✏️ 编辑敏感文字 ({len(terms)})", callback_data=f"edit_join_terms:{group_id}")],
        [InlineKeyboardButton(text=f"关键图相似拦截 {image_enabled}", callback_data=f"toggle_join_image_hash:{group_id}")],
        [InlineKeyboardButton(text=f"🎯 相似阈值: {_image_hash_max_distance(group_id)}", callback_data=f"edit_join_image_hash_distance:{group_id}")],
        [InlineKeyboardButton(text=f"🖼️ 广告图片库 ({len(samples)})", callback_data=f"submenu_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _image_hash_return_keyboard(group_id: int, return_menu: str):
    if return_menu.startswith("submenu_semantic_ad:"):
        return get_semantic_ad_menu_keyboard(group_id)
    return get_join_approval_menu_keyboard(group_id)


def _image_hash_context_title(return_menu: str) -> str:
    if return_menu.startswith("submenu_semantic_ad:"):
        return "广告图片库"
    return "关键图样本"

def get_basic_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    enabled = "✅" if cfg.get("enabled") else "❌"
    link_count = len(_get_bio_watch_links(group_id))
    buttons = [
        [InlineKeyboardButton(text=f"状态: {enabled}", callback_data=f"toggle_group:{group_id}")],
        [InlineKeyboardButton(text=f"🔗 黑名单链接 ({link_count})", callback_data=f"edit_bio_links:{group_id}")],
        [InlineKeyboardButton(text="📄 导出监听日志（近10条）", callback_data=f"export_listen_log:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_log_pager(prefix: str, group_id: int, page: int, total: int, page_size: int = 10) -> InlineKeyboardMarkup:
    last_page = max(0, (max(total, 1) - 1) // page_size)
    page = max(0, min(page, last_page))
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"{prefix}:{group_id}:{page-1}"))
    if page < last_page:
        nav.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=f"{prefix}:{group_id}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_join_approval_summary(title: str, group_id: int) -> str:
    terms = _get_join_approval_terms(group_id)
    preview = "\n".join(f"- {term}" for term in terms[:8]) if terms else "（空）"
    if len(terms) > 8:
        preview += f"\n… 共 {len(terms)} 条"
    samples = get_image_fuzzy_blocker().list_group_samples(group_id)
    image_status = "✅ 已开启" if _image_hash_enabled(group_id) else "❌ 已关闭"
    return (
        f"<b>{title}</b> › 入群风控\n\n"
        "当前规则：\n"
        "1. 关键图相似哈希命中：拒绝\n"
        "2. 没有头像：通过\n"
        "3. 头像不是文字：通过\n"
        "4. 文字头像但不含敏感词：通过\n"
        "5. 文字头像且命中敏感词：拒绝\n\n"
        f"敏感词数量：{len(terms)}\n"
        f"关键图拦截：{image_status}\n"
        f"关键图阈值：{_image_hash_max_distance(group_id)}\n"
        f"广告图片库：{len(samples)} 张（在广告风控菜单维护）\n"
        "群内发图命中：删除该用户近期消息并关闭全部权限\n\n"
        f"当前敏感词预览：\n{preview}"
    )

def get_repeat_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    text_w = cfg.get("repeat_window_seconds", 7200)
    media_w = cfg.get("repeat_media_window_seconds", text_w)
    m = cfg.get("repeat_max_count", 3)
    b = cfg.get("repeat_ban_seconds", 86400)
    kw = cfg.get("repeat_exempt_keywords", []) or []
    n_kw = len(kw) if isinstance(kw, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"⏱ 文字窗口: {fmt_duration(text_w)}", callback_data=f"edit_repeat_window:{group_id}")],
        [InlineKeyboardButton(text=f"🖼 媒体窗口: {fmt_duration(media_w)}", callback_data=f"edit_repeat_media_window:{group_id}")],
        [InlineKeyboardButton(text=f"触发次数: {m}次", callback_data=f"edit_repeat_max:{group_id}")],
        [InlineKeyboardButton(text=f"🔇 首次禁言: {fmt_duration(b)}", callback_data=f"edit_repeat_ban:{group_id}")],
        [InlineKeyboardButton(text=f"📋 豁免词(白名单) ({n_kw})", callback_data=f"edit_repeat_exempt:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_perm_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    msg = cfg.get("media_unlock_msg_count", 50)
    buttons = [
        [InlineKeyboardButton(text=f"解锁所需消息数: {msg}", callback_data=f"edit_media_msg:{group_id}")],
        [InlineKeyboardButton(text="⬅️ 返回", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_report_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
    max_day = cfg.get("media_report_max_per_day", 3)
    del_th = cfg.get("media_report_delete_threshold", 2)
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
    """进入管理面板，先选择要单独配置的群组。"""
    if _is_group_chat_message(message):
        _remember_group(message.chat.id)
    group_ids = sorted(_get_managed_group_ids())
    if not group_ids:
        await message.reply("当前尚未发现可管理群组。先把 bot 拉进群并授予管理权限，同时保证管理员账号也在群内。")
        return
    titles = await _resolve_group_titles(group_ids)
    text = (
        "👮 管理员面板\n\n"
        f"可管理群组数: {len(group_ids)}\n"
        "请选择要配置的群组。每个群组的开关和参数独立保存。"
    )
    kb = get_group_picker_keyboard(group_ids, titles)
    await message.reply(text, reply_markup=kb)
    await state.set_state(AdminStates.MainMenu)

# ==================== 回调处理 ====================
@router.callback_query(F.data == "choose_group", F.from_user.id.in_(ADMIN_IDS))
async def choose_group_callback(callback: CallbackQuery, state: FSMContext):
    """选择要单独配置的群组。"""
    group_ids = sorted(_get_managed_group_ids())
    if not group_ids:
        await callback.answer("尚未发现可管理群组。", show_alert=True)
        return
    titles = await _resolve_group_titles(group_ids)
    text = (
        "👮 管理员面板\n\n"
        f"可管理群组数: {len(group_ids)}\n"
        "请选择要配置的群组。每个群组的开关和参数独立保存。"
    )
    kb = get_group_picker_keyboard(group_ids, titles)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.MainMenu)
    await callback.answer()

@router.callback_query(F.data == "group_menu_single", F.from_user.id.in_(ADMIN_IDS))
async def group_menu_single(callback: CallbackQuery, state: FSMContext):
    await choose_group_callback(callback, state)


@router.message(
    StateFilter("*"),
    F.from_user.id.in_(ADMIN_IDS),
    F.forward_origin | F.forward_from | F.forward_from_chat,
)
async def forward_learn_entry(message: Message):
    """任意状态下优先处理管理员转发学习。"""
    await on_forward_learn_ad(message)


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
            "分区：广告风控 / 入群风控 / 消息媒体风控 / 日志与基础设置"
        )
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data == "back_main", F.from_user.id.in_(ADMIN_IDS))
@router.callback_query(F.data == "back_choose_group", F.from_user.id.in_(ADMIN_IDS))
async def back_choose_group(callback: CallbackQuery, state: FSMContext):
    """返回群组选择。"""
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
            f"群组: <code>{group_id}</code>\n"
            f"状态: {status}\n"
            f"可管理群组数: {len(_get_managed_group_ids())}\n\n"
            "当前只管理这个群组的配置。\n"
            "分区：广告风控 / 入群风控 / 消息媒体风控 / 日志与基础设置"
        )
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("submenu_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def semantic_ad_submenu(callback: CallbackQuery):
    """AD机器学习子菜单."""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        text = _build_semantic_ad_summary(title, group_id)
        kb = get_semantic_ad_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("submenu_join_approval:"), F.from_user.id.in_(ADMIN_IDS))
async def join_approval_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        text = _build_join_approval_summary(title, group_id)
        kb = get_join_approval_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("edit_join_terms:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_join_terms_callback(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(group_id=group_id)
        terms = _get_join_approval_terms(group_id)
        text = (
            "编辑入群审批敏感文字\n"
            "一行一个，发送后将覆盖当前列表。\n"
            "发送 /default 恢复默认词表，发送 /clear 清空。\n\n"
            "当前列表：\n"
            + ("\n".join(terms) if terms else "（空）")
        )
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_join_approval:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.EditJoinApprovalTerms)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("toggle_join_image_hash:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_join_image_hash(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = bool(cfg.get("image_fuzzy_block_enabled", True))
        cfg["image_fuzzy_block_enabled"] = not current
        await save_config()
        title = await get_chat_title_safe(callback.bot, group_id)
        await callback.message.edit_text(
            _build_join_approval_summary(title, group_id),
            reply_markup=get_join_approval_menu_keyboard(group_id),
        )
        await callback.answer(f"关键图相似拦截已{'开启' if cfg['image_fuzzy_block_enabled'] else '关闭'}")
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("edit_join_image_hash_distance:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_join_image_hash_distance(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(group_id=group_id)
        current = _image_hash_max_distance(group_id)
        await callback.message.edit_text(
            "设置关键图相似阈值\n"
            "数字越小越严格，越大越宽松。\n"
            "建议范围：6-16。\n\n"
            f"当前值：{current}\n"
            "请发送新数字，发送 /cancel 取消。",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_join_approval:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.EditJoinImageHashDistance)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("add_join_image_hash_sample:"), F.from_user.id.in_(ADMIN_IDS))
async def add_join_image_hash_sample(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(
            group_id=group_id,
            image_hash_return_menu=f"submenu_join_approval:{group_id}",
        )
        await callback.message.edit_text(
            "添加关键图样本\n"
            "请直接发送一张图片，或回复一张图片后再发送任意文字。\n"
            "支持照片和 image/* 文档。\n"
            "发送 /cancel 取消。",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_join_approval:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.AddJoinImageHashSample)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("view_join_image_hash_samples:"), F.from_user.id.in_(ADMIN_IDS))
async def view_join_image_hash_samples(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        items = get_image_fuzzy_blocker().list_group_samples(group_id)
        if not items:
            text = f"<b>{title}</b> › 入群审批 › 关键图样本库\n\n当前没有样本。"
        else:
            lines = []
            for item in items[:50]:
                ts = time.strftime("%m-%d %H:%M", time.localtime(int(item.get("created_at", 0) or 0)))
                lines.append(f"{item['id']}. [{ts}] {item.get('label', '-') or '-'}")
            if len(items) > 50:
                lines.append(f"… 共 {len(items)} 条")
            text = (
                f"<b>{title}</b> › 入群审批 › 关键图样本库\n\n"
                f"当前共 {len(items)} 张：\n" + "\n".join(lines)
            )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_join_approval:{group_id}")]]
        )
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("remove_join_image_hash_sample:"), F.from_user.id.in_(ADMIN_IDS))
async def remove_join_image_hash_sample(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(
            group_id=group_id,
            image_hash_return_menu=f"submenu_join_approval:{group_id}",
        )
        items = get_image_fuzzy_blocker().list_group_samples(group_id)
        preview = "\n".join(
            f"{item['id']}. {item.get('label', '-') or '-'}" for item in items[:20]
        ) if items else "（空）"
        if len(items) > 20:
            preview += f"\n… 共 {len(items)} 条"
        await callback.message.edit_text(
            "删除关键图样本\n"
            "请输入要删除的样本 ID，支持空格或换行分隔。\n"
            "发送 /cancel 取消。\n\n"
            f"当前样本预览：\n{preview}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_join_approval:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.RemoveJoinImageHashSample)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("add_semantic_ad_image:"), F.from_user.id.in_(ADMIN_IDS))
async def add_semantic_ad_image(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(
            group_id=group_id,
            image_hash_return_menu=f"submenu_semantic_ad:{group_id}",
        )
        await callback.message.edit_text(
            "添加广告图片样本\n"
            "请直接发送一张图片，或回复一张图片后再发送任意文字。\n"
            "支持照片和 image/* 文档。\n"
            "上传后会立即学习感知哈希，并用于群内发图执法和入群头像拦截。\n"
            "发送 /cancel 取消。",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_semantic_ad:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.AddJoinImageHashSample)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("view_semantic_ad_image:"), F.from_user.id.in_(ADMIN_IDS))
async def view_semantic_ad_image(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        items = get_image_fuzzy_blocker().list_group_samples(group_id)
        if not items:
            text = f"<b>{title}</b> › 广告图片库\n\n当前没有样本。"
        else:
            lines = []
            for item in items[:50]:
                ts = time.strftime("%m-%d %H:%M", time.localtime(int(item.get("created_at", 0) or 0)))
                lines.append(f"{item['id']}. [{ts}] {item.get('label', '-') or '-'}")
            if len(items) > 50:
                lines.append(f"… 共 {len(items)} 条")
            text = (
                f"<b>{title}</b> › 广告图片库\n\n"
                f"当前共 {len(items)} 张：\n" + "\n".join(lines)
            )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_semantic_ad:{group_id}")]]
        )
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("remove_semantic_ad_image:"), F.from_user.id.in_(ADMIN_IDS))
async def remove_semantic_ad_image(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(
            group_id=group_id,
            image_hash_return_menu=f"submenu_semantic_ad:{group_id}",
        )
        items = get_image_fuzzy_blocker().list_group_samples(group_id)
        preview = "\n".join(
            f"{item['id']}. {item.get('label', '-') or '-'}" for item in items[:20]
        ) if items else "（空）"
        if len(items) > 20:
            preview += f"\n… 共 {len(items)} 条"
        await callback.message.edit_text(
            "删除广告图片样本\n"
            "请输入要删除的样本 ID，支持空格或换行分隔。\n"
            "发送 /cancel 取消。\n\n"
            f"当前样本预览：\n{preview}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_semantic_ad:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.RemoveJoinImageHashSample)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.message(StateFilter(AdminStates.EditJoinApprovalTerms), F.from_user.id.in_(ADMIN_IDS))
async def process_join_terms(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = int(data.get("group_id"))
    if message.text and message.text.strip().lower() == "/clear":
        new_terms: list[str] = []
    elif message.text and message.text.strip().lower() == "/default":
        try:
            from join_approval_risk_terms import DEFAULT_RISK_TERMS

            new_terms = list(DEFAULT_RISK_TERMS)
        except Exception:
            new_terms = []
    else:
        raw_lines = (message.text or "").splitlines()
        new_terms = [line.strip() for line in raw_lines if line.strip()]

    cfg = get_group_config(group_id)
    cfg["join_approval_avatar_terms"] = new_terms
    await save_config()
    kb = get_join_approval_menu_keyboard(group_id)
    preview = "\n".join(f"- {term}" for term in new_terms[:12]) if new_terms else "（空）"
    if len(new_terms) > 12:
        preview += f"\n… 共 {len(new_terms)} 条"
    await message.reply(
        "✅ 已更新入群审批敏感文字列表。\n\n"
        f"当前共 {len(new_terms)} 条：\n{preview}",
        reply_markup=kb,
    )
    await state.set_state(AdminStates.GroupMenu)


@router.message(StateFilter(AdminStates.EditJoinImageHashDistance), F.from_user.id.in_(ADMIN_IDS))
async def process_join_image_hash_distance(message: Message, state: FSMContext):
    try:
        if message.text and message.text.strip() == "/cancel":
            data = await state.get_data()
            group_id = int(data.get("group_id"))
            await message.reply("已取消。", reply_markup=get_join_approval_menu_keyboard(group_id))
            await state.set_state(AdminStates.GroupMenu)
            return
        value = int((message.text or "").strip())
        if value < 0 or value > 64:
            raise ValueError("范围必须在 0-64")
        data = await state.get_data()
        group_id = int(data.get("group_id"))
        cfg = get_group_config(group_id)
        cfg["image_fuzzy_block_distance"] = value
        await save_config()
        await message.reply(
            f"✅ 已更新关键图相似阈值为 {value}",
            reply_markup=get_join_approval_menu_keyboard(group_id),
        )
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"❌ 请输入 0-64 的数字。{e}")


@router.message(StateFilter(AdminStates.AddJoinImageHashSample), F.from_user.id.in_(ADMIN_IDS))
async def process_add_join_image_hash_sample(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = int(data.get("group_id"))
        return_menu = str(data.get("image_hash_return_menu") or f"submenu_join_approval:{group_id}")
        return_kb = _image_hash_return_keyboard(group_id, return_menu)
        context_title = _image_hash_context_title(return_menu)
        if message.text and message.text.strip() == "/cancel":
            await message.reply("已取消。", reply_markup=return_kb)
            await state.set_state(AdminStates.GroupMenu)
            return
        target = message.reply_to_message or message
        image_bytes = await _extract_message_image_bytes(target)
        if not image_bytes:
            await message.reply("❌ 没检测到图片。请直接发送图片，或回复图片后再发送。发送 /cancel 取消。")
            return
        label_source = target.caption or target.text or message.caption or message.text or f"msg:{target.message_id}"
        item = await _add_image_fuzzy_sample_and_sync(
            group_id=group_id,
            label=_clip_text(label_source, 60),
            image_bytes=image_bytes,
        )
        await message.reply(
            f"✅ 已添加{context_title}。\nID: {item['id']}\n标签: {item['label'] or '-'}",
            reply_markup=return_kb,
        )
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        print(f"添加关键图样本失败: {e}")
        await message.reply("❌ 添加关键图样本失败。")


@router.message(StateFilter(AdminStates.RemoveJoinImageHashSample), F.from_user.id.in_(ADMIN_IDS))
async def process_remove_join_image_hash_sample(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = int(data.get("group_id"))
        return_menu = str(data.get("image_hash_return_menu") or f"submenu_join_approval:{group_id}")
        return_kb = _image_hash_return_keyboard(group_id, return_menu)
        context_title = _image_hash_context_title(return_menu)
        if message.text and message.text.strip() == "/cancel":
            await message.reply("已取消。", reply_markup=return_kb)
            await state.set_state(AdminStates.GroupMenu)
            return
        sample_ids: list[int] = []
        for item in re.split(r"[\s,，]+", (message.text or "").strip()):
            if not item:
                continue
            sample_ids.append(int(item))
        removed = await _remove_image_fuzzy_samples_and_sync(
            group_id=group_id,
            sample_ids=sample_ids,
        )
        if removed:
            await message.reply(
                f"✅ 已删除{context_title}: {', '.join(map(str, removed))}",
                reply_markup=return_kb,
            )
            await state.set_state(AdminStates.GroupMenu)
            return
        await message.reply("❌ 没找到对应样本 ID。发送 /cancel 取消。")
    except Exception as e:
        await message.reply(f"❌ 请输入有效的数字 ID。{e}")


@router.callback_query(F.data.startswith("edit_bio_links:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_bio_links_callback(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await state.update_data(group_id=group_id)
        links = _get_bio_watch_links(group_id)
        text = (
            "编辑简介黑名单链接\n"
            "一行一个，发送后将覆盖当前列表。\n"
            "只要用户 bio 中出现这些 t.me/+... 链接之一，就会在其消息成功存活 2 秒后执行删除与封禁。\n"
            "发送 /default 恢复默认列表，发送 /clear 清空。\n\n"
            "当前列表：\n"
            + ("\n".join(links) if links else "（空）")
        )
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data=f"submenu_basic:{group_id}")]]
            ),
        )
        await state.set_state(AdminStates.EditBioBlacklistLinks)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.message(StateFilter(AdminStates.EditBioBlacklistLinks), F.from_user.id.in_(ADMIN_IDS))
async def process_bio_links(message: Message, state: FSMContext):
    data = await state.get_data()
    group_id = int(data.get("group_id"))
    if message.text and message.text.strip().lower() == "/clear":
        new_links: list[str] = []
    elif message.text and message.text.strip().lower() == "/default":
        new_links = list(BIO_WATCH_DEFAULT_LINKS)
    else:
        raw_lines = (message.text or "").splitlines()
        new_links = [line.strip() for line in raw_lines if line.strip()]

    cfg = get_group_config(group_id)
    cfg["bio_watch_blacklist_links"] = new_links
    # 群配置发生变化时，清掉该群现有 bio 命中缓存，避免旧规则继续生效。
    for key in [key for key in bio_watch_cache.keys() if key[0] == group_id]:
        bio_watch_cache.pop(key, None)
    await save_config()
    kb = get_basic_menu_keyboard(group_id)
    preview = "\n".join(f"- {item}" for item in new_links[:12]) if new_links else "（空）"
    if len(new_links) > 12:
        preview += f"\n… 共 {len(new_links)} 条"
    await message.reply(
        "✅ 已更新简介黑名单链接列表。\n\n"
        f"当前共 {len(new_links)} 条：\n{preview}",
        reply_markup=kb,
    )
    await state.set_state(AdminStates.GroupMenu)


@router.callback_query(F.data.startswith("view_join_logs:"), F.from_user.id.in_(ADMIN_IDS))
async def view_join_logs(callback: CallbackQuery):
    try:
        _, group_id_str, page_str = callback.data.split(":", 2)
        group_id = int(group_id_str)
        page = max(0, int(page_str))
        page_size = 10
        items = [
            item for item in join_review_logs
            if int(item.get("chat_id", 0) or 0) == group_id
        ]
        items.sort(key=lambda item: int(item.get("ts", 0) or 0), reverse=True)
        start = page * page_size
        chunk = items[start : start + page_size]
        if not chunk:
            total_logs = len(join_review_logs)
            if total_logs:
                text = (
                    "🚪 入群审批记录\n\n"
                    "当前群组暂无记录。\n"
                    f"系统内共有 {total_logs} 条审批记录，请返回选择对应群组查看。"
                )
            else:
                text = "🚪 入群审批记录\n\n暂无记录。"
        else:
            lines = []
            for idx, item in enumerate(chunk, start=1 + start):
                ts = _format_ts(int(item.get("ts", 0) or 0))
                lines.append(
                    f"{idx}. [{ts}] {item.get('user_label', '-')}\n"
                    f"   结果: {item.get('decision_label', '-')}\n"
                    f"   原因: {item.get('reason_label', '-')}"
                )
            text = f"🚪 入群审批记录（时区: {BOT_TIMEZONE}）\n\n" + "\n".join(lines)
        kb = _build_log_pager("view_join_logs", group_id, page, len(items), page_size)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("view_mod_logs:"), F.from_user.id.in_(ADMIN_IDS))
async def view_mod_logs(callback: CallbackQuery):
    try:
        _, group_id_str, page_str = callback.data.split(":", 2)
        group_id = int(group_id_str)
        page = max(0, int(page_str))
        page_size = 10
        items = [
            item for item in moderation_logs
            if int(item.get("group_id", 0) or 0) == group_id
        ]
        items.sort(key=lambda item: int(item.get("ts", 0) or 0), reverse=True)
        start = page * page_size
        chunk = items[start : start + page_size]
        if not chunk:
            total_logs = len(moderation_logs)
            if total_logs:
                text = (
                    "📝 处理记录\n\n"
                    "当前群组暂无记录。\n"
                    f"系统内共有 {total_logs} 条处理记录，请返回选择对应群组查看。"
                )
            else:
                text = "📝 处理记录\n\n暂无记录。"
        else:
            lines = []
            for idx, item in enumerate(chunk, start=1 + start):
                ts = _format_ts(int(item.get("ts", 0) or 0))
                lines.append(
                    f"{idx}. [{ts}] {item.get('user_label', '-')}\n"
                    f"   动作: {item.get('action', '-')}\n"
                    f"   原因: {item.get('reason', '-')}"
                )
            text = f"📝 处理记录（时区: {BOT_TIMEZONE}）\n\n" + "\n".join(lines)
        kb = _build_log_pager("view_mod_logs", group_id, page, len(items), page_size)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)


@router.callback_query(F.data == "noop", F.from_user.id.in_(ADMIN_IDS))
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


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
        is_forward = bool(
            getattr(message, "forward_origin", None)
            or getattr(message, "forward_from", None)
            or getattr(message, "forward_from_chat", None)
        )
        if is_forward:
            await state.set_state(AdminStates.GroupMenu)
            await on_forward_learn_ad(message)
            return
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
            sample = _add_semantic_ad_sample(ln)
            if sample is None:
                skipped += 1
            else:
                added_ids.append(sample.id)
        # 有新增或确认与现有样本高度相似时，都应开启语义检测，避免“库里有样本但开关仍关闭”
        if (added_ids or skipped) and group_id:
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

        samples = get_semantic_ad_detector().list_samples()
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
            ok = _remove_semantic_ad_sample(sid)
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

# ==================== 重复发言 ====================
@router.callback_query(F.data.startswith("submenu_repeat:"), F.from_user.id.in_(ADMIN_IDS))
async def repeat_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        text_w = cfg.get("repeat_window_seconds", 7200)
        media_w = cfg.get("repeat_media_window_seconds", text_w)
        m = cfg.get("repeat_max_count", 3)
        b = cfg.get("repeat_ban_seconds", 86400)
        kw = cfg.get("repeat_exempt_keywords", []) or []
        n_kw = len(kw) if isinstance(kw, list) else 0
        text = (
            f"<b>{title}</b> › 重复发言\n\n"
            f"⏱ 文字窗口: {fmt_duration(text_w)}\n"
            f"🖼 媒体窗口: {fmt_duration(media_w)}\n"
            f"触发: {m} 次\n"
            f"🔇 首次禁言: {fmt_duration(b)}\n"
            f"📋 豁免词: {n_kw} 个"
        )
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

@router.callback_query(F.data.startswith("edit_repeat_media_window:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_media_window(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_media_window_seconds", cfg.get("repeat_window_seconds", 7200))
        await callback.message.edit_text(f"媒体重复检测时间窗口（小时）（当前: {current // 3600}）：", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatMediaWindow)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"❌ {str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatMediaWindow), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_media_window(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_media_window_seconds"] = int(message.text.strip()) * 3600
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
        text = f"<b>{title}</b> › 媒体权限\n\n解锁所需合规消息: {msg}"
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

# ==================== 媒体举报 ====================
@router.callback_query(F.data.startswith("submenu_media_report:"), F.from_user.id.in_(ADMIN_IDS))
async def media_report_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
        max_day = cfg.get("media_report_max_per_day", 3)
        del_th = cfg.get("media_report_delete_threshold", 2)
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
        current = cfg.get("media_report_delete_threshold", 2)
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

# ==================== 基础设置 ====================
@router.callback_query(F.data.startswith("submenu_basic:"), F.from_user.id.in_(ADMIN_IDS))
async def basic_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "✅ 运行中" if cfg.get("enabled") else "❌ 已停用"
        links = _get_bio_watch_links(group_id)
        text = (
            f"<b>{title}</b> › 基础设置\n\n"
            f"<code>ID: {group_id}</code>\n"
            f"状态: {status}\n"
            f"简介黑名单链接: {len(links)} 条"
        )
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
        links = _get_bio_watch_links(group_id)
        text = (
            f"<b>{title}</b> › 基础设置\n\n"
            f"<code>ID: {group_id}</code>\n"
            f"状态: {status_display}\n"
            f"简介黑名单链接: {len(links)} 条"
        )
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
                "3) 你的管理员账号是否与 bot 同时在该群内"
            )
            await callback.message.reply(text)
            await callback.answer()
            return

        # 最新在后，导出时按“新→旧”
        lines = [f"{title} 监听日志（近10条，新→旧）", f"导出时间: {_format_ts(time.time(), '%Y-%m-%d %H:%M:%S')} ({BOT_TIMEZONE})", ""]
        for it in reversed(rows):
            gid = it.get("group_id")
            uid = it.get("user_id")
            mid = it.get("msg_id")
            ts = it.get("ts", 0)
            tstr = _format_ts(ts) if ts else "??"
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

# ==================== 检测和回复核心逻辑 ====================
# key: (group_id, user_id, signature_hash) -> deque[timestamp]；避免长文本直接常驻内存
repeat_message_history = {}
repeat_message_history_last = {}  # key -> last_activity_time，用于淘汰
REPEAT_HISTORY_MAX_KEYS = max(500, int((os.getenv("REPEAT_HISTORY_MAX_KEYS") or "3000").strip()))
MEDIA_REPORT_LAST_MAX = 800


def _normalize_text(text: str) -> str:
    """统一文本格式用于重复检测"""
    return " ".join((text or "").strip().split()).lower()

def _remember_forward_match(group_id: int, user_id: int, text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    forward_match_memory[norm] = _pack_forward_match_value(group_id, user_id, int(time.time()))
    _mark_forward_match_memory_dirty()
    return True

def _get_remembered_user_id_by_text(group_id: int, text: str) -> int | None:
    norm = _normalize_text(text)
    if not norm:
        return None
    unpacked = _unpack_forward_match_value(forward_match_memory.get(norm))
    if unpacked is None:
        return None
    remembered_group_id, remembered_user_id, updated_at = unpacked
    if remembered_group_id != int(group_id):
        return None
    if int(time.time()) - updated_at > USER_MSG_24H_SEC:
        return None
    return remembered_user_id

async def _remember_recent_user_texts(group_id: int, user_id: int) -> bool:
    msgs = await _recent_messages_fetch_by_user(group_id, user_id)
    changed = False
    for _msg_id, _ts, txt in msgs:
        if not txt:
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


def _format_user_mention(user_obj, user_id: int) -> str:
    username = getattr(user_obj, "username", None)
    if username:
        return f"@{username}"
    full_name = getattr(user_obj, "full_name", None) or f"用户{user_id}"
    safe_name = (
        str(full_name).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


def _render_reporter_lines(reporter_labels: dict | None) -> str:
    if not isinstance(reporter_labels, dict) or not reporter_labels:
        return "👥 举报人：暂无"
    return "👥 举报人：\n" + "\n".join(f"- {str(label)}" for label in reporter_labels.values())


def _repeat_signature_hash(signature: str) -> int:
    digest = hashlib.blake2b((signature or "").encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


def _prune_repeat_history(now: float, window_sec: int) -> None:
    cutoff = now - max(window_sec, USER_MSG_24H_SEC)
    stale_keys = [
        key for key, last_ts in repeat_message_history_last.items()
        if float(last_ts) < cutoff
    ]
    for key in stale_keys:
        repeat_message_history.pop(key, None)
        repeat_message_history_last.pop(key, None)
    if len(repeat_message_history) < REPEAT_HISTORY_MAX_KEYS:
        return
    trim_count = max(1000, len(repeat_message_history) - REPEAT_HISTORY_MAX_KEYS + 1000)
    for key in sorted(repeat_message_history_last, key=repeat_message_history_last.get)[:trim_count]:
        repeat_message_history.pop(key, None)
        repeat_message_history_last.pop(key, None)


async def _handle_repeat_signature(message: Message, signature: str, repeat_label: str, window_sec: int | None = None) -> bool:
    """按统一规则处理重复文字或重复媒体。"""
    user_id = message.from_user.id
    group_id = message.chat.id
    cfg = get_group_config(group_id)
    if window_sec is None:
        window_sec = cfg.get("repeat_window_seconds", 2 * 3600)
    max_count = cfg.get("repeat_max_count", 3)
    ban_sec = cfg.get("repeat_ban_seconds", 86400)
    now = time.time()
    key = (group_id, user_id, _repeat_signature_hash(signature))

    if key not in repeat_message_history:
        _prune_repeat_history(now, window_sec)
        repeat_message_history[key] = deque(maxlen=max(max_count + 2, 6))
    history = repeat_message_history[key]
    repeat_message_history_last[key] = now

    while history and now - history[0] > window_sec:
        history.popleft()

    history.append(now)
    count = len(history)

    if count == 2:
        if not await _has_repeat_first_trigger_skipped(group_id, user_id):
            await _mark_repeat_first_trigger_skipped(group_id, user_id)
            print(
                "[repeat] first trigger skip warning "
                f"group_id={group_id} user_id={user_id} count=2/{max_count}"
            )
            return False
        warn_text = (
            f"⚠️ 检测到你在 {window_sec // 3600} 小时内重复发送{repeat_label}（2/{max_count}），请勿刷屏。"
        )
        try:
            w = await _send_delayed_reply_if_original_exists(message, warn_text)
            if w:
                repeat_warning_msg_id[(group_id, user_id)] = w.message_id
                _track_group_reply(message, w)
        except Exception:
            pass
        return False

    if count >= max_count:
        current_level = await _get_repeat_violation_level(group_id, user_id)
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
            await _set_repeat_violation_level(group_id, user_id, 1)
            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：在配置时间窗口内多次重复发送{repeat_label}（{max_count}/{max_count}）。\n"
                f"🔒 处理结果：因刷屏已被本群禁言 1 天。\n{MISJUDGE_BOT_MENTION}"
            )
            try:
                await bot.send_message(group_id, notice)
            except Exception:
                pass
            await _record_moderation_log(
                group_id=group_id,
                user_id=user_id,
                user_label=display_name,
                action="禁言",
                reason=f"重复发送{repeat_label}",
            )
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
            await _set_repeat_violation_level(group_id, user_id, 2)
            notice = (
                f"🚫 用户 {display_name}\n"
                f"📌 触发原因：多次在 2 小时内重复发送{repeat_label}，且在被解禁后仍然继续违规。\n"
                f"🔒 处理结果：已被本群永久禁止发言。{MISJUDGE_BOT_MENTION}"
            )
            try:
                await bot.send_message(group_id, notice)
            except Exception:
                pass
            await _record_moderation_log(
                group_id=group_id,
                user_id=user_id,
                user_label=display_name,
                action="永久封禁",
                reason=f"重复发送{repeat_label}",
            )
            return True

    return False


async def handle_repeat_message(message: Message) -> bool:
    """
    检测用户是否在配置时间窗口内重复发送相同内容
    返回 True 表示已经进行了处罚/提醒并且本次消息后续逻辑应中止
    """
    if not message.text:
        return False

    cfg = get_group_config(message.chat.id)
    exempt_kw = cfg.get("repeat_exempt_keywords", []) or []
    if isinstance(exempt_kw, list) and exempt_kw:
        text_lower = (message.text or "").lower()
        if any((k or "").strip().lower() in text_lower for k in exempt_kw if k):
            return False

    norm_text = _normalize_text(message.text)
    if not norm_text:
        return False
    return await _handle_repeat_signature(message, norm_text, "相同内容")


def _get_media_repeat_signature(message: Message) -> str:
    """提取媒体重复检测指纹，优先使用 file_unique_id。"""
    if message.photo:
        try:
            largest = message.photo[-1]
            if largest and getattr(largest, "file_unique_id", None):
                return f"photo:{largest.file_unique_id}"
        except Exception:
            pass
    for attr in ("video", "document", "animation", "audio", "voice", "video_note"):
        obj = getattr(message, attr, None)
        if obj and getattr(obj, "file_unique_id", None):
            return f"{attr}:{obj.file_unique_id}"
    return ""


async def _download_tg_file_bytes(file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    if not tg_file.file_path:
        raise ValueError("empty file_path")
    downloaded = await bot.download_file(tg_file.file_path)
    if downloaded is None:
        raise ValueError("download returned None")
    return downloaded.getvalue() if isinstance(downloaded, io.BytesIO) else downloaded.read()


async def _extract_message_image_bytes(message: Message) -> bytes | None:
    if message.photo:
        largest = message.photo[-1]
        if largest and getattr(largest, "file_id", None):
            return await _download_tg_file_bytes(largest.file_id)
    document = getattr(message, "document", None)
    if document and getattr(document, "file_id", None):
        mime_type = str(getattr(document, "mime_type", "") or "").lower()
        file_name = str(getattr(document, "file_name", "") or "").lower()
        if mime_type.startswith("image/") or file_name.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
            return await _download_tg_file_bytes(document.file_id)
    return None


async def _match_fuzzy_blocked_image(group_id: int, image_bytes: bytes) -> Any | None:
    if not _image_hash_enabled(group_id):
        return None
    try:
        return await asyncio.to_thread(
            get_image_fuzzy_blocker().check_image,
            group_id=group_id,
            image_bytes=image_bytes,
            max_total_distance=_image_hash_max_distance(group_id),
            max_single_distance=_image_hash_max_single_distance(group_id),
        )
    except Exception as e:
        print(f"image fuzzy block match failed group_id={group_id}: {e}")
        return None


async def _build_image_hashes_for_message(image_bytes: bytes) -> dict[str, int] | None:
    try:
        return await asyncio.to_thread(get_image_fuzzy_blocker().build_hashes, image_bytes)
    except Exception as e:
        print(f"build image hashes failed: {e}")
        return None


async def _find_recent_image_matches(
    group_id: int,
    image_bytes: bytes,
    *,
    hinted_user_id: int | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    await _flush_recent_messages_writes()
    candidate_hashes = await _build_image_hashes_for_message(image_bytes)
    if not candidate_hashes:
        return []
    cutoff = time.time() - USER_MSG_24H_SEC
    conn = _get_recent_messages_conn()
    query = (
        """
        SELECT message_id, user_id, ts, text, image_ahash, image_dhash, image_phash
        FROM recent_messages
        WHERE group_id = ? AND ts >= ? AND has_image = 1
        """
    )
    params: list[Any] = [int(group_id), cutoff]
    if hinted_user_id:
        query += " AND user_id = ?"
        params.append(int(hinted_user_id))
    query += " ORDER BY ts DESC, message_id DESC LIMIT 400"
    async with recent_messages_lock:
        rows = conn.execute(query, tuple(params)).fetchall()

    matches: list[dict[str, Any]] = []
    for row in rows:
        sample = {
            "id": int(row["message_id"] or 0),
            "label": str(row["text"] or ""),
            "ahash": _decode_sqlite_u64(row["image_ahash"] or 0),
            "dhash": _decode_sqlite_u64(row["image_dhash"] or 0),
            "phash": _decode_sqlite_u64(row["image_phash"] or 0),
        }
        match = get_image_fuzzy_blocker().match_candidate_hashes(
            sample,
            candidate_hashes,
            max_total_distance=_image_hash_max_distance(group_id),
            max_single_distance=_image_hash_max_single_distance(group_id),
        )
        if match is None:
            continue
        matches.append(
            {
                "group_id": int(group_id),
                "user_id": int(row["user_id"] or 0),
                "message_id": int(row["message_id"] or 0),
                "ts": float(row["ts"] or 0.0),
                "text": str(row["text"] or ""),
                "match": match,
            }
        )
    matches.sort(
        key=lambda item: (
            int(item["match"].total_distance),
            -int(item["match"].matched_hashes),
            -float(item["ts"]),
            -int(item["message_id"]),
        )
    )
    deduped: list[dict[str, Any]] = []
    seen_users: set[int] = set()
    for item in matches:
        user_id = int(item["user_id"])
        if user_id in seen_users:
            continue
        seen_users.add(user_id)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


async def handle_repeat_media_message(message: Message) -> bool:
    """检测同一图片/媒体的重复发送，处罚规则与重复文字一致。"""
    signature = _get_media_repeat_signature(message)
    if not signature:
        return False
    cfg = get_group_config(message.chat.id)
    window_sec = cfg.get("repeat_media_window_seconds", cfg.get("repeat_window_seconds", 2 * 3600))
    return await _handle_repeat_signature(message, signature, "同一图片/媒体", window_sec=window_sec)


async def handle_repeat_media_group_message(message: Message, signatures: set[str]) -> bool:
    """检测同一组相册/媒体是否被重复发送；组内重复图片只算一次。"""
    cleaned = sorted(sig for sig in signatures if sig)
    if not cleaned:
        return False
    group_signature = "album:" + "|".join(cleaned)
    cfg = get_group_config(message.chat.id)
    window_sec = cfg.get("repeat_media_window_seconds", cfg.get("repeat_window_seconds", 2 * 3600))
    return await _handle_repeat_signature(message, group_signature, "同一组图片/媒体", window_sec=window_sec)

def _report_key(gid: int, mid: int) -> tuple:
    return (gid, mid)

def _report_key_str(gid: int, mid: int) -> str:
    return f"{gid}_{mid}"

async def load_data():
    global reports, reports_dirty
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
        reports_dirty = False
    except Exception as e:
        print("数据加载失败（首次正常）:", e)
        reports_dirty = False

def _mark_reports_dirty() -> None:
    global reports_dirty
    reports_dirty = True


async def save_data(force: bool = False):
    global reports_dirty
    if not force and not reports_dirty:
        return
    async with lock:
        try:
            data_to_save = {
                _report_key_str(k[0], k[1]): {**v, "reporters": list(v["reporters"]), "timestamp": v.get("timestamp", time.time())}
                for k, v in reports.items()
            }
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
            reports_dirty = False
        except Exception as e:
            print("保存失败:", e)


async def _reports_flush_worker() -> None:
    while True:
        await asyncio.sleep(10)
        try:
            await save_data()
        except Exception as e:
            print(f"reports flush failed: {e}")

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

def _media_reply_buttons(chat_id: int, media_msg_id: int, report_count: int, garbage_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"举报儿童色情⚠️ {report_count}人", callback_data=f"mr:{chat_id}:{media_msg_id}"),
            InlineKeyboardButton(text=f"举报垃圾信息🚫 {garbage_count}人", callback_data=f"mg:{chat_id}:{media_msg_id}"),
        ]
    ])


def _build_media_summary_text(media_count: int, caption: str) -> str:
    summary = f"📎 媒体消息（共 {media_count} 条）"
    if caption:
        summary += f"\n📝 文字：{_clip_text(caption, 100)}"
    return summary


async def _attach_to_existing_media_group_report(
    chat_id: int,
    media_group_id: str,
    message: Message,
    repeat_signature: str,
) -> bool:
    """
    如果同一 media_group_id 已经生成过举报卡片，则把迟到的媒体并入原卡片，
    避免因为单张消息处理快慢不同而出现两次回复。
    """
    async with media_reports_lock:
        primary_mid = media_group_report_index.get((chat_id, media_group_id))
        if primary_mid is None:
            return False
        key = (chat_id, primary_mid)
        data = media_reports.get(key)
        if not data or data.get("deleted"):
            media_group_report_index.pop((chat_id, media_group_id), None)
            return False
        media_msg_ids = data.setdefault("media_msg_ids", [])
        if message.message_id not in media_msg_ids:
            media_msg_ids.append(message.message_id)
            media_msg_ids.sort()
        if repeat_signature:
            repeat_signatures = data.setdefault("repeat_signatures", set())
            if isinstance(repeat_signatures, set):
                repeat_signatures.add(repeat_signature)
        if message.caption:
            data["caption"] = message.caption
        data["updated_ts"] = time.time()
        reply_id = int(data["reply_msg_id"])
        report_count = len(data.get("reporters", set()))
        garbage_count = len(data.get("garbage_reporters", set()))
        caption = str(data.get("caption") or "")
        summary_text = _build_media_summary_text(len(media_msg_ids), caption)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=reply_id,
            text=summary_text,
            reply_markup=_media_reply_buttons(chat_id, primary_mid, report_count, garbage_count),
        )
    except Exception:
        pass
    return True


async def _finalize_media_group(chat_id: int, media_group_id: str) -> None:
    key = (chat_id, media_group_id)
    while True:
        await asyncio.sleep(MEDIA_GROUP_SETTLE_SEC)
        data = pending_media_groups.get(key)
        if not data:
            return
        last_update_ts = float(data.get("last_update_ts", 0.0) or 0.0)
        if time.time() - last_update_ts >= MEDIA_GROUP_SETTLE_SEC:
            data = pending_media_groups.pop(key, None)
            break
    if not data:
        return

    first_message_id = int(data.get("first_message_id", 0) or 0)
    first_user_id = int(data.get("user_id", 0) or 0)
    first_display_name = str(data.get("display_name") or f"ID {first_user_id}")
    message_ids = list(dict.fromkeys(data.get("message_ids", [])))
    if not first_message_id or not message_ids:
        return
    first_message = _DeferredReplyMessageProxy(
        chat_id=chat_id,
        message_id=first_message_id,
        user_id=first_user_id,
        display_name=first_display_name,
        caption=str(data.get("caption") or ""),
    )
    if await _attach_to_existing_media_group_report(
        chat_id,
        media_group_id,
        first_message,
        "",
    ):
        async with media_reports_lock:
            primary_mid = media_group_report_index.get((chat_id, media_group_id))
            if primary_mid is not None:
                report_data = media_reports.get((chat_id, primary_mid))
                if report_data:
                    ids = report_data.setdefault("media_msg_ids", [])
                    for mid in message_ids:
                        if mid not in ids:
                            ids.append(mid)
                    ids.sort()
                    caption = (data.get("caption") or "").strip()
                    if caption:
                        report_data["caption"] = caption
                    repeat_signatures = report_data.setdefault("repeat_signatures", set())
                    if isinstance(repeat_signatures, set):
                        repeat_signatures.update(set(data.get("repeat_signatures", set())))
                    report_data["updated_ts"] = time.time()
        return
    if await handle_repeat_media_group_message(first_message, set(data.get("repeat_signatures", set()))):
        return

    caption = (data.get("caption") or "").strip()
    summary = _build_media_summary_text(len(message_ids), caption)
    reply = await first_message.reply(
        summary,
        reply_markup=_media_reply_buttons(chat_id, message_ids[0], 0, 0),
    )
    _track_group_reply(first_message, reply)
    async with media_reports_lock:
        media_reports[(chat_id, message_ids[0])] = {
            "chat_id": chat_id,
            "media_msg_id": message_ids[0],
            "media_msg_ids": message_ids,
            "media_group_id": media_group_id,
            "reply_msg_id": reply.message_id,
            "reporters": set(),
            "garbage_reporters": set(),
            "deleted": False,
            "caption": caption,
            "repeat_signatures": set(data.get("repeat_signatures", set())),
            "created_ts": time.time(),
            "updated_ts": time.time(),
        }
        media_group_report_index[(chat_id, media_group_id)] = message_ids[0]

def _message_link(chat_id: int, msg_id: int) -> str:
    """群内消息链接，便于管理员定位"""
    cid = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{cid}/{msg_id}"

async def _delete_user_recent_and_warnings(group_id: int, user_id: int, orig_msg_id: int | None, keep_one_text: str = "", auto_delete_sec: int = 0):
    """删除该用户最近 24 小时内消息、机器人对其的警告，仅保留一条最终公告（带误封联系）。
    auto_delete_sec > 0 时，公告消息在指定秒数后自动删除。"""
    memory_changed = False
    recent_msgs = await _recent_messages_fetch_by_user(group_id, user_id)
    deleted_message_ids: set[int] = set()
    for msg_id, _ts, txt in recent_msgs:
        if txt:
            try:
                _add_semantic_ad_sample(txt)
                memory_changed = _remember_forward_match(group_id, user_id, txt) or memory_changed
            except Exception as e:
                print(f"删除用户消息时学习样本失败: {e}")
        await _delete_original_and_linked_reply(group_id, msg_id)
        deleted_message_ids.add(int(msg_id))
    if memory_changed:
        _mark_forward_match_memory_dirty()
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
    _mark_reports_dirty()
    if orig_msg_id:
        await _delete_original_and_linked_reply(group_id, orig_msg_id)
        deleted_message_ids.add(int(orig_msg_id))
    if keep_one_text:
        try:
            sent = await bot.send_message(group_id, keep_one_text)
            if auto_delete_sec > 0:
                _track_bot_message(group_id, sent.message_id, auto_delete_sec)
        except Exception:
            pass
    return {
        "deleted_messages": len(deleted_message_ids),
        "deleted_warnings": len(to_remove),
        "memory_changed": memory_changed,
    }

@router.message(
    F.photo | F.video | F.voice | F.video_note | F.document | F.animation | F.audio,
)
async def on_media_message(message: Message):
    """媒体消息统一入口：先跑广告匹配，再做媒体权限拦截，最后挂举报按钮。"""
    if not _is_group_chat_message(message):
        return
    active, _reason_guard = await _is_group_eligible(message.chat.id)
    if not active:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    now = time.time()
    image_bytes = await _extract_message_image_bytes(message)
    image_hashes = await _build_image_hashes_for_message(image_bytes) if image_bytes else None
    _track_user_message(
        group_id,
        user_id,
        message.message_id,
        message.caption or "",
        image_hashes=image_hashes,
    )
    _schedule_bio_watch_check(message)
    if await _check_and_block_fuzzy_image_message(
        message,
        group_id=group_id,
        user_id=user_id,
        image_bytes=image_bytes,
    ):
        return
    semantic_text = (message.caption or "").strip()
    if semantic_text:
        if await _check_and_delete_semantic_ad_message(message, semantic_text, group_id=group_id, user_id=user_id):
            return
    if not await _can_send_media(group_id, user_id):
        await _delete_original_and_linked_reply(group_id, message.message_id)
        need_msg = cfg.get("media_unlock_msg_count", 50)
        count, _unlocked = await _get_media_progress(group_id, user_id)
        name = _get_display_name_from_message(message, user_id)
        sk = (group_id, user_id)
        # 计算连续无权限发媒体次数（超过一定时间未再触发则重置）
        strike_count, last_ts = media_no_perm_strikes.get(sk, (0, 0))
        if now - last_ts > MEDIA_NO_PERM_STRIKE_RESET_SEC:
            strike_count = 0
        strike_count += 1
        media_no_perm_strikes[sk] = (strike_count, now)

        if strike_count >= 2:
            # 不再调用 Telegram 成员权限变更，避免群里产生无意义的“修改了权限”系统消息。
            # 未解锁用户后续继续发媒体时，机器人仍会直接删除并提示。
            prev_msg_id = last_media_no_perm_msg.pop(sk, None)
            if prev_msg_id is not None:
                try:
                    await bot.delete_message(group_id, prev_msg_id)
                except Exception:
                    pass
            return

        prev_msg_id = last_media_no_perm_msg.get(sk)
        if prev_msg_id is not None:
            try:
                await bot.delete_message(group_id, prev_msg_id)
            except Exception:
                pass
            finally:
                bot_sent_messages.pop((group_id, prev_msg_id), None)
        sent = await bot.send_message(
            group_id,
            f"⚠️ {name} 尚未解锁发媒体。\n"
            f"📊 您的进度：发送合规消息 {count}/{need_msg}。\n"
            f"达到条数后会自动解锁发图权限。输入「权限」可查询进度。"
        )
        last_media_no_perm_msg[sk] = sent.message_id
        _track_bot_message(group_id, sent.message_id, MEDIA_NO_PERM_DELETE_AFTER_SEC)
        return
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        media_group_id = str(media_group_id)
        repeat_signature = _get_media_repeat_signature(message)
        if await _attach_to_existing_media_group_report(group_id, media_group_id, message, repeat_signature):
            return
        key = (group_id, media_group_id)
        group_data = pending_media_groups.get(key)
        if group_data is None:
            group_data = {
                "message_ids": [],
                "caption": "",
                "first_message_id": message.message_id,
                "user_id": user_id,
                "display_name": _get_display_name_from_message(message, user_id),
                "last_update_ts": now,
                "repeat_signatures": set(),
            }
            pending_media_groups[key] = group_data
            asyncio.create_task(_finalize_media_group(group_id, media_group_id))
        group_data["message_ids"].append(message.message_id)
        group_data["last_update_ts"] = now
        if repeat_signature:
            group_data["repeat_signatures"].add(repeat_signature)
        if message.caption:
            group_data["caption"] = message.caption
        if message.message_id < int(group_data.get("first_message_id", message.message_id) or message.message_id):
            group_data["first_message_id"] = message.message_id
            group_data["display_name"] = _get_display_name_from_message(message, user_id)
        return
    if await handle_repeat_media_message(message):
        return

    summary = "📎 媒体消息"
    if message.caption:
        summary += f"\n📝 文字：{_clip_text(message.caption, 100)}"
    reply = await message.reply(summary, reply_markup=_media_reply_buttons(group_id, message.message_id, 0, 0))
    _track_group_reply(message, reply)
    async with media_reports_lock:
        media_reports[(group_id, message.message_id)] = {
            "chat_id": group_id,
            "media_msg_id": message.message_id,
            "media_msg_ids": [message.message_id],
            "reply_msg_id": reply.message_id,
            "reporters": set(),
            "garbage_reporters": set(),
            "deleted": False,
            "caption": message.caption or "",
            "created_ts": time.time(),
            "updated_ts": time.time(),
        }

def _track_user_message(
    group_id: int,
    user_id: int,
    msg_id: int,
    text: str = "",
    *,
    image_hashes: dict[str, int] | None = None,
):
    """记录用户消息到 SQLite 队列，用于 24 小时内回溯删除与转发学习。"""
    _queue_recent_message_upsert(
        group_id,
        user_id,
        msg_id,
        time.time(),
        text or "",
        image_hashes=image_hashes,
    )


def _track_bot_message(group_id: int, msg_id: int, auto_delete_sec: int = BOT_MSG_AUTO_DELETE_SEC):
    """跟踪机器人发送的消息，交由统一清理协程删除，避免大量 sleep 任务常驻内存。"""
    bot_sent_messages[(group_id, msg_id)] = time.time() + max(1, int(auto_delete_sec))


def _track_group_reply(message: Message, reply: Message):
    """仅记录在目标群里的引用回复，后续做补偿删除"""
    try:
        chat = message.chat
        if not chat or getattr(chat, "type", None) not in {"group", "supergroup"}:
            return
        _remember_group(chat.id)
        bot_reply_links[(chat.id, reply.message_id)] = (message.message_id, time.time())
    except Exception:
        pass


async def _is_original_message_still_tracked(group_id: int, original_msg_id: int | None) -> bool:
    """检查原消息是否仍在最近消息存储中。"""
    if not original_msg_id:
        return False
    await _flush_recent_messages_writes()
    return await _recent_message_exists(group_id, original_msg_id)


def _forget_tracked_user_message(group_id: int, original_msg_id: int | None) -> None:
    """从最近消息存储中移除已删除的原消息。"""
    if not original_msg_id:
        return
    _queue_recent_message_delete(group_id, original_msg_id)


async def _send_delayed_reply_if_original_exists(
    message: Message,
    text: str,
    delay_sec: float = 1.0,
    **kwargs,
) -> Message | None:
    """延迟回复；若原消息已被删除，则不再发送机器人回复。"""
    if delay_sec > 0:
        await asyncio.sleep(delay_sec)
    try:
        return await message.reply(text, **kwargs)
    except Exception as e:
        err = str(e).lower()
        if "replied message not found" in err or "message to be replied not found" in err:
            return None
        raise


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
            for key, warning_msg_id in list(repeat_warning_msg_id.items()):
                if warning_msg_id == bot_msg_id:
                    repeat_warning_msg_id.pop(key, None)


async def _drop_report_by_warning_id(group_id: int, warning_id: int) -> None:
    removed = False
    async with lock:
        for rk, data in list(reports.items()):
            if rk[0] == group_id and int(data.get("warning_id", 0) or 0) == int(warning_id):
                reports.pop(rk, None)
                removed = True
    if removed:
        _mark_reports_dirty()


async def _delete_original_and_linked_reply(group_id: int, original_msg_id: int | None):
    """删除原消息，并同步删除机器人对该消息的引用回复。"""
    if not original_msg_id:
        return
    try:
        await bot.delete_message(group_id, original_msg_id)
    except Exception:
        pass
    _forget_tracked_user_message(group_id, original_msg_id)
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

async def _record_semantic_ad_deletion(group_id: int, user_id: int, message_id: int, text: str, score: float) -> bool:
    learned = False
    try:
        sample = _add_semantic_ad_sample(text)
        learned = sample is not None
    except Exception as e:
        print(f"learn ad sample on delete failed: {e}")
    return learned


async def _check_and_delete_semantic_ad_message(message: Message, text: str, *, group_id: int, user_id: int) -> bool:
    """
    用已学习的广告库主动匹配当前消息。
    命中后直接删除原消息和相关机器人回复。
    """
    if not _semantic_detection_enabled_for_group(group_id):
        return False
    if len((text or "").strip()) < 4:
        return False

    is_semantic_ad, sim, _ = get_semantic_ad_detector().check_text(text)
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

    learned = await _record_semantic_ad_deletion(group_id, user_id, message.message_id, text, sim)
    await _delete_original_and_linked_reply(group_id, message.message_id)
    await _record_moderation_log(
        group_id=group_id,
        user_id=user_id,
        user_label=_get_display_name_from_message(message, user_id),
        action="广告删除",
        reason=f"命中AD语义库，score={sim:.3f}，学习={'是' if learned else '否'}",
    )
    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message.message_id,
        text=text,
        verdict="AD_DELETE",
        details="命中AD语义库，已执行删除",
    )
    return True


async def _check_and_block_fuzzy_image_message(
    message: Message,
    *,
    group_id: int,
    user_id: int,
    image_bytes: bytes | None = None,
) -> bool:
    if user_id in ADMIN_IDS:
        return False
    if image_bytes is None:
        image_bytes = await _extract_message_image_bytes(message)
    if not image_bytes:
        return False
    match = await _match_fuzzy_blocked_image(group_id, image_bytes)
    if match is None:
        return False

    cleanup_stats = await _delete_user_recent_and_warnings(group_id, user_id, message.message_id)
    restricted = await _restrict_user_all_permissions(group_id, user_id)
    action = "图片相似哈希禁言" if restricted else "图片相似哈希回删"
    reason = (
        f"命中关键图样本#{match.sample_id}"
        f"（ahash={match.ahash_distance}, dhash={match.dhash_distance}, "
        f"phash={match.phash_distance if match.phash_distance is not None else '-'}, "
        f"matched={match.matched_hashes}, total={match.total_distance}）"
    )
    await _record_moderation_log(
        group_id=group_id,
        user_id=user_id,
        user_label=_get_display_name_from_message(message, user_id),
        action=action,
        reason=(
            f"{reason}；已清理近期消息 {int(cleanup_stats.get('deleted_messages', 0) or 0)} 条，"
            f"警告 {int(cleanup_stats.get('deleted_warnings', 0) or 0)} 条"
        ),
    )
    return True


def _get_only_group_id() -> int | None:
    """仅配置了一个受控群时，返回该群 ID，便于单群模式兜底。"""
    managed = _get_managed_group_ids()
    if len(managed) != 1:
        return None
    return next(iter(managed))


async def _find_recent_user_ids_by_text(group_id: int, text: str, *, limit: int = 3) -> list[int]:
    """
    在最近消息存储里按文案反查用户。
    单群转发学习时，Telegram 经常不给原始 user/chat 信息，这里做本地兜底。
    """
    await _flush_recent_messages_writes()
    return await _recent_messages_find_user_ids_by_text(group_id, text, limit=limit)


async def _delete_recent_messages_by_text(group_id: int, text: str) -> int:
    """
    当拿不到 user_id 时，退化为按同文案删除最近消息，并清掉对应机器人警告。
    返回删除的原消息条数。
    """
    await _flush_recent_messages_writes()
    message_ids = await _recent_messages_delete_by_text(group_id, text)
    deleted = 0
    seen: set[int] = set()
    for msg_id in message_ids:
        if msg_id in seen:
            continue
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


@router.message(F.text)
async def detect_and_warn(message: Message):
    """文本消息主流程：AD 语义匹配 -> 权限查询 -> 重复文字处罚 -> 合规计入媒体解锁。"""
    if not _is_group_chat_message(message):
        return
    active, reason_guard = await _is_group_eligible(message.chat.id)
    if not active:
        _push_listen_log(
            group_id=getattr(message.chat, "id", None),
            user_id=getattr(getattr(message, "from_user", None), "id", None),
            msg_id=getattr(message, "message_id", None),
            text=(message.text or ""),
            verdict="SKIP",
            details=f"群不满足运行条件: {reason_guard}",
        )
        return
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
    _schedule_bio_watch_check(message)

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

    # 「权限」查询发媒体进度
    if message.text and message.text.strip() == "权限":
        count, unlocked = await _get_media_progress(group_id, user_id)
        need_msg = cfg.get("media_unlock_msg_count", 50)
        if unlocked:
            await message.reply(f"✅ 已解锁发媒体（发送合规消息已满 {need_msg} 条）。")
            return
        await message.reply(
            f"📊 发媒体进度\n"
            f"· 发送合规消息：{count}/{need_msg}\n"
            f"（刷屏/重复内容不计入）"
        )
        return

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

    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message.message_id,
        text=text,
        verdict="PASS",
        details="未命中AD语义库；未触发重复文本处罚",
    )
    await _try_count_media_and_notify(message, group_id, user_id, cfg)


@router.message(F.content_type.in_(_OTHER_CONTENT))
async def on_other_content_message(message: Message):
    """其他内容类型只挂简介频道延迟执法，不额外做旧风控。"""
    if not _is_group_chat_message(message):
        return
    active, _reason_guard = await _is_group_eligible(message.chat.id)
    if not active:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    cfg = get_group_config(message.chat.id)
    if not cfg.get("enabled", True):
        return
    _track_user_message(message.chat.id, message.from_user.id, message.message_id, "")
    _schedule_bio_watch_check(message)


@router.message(Command(commands=["imgban", "IMGBAN", "Imgban"]), F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def cmd_imgban(message: Message):
    try:
        target = message.reply_to_message
        if not target:
            await message.reply("请回复一张图片消息后再执行 /imgban。")
            return
        image_bytes = await _extract_message_image_bytes(target)
        if not image_bytes:
            await message.reply("目标消息不是可识别图片。支持照片和 image/* 文档。")
            return
        label = _clip_text(target.caption or target.text or f"msg:{target.message_id}", 60)
        item = await _add_image_fuzzy_sample_and_sync(
            group_id=message.chat.id,
            label=label,
            image_bytes=image_bytes,
        )
        await message.reply(
            f"✅ 已加入关键图黑样本库。\n"
            f"ID: {item['id']}\n"
            f"标签: {item['label'] or '-'}\n"
            f"当前策略: 相似图自动{'封禁' if _image_hash_should_ban(message.chat.id) else '删除'}"
        )
    except Exception as e:
        print(f"/imgban 命令异常: {e}")
        await message.reply("❌ 加入关键图样本失败。")


@router.message(Command(commands=["imgbanlist", "IMGBANLIST", "Imgbanlist"]), F.from_user.id.in_(ADMIN_IDS))
async def cmd_imgbanlist(message: Message):
    try:
        items = get_image_fuzzy_blocker().list_group_samples(message.chat.id)
        if not items:
            await message.reply("当前群没有关键图黑样本。")
            return
        lines = ["🖼️ 关键图黑样本库："]
        for item in items[:50]:
            ts = time.strftime("%m-%d %H:%M", time.localtime(int(item.get("created_at", 0) or 0)))
            lines.append(f"{item['id']}. [{ts}] {item.get('label', '-') or '-'}")
        if len(items) > 50:
            lines.append(f"… 共 {len(items)} 条")
        await message.reply("\n".join(lines))
    except Exception as e:
        print(f"/imgbanlist 命令异常: {e}")
        await message.reply("❌ 读取关键图样本失败。")


@router.message(Command(commands=["imgunban", "IMGUNBAN", "Imgunban"]), F.from_user.id.in_(ADMIN_IDS))
async def cmd_imgunban(message: Message):
    try:
        raw = (message.text or "").split()[1:]
        sample_ids: list[int] = []
        for item in raw:
            try:
                sample_ids.append(int(item))
            except Exception:
                continue
        if not sample_ids:
            await message.reply("用法：/imgunban 12 15 18")
            return
        removed = await _remove_image_fuzzy_samples_and_sync(
            group_id=message.chat.id,
            sample_ids=sample_ids,
        )
        if removed:
            await message.reply(f"✅ 已删除关键图样本: {', '.join(map(str, removed))}")
        else:
            await message.reply("没有找到可删除的关键图样本 ID。")
    except Exception as e:
        print(f"/imgunban 命令异常: {e}")
        await message.reply("❌ 删除关键图样本失败。")


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
                sample = _add_semantic_ad_sample(text)
                await _enable_semantic_detection_for_group(group_id)
                if sample is None:
                    print(f"/ad 样本已存在或被去重: {text[:80]}")
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


async def _restrict_user_all_permissions(group_id: int, user_id: int) -> bool:
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
                can_pin_messages=False,
            ),
        )
        return True
    except Exception as e:
        print(f"restrict all permissions failed group_id={group_id} user_id={user_id}: {e}")
        return False


async def _execute_forward_image_cleanup_for_group(
    *,
    group_id: int,
    image_bytes: bytes,
    hinted_user_id: int | None = None,
) -> dict[str, Any]:
    _remember_group(group_id)
    result: dict[str, Any] = {
        "group_id": int(group_id),
        "title": str(group_id),
        "matched_users": [],
        "restricted_users": [],
        "deleted_messages": 0,
        "deleted_warnings": 0,
    }
    try:
        title_map = await _resolve_group_titles([group_id])
        result["title"] = title_map.get(int(group_id)) or str(group_id)
    except Exception:
        pass

    matches = await _find_recent_image_matches(
        group_id,
        image_bytes,
        hinted_user_id=hinted_user_id,
        limit=3,
    )
    if not matches:
        return result

    for item in matches:
        user_id = int(item["user_id"])
        cleanup_stats = await _delete_user_recent_and_warnings(
            group_id,
            user_id,
            orig_msg_id=int(item["message_id"]),
        )
        restricted = await _restrict_user_all_permissions(group_id, user_id)
        match = item["match"]
        await _record_moderation_log(
            group_id=group_id,
            user_id=user_id,
            user_label=f"ID {user_id}",
            action="图片哈希追溯禁言" if restricted else "图片哈希追溯回删",
            reason=(
                f"管理员转发学习图片命中消息#{item['message_id']}"
                f"（ahash={match.ahash_distance}, dhash={match.dhash_distance}, "
                f"phash={match.phash_distance if match.phash_distance is not None else '-'}, "
                f"matched={match.matched_hashes}）"
            ),
        )
        result["matched_users"].append(
            {
                "user_id": user_id,
                "message_id": int(item["message_id"]),
                "match": match,
            }
        )
        if restricted:
            result["restricted_users"].append(user_id)
        result["deleted_messages"] += int(cleanup_stats.get("deleted_messages", 0) or 0)
        result["deleted_warnings"] += int(cleanup_stats.get("deleted_warnings", 0) or 0)
    return result


async def _execute_forward_cleanup_for_group(*, group_id: int, text: str, hinted_user_id: int | None = None) -> dict[str, Any]:
    _remember_group(group_id)
    result: dict[str, Any] = {
        "group_id": int(group_id),
        "title": "",
        "matched_user_id": None,
        "deleted_by_user": False,
        "deleted_by_text": 0,
        "memory_changed": False,
    }

    try:
        title_map = await _resolve_group_titles([group_id])
        result["title"] = title_map.get(int(group_id)) or str(group_id)
    except Exception:
        result["title"] = str(group_id)

    user_id = int(hinted_user_id) if hinted_user_id else None
    if not user_id:
        user_id = _get_remembered_user_id_by_text(group_id, text)
        if not user_id:
            matched_user_ids = await _find_recent_user_ids_by_text(group_id, text, limit=3)
            if matched_user_ids:
                user_id = matched_user_ids[0]

    memory_changed = False
    if user_id:
        try:
            result["matched_user_id"] = int(user_id)
            recent_msgs = await _recent_messages_fetch_by_user(group_id, user_id)
            async with lock:
                has_warning_records = any(
                    gid == group_id and data.get("suspect_id") == user_id
                    for (gid, _mid), data in reports.items()
                )
            if recent_msgs or has_warning_records:
                memory_changed = _remember_forward_match(group_id, user_id, text) or memory_changed
                memory_changed = await _remember_recent_user_texts(group_id, user_id) or memory_changed
                await _delete_user_recent_and_warnings(group_id, user_id, orig_msg_id=None)
                result["deleted_by_user"] = True
        except Exception as e:
            print(f"forward cleanup delete by user failed group_id={group_id}: {e}")

    try:
        deleted_by_text = await _delete_recent_messages_by_text(group_id, text)
        result["deleted_by_text"] = int(deleted_by_text)
    except Exception as e:
        print(f"forward cleanup delete by text failed group_id={group_id}: {e}")

    if memory_changed:
        _mark_forward_match_memory_dirty()
    result["memory_changed"] = memory_changed
    return result


async def on_forward_learn_ad(message: Message):
    """
    管理员转发用户消息给机器人：
    1) 学习该条广告文本
    2) 根据原始群ID和用户ID，删除其最近24小时内的全部消息和警告，并学习这些文本
    """
    try:
        is_forward = bool(
            getattr(message, "forward_origin", None)
            or getattr(message, "forward_from", None)
            or getattr(message, "forward_from_chat", None)
        )
        if not is_forward:
            return

        f_user = getattr(message, "forward_from", None)
        f_chat = getattr(message, "forward_from_chat", None)
        f_origin = getattr(message, "forward_origin", None)

        # 兼容新版 forward_origin（user/chat/channel）
        if not f_user and f_origin is not None:
            f_user = getattr(f_origin, "sender_user", None)
            if f_chat is None:
                f_chat = getattr(f_origin, "sender_chat", None)

        source_gid: int | None = None
        if f_chat:
            try:
                source_gid = int(f_chat.id)
                _remember_group(source_gid)
            except Exception:
                pass
        target_group_ids = set(_get_managed_group_ids())
        if source_gid is not None and source_gid < 0:
            target_group_ids.add(source_gid)

        image_bytes = await _extract_message_image_bytes(message)
        if image_bytes:
            if not target_group_ids:
                await message.reply("⚠️ 当前没有可学习图片样本的已管理群。")
                return
            label = _clip_text(
                message.caption or message.text or f"forward-img:{int(time.time())}",
                60,
            )
            learned_samples = 0
            for gid in sorted(target_group_ids):
                try:
                    await _add_image_fuzzy_sample_and_sync(
                        group_id=gid,
                        label=label,
                        image_bytes=image_bytes,
                    )
                    learned_samples += 1
                except Exception as e:
                    print(f"forward image sample add failed group_id={gid}: {e}")

            hinted_user_id = int(f_user.id) if f_user else None
            executed_results: list[dict[str, Any]] = []
            skipped_results: list[tuple[int, str]] = []
            for gid in sorted(target_group_ids):
                active, reason_guard = await _is_group_eligible(gid, force_refresh=True)
                if not active:
                    skipped_results.append((gid, reason_guard))
                    continue
                executed_results.append(
                    await _execute_forward_image_cleanup_for_group(
                        group_id=gid,
                        image_bytes=image_bytes,
                        hinted_user_id=hinted_user_id,
                    )
                )

            matched_groups = 0
            restricted_groups = 0
            detail_lines: list[str] = []
            for result in executed_results:
                gid = int(result["group_id"])
                title = str(result.get("title") or gid)
                matched_users = list(result.get("matched_users") or [])
                restricted_users = list(result.get("restricted_users") or [])
                deleted_messages = int(result.get("deleted_messages", 0) or 0)
                deleted_warnings = int(result.get("deleted_warnings", 0) or 0)
                if matched_users:
                    matched_groups += 1
                    if restricted_users:
                        restricted_groups += 1
                    match_preview = ", ".join(
                        f"{item['user_id']}#msg{item['message_id']}" for item in matched_users[:3]
                    )
                    detail_lines.append(
                        f"- {title} ({gid}): 命中 {len(matched_users)} 人，"
                        f"禁言 {len(restricted_users)} 人，"
                        f"清理消息 {deleted_messages} 条，警告 {deleted_warnings} 条；{match_preview}"
                    )
                else:
                    detail_lines.append(f"- {title} ({gid}): 未定位到发图用户")

            skipped_text = ""
            if skipped_results:
                skipped_text = "；跳过 " + ", ".join(f"{gid}({reason})" for gid, reason in skipped_results[:5])
                if len(skipped_results) > 5:
                    skipped_text += f" 等 {len(skipped_results)} 个群"

            summary = (
                f"✅ 已学习图片样本到 {learned_samples}/{len(target_group_ids)} 个群。"
                f"\n已在 {len(executed_results)} 个有权限群执行图片追溯："
                f"{matched_groups} 个群定位到发图用户，{restricted_groups} 个群已完成禁言"
                f"{skipped_text}。"
            )
            if detail_lines:
                preview = "\n".join(detail_lines[:12])
                if len(detail_lines) > 12:
                    preview += f"\n… 其余 {len(detail_lines) - 12} 个群已省略"
                summary += f"\n\n{preview}"
            await message.reply(summary)
            return

        text = (message.text or message.caption or "").strip()
        if not text:
            await message.reply("⚠️ 这条转发没有可学习文本，无法执行学习/回删。")
            return

        learned = False
        try:
            sample = _add_semantic_ad_sample(text)
            learned = sample is not None
            if not learned:
                print(f"转发学习样本已存在或被去重: {text[:80]}")
        except Exception as e:
            print(f"转发学习广告样本失败: {e}")

        if not target_group_ids:
            learned_text = "已新增学习广告内容" if learned else "该广告内容已在库中"
            await message.reply(f"✅ {learned_text}，但当前没有可回删的已管理群。")
            return

        hinted_user_id = int(f_user.id) if f_user else None
        executed_results: list[dict[str, Any]] = []
        skipped_results: list[tuple[int, str]] = []

        for gid in sorted(target_group_ids):
            active, reason_guard = await _is_group_eligible(gid, force_refresh=True)
            if not active:
                skipped_results.append((gid, reason_guard))
                continue
            await _enable_semantic_detection_for_group(gid)
            executed_results.append(
                await _execute_forward_cleanup_for_group(
                    group_id=gid,
                    text=text,
                    hinted_user_id=hinted_user_id,
                )
            )

        learned_text = "已新增学习广告内容" if learned else "该广告内容已在库中"
        if not executed_results:
            if skipped_results:
                skipped_preview = "\n".join(f"- {gid}: {reason}" for gid, reason in skipped_results[:10])
                if len(skipped_results) > 10:
                    skipped_preview += f"\n… 共 {len(skipped_results)} 个群不可执行"
                await message.reply(
                    f"✅ {learned_text}，但当前没有可执行回删的群。\n\n不可执行群：\n{skipped_preview}"
                )
            else:
                await message.reply(f"✅ {learned_text}，但当前没有可执行回删的群。")
            return

        success_count = 0
        partial_count = 0
        miss_count = 0
        detail_lines: list[str] = []
        for result in executed_results:
            gid = int(result["group_id"])
            title = str(result.get("title") or gid)
            matched_user = result.get("matched_user_id")
            deleted_by_user = bool(result.get("deleted_by_user"))
            deleted_by_text = int(result.get("deleted_by_text", 0) or 0)
            if deleted_by_user or deleted_by_text:
                if deleted_by_user:
                    success_count += 1
                    detail = f"- {title} ({gid}): 已按用户回删"
                else:
                    partial_count += 1
                    detail = f"- {title} ({gid}): 未定位到用户，按文案回删 {deleted_by_text} 条"
            else:
                miss_count += 1
                detail = f"- {title} ({gid}): 未找到可回删消息"
            if matched_user:
                detail += f"；user_id={matched_user}"
            detail_lines.append(detail)

        if skipped_results:
            skipped_text = "；跳过 " + ", ".join(f"{gid}({reason})" for gid, reason in skipped_results[:5])
            if len(skipped_results) > 5:
                skipped_text += f" 等 {len(skipped_results)} 个群"
        else:
            skipped_text = ""

        summary = (
            f"✅ {learned_text}。\n"
            f"已在 {len(executed_results)} 个有权限群执行批量回删："
            f"{success_count} 个按用户命中，{partial_count} 个按文案兜底，{miss_count} 个未命中"
            f"{skipped_text}。"
        )
        if detail_lines:
            preview = "\n".join(detail_lines[:12])
            if len(detail_lines) > 12:
                preview += f"\n… 其余 {len(detail_lines) - 12} 个群已省略"
            summary += f"\n\n{preview}"
        await message.reply(summary)
    except Exception as e:
        print("转发学习命令异常:", e)


@router.message(F.left_chat_member)
async def on_member_left(message: Message):
    """成员退群：删除其在本群的最近消息和全部警告"""
    try:
        if not _is_group_chat_message(message):
            return
        active, _reason_guard = await _is_group_eligible(message.chat.id)
        if not active:
            return
        if not message.left_chat_member or message.left_chat_member.is_bot:
            return
        group_id = message.chat.id
        user_id = message.left_chat_member.id
        # 利用已有工具函数：删除最近24小时内消息 + 所有警告记录
        await _delete_user_recent_and_warnings(group_id, user_id, orig_msg_id=None)
    except Exception as e:
        print(f"处理退群用户消息清理失败: {e}")

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
        
        display_name = f"ID {user_id}"
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
        await _record_moderation_log(
            group_id=group_id,
            user_id=user_id,
            user_label=display_name,
            action="永久封禁",
            reason="管理员一键封禁",
        )
        await callback.answer("✅ 已处理")
    except Exception as e:
        print(f"管理员封禁失败: {e}")
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    """举报处理；动态更新举报人名单，达到当前规则阈值时执行删除/封禁。"""
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
            reporter_labels = data.setdefault("reporter_labels", {})
            reporter_labels[str(reporter_id)] = _format_user_mention(callback.from_user, reporter_id)
            count = len(data["reporters"])
            user_id = data["suspect_id"]
            warning_id = data["warning_id"]
            reason = data["reason"]
            _mark_reports_dirty()
        
        # 尽早返回响应，后续操作不阻塞用户
        await callback.answer(f"✅ 举报({count}人)")
        
        # 修改警告消息 - 关键：显示举报数 + 根据举报数决定按钮
        display_name = data.get("suspect_name") or f"ID {user_id}"
        updated_text = (
            "🚨 已收到群成员的举报\n\n"
            f"👤 用户：{display_name}（ID: {user_id}）\n"
            f"📌 触发原因：{reason}\n"
            f"📣 当前举报人数：{count} 人\n"
            f"{_render_reporter_lines(data.get('reporter_labels'))}\n\n"
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
                await _record_moderation_log(
                    group_id=group_id,
                    user_id=user_id,
                    user_label=display_name,
                    action="永久封禁",
                    reason=f"{reason}（{count}人举报）",
                )
                async with lock:
                    reports.pop(rk, None)
                _mark_reports_dirty()
                return
            except Exception as e:
                print("2层2举报永封失败:", e)
        _mark_reports_dirty()
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
            await _record_moderation_log(
                group_id=group_id,
                user_id=user_id,
                user_label=display_name,
                action="永久封禁",
                reason=f"{reason}（管理员处理）",
            )
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
            await _record_moderation_log(
                group_id=group_id,
                user_id=user_id,
                user_label=display_name,
                action="禁言24小时",
                reason=f"{reason}（管理员处理）",
            )

        # 删除所有已封禁的警告消息（替换原来的只删上一条）
        await _delete_all_banned_warnings(group_id)

        await callback.answer(f"✅ {ban_type}")
        async with lock:
            reports.pop(rk, None)
        _mark_reports_dirty()
    
    except TelegramBadRequest:
        await callback.answer("❌ 失败", show_alert=True)
    except Exception as e:
        print("封禁异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("exempt:"))
async def handle_exempt(callback: CallbackQuery):
    """误判处理：仅删除当前警告并移除对应举报记录，不再写入旧白名单。"""
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
        if caller_id not in ADMIN_IDS:
            await callback.answer("仅管理员操作", show_alert=True)
            return
        try:
            await bot.delete_message(group_id, warning_id)
        except Exception:
            pass
        await callback.answer("✅ 已豁免")
        async with lock:
            reports.pop(rk, None)
        _mark_reports_dirty()
    except Exception as e:
        print("豁免异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("report_history_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def handle_report_history_exempt(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)


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
                _add_semantic_ad_sample(orig_text)
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
        _mark_reports_dirty()
        # 不弹窗，仅静默确认
        await callback.answer()
    except Exception as e:
        print("标记广告异常:", e)
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
            garbage_count = len(data.get("garbage_reporters", set()))
            reply_id = data["reply_msg_id"]
            media_msg_ids = list(data.get("media_msg_ids", [media_msg_id]))
            data["updated_ts"] = time.time()

        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=reply_id,
                reply_markup=_media_reply_buttons(chat_id, media_msg_id, report_count, garbage_count)
            )
        except Exception:
            pass
        await callback.answer()

        if report_count >= 2:
            for original_mid in media_msg_ids:
                await _delete_original_and_linked_reply(chat_id, original_mid)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=reply_id,
                    text=f"⚠️ 多人举报，已删除该媒体消息（共 {len(media_msg_ids)} 条）。",
                    reply_markup=None
                )
            except Exception:
                pass
            async with media_reports_lock:
                if key in media_reports:
                    media_reports[key]["deleted"] = True
                    mgid = media_reports[key].get("media_group_id")
                    if mgid:
                        media_group_report_index.pop((chat_id, str(mgid)), None)
    except Exception as e:
        print("媒体举报异常:", e)
        await callback.answer("❌ 失败", show_alert=True)

@router.callback_query(F.data.startswith("mg:"))
async def handle_media_garbage_report(callback: CallbackQuery):
    """举报垃圾信息：两人举报即删。"""
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
            if data["deleted"]:
                await callback.answer("该媒体已被删除")
                return
            garbage_reporters = data.setdefault("garbage_reporters", set())
            if uid in garbage_reporters:
                await callback.answer("已举报过")
                return
            garbage_reporters.add(uid)
            garbage_count = len(garbage_reporters)
            reply_id = data["reply_msg_id"]
            report_count = len(data["reporters"])
            media_msg_ids = list(data.get("media_msg_ids", [media_msg_id]))
            data["updated_ts"] = time.time()
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=reply_id,
                reply_markup=_media_reply_buttons(chat_id, media_msg_id, report_count, garbage_count)
            )
        except Exception:
            pass
        await callback.answer()
        if garbage_count >= 2:
            for original_mid in media_msg_ids:
                await _delete_original_and_linked_reply(chat_id, original_mid)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=reply_id,
                    text=f"⚠️ 多人举报，已删除该媒体消息（共 {len(media_msg_ids)} 条）。",
                    reply_markup=None
                )
            except Exception:
                pass
            async with media_reports_lock:
                if key in media_reports:
                    media_reports[key]["deleted"] = True
                    mgid = media_reports[key].get("media_group_id")
                    if mgid:
                        media_group_report_index.pop((chat_id, str(mgid)), None)
    except Exception as e:
        print("媒体垃圾举报异常:", e)
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
            _mark_reports_dirty()
        await asyncio.sleep(1)


async def cleanup_bot_messages():
    """统一清理到期的机器人临时消息，避免为每条消息创建单独延迟任务。"""
    while True:
        await asyncio.sleep(BOT_MESSAGE_SWEEP_SEC)
        now = time.time()
        due_items = [
            (group_id, msg_id)
            for (group_id, msg_id), expire_at in list(bot_sent_messages.items())
            if now >= float(expire_at)
        ]
        if not due_items:
            continue
        for group_id, msg_id in due_items:
            try:
                await bot.delete_message(group_id, msg_id)
            except Exception:
                pass
            finally:
                bot_sent_messages.pop((group_id, msg_id), None)
                for key, tracked_msg_id in list(last_media_no_perm_msg.items()):
                    if key[0] == group_id and tracked_msg_id == msg_id:
                        last_media_no_perm_msg.pop(key, None)
                for key, warning_msg_id in list(repeat_warning_msg_id.items()):
                    if warning_msg_id == msg_id:
                        repeat_warning_msg_id.pop(key, None)


async def cleanup_orphan_replies():
    """每 5 分钟清理一次孤儿引用回复；对无法确认的旧回复也做兜底回收。"""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        items = list(bot_reply_links.items())
        if not items:
            continue
        for (group_id, bot_msg_id), (orig_msg_id, created_ts) in items:
            if await _is_original_message_still_tracked(group_id, orig_msg_id) and (now - float(created_ts)) < BOT_REPLY_ORPHAN_MAX_AGE_SEC:
                continue
            try:
                await bot.delete_message(group_id, bot_msg_id)
            except TelegramBadRequest:
                pass
            except Exception:
                pass
            finally:
                bot_reply_links.pop((group_id, bot_msg_id), None)
                for key, warning_msg_id in list(repeat_warning_msg_id.items()):
                    if warning_msg_id == bot_msg_id:
                        repeat_warning_msg_id.pop(key, None)
                await _drop_report_by_warning_id(group_id, bot_msg_id)


async def cleanup_media_runtime_state():
    """定期回收媒体举报相关热表，限制高活跃群下的内存增长。"""
    while True:
        await asyncio.sleep(120)
        now = time.time()
        _prune_bio_watch_cache()

        stale_group_keys = [
            key
            for key, data in list(pending_media_groups.items())
            if now - float(data.get("last_update_ts", 0.0) or 0.0) >= MEDIA_GROUP_STALE_SEC
        ]
        for key in stale_group_keys:
            pending_media_groups.pop(key, None)

        for uid, (_mid, last_ts) in list(media_report_last.items()):
            if now - float(last_ts) >= MEDIA_REPORT_LAST_TTL_SEC:
                media_report_last.pop(uid, None)

        today_str = time.strftime("%Y-%m-%d", time.localtime(now))
        for key in list(media_report_day_count.keys()):
            if key[1] != today_str:
                media_report_day_count.pop(key, None)

        expired_warning_keys = [
            key
            for key, (_count, last_ts) in list(media_no_perm_strikes.items())
            if now - float(last_ts) >= MEDIA_NO_PERM_STRIKE_RESET_SEC
        ]
        for key in expired_warning_keys:
            media_no_perm_strikes.pop(key, None)

        expired_cooldown_keys = [
            key
            for key, (last_ts, _msg_id) in list(user_last_warning.items())
            if now - float(last_ts) >= USER_WARNING_COOLDOWN_SEC
        ]
        for key in expired_cooldown_keys:
            user_last_warning.pop(key, None)

        _prune_join_approval_avatar_cache()

        expired_reply_updates: list[tuple[int, int]] = []
        async with media_reports_lock:
            expired_report_keys = []
            expired_group_index_keys = []
            for key, data in list(media_reports.items()):
                base_ts = float(data.get("updated_ts") or data.get("created_ts") or 0.0)
                ttl = MEDIA_REPORT_DELETED_TTL_SEC if data.get("deleted") else MEDIA_REPORT_ENTRY_TTL_SEC
                if base_ts and (now - base_ts) < ttl:
                    continue
                expired_report_keys.append(key)
                expired_reply_updates.append((int(data.get("chat_id", key[0])), int(data.get("reply_msg_id", 0) or 0)))
                media_group_id = data.get("media_group_id")
                if media_group_id:
                    expired_group_index_keys.append((int(data.get("chat_id", key[0])), str(media_group_id)))
            for group_key in expired_group_index_keys:
                media_group_report_index.pop(group_key, None)
            for key in expired_report_keys:
                media_reports.pop(key, None)

        for chat_id, reply_id in expired_reply_updates:
            if reply_id <= 0:
                continue
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=reply_id, reply_markup=None)
            except Exception:
                pass


async def _logs_flush_worker() -> None:
    """批量刷盘审批/处理日志，减少高频 JSON 序列化抖动。"""
    while True:
        await asyncio.sleep(10)
        await save_join_review_logs()
        await save_moderation_logs()

async def main():
    print("🚀 机器人启动")
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=WORKER_THREAD_MAX,
            thread_name_prefix="bot-worker",
        )
    )
    await load_config()
    for gid in _get_managed_group_ids():
        get_group_config(gid)
    await save_config()
    await load_data()
    await load_recent_messages_cache()
    await load_forward_match_memory()
    await load_join_review_logs()
    await load_moderation_logs()
    await load_repeat_levels()
    await load_media_stats()
    asyncio.create_task(_recent_messages_flush_worker())
    asyncio.create_task(_forward_match_flush_worker())
    asyncio.create_task(_reports_flush_worker())
    asyncio.create_task(_logs_flush_worker())
    asyncio.create_task(cleanup_bot_messages())
    asyncio.create_task(cleanup_deleted_messages())
    asyncio.create_task(cleanup_orphan_replies())
    asyncio.create_task(cleanup_media_runtime_state())
    asyncio.create_task(bio_watch_enforcement_worker())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
