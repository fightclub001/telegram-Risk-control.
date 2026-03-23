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

# ==================== 鐜閰嶇疆 ====================
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
        raise ValueError("GROUP_IDS 鎴?ADMIN_IDS 涓虹┖")
except Exception as e:
    raise ValueError(f"鉂?鐜鍙橀噺閿欒: {e}")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("鉂?璇疯缃?BOT_TOKEN")

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

# ==================== 鏁版嵁鏂囦欢 ====================
# 浣跨敤鐜鍙橀噺 DATA_DIR锛汻ailway 闇€灏?Volume 鎸傝浇鍒拌璺緞锛堝 /data锛夛紝閲嶆柊閮ㄧ讲鍚庨厤缃笌鍚嶅崟鎵嶄笉涓㈠け
# 浠ヤ笅鏁版嵁鍧囨寔涔呭寲锛岄噸鍚笉涓㈠け锛欳ONFIG_FILE锛堣眮鍏嶅悕鍗?exempt_users銆佸獟浣撶櫧鍚嶅崟 media_unlock_whitelist銆?
# 閲嶅鍙戣█璞佸厤璇?repeat_exempt_keywords銆佸悇缇ゅ叧閿瘝涓庡紑鍏崇瓑锛夛紱DATA_FILE 涓炬姤璁板綍锛汳EDIA_STATS_FILE 鍚堣鏁?鍔╁姏/瑙ｉ攣
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "reports.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USER_VIOLATIONS_FILE = os.path.join(DATA_DIR, "user_violations.json")
MEDIA_STATS_FILE = os.path.join(DATA_DIR, "media_stats.json")
REPEAT_LEVEL_FILE = os.path.join(DATA_DIR, "repeat_levels.json")
LINK_REF_LEVELS_FILE = os.path.join(DATA_DIR, "link_ref_levels.json")
REPORT_ACTIONS_FILE = os.path.join(DATA_DIR, "report_actions.json")
FORWARD_MATCH_FILE = os.path.join(DATA_DIR, "forward_match_memory.json")

reports = {}  # key: (group_id, message_id)
lock = asyncio.Lock()
user_violations = {}  # key: "gid_uid" -> { msg_id: { "time", "reporters": [] } }
user_recent_message_ids = {}  # (group_id, user_id) -> deque of (msg_id, time, text), for 24h delete & learning
mild_trigger_entries = {}  # (group_id, user_id) -> list of (orig_msg_id, warning_msg_id), max 3
repeat_warning_msg_id = {}  # (group_id, user_id) -> msg_id of "2娆? repeat warning, delete if orig deleted
# 澶栭儴寮曠敤 / 娑堟伅閾炬帴锛?=鏈Е鍙戣繃锛?=宸茶Е鍙戜竴娆★紙涓嬫姘稿皝锛?
external_ref_level = {}  # (group_id, user_id) -> 0|1
message_link_level = {}  # (group_id, user_id) -> 0|1
config = {}
report_action_state = {}  # key: "gid_uid" -> {"last_trigger_count": int}
forward_match_memory = {}  # normalized_text -> {"group_id": int, "user_id": int, "updated_at": int}
# 濯掍綋鏉冮檺缁熻锛氬悎瑙勬秷鎭暟銆佸悓鏉¤秴杩?0娆′笉璁℃暟銆佸凡瑙ｉ攣鍚嶅崟銆佸姪鍔涙暟锛堟寔涔呭寲鍒?MEDIA_STATS_FILE锛岄噸鏂伴儴缃查』淇濈暀 DATA_DIR 鍗凤級
media_stats = {"message_counts": {}, "text_counts": {}, "unlocked": {}, "boosts": {}}
media_stats_loaded = False
# 濯掍綋娑堟伅涓炬姤/鐐硅禐锛堝唴瀛樺嵆鍙紝鎸夋秷鎭淮搴︼級
media_reports = {}
media_reports_lock = asyncio.Lock()
media_report_last = {}  # (uid,) -> (msg_id, time) 鏈€杩戜竴娆′妇鎶ョ殑濯掍綋
media_report_day_count = {}  # (uid, date_str) -> count
SEMANTIC_AD_DATA_DIR = os.path.join(DATA_DIR, "semantic_ads")
semantic_ad_detector = SemanticAdDetector(SEMANTIC_AD_DATA_DIR)
# 鍙敜浠ｅ彂锛氭湭瑙ｉ攣鐢ㄦ埛鍙戙€屽彫鍞ゃ€嶅悗涓嬩竴娆″獟浣撶敱鏈哄櫒浜轰唬鍙戯紙閬垮厤鐐哥兢锛?
summon_pending = {}  # (group_id, user_id) -> timestamp
SUMMON_TIMEOUT_SEC = 300
# 鏃犳潈闄愬彂濯掍綋璀﹀憡锛氬悓鐢ㄦ埛鍒犱笂涓€鏉★紱(group_id, user_id) -> 涓婁竴鏉℃満鍣ㄤ汉璀﹀憡 message_id
last_media_no_perm_msg = {}
MEDIA_NO_PERM_DELETE_AFTER_SEC = 60  # 涓嶅悓鐢ㄦ埛鐨勮鍛?1 鍒嗛挓鍚庤嚜鍔ㄥ垹闄?
media_no_perm_strikes = {}  # (group_id, user_id) -> (count, last_time) 杩炵画鏃犳潈闄愬彂濯掍綋璁℃暟
MEDIA_NO_PERM_STRIKE_RESET_SEC = 300  # 瓒呰繃姝ゆ椂闂存湭鍐嶈Е鍙戝垯瑙嗕负閲嶆柊璁＄畻杩炵画娆℃暟
# 涓炬姤鎸夐挳瑙勫垯锛氱鐞嗗憳鏈偣鍑诲皝绂?璇垽璞佸厤鍓嶏紝鎸夐挳姘镐笉杩囨湡锛堜笉鍥犲師娑堟伅琚垹鑰岀Щ闄わ級銆?
# (1) 鏈哄櫒浜鸿嚜鍔ㄥ皝绂?(2) 绠＄悊鍛樼偣鍑诲皝绂侊細绉婚櫎鎸夐挳骞朵繚鐣?鏇存柊鏂囨锛?3) 绠＄悊鍛樼偣鍑昏鍒よ眮鍏嶏細鍒犻櫎璀﹀憡娑堟伅銆?
# 瓒呰繃姝ゆ椂闀夸粛鏈鐞嗭細浠呴殣钘忔寜閽€佷繚鐣欐秷鎭枃鏈紝骞朵粠鍐呭瓨绉婚櫎璁板綍銆?
REPORT_BUTTON_HIDE_AFTER_SEC = 24 * 3600
REPORT_BAN_HOURS_CAP = 72
last_ban_warning_msg = {}  # group_id -> warning_id锛氫笂涓€鏉″凡澶勭悊鐨勫皝绂佽鍛婏紝涓嬫灏佺鏃?15 绉掑悗鍒犻櫎
MISJUDGE_BOT_MENTION = "濡傛湁璇皝锛岃鐩存帴鑱旂郴鏈兢绠＄悊鍛樺鐞嗐€?
USER_MSG_TRACK_MAXLEN = 500
USER_MSG_24H_SEC = 24 * 3600
BOT_MSG_AUTO_DELETE_SEC = 24 * 3600  # 鏈哄櫒浜烘秷鎭?4灏忔椂鍚庤嚜鍔ㄥ垹闄?

# 鏈哄櫒浜烘秷鎭窡韪細(group_id, msg_id) -> timestamp
bot_sent_messages = {}
# 鏈哄櫒浜哄湪缇ら噷鐨勨€滃紩鐢ㄥ洖澶嶁€濊窡韪細(group_id, bot_reply_msg_id) -> (original_msg_id, created_ts)
bot_reply_links = {}
# 鍚岀敤鎴疯繛缁Е鍙戣鍛婇槻鍒峰睆锛?group_id, user_id) -> (last_warning_time, last_warning_msg_id)
user_last_warning = {}
USER_WARNING_COOLDOWN_SEC = 60  # 鍚岀敤鎴?0绉掑唴鍙彂涓€鏉¤鍛?
# 宸插皝绂佽鍛婃秷鎭垪琛細group_id -> list of warning_msg_id锛堢敤浜庝竴娆℃€у垹闄ゆ墍鏈夊凡灏佺璀﹀憡锛?
banned_warning_messages = {}

# ==================== 鐩戝惉鍐崇瓥鏃ュ織锛堜粎淇濈暀鏈€杩?0鏉★級 ====================
listen_decision_logs = deque(maxlen=10)  # newest appended to right


def _clip_text(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "鈥?


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
    璁板綍涓€娆♀€滄敹鍒版秷鎭啋鍐崇瓥璺緞鈫掔粨鏋溾€濈殑鎽樿銆?
    verdict 绀轰緥锛歋KIP / PASS / AD_DELETE / RULE_DELETE / RULE_BAN / ERROR 绛?
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

# ==================== 閰嶇疆鍑芥暟 ====================
def _default_group_config():
    """鍗曠兢榛樿閰嶇疆锛堝叧閿瘝绛変細闅忕鐞嗗憳缂栬緫鎸佷箙鍖栧埌 CONFIG_FILE锛?""
    return {
        "enabled": True,
        "display_keywords": ["鍔爒", "鍔犲井淇?, "鍔爍q", "鍔犳墸", "绂忓埄鍔?, "绾?, "绾︾偖", "璧勬簮绉佽亰", "绉佹垜", "绉佽亰鎴?, "椋炴満", "绾搁鏈?, "绂忓埄", "澶栧洿", "鍙嶅樊", "瀚╂ā", "瀛︾敓濡?, "绌哄", "浜哄", "鐔熷コ", "onlyfans", "of", "leak", "nudes", "鍗佸叓+", "av"],
        "check_display_keywords": True,
        "message_keywords": ["qq:", "qq鍙?, "寰俊", "wx:", "骞煎コ", "钀濊帀", "绂忓埄", "绾︾偖", "onlyfans"],
        "check_message_keywords": True,
        "message_keyword_normalize": True,  # 闃叉嫾瀛楄閬匡細蹇界暐绌烘牸鏍囩偣鍚庡尮閰嶏紙濡?A  bc锛孌 鍛戒腑 abcd锛?
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
        "exempt_users": [],  # 绠＄悊鍛樻墜鍔ㄧ淮鎶ょ殑璞佸厤锛堜笌鍙戝浘鏉冮檺鏃犲叧锛?
        "misjudge_whitelist": [],  # 浠呯鐞嗗憳鐐瑰嚮銆岃鍒ゃ€嶅悗鍔犲叆锛岃眮鍏嶅灞傚唴瀹规娴?
        "mild_exempt_whitelist": [],  # 杞诲害瑙﹀彂璞佸厤鍚嶅崟锛堢鐞嗗憳閫氳繃绉佽亰鎸夐挳璁剧疆锛?
        "repeat_window_seconds": 2 * 3600,
        "repeat_max_count": 3,
        "repeat_ban_seconds": 86400,
        "repeat_exempt_keywords": [],  # 鍚换涓€璇嶇殑娑堟伅涓嶈Е鍙戦噸澶嶅彂瑷€妫€娴嬶紙鐧藉悕鍗曡瘝锛?
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
    """浠?CONFIG_FILE 鍔犺浇閰嶇疆锛涘凡淇濆瓨鐨勮眮鍏嶅悕鍗曘€佺櫧鍚嶅崟銆佽眮鍏嶈瘝绛夊叏閮ㄤ繚鐣欙紝浠呭缂哄け椤硅ˉ榛樿鍊?""
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
                    "autoreply",
                    "violation_mute_hours",
                    "reported_message_threshold",
                ):
                    saved.pop(obsolete_key, None)
                config["groups"][gid] = saved
        else:
            config = {"groups": {}}
            await save_config()
    except Exception as e:
        print(f"閰嶇疆鍔犺浇澶辫触: {e}")
        config = {"groups": {}}

async def save_config():
    """淇濆瓨閰嶇疆鍒?CONFIG_FILE锛岃眮鍏嶅悕鍗?鐧藉悕鍗?璞佸厤璇嶇瓑鎵€鏈夊悕鍗曞潎鍦ㄦ鎸佷箙鍖栵紝閲嶅惎涓嶄涪澶?""
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
        print(f"閰嶇疆淇濆瓨澶辫触: {e}")

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
        print(f"杩濊璁板綍鍔犺浇澶辫触: {e}")


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
        print(f"鍘嗗彶涓炬姤澶勭綒鐘舵€佸姞杞藉け璐? {e}")
        report_action_state = {}


async def save_report_action_state():
    try:
        with open(REPORT_ACTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(report_action_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"鍘嗗彶涓炬姤澶勭綒鐘舵€佷繚瀛樺け璐? {e}")


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

def _prune_user_violations():
    """淇濈暀姣忕敤鎴锋渶杩?50 鏉′笖 30 澶╁唴鐨勪妇鎶ヨ褰曪紝閬垮厤鏂囦欢鏃犻檺澧為暱"""
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
        print(f"杩濊璁板綍淇濆瓨澶辫触: {e}")

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
        print(f"濯掍綋缁熻鍔犺浇澶辫触: {e}锛堟湰娆¤繍琛屼笉鍐欏叆锛岄伩鍏嶈鐩栫鐩樺師鏈夋暟鎹級")

async def save_media_stats():
    global media_stats_loaded
    if not media_stats_loaded:
        return
    try:
        with open(MEDIA_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(media_stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"濯掍綋缁熻淇濆瓨澶辫触: {e}")

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
        print(f"閲嶅杩濊绾у埆鍔犺浇澶辫触: {e}")

async def save_repeat_levels():
    try:
        data = {f"{g}_{u}": v for (g, u), v in repeat_violation_level.items()}
        with open(REPEAT_LEVEL_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"閲嶅杩濊绾у埆淇濆瓨澶辫触: {e}")


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
        print(f"閾炬帴/寮曠敤绾у埆鍔犺浇澶辫触: {e}")


async def save_link_ref_levels():
    try:
        data = {
            "external_ref": {f"{g}_{u}": v for (g, u), v in external_ref_level.items()},
            "message_link": {f"{g}_{u}": v for (g, u), v in message_link_level.items()},
        }
        with open(LINK_REF_LEVELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"閾炬帴/寮曠敤绾у埆淇濆瓨澶辫触: {e}")


def _media_key(group_id: int, user_id: int) -> str:
    return f"{group_id}_{user_id}"

async def _refresh_user_boosts(group_id: int, user_id: int) -> None:
    """鐢?Telegram API 鎷夊彇鐢ㄦ埛瀵规湰缇ょ殑鍔╁姏鏁板苟鍐欏洖 media_stats锛堜粎浼氬憳鍙姪鍔涳級"""
    if not media_stats_loaded:
        return
    try:
        res = await bot.get_user_chat_boosts(chat_id=group_id, user_id=user_id)
        count = len(getattr(res, "boosts", []) or [])
        key = _media_key(group_id, user_id)
        media_stats["boosts"][key] = count
        await save_media_stats()

        # 濡傛灉鍔╁姏鏁板凡杈惧埌瑙ｉ攣鏉′欢锛屽垯鏍囪涓哄凡瑙ｉ攣骞跺皾璇曟仮澶嶅叾鍙戝獟浣撴潈闄?
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
                # 鎭㈠鏉冮檺澶辫触涓嶄細褰卞搷鍚庣画瑙ｉ攣鍒ゆ柇
                pass
    except Exception:
        pass

def _can_send_media(group_id: int, user_id: int, username: str | None = None) -> bool:
    """鏄惁宸茶В閿佸彂濯掍綋锛氫粎鐪嬫湰澶勫獟浣撹В閿佺櫧鍚嶅崟 / 鍚堣娑堟伅鏁?/ 鍔╁姏娆℃暟锛堜笌璞佸厤妫€娴?exempt_users 鏃犲叧锛夈€?""
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
    """鍚堣娑堟伅璁℃暟锛堝悓涓€鏉¤秴杩?10 娆′笉璁℃暟锛夈€傚凡瑙ｉ攣=婊?0鏉?鐧藉悕鍗?鍔╁姏鐨勭敤鎴蜂笉鍐嶇粺璁★紙涓嶅惈涓€鍙戝浘灏辫鍒犵殑鐢ㄦ埛锛岄伩鍏嶉€昏緫寰幆锛夈€傝繑鍥炴槸鍚﹀洜鏈杈惧埌闃堝€艰€屽垰瑙ｉ攣銆?""
    cfg = get_group_config(group_id)
    need_count = cfg.get("media_unlock_msg_count", 50)
    key = _media_key(group_id, user_id)
    if media_stats["unlocked"].get(key):  # 宸茶兘鍙戝獟浣擄紝涓嶅啀缁熻
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
    """鍚堣娑堟伅璁″叆骞跺彲鑳藉彂閫佽В閿佽春淇°€備粎瀵广€屾湭瑙ｉ攣銆嶇敤鎴风粺璁★紙宸茶В閿?婊?0鏉?鐧藉悕鍗?鍔╁姏锛屼笉鍖呭惈涓€鍙戝浘灏辫鍒犵殑鐢ㄦ埛锛岄伩鍏嶉€昏緫寰幆锛夈€?""
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
                    f"馃帀 {name} 宸插湪鏈兢鍙戦€佸悎瑙勬秷鎭弧 {need_msg} 鏉★紝瑙ｉ攣鐩存帴鍙戦€佸浘鐗?瑙嗛/璇煶鐨勬潈闄愩€?
                )
            except Exception:
                pass
            # 鑻ヨ鐢ㄦ埛姝ゅ墠鍥犲娆¤繚瑙勮鍏抽棴濯掍綋鏉冮檺锛屽垯鍦ㄨ揪鍒拌В閿佹潯浠跺悗鑷姩鎭㈠鍏跺彂濯掍綋鏉冮檺
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
                # 濡傛灉鎭㈠鏉冮檺澶辫触锛屼笉褰卞搷瑙ｉ攣閫昏緫鏈韩
                pass
    except Exception as e:
        print(f"濯掍綋璁℃暟澶辫触: {e}")


def get_group_config(group_id: int):
    gid = str(group_id)
    if gid not in config["groups"]:
        config["groups"][gid] = _default_group_config()
    return config["groups"][gid]


def get_primary_group_id() -> int:
    if not GROUP_IDS:
        raise ValueError("GROUP_IDS 涓虹┖")
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
    """灏嗙鏁版牸寮忓寲涓轰汉绫诲彲璇绘椂闀?""
    if seconds == 0:
        return "姘镐箙"
    if seconds < 60:
        return f"{seconds}绉?
    if seconds < 3600:
        return f"{seconds // 60}鍒嗛挓"
    if seconds < 86400:
        return f"{seconds // 3600}灏忔椂"
    if seconds < 604800:
        return f"{seconds // 86400}澶?
    return f"{seconds // 604800}鍛?


async def get_chat_title_safe(bot, chat_id: int) -> str:
    """鑾峰彇缇ょ粍/鑱婂ぉ鏍囬锛屽け璐ユ椂杩斿洖 ID"""
    try:
        chat = await bot.get_chat(chat_id)
        return (chat.title or "").strip() or f"ID {chat_id}"
    except Exception:
        return str(chat_id)


# ==================== FSM 鐘舵€?====================
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
    EditReportHistoryWhitelist = State()
    EditMediaDeleteThreshold = State()
    EditMediaBroadcastInterval = State()
    EditSemanticAdAdd = State()
    EditSemanticAdRemove = State()

# ==================== UI 閿洏 ====================
def get_main_menu_keyboard():
    """鍗曠兢妯″紡锛氫繚鐣欏崰浣嶏紝涓嶅啀浣跨敤鏃ч椤甸敭鐩樸€?""
    buttons = [
        [InlineKeyboardButton(text="鈿欙笍 杩涘叆鏈兢鎺у埗鍙?, callback_data="group_menu_single")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def get_group_list_keyboard(bot):
    """寮傛鐢熸垚缇ょ粍鍒楄〃閿洏锛屾樉绀虹兢鍚?+ ID"""
    buttons = []
    for gid in sorted(GROUP_IDS):
        title = await get_chat_title_safe(bot, gid)
        label = f"馃懃 {title}" if title != str(gid) else f"馃懃 {gid}"
        # 鏍囬杩囬暱鏃舵埅鏂紝淇濈暀 ID 淇℃伅
        if len(label) > 35:
            label = label[:32] + "..."
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"select_group:{gid}")])
    buttons.append([InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_group_menu_keyboard(group_id: int):
    buttons = [
        [InlineKeyboardButton(text="AD鏈哄櫒瀛︿範", callback_data=f"submenu_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="涓炬姤澶勭疆", callback_data=f"submenu_violation:{group_id}")],
        [InlineKeyboardButton(text="閲嶅鍙戣█", callback_data=f"submenu_repeat:{group_id}")],
        [InlineKeyboardButton(text="濯掍綋鏉冮檺", callback_data=f"submenu_media_perm:{group_id}")],
        [InlineKeyboardButton(text="濯掍綋涓炬姤", callback_data=f"submenu_media_report:{group_id}")],
        [InlineKeyboardButton(text="鍩虹璁剧疆", callback_data=f"submenu_basic:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_bio_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    link_status = "鉁? if cfg.get("check_bio_link") else "鉂?
    kw_status = "鉁? if cfg.get("check_bio_keywords") else "鉂?
    buttons = [
        [InlineKeyboardButton(text=f"閾炬帴 {link_status}", callback_data=f"toggle_bio_link:{group_id}")],
        [InlineKeyboardButton(text=f"鏁忔劅璇?{kw_status}", callback_data=f"toggle_bio_keywords:{group_id}")],
        [InlineKeyboardButton(text="馃搵 缂栬緫璇嶆眹", callback_data=f"edit_bio_kw:{group_id}")],
        [InlineKeyboardButton(text="馃憖 鏌ョ湅", callback_data=f"view_bio_kw:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_display_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    status = "鉁? if cfg.get("check_display_keywords") else "鉂?
    buttons = [
        [InlineKeyboardButton(text=f"鍚敤 {status}", callback_data=f"toggle_display:{group_id}")],
        [InlineKeyboardButton(text="馃搵 缂栬緫璇嶆眹", callback_data=f"edit_display_kw:{group_id}")],
        [InlineKeyboardButton(text="馃憖 鏌ョ湅", callback_data=f"view_display_kw:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_message_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    status = "鉁? if cfg.get("check_message_keywords") else "鉂?
    msg_link_status = "鉁? if cfg.get("check_message_link", True) else "鉂?
    norm_status = "鉁? if cfg.get("message_keyword_normalize", True) else "鉂?
    buttons = [
        [InlineKeyboardButton(text=f"鏁忔劅璇?{status}", callback_data=f"toggle_message:{group_id}")],
        [InlineKeyboardButton(text=f"閾炬帴/@寮曟祦 {msg_link_status}", callback_data=f"toggle_message_link:{group_id}")],
        [InlineKeyboardButton(text=f"闃叉嫾瀛楄閬?{norm_status}", callback_data=f"toggle_message_normalize:{group_id}")],
        [InlineKeyboardButton(text="馃搵 缂栬緫璇嶆眹", callback_data=f"edit_message_kw:{group_id}")],
        [InlineKeyboardButton(text="馃憖 鏌ョ湅", callback_data=f"view_message_kw:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_short_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    short_enabled = "鉁? if cfg.get("short_msg_detection") else "鉂?
    fill_enabled = "鉁? if cfg.get("fill_garbage_detection") else "鉂?
    window_sec = cfg.get("time_window_seconds", 60)
    buttons = [
        [InlineKeyboardButton(text=f"鐭秷鎭?{short_enabled}", callback_data=f"toggle_short:{group_id}")],
        [InlineKeyboardButton(text=f"瀛楁暟: {cfg.get('short_msg_threshold')}", callback_data=f"edit_threshold:{group_id}")],
        [InlineKeyboardButton(text=f"杩炵画: {cfg.get('min_consecutive_count')}", callback_data=f"edit_consecutive:{group_id}")],
        [InlineKeyboardButton(text=f"绐楀彛: {fmt_duration(window_sec)}", callback_data=f"edit_window:{group_id}")],
        [InlineKeyboardButton(text=f"鍨冨溇 {fill_enabled}", callback_data=f"toggle_fill:{group_id}")],
        [InlineKeyboardButton(text=f"鏈€灏? {cfg.get('fill_garbage_min_raw_len')}", callback_data=f"edit_fill_min:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_semantic_ad_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    enabled = "鉁? if cfg.get("semantic_ad_enabled", False) else "鉂?
    buttons = [
        [InlineKeyboardButton(text=f"寮€鍏?{enabled}", callback_data=f"toggle_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="鉃?澧炲姞骞垮憡璇彞", callback_data=f"add_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="鉃?鍑忓皯骞垮憡璇彞", callback_data=f"remove_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="馃搨 骞垮憡璇嶅簱灞曠ず", callback_data=f"view_semantic_ad:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_violation_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    mute_h = cfg.get("report_history_mute_hours", 24)
    threshold = cfg.get("report_history_threshold", 3)
    wl = cfg.get("report_history_whitelist", []) or []
    n = len(wl) if isinstance(wl, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"绂佽█鏃堕暱: {fmt_duration(mute_h * 3600)}", callback_data=f"edit_mute:{group_id}")],
        [InlineKeyboardButton(text=f"瑙﹀彂绾? {threshold} 鏉″巻鍙蹭妇鎶?, callback_data=f"edit_report_threshold:{group_id}")],
        [InlineKeyboardButton(text=f"鐧藉悕鍗? {n} 浜?, callback_data=f"edit_report_history_whitelist:{group_id}")],
        [InlineKeyboardButton(text="杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_autoreply_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    ar = cfg.get("autoreply", {})
    enabled = "鉁? if ar.get("enabled") else "鉂?
    buttons = [
        [InlineKeyboardButton(text=f"鍚敤 {enabled}", callback_data=f"toggle_ar:{group_id}")],
        [InlineKeyboardButton(text="馃攽 鍏抽敭璇?, callback_data=f"edit_ar_kw:{group_id}")],
        [InlineKeyboardButton(text="馃摑 鏂囨湰", callback_data=f"edit_ar_text:{group_id}")],
        [InlineKeyboardButton(text="馃敇 鎸夐挳", callback_data=f"edit_ar_btn:{group_id}")],
        [InlineKeyboardButton(text="鈴憋笍 寤舵椂", callback_data=f"edit_ar_del:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_basic_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    enabled = "鉁? if cfg.get("enabled") else "鉂?
    exempt = cfg.get("exempt_users") or []
    n = len(exempt) if isinstance(exempt, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"鐘舵€? {enabled}", callback_data=f"toggle_group:{group_id}")],
        [InlineKeyboardButton(text=f"馃洝锔?璞佸厤鐢ㄦ埛 ({n})", callback_data=f"submenu_exempt:{group_id}")],
        [InlineKeyboardButton(text="馃搫 瀵煎嚭鐩戝惉鏃ュ織锛堣繎10鏉★級", callback_data=f"export_listen_log:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_exempt_menu_keyboard(group_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="馃搵 缂栬緫", callback_data=f"edit_exempt:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"submenu_basic:{group_id}")],
    ])

def get_repeat_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    w = cfg.get("repeat_window_seconds", 7200)
    m = cfg.get("repeat_max_count", 3)
    b = cfg.get("repeat_ban_seconds", 86400)
    kw = cfg.get("repeat_exempt_keywords", []) or []
    n_kw = len(kw) if isinstance(kw, list) else 0
    buttons = [
        [InlineKeyboardButton(text=f"鈴?鏃堕棿绐楀彛: {fmt_duration(w)}", callback_data=f"edit_repeat_window:{group_id}")],
        [InlineKeyboardButton(text=f"瑙﹀彂娆℃暟: {m}娆?, callback_data=f"edit_repeat_max:{group_id}")],
        [InlineKeyboardButton(text=f"馃攪 棣栨绂佽█: {fmt_duration(b)}", callback_data=f"edit_repeat_ban:{group_id}")],
        [InlineKeyboardButton(text=f"馃搵 璞佸厤璇?鐧藉悕鍗? ({n_kw})", callback_data=f"edit_repeat_exempt:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_perm_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    msg = cfg.get("media_unlock_msg_count", 50)
    boost = cfg.get("media_unlock_boosts", 4)
    wl = cfg.get("media_unlock_whitelist", [])
    n = len(wl) if isinstance(wl, list) else 0
    broadcast_on = "鉁? if cfg.get("media_rules_broadcast", True) else "鉂?
    interval = cfg.get("media_rules_broadcast_interval_minutes", 120)
    buttons = [
        [InlineKeyboardButton(text=f"瑙ｉ攣鎵€闇€娑堟伅鏁? {msg}", callback_data=f"edit_media_msg:{group_id}")],
        [InlineKeyboardButton(text=f"瑙ｉ攣鎵€闇€鍔╁姏: {boost}", callback_data=f"edit_media_boosts:{group_id}")],
        [InlineKeyboardButton(text=f"馃搵 濯掍綋瑙ｉ攣鐧藉悕鍗?({n})", callback_data=f"submenu_media_whitelist:{group_id}")],
        [InlineKeyboardButton(text=f"瑙勫垯骞挎挱: {broadcast_on}", callback_data=f"toggle_media_broadcast:{group_id}")],
        [InlineKeyboardButton(text=f"骞挎挱闂撮殧: {interval}鍒嗛挓", callback_data=f"edit_media_broadcast_interval:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
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
        buttons.append([InlineKeyboardButton(text=f"鉂?{s}", callback_data=f"remove_mw:{group_id}:{i}")])
    buttons.append([InlineKeyboardButton(text="鉃?娣诲姞", callback_data=f"add_media_whitelist:{group_id}")])
    buttons.append([InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"submenu_media_perm:{group_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_media_report_menu_keyboard(group_id: int):
    cfg = get_group_config(group_id)
    cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
    max_day = cfg.get("media_report_max_per_day", 3)
    del_th = cfg.get("media_report_delete_threshold", 3)
    buttons = [
        [InlineKeyboardButton(text=f"鈴?杩炵画涓炬姤鍐峰嵈: {fmt_duration(cooldown)}", callback_data=f"edit_media_cooldown:{group_id}")],
        [InlineKeyboardButton(text=f"姣忔棩涓炬姤涓婇檺: {max_day}娆?, callback_data=f"edit_media_maxday:{group_id}")],
        [InlineKeyboardButton(text=f"涓炬姤杈惧灏戜汉鍒犲獟浣? {del_th}", callback_data=f"edit_media_delete_threshold:{group_id}")],
        [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== 绠＄悊鍛樺懡浠?====================
@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_panel(message: Message, state: FSMContext):
    if not GROUP_IDS:
        await message.reply("褰撳墠鏈厤缃换浣曞彈鎺х兢缁勩€?)
        return
    group_id = get_primary_group_id()
    await state.update_data(group_id=group_id)
    cfg = get_group_config(group_id)
    status = "鉁?杩愯涓? if cfg.get("enabled", True) else "鉂?宸插仠鐢?
    text = (
        "馃洜 鍏ㄥ眬绠＄悊鍛橀潰鏉縗\n\\n"
        f"鍙楁帶缇ゆ暟: {len(GROUP_IDS)}\\n"
        f"鍏ㄥ眬鐘舵€? {status}\\n\\n"
        "涓嬮潰鎵€鏈夎皟鏁撮兘浼氬悓姝ュ簲鐢ㄥ埌鍏ㄩ儴鍙楁帶缇ょ粍銆?
    )
    kb = get_group_menu_keyboard(group_id)
    await message.reply(text, reply_markup=kb)
    await state.set_state(AdminStates.GroupMenu)

    await state.set_state(AdminStates.GroupMenu)

# ==================== 鍥炶皟澶勭悊 ====================
@router.callback_query(F.data == "choose_group", F.from_user.id.in_(ADMIN_IDS))
async def choose_group_callback(callback: CallbackQuery, state: FSMContext):
    """鍏煎鏃у叆鍙ｏ細鍦ㄥ崟缇ゆā寮忎笅鐩存帴璺冲洖鏈兢鎺у埗鍙般€?""
    if not GROUP_IDS:
        await callback.answer("鏈厤缃彈鎺х兢缁勩€?, show_alert=True)
        return
    group_id = list(GROUP_IDS)[0]
    await state.update_data(group_id=group_id)
    title = await get_chat_title_safe(callback.bot, group_id)
    cfg = get_group_config(group_id)
    status = "鉁?杩愯涓? if cfg.get("enabled", True) else "鉂?宸插仠鐢?
    text = (
        f"馃懏 绠＄悊鍛橀潰鏉匡紙鍗曠兢妯″紡锛塡n\n"
        f"馃懃 <b>{title}</b>\n"
        f"<code>ID: {group_id}</code>  |  鐘舵€? {status}\n\n"
        "璇烽€夋嫨瑕佺鐞嗙殑鍔熻兘锛?
    )
    kb = get_group_menu_keyboard(group_id)
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.GroupMenu)
    await callback.answer()

@router.callback_query(F.data.startswith("select_group:"), F.from_user.id.in_(ADMIN_IDS))
async def select_group(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "鉁?杩愯涓? if cfg.get("enabled", True) else "鉂?宸插仠鐢?
        text = (
            f"馃懃 <b>{title}</b>\n"
            f"<code>ID: {group_id}</code>  |  鐘舵€? {status}\n\n"
            "閫夋嫨瑕佺鐞嗙殑鍔熻兘锛?
        )
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data == "back_main", F.from_user.id.in_(ADMIN_IDS))
async def back_main(callback: CallbackQuery, state: FSMContext):
    text = "馃懏 绠＄悊鍛橀潰鏉?
    kb = get_main_menu_keyboard()
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AdminStates.MainMenu)
    await callback.answer()

@router.callback_query(F.data == "back_choose_group", F.from_user.id.in_(ADMIN_IDS))
async def back_choose_group(callback: CallbackQuery, state: FSMContext):
    """鍗曠兢妯″紡涓嬶紝杩斿洖鍗冲洖鍒版湰缇ゆ帶鍒跺彴銆?""
    await choose_group_callback(callback, state)

@router.callback_query(F.data.startswith("group_menu:"), F.from_user.id.in_(ADMIN_IDS))
async def group_menu(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        get_group_config(group_id)
        await state.update_data(group_id=group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "鉁?杩愯涓? if cfg.get("enabled", True) else "鉂?宸插仠鐢?
        text = (
            f"馃懃 <b>{title}</b>\n"
            f"<code>ID: {group_id}</code>  |  鐘舵€? {status}\n\n"
            "閫夋嫨瑕佺鐞嗙殑鍔熻兘锛?
        )
        kb = get_group_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("submenu_multi_rules:"), F.from_user.id.in_(ADMIN_IDS))
async def multi_rules_submenu(callback: CallbackQuery):
    """澶氬姛鑳藉彔鍔犺鍒欙細灞曠ず褰撳墠鍚勬ā鍧楃殑瑙﹀彂椤哄簭涓庝紭鍏堢骇銆?""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        text = (
            f"<b>{title}</b> 鈥?澶氬姛鑳藉彔鍔犺鍒橽n\n"
            "褰撳墠瑙﹀彂椤哄簭锛堜粠涓婂埌涓嬶紝鍓嶈€呬紭鍏堬級锛歕n"
            "1锔忊儯 AD 璇箟骞垮憡妫€娴嬶細鍛戒腑鍚庣洿鎺ュ垹闄よ鏉℃秷鎭紝涓嶅啀鎵ц鍚庣画妫€娴嬨€俓n"
            "2锔忊儯 涓炬姤闃堝€肩瑷€锛氳闈炵鐞嗗憳涓炬姤娑堟伅鏁?鈮?闃堝€?"
            f"锛堝綋鍓? {cfg.get('reported_message_threshold', 3)}锛夋椂鎸夋鏁板皝绂佸苟鍒犻櫎娑堟伅銆俓n"
            "3锔忊儯 澶氬眰椋庢帶妫€娴嬶細绠€浠?鏄电О/閾炬帴绛夊懡涓舰鎴愬灞傝Е鍙戯紝3 灞傚強浠ヤ笂鐩存帴灏佺骞舵竻鐞?24 灏忔椂鍐呮秷鎭€俓n"
            "4锔忊儯 杞诲害瑙﹀彂绱锛?锝? 灞傝Е鍙戣鍏ヨ交搴﹁鍛婏紝杈惧埌 3 娆℃椂浠呴€氱煡绠＄悊鍛橈紝鍙湪 AD 闈㈡澘涓眮鍏嶈交搴︺€俓n"
            "5锔忊儯 閲嶅鍙戣█妫€娴嬶細鍦ㄩ厤缃獥鍙ｅ唴澶氭閲嶅鍚屼竴鍐呭锛屾寜杩濊绛夌骇绂佽█骞舵竻鐞嗚繎鏈熸秷鎭€俓n\n"
            "褰撳墠绛栫暐涓猴細\n"
            "- AD 鍛戒腑鍚庯紝涓嶅啀杩涘叆涓炬姤銆佸灞傘€侀噸澶嶆娴嬶紙閬垮厤澶氭澶勭綒锛夈€俓n"
            "- 褰撳灞傞鎺у拰閲嶅鍙戣█鍚屾椂婊¤冻鏃讹紝浠ュ灞傞鎺х粨鏋滀负鍑嗭紙鏇撮噸鐨勫缃氳鐩栬緝杞诲缃氾級銆俓n\n"
            "鍚庣画濡傛灉闇€瑕侊紝鍙互鍦ㄨ繖閲屽鍔犲彲璋冪瓥鐣ワ紝渚嬪鍒囨崲浼樺厛绾ф垨鏄惁璁?AD 鍛戒腑涔熻鍏ュ叾瀹冪粺璁°€?
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"group_menu:{group_id}")]
            ]
        )
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

# ==================== 绠€浠嬫娴?====================
@router.callback_query(F.data.startswith("submenu_bio:"), F.from_user.id.in_(ADMIN_IDS))
async def bio_submenu(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)


@router.callback_query(F.data.startswith("submenu_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def semantic_ad_submenu(callback: CallbackQuery):
    """AD鏈哄櫒瀛︿範瀛愯彍鍗?"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        enabled = "鉁? if cfg.get("semantic_ad_enabled", False) else "鉂?
        text = f"<b>{title}</b> 鈥?AD鏈哄櫒瀛︿範\n\n褰撳墠鐘舵€? {enabled}"
        kb = get_semantic_ad_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("toggle_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_semantic_ad(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("semantic_ad_enabled", False)
        cfg["semantic_ad_enabled"] = not current
        await save_config()
        enabled = "鉁? if cfg["semantic_ad_enabled"] else "鉂?
        await callback.answer(f"AD鏈哄櫒瀛︿範: {enabled}", show_alert=True)
        kb = get_semantic_ad_menu_keyboard(group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{title}</b> 鈥?AD鏈哄櫒瀛︿範\n\n褰撳墠鐘舵€? {enabled}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("add_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def add_semantic_ad_callback(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        text = (
            f"<b>{title}</b> 鈥?AD鏈哄櫒瀛︿範 鈥?澧炲姞骞垮憡璇彞\n\n"
            "璇峰彂閫佷竴鏉″箍鍛婃牱鏈枃鏈紙浠呭唴瀹归儴鍒嗭級锛屾垜浼氬皢鍏跺姞鍏ュ箍鍛婅涔夊簱銆俓n"
            "鍙戦€?/cancel 鍙栨秷銆?
        )
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditSemanticAdAdd)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)


@router.message(StateFilter(AdminStates.EditSemanticAdAdd), F.from_user.id.in_(ADMIN_IDS))
async def process_semantic_ad_add(message: Message, state: FSMContext):
    try:
        if not message.text:
            await message.reply("鉂?璇疯緭鍏ユ枃鏈€傚彂閫?/cancel 鍙栨秷銆?)
            return
        if message.text.strip() == "/cancel":
            data = await state.get_data()
            group_id = data.get("group_id")
            kb = get_semantic_ad_menu_keyboard(group_id)
            await message.reply("宸插彇娑堛€?, reply_markup=kb)
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
        # 鍙鎴愬姛瀛︿範鍒版牱鏈紝灏辫嚜鍔ㄥ紑鍚缇ょ殑 AD 璇箟妫€娴嬶紝閬垮厤鈥滃彧鏀跺綍涓嶇敓鏁堚€?
        if added_ids and group_id:
            cfg = get_group_config(group_id)
            if not cfg.get("semantic_ad_enabled", False):
                cfg["semantic_ad_enabled"] = True
                await save_config()
        if added_ids:
            await message.reply(f"鉁?宸叉坊鍔?{len(added_ids)} 鏉″箍鍛婃牱鏈紝ID: {', '.join(map(str, added_ids))}銆?, reply_markup=kb)
        if skipped and not added_ids:
            await message.reply("鉁?鎵€鏈夎涓庣幇鏈夋牱鏈珮搴︾浉浼硷紝宸茶涓洪噸澶嶏紝鏈柊澧炪€?, reply_markup=kb)
        elif skipped:
            await message.reply(f"鈩癸笍 鍏朵腑 {skipped} 琛屼笌鐜版湁鏍锋湰楂樺害鐩镐技锛屽凡璺宠繃銆?, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?娣诲姞澶辫触: {e}")


@router.callback_query(F.data.startswith("view_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def view_semantic_ad(callback: CallbackQuery):
    try:
        # 瑙ｆ瀽椤电爜锛堥粯璁ょ 0 椤?= 鏈€鏂颁竴椤碉級
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
            await callback.answer("褰撳墠骞垮憡璇箟搴撲负绌恒€?, show_alert=False)
            return

        PAGE_SIZE = 20
        total = len(samples)
        # 鎸夋椂闂存帓搴忓悗锛屾渶鏂板湪鏈€鍚庝竴鏉★紱鍒嗛〉鏃朵粠鏈€鏂板線鍓嶇炕
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
        header = f"骞垮憡璇箟搴擄紙鍏?{total} 鏉★紝褰撳墠绗?{page + 1}/{max_page + 1} 椤碉紝ID: 鏂囨湰锛塡n"
        text = header + "\n".join(lines)

        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="猬咃笍 涓婁竴椤?, callback_data=f"view_semantic_ad:{page-1}"))
        if page < max_page:
            buttons.append(InlineKeyboardButton(text="涓嬩竴椤?鉃★笍", callback_data=f"view_semantic_ad:{page+1}"))
        rows = []
        if buttons:
            rows.append(buttons)
        # 杩斿洖 AD 鑿滃崟
        rows.append([InlineKeyboardButton(text="猬咃笍 杩斿洖", callback_data=f"submenu_semantic_ad:{callback.message.chat.id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?鏌ョ湅澶辫触: {e}", show_alert=False)


@router.callback_query(F.data.startswith("remove_semantic_ad:"), F.from_user.id.in_(ADMIN_IDS))
async def remove_semantic_ad_callback(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        text = (
            f"<b>{title}</b> 鈥?AD鏈哄櫒瀛︿範 鈥?鍑忓皯骞垮憡璇彞\n\n"
            "璇峰彂閫佽鍒犻櫎鐨勫箍鍛婃牱鏈?ID锛堟暟瀛楋級銆傚彲浠ュ厛鐐瑰嚮銆屽箍鍛婅瘝搴撳睍绀恒€嶆煡鐪?ID銆俓n"
            "鍙戦€?/cancel 鍙栨秷銆?
        )
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditSemanticAdRemove)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)


@router.message(StateFilter(AdminStates.EditSemanticAdRemove), F.from_user.id.in_(ADMIN_IDS))
async def process_semantic_ad_remove(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        if not message.text:
            await message.reply("鉂?璇疯緭鍏ヨ鍒犻櫎鐨勬牱鏈?ID锛堟暟瀛楋級銆傚彂閫?/cancel 鍙栨秷銆?)
            return
        if message.text.strip() == "/cancel":
            kb = get_semantic_ad_menu_keyboard(group_id)
            await message.reply("宸插彇娑堛€?, reply_markup=kb)
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
            await message.reply(f"鉁?宸插垹闄ゅ箍鍛婃牱鏈?ID: {', '.join(map(str, removed))}", reply_markup=kb)
        if not_found:
            await message.reply(f"鈩癸笍 鏈壘鍒版牱鏈?ID: {', '.join(map(str, not_found))}", reply_markup=kb)
        if invalid and not removed and not not_found:
            await message.reply("鉂?璇疯緭鍏ユ湁鏁堢殑鏁板瓧 ID锛堟瘡琛屼竴涓級銆傚彂閫?/cancel 鍙栨秷銆?, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?鍒犻櫎澶辫触: {e}")

@router.callback_query(F.data.startswith("toggle_bio_link:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_bio_link(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

@router.callback_query(F.data.startswith("toggle_bio_keywords:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_bio_keywords(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

@router.callback_query(F.data.startswith("edit_bio_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_bio_keywords(callback: CallbackQuery, state: FSMContext):
    await callback.answer("该功能已下线", show_alert=True)

@router.message(StateFilter(AdminStates.EditBioKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_bio_keywords(message: Message, state: FSMContext):
    await state.set_state(AdminStates.GroupMenu)
    await message.reply("该功能已下线")

@router.callback_query(F.data.startswith("view_bio_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def view_bio_keywords(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

# ==================== 鏄剧ず鍚嶇О妫€娴?====================
@router.callback_query(F.data.startswith("submenu_display:"), F.from_user.id.in_(ADMIN_IDS))
async def display_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "鉁? if cfg.get("check_display_keywords") else "鉂?
        text = f"<b>{title}</b> 鈥?鍚嶇О妫€娴? {status}"
        kb = get_display_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_display:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_display(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_display_keywords"] = not cfg.get("check_display_keywords", True)
        await save_config()
        status = "鉁? if cfg["check_display_keywords"] else "鉂?
        await callback.answer(f"鍚嶇О妫€娴? {status}", show_alert=True)
        kb = get_display_menu_keyboard(group_id)
        status_display = "鉁? if cfg.get("check_display_keywords") else "鉂?
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> 鈥?鍚嶇О妫€娴? {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_display_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_display_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        keywords = cfg.get("display_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"<b>{title}</b> 鈥?缂栬緫鍚嶇О鏁忔劅璇峔n\n褰撳墠鍒楄〃锛歕n" + (kw_text if kw_text else "锛堢┖锛?) + "\n\n鍙戦€佹柊璇嶏紙涓€琛屼竴涓級浼氳拷鍔犲埌鍒楄〃锛?clear 娓呯┖鍏ㄩ儴"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditDisplayKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
            await message.reply("鉁?宸叉竻绌哄悕绉版晱鎰熻瘝鍒楄〃", reply_markup=kb)
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
            await message.reply(f"鉁?宸茶拷鍔?{len(added)} 涓瘝锛屽綋鍓嶅叡 {len(existing)} 涓?, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("view_display_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def view_display_keywords(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("display_keywords", [])
        kw_text = "\n".join(keywords) if keywords else "锛堟棤锛?
        text = f"鍚嶇О鏁忔劅璇嶏紙{len(keywords)}涓級\n\n{kw_text}"
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

# ==================== 娑堟伅妫€娴?====================
@router.callback_query(F.data.startswith("submenu_message:"), F.from_user.id.in_(ADMIN_IDS))
async def message_submenu(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

@router.callback_query(F.data.startswith("toggle_message:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_message(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["check_message_keywords"] = not cfg.get("check_message_keywords", True)
        await save_config()
        status = "鉁? if cfg["check_message_keywords"] else "鉂?
        await callback.answer(f"娑堟伅妫€娴? {status}", show_alert=True)
        kb = get_message_menu_keyboard(group_id)
        status_display = "鉁? if cfg.get("check_message_keywords") else "鉂?
        msg_link = "鉁? if cfg.get("check_message_link", True) else "鉂?
        norm_status = "鉁? if cfg.get("message_keyword_normalize", True) else "鉂?
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> 鈥?娑堟伅妫€娴媆n\n鏁忔劅璇? {status_display}\n閾炬帴/@寮曟祦: {msg_link}\n闃叉嫾瀛楄閬? {norm_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_message_link:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_message_link(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

@router.callback_query(F.data.startswith("toggle_message_normalize:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_message_normalize(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["message_keyword_normalize"] = not cfg.get("message_keyword_normalize", True)
        await save_config()
        status = "鉁? if cfg["message_keyword_normalize"] else "鉂?
        await callback.answer(f"闃叉嫾瀛楄閬? {status}", show_alert=True)
        kb = get_message_menu_keyboard(group_id)
        title = await get_chat_title_safe(callback.bot, group_id)
        status_display = "鉁? if cfg.get("check_message_keywords") else "鉂?
        msg_link = "鉁? if cfg.get("check_message_link", True) else "鉂?
        norm_status = "鉁? if cfg.get("message_keyword_normalize", True) else "鉂?
        text = f"<b>{title}</b> 鈥?娑堟伅妫€娴媆n\n鏁忔劅璇? {status_display}\n閾炬帴/@寮曟祦: {msg_link}\n闃叉嫾瀛楄閬? {norm_status}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_message_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_message_keywords(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        keywords = cfg.get("message_keywords", [])
        kw_text = "\n".join(keywords)
        text = f"<b>{title}</b> 鈥?缂栬緫娑堟伅鏁忔劅璇峔n\n褰撳墠鍒楄〃锛歕n" + (kw_text if kw_text else "锛堢┖锛?) + "\n\n鍙戦€佹柊璇嶏紙涓€琛屼竴涓級浼氳拷鍔犲埌鍒楄〃锛?clear 娓呯┖鍏ㄩ儴"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMessageKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
            await message.reply("鉁?宸叉竻绌烘秷鎭晱鎰熻瘝鍒楄〃", reply_markup=kb)
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
            await message.reply(f"鉁?宸茶拷鍔?{len(added)} 涓瘝锛屽綋鍓嶅叡 {len(existing)} 涓?, reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("view_message_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def view_message_keywords(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        keywords = cfg.get("message_keywords", [])
        kw_text = "\n".join(keywords) if keywords else "锛堟棤锛?
        text = f"娑堟伅鏁忔劅璇嶏紙{len(keywords)}涓級\n\n{kw_text}"
        await callback.answer(text, show_alert=True)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

# ==================== 鐭秷鎭拰鍨冨溇妫€娴?====================
@router.callback_query(F.data.startswith("submenu_short:"), F.from_user.id.in_(ADMIN_IDS))
async def short_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        short_enabled = "鉁? if cfg.get("short_msg_detection") else "鉂?
        fill_enabled = "鉁? if cfg.get("fill_garbage_detection") else "鉂?
        th = cfg.get("short_msg_threshold", 3)
        n = cfg.get("min_consecutive_count", 2)
        w = cfg.get("time_window_seconds", 60)
        rule = f"杩炵画{n}鏉″瓧鏁扳墹{th}鍦▄w}绉掑唴鍗宠Е鍙戯紙闃层€岀偣銆嶃€屾垜銆嶃€屽ご銆嶃€屽儚銆嶅紡杩炲彂锛?
        text = f"<b>{title}</b> 鈥?鐭秷鎭?鍨冨溇\n\n鐭秷鎭? {short_enabled}\n瑙勫垯: {rule}\n\n鍨冨溇: {fill_enabled}"
        kb = get_short_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_short:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_short_msg(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["short_msg_detection"] = not cfg.get("short_msg_detection", True)
        await save_config()
        status = "鉁? if cfg["short_msg_detection"] else "鉂?
        await callback.answer(f"鐭秷鎭? {status}", show_alert=True)
        kb = get_short_menu_keyboard(group_id)
        short_enabled = "鉁? if cfg.get("short_msg_detection") else "鉂?
        fill_enabled = "鉁? if cfg.get("fill_garbage_detection") else "鉂?
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> 鈥?鐭秷鎭?鍨冨溇\n\n鐭秷鎭? {short_enabled}\n鍨冨溇: {fill_enabled}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_threshold:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_threshold(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("short_msg_threshold", 3)
        text = f"瀛楁暟闃堝€硷紙褰撳墠: {current}锛夛細"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditShortMsgThreshold)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
        await message.reply(f"鉁?宸茶涓?{value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("鉂?璇疯緭鍏ユ暟瀛?)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("edit_consecutive:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_consecutive(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("min_consecutive_count", 2)
        text = f"杩炵画鏉℃暟锛堝綋鍓? {current}锛夛細"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditConsecutiveCount)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
        await message.reply(f"鉁?宸茶涓?{value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("鉂?璇疯緭鍏ユ暟瀛?)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("edit_window:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_window(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("time_window_seconds", 60)
        text = f"鏃堕棿绐楀彛绉掓暟锛堝綋鍓? {current}锛夛細"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditTimeWindow)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
        await message.reply(f"鉁?宸茶涓?{value}s", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("鉂?璇疯緭鍏ユ暟瀛?)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("toggle_fill:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_fill(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["fill_garbage_detection"] = not cfg.get("fill_garbage_detection", True)
        await save_config()
        status = "鉁? if cfg["fill_garbage_detection"] else "鉂?
        await callback.answer(f"鍨冨溇妫€娴? {status}", show_alert=True)
        kb = get_short_menu_keyboard(group_id)
        short_enabled = "鉁? if cfg.get("short_msg_detection") else "鉂?
        fill_enabled = "鉁? if cfg.get("fill_garbage_detection") else "鉂?
        breadcrumb = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{breadcrumb}</b> 鈥?鐭秷鎭?鍨冨溇\n\n鐭秷鎭? {short_enabled}\n鍨冨溇: {fill_enabled}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_fill_min:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_fill_min(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("fill_garbage_min_raw_len", 12)
        text = f"鏈€灏忓師濮嬮暱搴︼紙褰撳墠: {current}锛夛細"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditFillGarbageMinRaw)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
        await message.reply(f"鉁?宸茶涓?{value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("鉂?璇疯緭鍏ユ暟瀛?)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

# ==================== 杩濊澶勭悊 ====================
@router.callback_query(F.data.startswith("submenu_violation:"), F.from_user.id.in_(ADMIN_IDS))
async def violation_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        mute_hours = cfg.get("report_history_mute_hours", 24)
        mute_sec = mute_hours * 3600
        threshold = cfg.get("report_history_threshold", 3)
        text = f"<b>{title}</b> 鈥?杩濊澶勭悊\n\n馃攪 绂佽█: {fmt_duration(mute_sec)}\n瑙﹀彂: {threshold} 鏉′妇鎶?
        kb = get_violation_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_mute:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_mute_hours(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("report_history_mute_hours", 24)
        text = f"绂佽█鏃堕暱灏忔椂鏁帮紙褰撳墠: {current}锛夛細"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMuteHours)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMuteHours), F.from_user.id.in_(ADMIN_IDS))
async def process_mute_hours(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        old_h = cfg.get("report_history_mute_hours", 24)
        value = max(1, int(message.text.strip()))
        cfg["report_history_mute_hours"] = value
        await save_config()
        title = await get_chat_title_safe(message.bot, group_id)
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"鉁?宸叉洿鏂? <b>{title}</b> 鈥?杩濊澶勭悊\n绂佽█鏃堕暱: {old_h}h 鈫?{value}h", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("鉂?璇疯緭鍏ユ暟瀛?)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("edit_report_threshold:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_report_threshold(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("report_history_threshold", 3)
        text = f"瑙﹀彂绂佽█鐨勪妇鎶ユ暟锛堝綋鍓? {current}锛夛細"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditReportedThreshold)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditReportedThreshold), F.from_user.id.in_(ADMIN_IDS))
async def process_report_threshold(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        value = max(1, int(message.text.strip()))
        cfg["report_history_threshold"] = value
        await save_config()
        kb = get_violation_menu_keyboard(group_id)
        await message.reply(f"鉁?宸茶涓?{value}", reply_markup=kb)
        await state.set_state(AdminStates.GroupMenu)
    except ValueError:
        await message.reply("鉂?璇疯緭鍏ユ暟瀛?)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("edit_report_history_whitelist:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_report_history_whitelist(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        whitelist = cfg.get("report_history_whitelist", []) or []
        if not isinstance(whitelist, list):
            whitelist = []
        text = (
            "缂栬緫鍘嗗彶涓炬姤鐧藉悕鍗曪紙鐢ㄦ埛ID锛屼竴琛屼竴涓級\n\n褰撳墠鍒楄〃锛歕n"
            + ("\n".join(str(x) for x in whitelist) if whitelist else "锛堢┖锛?")
            + "\n\n鍙戦€佹柊鐢ㄦ埛ID锛堜竴琛屼竴涓級浼氳拷鍔犲埌鍒楄〃锛?clear 娓呯┖鍏ㄩ儴"
        )
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditReportHistoryWhitelist)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditReportHistoryWhitelist), F.from_user.id.in_(ADMIN_IDS))
async def process_report_history_whitelist(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        if message.text.strip() == "/clear":
            cfg["report_history_whitelist"] = []
            await save_config()
            await message.reply("鉁?宸叉竻绌哄巻鍙蹭妇鎶ョ櫧鍚嶅崟", reply_markup=get_violation_menu_keyboard(group_id))
        else:
            existing = cfg.get("report_history_whitelist", []) or []
            if not isinstance(existing, list):
                existing = []
            new_users = [x.strip() for x in message.text.strip().splitlines() if x.strip()]
            added = [u for u in new_users if u not in existing]
            existing.extend(added)
            cfg["report_history_whitelist"] = existing
            await save_config()
            await message.reply(
                f"鉁?宸茶拷鍔?{len(added)} 浜猴紝褰撳墠鍏?{len(existing)} 浜?",
                reply_markup=get_violation_menu_keyboard(group_id),
            )
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

# ==================== 閲嶅鍙戣█ ====================
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
        text = f"<b>{title}</b> 鈥?閲嶅鍙戣█\n\n鈴?绐楀彛: {fmt_duration(w)}\n瑙﹀彂: {m} 娆n馃攪 棣栨绂佽█: {fmt_duration(b)}\n馃搵 璞佸厤璇? {n_kw} 涓?
        kb = get_repeat_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_repeat_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_exempt(callback: CallbackQuery, state: FSMContext):
    """缂栬緫閲嶅鍙戣█璞佸厤璇嶏紙鍚换涓€璇嶇殑娑堟伅涓嶈Е鍙戦噸澶嶆娴嬶級"""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        kw = cfg.get("repeat_exempt_keywords", []) or []
        if not isinstance(kw, list):
            kw = []
        text = "缂栬緫閲嶅鍙戣█璞佸厤璇嶏紙鐧藉悕鍗曪級\n鍚换涓€璇嶇殑娑堟伅涓嶈Е鍙戦噸澶嶆娴嬨€俓n\n褰撳墠鍒楄〃锛歕n" + ("\n".join(kw) if kw else "锛堢┖锛?) + "\n\n鍙戦€佹柊璇嶏紙涓€琛屼竴涓級浼氳拷鍔犲埌鍒楄〃锛?clear 娓呯┖鍏ㄩ儴"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatExemptKeywords)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatExemptKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_exempt(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        if message.text and message.text.strip() == "/clear":
            cfg["repeat_exempt_keywords"] = []
            await save_config()
            await message.reply("鉁?宸叉竻绌鸿眮鍏嶈瘝鍒楄〃", reply_markup=get_repeat_menu_keyboard(group_id))
        else:
            existing = cfg.get("repeat_exempt_keywords", []) or []
            if not isinstance(existing, list):
                existing = []
            new_words = [x.strip() for x in (message.text or "").strip().splitlines() if x.strip()]
            added = [w for w in new_words if w not in existing]
            existing.extend(added)
            cfg["repeat_exempt_keywords"] = existing
            await save_config()
            await message.reply(f"鉁?宸茶拷鍔?{len(added)} 涓瘝锛屽綋鍓嶅叡 {len(existing)} 涓眮鍏嶈瘝", reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

@router.callback_query(F.data.startswith("edit_repeat_window:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_window(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_window_seconds", 7200)
        await callback.message.edit_text(f"閲嶅妫€娴嬫椂闂寸獥鍙ｏ紙灏忔椂锛夛紙褰撳墠: {current // 3600}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatWindow)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatWindow), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_window(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_window_seconds"] = int(message.text.strip()) * 3600
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("edit_repeat_max:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_max(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_max_count", 3)
        await callback.message.edit_text(f"閲嶅鍑犳瑙﹀彂锛堝綋鍓? {current}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatMaxCount)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatMaxCount), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_max(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_max_count"] = int(message.text.strip())
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("edit_repeat_ban:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_repeat_ban(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("repeat_ban_seconds", 86400)
        await callback.message.edit_text(f"棣栨閲嶅杩濊绂佽█鏃堕暱锛堝皬鏃讹級锛堝綋鍓? {current // 3600}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditRepeatBanSec)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditRepeatBanSec), F.from_user.id.in_(ADMIN_IDS))
async def process_repeat_ban(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["repeat_ban_seconds"] = int(message.text.strip()) * 3600
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_repeat_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

# ==================== 濯掍綋鏉冮檺 ====================
@router.callback_query(F.data.startswith("submenu_media_perm:"), F.from_user.id.in_(ADMIN_IDS))
async def media_perm_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        msg = cfg.get("media_unlock_msg_count", 50)
        boost = cfg.get("media_unlock_boosts", 4)
        text = f"<b>{title}</b> 鈥?濯掍綋鏉冮檺\n\n瑙ｉ攣鎵€闇€娑堟伅: {msg}\n瑙ｉ攣鎵€闇€鍔╁姏: {boost}"
        kb = get_media_perm_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_media_msg:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_msg(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_unlock_msg_count", 50)
        await callback.message.edit_text(f"瑙ｉ攣鍙戝獟浣撴墍闇€鍚堣娑堟伅鏁帮紙褰撳墠: {current}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaUnlockMsg)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaUnlockMsg), F.from_user.id.in_(ADMIN_IDS))
async def process_media_msg(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_unlock_msg_count"] = int(message.text.strip())
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_media_perm_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("edit_media_boosts:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_boosts(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_unlock_boosts", 4)
        await callback.message.edit_text(f"瑙ｉ攣鍙戝獟浣撴墍闇€鍔╁姏娆℃暟锛堝綋鍓? {current}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaUnlockBoosts)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaUnlockBoosts), F.from_user.id.in_(ADMIN_IDS))
async def process_media_boosts(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_unlock_boosts"] = int(message.text.strip())
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_media_perm_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("toggle_media_broadcast:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_media_broadcast(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["media_rules_broadcast"] = not cfg.get("media_rules_broadcast", True)
        await save_config()
        on = "鉁? if cfg["media_rules_broadcast"] else "鉂?
        await callback.answer(f"瑙勫垯骞挎挱: {on}", show_alert=True)
        title = await get_chat_title_safe(callback.bot, group_id)
        msg = cfg.get("media_unlock_msg_count", 50)
        boost = cfg.get("media_unlock_boosts", 4)
        text = f"<b>{title}</b> 鈥?濯掍綋鏉冮檺\n\n瑙ｉ攣鎵€闇€娑堟伅: {msg}\n瑙ｉ攣鎵€闇€鍔╁姏: {boost}"
        await callback.message.edit_text(text, reply_markup=get_media_perm_menu_keyboard(group_id))
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_media_broadcast_interval:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_broadcast_interval(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_rules_broadcast_interval_minutes", 120)
        await callback.message.edit_text(f"瑙勫垯骞挎挱闂撮殧锛堝垎閽燂級锛堝綋鍓? {current}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaBroadcastInterval)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaBroadcastInterval), F.from_user.id.in_(ADMIN_IDS))
async def process_media_broadcast_interval(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_rules_broadcast_interval_minutes"] = max(1, int(message.text.strip()))
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_media_perm_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("submenu_media_whitelist:"), F.from_user.id.in_(ADMIN_IDS))
async def media_whitelist_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        wl = cfg.get("media_unlock_whitelist", [])
        if not isinstance(wl, list):
            wl = []
        text = f"<b>{title}</b> 鈥?濯掍綋瑙ｉ攣鐧藉悕鍗昞n\n鐢ㄦ埛ID鎴栫敤鎴峰悕锛屾弧瓒冲嵆鏃犻渶娑堟伅/鍔╁姏鍙彂濯掍綋銆俓n褰撳墠锛? + (", ".join(str(x) for x in wl) if wl else "锛堢┖锛?)
        await callback.message.edit_text(text, reply_markup=get_media_whitelist_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("add_media_whitelist:"), F.from_user.id.in_(ADMIN_IDS))
async def add_media_whitelist(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        await callback.message.edit_text("杈撳叆瑕佹坊鍔犵殑鐢ㄦ埛ID鎴栫敤鎴峰悕锛堜竴琛屼竴涓紝鏀寔澶氳锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaWhitelistAdd)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

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
        await message.reply(f"鉁?宸叉坊鍔狅紝褰撳墠鍏?{len(wl)} 椤?, reply_markup=get_media_whitelist_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

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
        text = f"<b>{title}</b> 鈥?濯掍綋瑙ｉ攣鐧藉悕鍗昞n\n褰撳墠锛? + (", ".join(str(x) for x in wl) if wl else "锛堢┖锛?)
        await callback.message.edit_text(text, reply_markup=get_media_whitelist_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

# ==================== 濯掍綋涓炬姤 ====================
@router.callback_query(F.data.startswith("submenu_media_report:"), F.from_user.id.in_(ADMIN_IDS))
async def media_report_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        cooldown = cfg.get("media_report_cooldown_sec", 20 * 60)
        max_day = cfg.get("media_report_max_per_day", 3)
        del_th = cfg.get("media_report_delete_threshold", 3)
        text = f"<b>{title}</b> 鈥?濯掍綋涓炬姤\n\n鈴?杩炵画涓炬姤鍐峰嵈: {fmt_duration(cooldown)}\n姣忔棩涓婇檺: {max_day} 娆n涓炬姤杈?{del_th} 浜哄垹濯掍綋"
        kb = get_media_report_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_media_cooldown:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_cooldown(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_report_cooldown_sec", 20 * 60)
        await callback.message.edit_text(f"杩炵画涓炬姤鍐峰嵈锛堝垎閽燂級锛堝綋鍓? {current // 60}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaReportCooldown)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaReportCooldown), F.from_user.id.in_(ADMIN_IDS))
async def process_media_cooldown(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_report_cooldown_sec"] = int(message.text.strip()) * 60
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_media_report_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("edit_media_maxday:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_maxday(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_report_max_per_day", 3)
        await callback.message.edit_text(f"姣忔棩涓炬姤娆℃暟涓婇檺锛堝綋鍓? {current}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaReportMaxDay)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaReportMaxDay), F.from_user.id.in_(ADMIN_IDS))
async def process_media_maxday(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_report_max_per_day"] = int(message.text.strip())
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_media_report_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

@router.callback_query(F.data.startswith("edit_media_delete_threshold:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_media_delete_threshold(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        current = cfg.get("media_report_delete_threshold", 3)
        await callback.message.edit_text(f"涓炬姤杈惧灏戜汉鍒犻櫎濯掍綋锛堝綋鍓? {current}锛夛細", reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditMediaDeleteThreshold)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditMediaDeleteThreshold), F.from_user.id.in_(ADMIN_IDS))
async def process_media_delete_threshold(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        cfg["media_report_delete_threshold"] = max(1, int(message.text.strip()))
        await save_config()
        await message.reply("鉁?宸叉洿鏂?, reply_markup=get_media_report_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except (ValueError, Exception) as e:
        await message.reply(f"鉂?璇疯緭鍏ユ暟瀛? {e}")

# ==================== 鑷姩鍥炲 ====================
@router.callback_query(F.data.startswith("submenu_autoreply:"), F.from_user.id.in_(ADMIN_IDS))
async def autoreply_submenu(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

@router.callback_query(F.data.startswith("toggle_ar:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_autoreply(callback: CallbackQuery):
    await callback.answer("该功能已下线", show_alert=True)

@router.callback_query(F.data.startswith("edit_ar_kw:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_keywords(callback: CallbackQuery, state: FSMContext):
    await callback.answer("该功能已下线", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyKeywords), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_keywords(message: Message, state: FSMContext):
    await state.set_state(AdminStates.GroupMenu)
    await message.reply("该功能已下线")

@router.callback_query(F.data.startswith("edit_ar_text:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_text(callback: CallbackQuery, state: FSMContext):
    await callback.answer("该功能已下线", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyText), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_text(message: Message, state: FSMContext):
    await state.set_state(AdminStates.GroupMenu)
    await message.reply("该功能已下线")

@router.callback_query(F.data.startswith("edit_ar_btn:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_buttons(callback: CallbackQuery, state: FSMContext):
    await callback.answer("该功能已下线", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyButtons), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_buttons(message: Message, state: FSMContext):
    await state.set_state(AdminStates.GroupMenu)
    await message.reply("该功能已下线")

@router.callback_query(F.data.startswith("edit_ar_del:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_ar_delete(callback: CallbackQuery, state: FSMContext):
    await callback.answer("该功能已下线", show_alert=True)

@router.message(StateFilter(AdminStates.EditAutoreplyDeleteTime), F.from_user.id.in_(ADMIN_IDS))
async def process_ar_delete(message: Message, state: FSMContext):
    await state.set_state(AdminStates.GroupMenu)
    await message.reply("该功能已下线")

# ==================== 鍩虹璁剧疆 ====================
@router.callback_query(F.data.startswith("submenu_basic:"), F.from_user.id.in_(ADMIN_IDS))
async def basic_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        status = "鉁?杩愯涓? if cfg.get("enabled") else "鉂?宸插仠鐢?
        text = f"<b>{title}</b> 鈥?鍩虹璁剧疆\n\n<code>ID: {group_id}</code>\n鐘舵€? {status}"
        kb = get_basic_menu_keyboard(group_id)
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("toggle_group:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_group(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        cfg["enabled"] = not cfg.get("enabled", True)
        await save_config()
        status = "鉁? if cfg["enabled"] else "鉂?
        await callback.answer(f"缇ょ粍鐘舵€? {status}", show_alert=True)
        kb = get_basic_menu_keyboard(group_id)
        status_display = "鉁?杩愯涓? if cfg.get("enabled") else "鉂?宸插仠鐢?
        title = await get_chat_title_safe(callback.bot, group_id)
        text = f"<b>{title}</b> 鈥?鍩虹璁剧疆\n\n<code>ID: {group_id}</code>\n鐘舵€? {status_display}"
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)


@router.callback_query(F.data.startswith("export_listen_log:"), F.from_user.id.in_(ADMIN_IDS))
async def export_listen_log(callback: CallbackQuery):
    """瀵煎嚭鏈€杩?10 鏉＄洃鍚喅绛栨棩蹇楋紙鐢ㄤ簬瀹氫綅锛氭槸鍚︽敹鍒扮兢娑堟伅銆佷负浣曟湭瑙﹀彂 AD/瑙勫垯锛夈€?""
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        rows = list(listen_decision_logs)
        if not rows:
            text = (
                f"<b>{title}</b> 鈥?鐩戝惉鏃ュ織锛堣繎10鏉★級\n\n"
                "褰撳墠娌℃湁浠讳綍鐩戝惉璁板綍銆俓n\n"
                "杩欓€氬父鎰忓懗鐫€锛氭満鍣ㄤ汉娌℃湁鏀跺埌缇ゆ秷鎭洿鏂般€俓n"
                "璇蜂紭鍏堟鏌ワ細\n"
                "1) BotFather 闅愮妯″紡锛?setprivacy锛夋槸鍚﹀叧闂璡n"
                "2) 鏈哄櫒浜烘槸鍚︽槸缇ょ鐞嗗憳 & 鏈夎鍙?鍒犻櫎鏉冮檺\n"
                "3) 鐜鍙橀噺 GROUP_IDS 鏄惁鍖呭惈璇ョ兢鐪熷疄 chat.id锛堝父瑙佷负 -100鈥︼級"
            )
            await callback.message.reply(text)
            await callback.answer()
            return

        # 鏈€鏂板湪鍚庯紝瀵煎嚭鏃舵寜鈥滄柊鈫掓棫鈥?
        lines = [f"{title} 鐩戝惉鏃ュ織锛堣繎10鏉★紝鏂扳啋鏃э級", f"瀵煎嚭鏃堕棿: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}", ""]
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

        # 1) 鍏堝彂涓€浠芥枃鏈紙渚夸簬蹇€熺湅锛?
        await callback.message.reply(f"<pre>{out}</pre>")

        # 2) 鍐嶅彂涓€浠?txt 浣滀负鈥滃鍑衡€?
        buf = out.encode("utf-8")
        filename = f"listen_log_{int(time.time())}.txt"
        await callback.message.reply_document(BufferedInputFile(buf, filename=filename))
        await callback.answer("鉁?宸插鍑?)
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("submenu_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def exempt_submenu(callback: CallbackQuery):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        title = await get_chat_title_safe(callback.bot, group_id)
        cfg = get_group_config(group_id)
        exempt = cfg.get("exempt_users") or []
        if isinstance(exempt, dict):
            exempt = list(exempt.keys())
        text = f"<b>{title}</b> 鈥?璞佸厤妫€娴嬶紙绠€浠?鏄电О绛夛紝涓庡彂鍥剧櫧鍚嶅崟鏃犲叧锛塡n\n褰撳墠: " + (", ".join(str(x) for x in exempt) if exempt else "锛堟棤锛?)
        await callback.message.edit_text(text, reply_markup=get_exempt_menu_keyboard(group_id))
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.callback_query(F.data.startswith("edit_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def edit_exempt(callback: CallbackQuery, state: FSMContext):
    try:
        group_id = int(callback.data.split(":", 1)[1])
        cfg = get_group_config(group_id)
        exempt = cfg.get("exempt_users") or []
        if isinstance(exempt, dict):
            exempt = list(exempt.keys())
        text = f"缂栬緫璞佸厤妫€娴嬬敤鎴凤紙鐢ㄦ埛ID锛屼竴琛屼竴涓紱璞佸厤绠€浠?鏄电О绛夋娴嬶紝鍙戝浘鍙︽湁鐧藉悕鍗曪級\n\n褰撳墠鍒楄〃锛歕n" + ("\n".join(str(x) for x in exempt) if exempt else "锛堢┖锛?) + "\n\n鍙戦€佹柊鐢ㄦ埛ID锛堜竴琛屼竴涓級浼氳拷鍔犲埌鍒楄〃锛?clear 娓呯┖鍏ㄩ儴"
        await callback.message.edit_text(text, reply_markup=None)
        await state.update_data(group_id=group_id)
        await state.set_state(AdminStates.EditExemptUsers)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

@router.message(StateFilter(AdminStates.EditExemptUsers), F.from_user.id.in_(ADMIN_IDS))
async def process_exempt(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        group_id = data.get("group_id")
        cfg = get_group_config(group_id)
        if message.text.strip() == "/clear":
            cfg["exempt_users"] = []
            await save_config()
            await message.reply("鉁?宸叉竻绌鸿眮鍏嶇敤鎴峰垪琛?, reply_markup=get_exempt_menu_keyboard(group_id))
        else:
            existing = cfg.get("exempt_users", []) or []
            if not isinstance(existing, list):
                existing = []
            new_users = [x.strip() for x in message.text.strip().splitlines() if x.strip()]
            added = [u for u in new_users if u not in existing]
            existing.extend(added)
            cfg["exempt_users"] = existing
            await save_config()
            await message.reply(f"鉁?宸茶拷鍔?{len(added)} 浜猴紝褰撳墠鍏?{len(existing)} 浜?, reply_markup=get_exempt_menu_keyboard(group_id))
        await state.set_state(AdminStates.GroupMenu)
    except Exception as e:
        await message.reply(f"鉂?{str(e)}")

# ==================== 鐘舵€佹煡鐪?====================
async def _build_status_text(bot) -> str:
    """鐢熸垚鐘舵€侀〉瀹屾暣鏂囨"""
    admin_count = len(ADMIN_IDS)
    async with lock:
        report_count = len(reports)
    lines = [
        "馃搳 <b>绯荤粺鐘舵€?/b>\n",
        "<b>缇ょ粍姒傝</b>:",
    ]
    for gid in sorted(GROUP_IDS):
        title = await get_chat_title_safe(bot, gid)
        cfg = get_group_config(gid)
        status = "鉁?杩愯涓? if cfg.get("enabled", True) else "鉂?宸插仠鐢?
        lines.append(f"鈹?{title} (<code>{gid}</code>)  {status}")
    lines.append("")
    lines.append("<b>鏁版嵁缁熻</b>:")
    lines.append(f"鈹?杩涜涓妇鎶? {report_count} 鏉?)
    try:
        uv_total = len(user_violations) if user_violations else 0
        lines.append(f"鈹?杩濊鐢ㄦ埛璁板綍: {uv_total} 鏉?)
    except Exception:
        lines.append("鈹?杩濊鐢ㄦ埛璁板綍: 鈥?)
    lines.append("")
    lines.append("<b>绯荤粺</b>: 鉁?杩愯姝ｅ父  |  绠＄悊鍛? %d" % admin_count)
    return "\n".join(lines)


@router.callback_query(F.data == "view_status", F.from_user.id.in_(ADMIN_IDS))
async def view_status(callback: CallbackQuery):
    try:
        text = await _build_status_text(callback.bot)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="馃攧 鍒锋柊", callback_data="view_status")],
            [InlineKeyboardButton(text="猬咃笍 杩斿洖涓昏彍鍗?, callback_data="back_main")],
        ])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    except Exception as e:
        await callback.answer(f"鉂?{str(e)}", show_alert=True)

# ==================== 妫€娴嬪拰鍥炲鏍稿績閫昏緫 ====================
FILL_CHARS = set(r" .,锛屻€傦紒锛?\\\~`-_=+[]{}()\"'\\|\n\t\r銆€")
# 闃叉嫾瀛楄閬匡細鍘绘帀绌烘牸銆佸父瑙佹爣鐐瑰悗鍖归厤鏁忔劅璇嶏紙濡?A  bc锛孌 鍛戒腑 abcd锛?
KEYWORD_NORMALIZE_CHARS = set(" .,锛屻€傦紒锛熴€侊紱锛歕"'锛堬級銆愩€慭n\t\r銆€*_~`-+=|")

def _normalize_for_keyword(text: str) -> str:
    """鍘绘帀绌烘牸鍜屽父瑙佹爣鐐广€佽浆灏忓啓锛岀敤浜庨槻鎷煎瓧瑙勯伩鍖归厤"""
    if not text:
        return ""
    return "".join(c for c in text.lower() if c not in KEYWORD_NORMALIZE_CHARS)

user_short_msg_history = {}

# key: (group_id, user_id, normalized_text) -> deque[timestamp]锛沰ey 鏁伴噺涓婇檺 REPEAT_HISTORY_MAX_KEYS锛岃秴鍒欐窐姹版渶涔呮湭鐢?
repeat_message_history = {}
repeat_message_history_last = {}  # key -> last_activity_time锛岀敤浜庢窐姹?
REPEAT_HISTORY_MAX_KEYS = 20000
# key: (group_id, user_id) -> int锛?/1/2锛夛紱鎸佷箙鍖栧埌 REPEAT_LEVEL_FILE锛?
repeat_violation_level = {}
MEDIA_REPORT_LAST_MAX = 5000


def _normalize_text(text: str) -> str:
    """Normalize text for short-term matching."""
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
    """浠庢秷鎭腑鑾峰彇鐢ㄤ簬灞曠ず鐨勭敤鎴峰悕"""
    name = None
    if message and message.from_user and message.from_user.id == user_id:
        name = message.from_user.full_name or message.from_user.username
    if not name:
        name = f"ID {user_id}"
    return name


async def handle_repeat_message(message: Message) -> bool:
    """
    妫€娴嬬敤鎴锋槸鍚﹀湪閰嶇疆鏃堕棿绐楀彛鍐呴噸澶嶅彂閫佺浉鍚屽唴瀹?
    杩斿洖 True 琛ㄧず宸茬粡杩涜浜嗗缃?鎻愰啋骞朵笖鏈娑堟伅鍚庣画閫昏緫搴斾腑姝?
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
            f"鈿狅笍 妫€娴嬪埌浣犲湪 {window_sec // 3600} 灏忔椂鍐呴噸澶嶅彂閫佺浉鍚屽唴瀹癸紙2/{max_count}锛夛紝璇疯皟鏁存枃瀛楀唴瀹广€?
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
                print(f"閲嶅鍙戣█绂佽█澶辫触: {e}")
                return False
            repeat_violation_level[level_key] = 1
            await save_repeat_levels()
            notice = (
                f"馃毇 鐢ㄦ埛 {display_name}\n"
                f"馃搶 瑙﹀彂鍘熷洜锛氬湪閰嶇疆鏃堕棿绐楀彛鍐呭娆￠噸澶嶅彂閫佺浉鍚屽唴瀹癸紙{max_count}/{max_count}锛夈€俓n"
                f"馃敀 澶勭悊缁撴灉锛氬洜鍒峰睆宸茶鏈兢绂佽█ 1 澶┿€俓n{MISJUDGE_BOT_MENTION}"
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
                print(f"閲嶅鍙戣█姘稿皝澶辫触: {e}")
                return False
            repeat_violation_level[level_key] = 2
            await save_repeat_levels()
            notice = (
                f"馃毇 鐢ㄦ埛 {display_name}\n"
                f"馃搶 瑙﹀彂鍘熷洜锛氬娆″湪 2 灏忔椂鍐呴噸澶嶅彂閫佺浉鍚屽唴瀹癸紝涓斿湪琚В绂佸悗浠嶇劧缁х画杩濊銆俓n"
                f"馃敀 澶勭悊缁撴灉锛氬凡琚湰缇ゆ案涔呯姝㈠彂瑷€銆倇MISJUDGE_BOT_MENTION}"
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
        print("鏁版嵁鍔犺浇澶辫触锛堥娆℃甯革級:", e)

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
            print("淇濆瓨澶辫触:", e)

def count_user_reported_messages(user_id: int, group_id: int) -> int:
    """浠呯粺璁¤闈炵鐞嗗憳涓炬姤鐨勬秷鎭潯鏁?""
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
    state = report_action_state.get(_report_action_key(group_id, user_id), {})
    try:
        return int(state.get("last_trigger_count", 0))
    except Exception:
        return 0


def build_report_history_exempt_keyboard(group_id: int, user_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="璞佸厤鍘嗗彶涓炬姤", callback_data=f"report_history_exempt:{group_id}:{user_id}")]
        ]
    )

def build_warning_buttons(group_id: int, msg_id: int, report_count: int):
    """鏋勫缓璀﹀憡娑堟伅鎸夐挳锛沜allback 甯?group_id 閬垮厤澶氱兢涓叉锛涗妇鎶ユ寜閽樉绀哄綋鍓嶄汉鏁?""
    report_text = f"涓炬姤 ({report_count}浜?" if report_count > 0 else "涓炬姤"
    buttons = [
        [
            InlineKeyboardButton(text=report_text, callback_data=f"report:{group_id}:{msg_id}"),
            InlineKeyboardButton(text="璇垽馃懏鈥嶁檪锔?, callback_data=f"exempt:{group_id}:{msg_id}")
        ]
    ]
    if report_count > 0:
        buttons.append([
            InlineKeyboardButton(text="绂?4h馃懏鈥嶁檪锔?, callback_data=f"ban24h:{group_id}:{msg_id}"),
            InlineKeyboardButton(text="姘稿皝馃懏鈥嶁檪锔?, callback_data=f"banperm:{group_id}:{msg_id}")
        ])
    # 绠＄悊鍛樻爣璁板箍鍛婂苟鍒犻櫎锛氬涔犲箍鍛婃牱鏈?+ 鍒犻櫎璇ョ敤鎴疯繎鏈熷叏閮ㄦ秷鎭?
    buttons.append([
        InlineKeyboardButton(text="鏍囪骞垮憡骞跺垹闄ゐ煈€嶁檪锔?, callback_data=f"markad:{group_id}:{msg_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def _media_reply_buttons(chat_id: int, media_msg_id: int, report_count: int, like_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"涓炬姤鍎跨鑹叉儏鈿狅笍 {report_count}浜?, callback_data=f"mr:{chat_id}:{media_msg_id}"),
            InlineKeyboardButton(text=f"鐐硅禐馃憤 {like_count}浜?, callback_data=f"ml:{chat_id}:{media_msg_id}"),
        ]
    ])

def _message_link(chat_id: int, msg_id: int) -> str:
    """缇ゅ唴娑堟伅閾炬帴锛屼究浜庣鐞嗗憳瀹氫綅"""
    cid = str(chat_id).replace("-100", "")
    return f"https://t.me/c/{cid}/{msg_id}"

async def _delete_user_recent_and_warnings(group_id: int, user_id: int, orig_msg_id: int | None, keep_one_text: str = "", auto_delete_sec: int = 0):
    """鍒犻櫎璇ョ敤鎴锋渶杩?24 灏忔椂鍐呮秷鎭€佹満鍣ㄤ汉瀵瑰叾鐨勮鍛婏紝浠呬繚鐣欎竴鏉℃渶缁堝叕鍛婏紙甯﹁灏佽仈绯伙級銆?
    auto_delete_sec > 0 鏃讹紝鍏憡娑堟伅鍦ㄦ寚瀹氱鏁板悗鑷姩鍒犻櫎銆?""
    key = (group_id, user_id)
    now = time.time()
    cutoff = now - USER_MSG_24H_SEC
    memory_changed = False
    if key in user_recent_message_ids:
        for msg_id, t, txt in list(user_recent_message_ids[key]):
            if t >= cutoff:
                # 琚満鍣ㄤ汉鍒犻櫎鐨勬秷鎭篃浣滀负骞垮憡鏍锋湰瀛︿範
                if txt:
                    try:
                        semantic_ad_detector.add_ad_sample(txt)
                        memory_changed = _remember_forward_match(group_id, user_id, txt) or memory_changed
                    except Exception as e:
                        print(f"鍒犻櫎鐢ㄦ埛娑堟伅鏃跺涔犳牱鏈け璐? {e}")
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
    """绠＄悊鍛樺湪缇ゅ唴鍥炲鏌愭潯娑堟伅骞跺彂閫?/setboost 4锛屽皢璇ョ敤鎴风殑缇ょ粍鍔╁姏娆℃暟璁句负 4锛堢敤浜庤В閿佸彂濯掍綋锛?""
    try:
        text = (message.text or "").strip().split()
        if len(text) != 2:
            await message.reply("鐢ㄦ硶锛氬洖澶嶈璁剧疆鐨勭敤鎴风殑娑堟伅锛屽彂閫?/setboost 鏁板瓧锛堝 /setboost 4锛?)
            return
        count = int(text[1])
        if count < 0 or count > 100:
            await message.reply("鍔╁姏娆℃暟璇峰～ 0锝?00")
            return
        target = message.reply_to_message.from_user
        if not target or target.is_bot:
            await message.reply("璇峰洖澶嶇湡瀹炵敤鎴风殑娑堟伅")
            return
        key = _media_key(message.chat.id, target.id)
        media_stats["boosts"][key] = count
        await save_media_stats()
        name = target.full_name or target.username or target.id
        await message.reply(f"宸插皢璇ョ敤鎴峰湪鏈兢鐨勫姪鍔涙鏁拌涓?{count}銆倇name} 鐜板彲鍙戝獟浣撱€?)
    except ValueError:
        await message.reply("璇峰彂閫佹暟瀛楋紝濡?/setboost 4")
    except Exception as e:
        await message.reply(f"璁剧疆澶辫触: {e}")

@router.message(F.chat.id.in_(GROUP_IDS), F.photo | F.video | F.voice | F.video_note)
async def on_media_message(message: Message):
    """濯掍綋娑堟伅锛氬厛妫€澶栭儴寮曠敤锛涙棤鏉冮檺鍒欏垹闄ゅ苟鎻愮ず锛涙湁鏉冮檺鍒欏洖澶嶄妇鎶?鐐硅禐鎸夐挳"""
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
        # 璁＄畻杩炵画鏃犳潈闄愬彂濯掍綋娆℃暟锛堣秴杩囦竴瀹氭椂闂存湭鍐嶈Е鍙戝垯閲嶇疆锛?
        strike_count, last_ts = media_no_perm_strikes.get(sk, (0, 0))
        if now - last_ts > MEDIA_NO_PERM_STRIKE_RESET_SEC:
            strike_count = 0
        strike_count += 1
        media_no_perm_strikes[sk] = (strike_count, now)

        if strike_count >= 2:
            # 杩炵画涓ゆ浠ヤ笂瑙﹀彂鏃犳潈闄愬彂濯掍綋锛岀洿鎺ュ叧闂叾鍙戝獟浣撴潈闄愶紝闃叉缁х画鍒峰睆
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
                print(f"鍏抽棴濯掍綋鏉冮檺澶辫触: {e}")
            return

        prev_msg_id = last_media_no_perm_msg.get(sk)
        if prev_msg_id is not None:
            try:
                await bot.delete_message(group_id, prev_msg_id)
            except Exception:
                pass
        sent = await bot.send_message(
            group_id,
            f"鈿狅笍 {name} 灏氭湭瑙ｉ攣鍙戝獟浣撱€俓n"
            f"馃搳 鎮ㄧ殑杩涘害锛氬彂閫佸悎瑙勬秷鎭?{count}/{need_msg}锛屽姪鍔?{boosts}/{need_boosts}锛堟弧鍏朵竴鍗冲彲瑙ｉ攣锛夈€俓n"
            f"杈撳叆銆屾潈闄愩€嶆煡杩涘害锛岃緭鍏ャ€屽彫鍞ゃ€嶄娇鐢ㄦ満鍣ㄤ汉浠ｅ彂鍥俱€?
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
    reply = await message.reply("馃搸 濯掍綋娑堟伅", reply_markup=_media_reply_buttons(group_id, message.message_id, 0, 0))
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
    """璁板綍鐢ㄦ埛娑堟伅 id 鍜屾枃鏈紝鐢ㄤ簬 24 灏忔椂鍐呭彲鍒犲苟鍙涔?""
    key = (group_id, user_id)
    if key not in user_recent_message_ids:
        user_recent_message_ids[key] = deque(maxlen=USER_MSG_TRACK_MAXLEN)
    user_recent_message_ids[key].append((msg_id, time.time(), text or ""))


def _track_bot_message(group_id: int, msg_id: int, auto_delete_sec: int = BOT_MSG_AUTO_DELETE_SEC):
    """璺熻釜鏈哄櫒浜哄彂閫佺殑娑堟伅锛屽畨鎺掕嚜鍔ㄥ垹闄?""
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
    """浠呰褰曞湪鐩爣缇ら噷鐨勫紩鐢ㄥ洖澶嶏紝鍚庣画鍋氳ˉ鍋垮垹闄?""
    try:
        chat = message.chat
        if not chat or chat.id not in GROUP_IDS:
            return
        bot_reply_links[(chat.id, reply.message_id)] = (message.message_id, time.time())
    except Exception:
        pass


async def _delete_linked_bot_replies(group_id: int, original_msg_id: int | None):
    """鍒犻櫎寮曠敤浜嗘煇鏉″師娑堟伅鐨勬満鍣ㄤ汉鍥炲锛岄伩鍏嶅師娑堟伅鍒犻櫎鍚庣兢閲屾畫鐣欐満鍣ㄤ汉鐨勫憡璀︺€?""
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
    """鍒犻櫎鍘熸秷鎭紝骞跺悓姝ュ垹闄ゆ満鍣ㄤ汉瀵硅娑堟伅鐨勫紩鐢ㄥ洖澶嶃€?""
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
    """瀛︿範鍒板箍鍛婃牱鏈悗锛岀‘淇濆搴旂兢缁勫紑鍚涔夊箍鍛婃娴嬨€?""
    cfg = get_group_config(group_id)
    if cfg.get("semantic_ad_enabled", False):
        return False
    cfg["semantic_ad_enabled"] = True
    await save_config()
    return True


async def _check_and_delete_semantic_ad_message(message: Message, text: str, *, group_id: int, user_id: int) -> bool:
    """
    鐢ㄥ凡瀛︿範鐨勫箍鍛婂簱涓诲姩鍖归厤褰撳墠娑堟伅銆?    鍛戒腑鍚庣洿鎺ュ垹闄ゅ師娑堟伅鍜岀浉鍏虫満鍣ㄤ汉鍥炲銆?    """
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
            details="杩涘叆AD妫€娴嬩絾鍛戒腑璞佸厤鐢ㄦ埛 exempt_users锛岃烦杩嘇D鍖归厤",
        )
        return False

    wl_words = cfg.get("repeat_exempt_keywords") or []
    if any(w and w in text for w in wl_words):
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="PASS",
            details="杩涘叆AD妫€娴嬩絾鍛戒腑璞佸厤璇?repeat_exempt_keywords锛岃烦杩嘇D鍖归厤",
        )
        return False

    is_semantic_ad, sim, _ = semantic_ad_detector.check_text(text)
    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message.message_id,
        text=text,
        verdict="AD_HIT" if is_semantic_ad else "AD_MISS",
        details=f"AD鍖归厤缁撴灉: is_ad={is_semantic_ad}, score={sim:.3f}",
    )
    if not is_semantic_ad:
        return False

    await _delete_original_and_linked_reply(group_id, message.message_id)
    _push_listen_log(
        group_id=group_id,
        user_id=user_id,
        msg_id=message.message_id,
        text=text,
        verdict="AD_DELETE",
        details="鍛戒腑AD璇箟搴擄紝宸叉墽琛屽垹闄?,
    )
    return True


def _get_only_group_id() -> int | None:
    """浠呴厤缃簡涓€涓彈鎺х兢鏃讹紝杩斿洖璇ョ兢 ID锛屼究浜庡崟缇ゆā寮忓厹搴曘€?""
    if len(GROUP_IDS) != 1:
        return None
    return next(iter(GROUP_IDS))


def _find_recent_user_ids_by_text(group_id: int, text: str, *, limit: int = 3) -> list[int]:
    """
    鍦ㄦ渶杩戠紦瀛橀噷鎸夋枃妗堝弽鏌ョ敤鎴枫€?    鍗曠兢杞彂瀛︿範鏃讹紝Telegram 缁忓父涓嶇粰鍘熷 user/chat 淇℃伅锛岃繖閲屽仛鏈湴鍏滃簳銆?    """
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
    褰撴嬁涓嶅埌 user_id 鏃讹紝閫€鍖栦负鎸夊悓鏂囨鍒犻櫎鏈€杩戞秷鎭紝骞舵竻鎺夊搴旀満鍣ㄤ汉璀﹀憡銆?    杩斿洖鍒犻櫎鐨勫師娑堟伅鏉℃暟銆?    """
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
    """妫€鏌ユ槸鍚﹀簲璇ヤ负璇ョ敤鎴峰彂閫佹柊璀﹀憡锛堥槻姝㈠埛灞忥級"""
    key = (group_id, user_id)
    now = time.time()
    last = user_last_warning.get(key)
    if last:
        last_time, last_msg_id = last
        if now - last_time < USER_WARNING_COOLDOWN_SEC:
            return False
    return True


def _record_warning_sent(group_id: int, user_id: int, msg_id: int):
    """璁板綍宸插彂閫佺殑璀﹀憡"""
    user_last_warning[(group_id, user_id)] = (time.time(), msg_id)


def _add_banned_warning(group_id: int, warning_msg_id: int):
    """娣诲姞宸插皝绂佽鍛婃秷鎭埌鍒楄〃"""
    if group_id not in banned_warning_messages:
        banned_warning_messages[group_id] = []
    if warning_msg_id not in banned_warning_messages[group_id]:
        banned_warning_messages[group_id].append(warning_msg_id)


async def _delete_all_banned_warnings(group_id: int):
    """鍒犻櫎璇ョ兢鎵€鏈夊凡灏佺鐨勮鍛婃秷鎭?""
    if group_id not in banned_warning_messages:
        return
    for msg_id in banned_warning_messages[group_id]:
        try:
            await bot.delete_message(group_id, msg_id)
        except Exception:
            pass
    banned_warning_messages[group_id] = []

def _message_has_link_or_external_at(text: str) -> bool:
    """鏂囨湰寮曟祦锛氬寘鍚摼鎺?鎴?@澶栭儴鐢ㄦ埛锛堜换鎰?@xxx 鍧囪涓哄閮級銆?""
    if not text:
        return False
    has_link = any(x in text for x in ["http://", "https://", "t.me/"])
    mentions = re.findall(r"@(\w+)", text)
    has_external_at = bool(mentions)
    return has_link or has_external_at


def _has_external_reference(message: Message) -> bool:
    """澶栭儴寮曠敤锛欰. 娑堟伅涓鸿浆鍙?鎴?B. 鍥炲浜嗚浆鍙戞秷鎭?""
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
    """鍙戣█鏃舵娴嬪苟鍙戦€佽鍛娿€傞『搴忥細璞佸厤 -> 鍙敜(鏃犳搷浣? -> 鏉冮檺 -> 涓炬姤闃堝€?-> 澶氬眰 -> 5.1 -> 5.3 -> 閲嶅锛涘悎瑙勪粎褰?triggers<=1 涓旀棤澶勭綒銆?""
    if not message.from_user or message.from_user.is_bot:
        _push_listen_log(
            group_id=getattr(message.chat, "id", None),
            user_id=getattr(getattr(message, "from_user", None), "id", None),
            msg_id=getattr(message, "message_id", None),
            text=(message.text or ""),
            verdict="SKIP",
            details="from_user 涓虹┖鎴栨秷鎭潵鑷満鍣ㄤ汉",
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
            details="缇ょ粍鎬诲紑鍏?enabled=false锛屾湭鎵ц浠讳綍妫€娴?,
        )
        return
    user_id = message.from_user.id
    group_id = message.chat.id
    text = message.text or ""
    _track_user_message(group_id, user_id, message.message_id, text)

    # 璇箟骞垮憡妫€娴嬶紙浼樺厛绾ф渶楂橈紱鍛戒腑鍚庣洿鎺ュ垹闄や笉鍋氭彁閱掞級
    if cfg.get("semantic_ad_enabled", False) and len((message.text or "").strip()) >= 4:
        if await _check_and_delete_semantic_ad_message(message, text, group_id=group_id, user_id=user_id):
            return
    else:
        # 璁板綍涓轰粈涔堟病鏈夎繘鍏?AD 妫€娴嬶紙鏂逛究鎺掓煡鈥滀紭鍏堢骇鏈€楂樹絾涓嶆墽琛屸€濓級
        reason = []
        if not cfg.get("semantic_ad_enabled", False):
            reason.append("semantic_ad_enabled=false")
        if len((message.text or "").strip()) < 4:
            reason.append("鏂囨湰闀垮害<4")
        if reason:
            _push_listen_log(
                group_id=group_id,
                user_id=user_id,
                msg_id=message.message_id,
                text=text,
                verdict="PASS",
                details="鏈繘鍏D妫€娴? " + "锛?.join(reason),
            )

    # 璇垽璞佸厤锛氫粎涓嶅仛澶氬眰鍐呭妫€娴嬶紝涓炬姤闃堝€?澶栭儴寮曠敤/5.1/閲嶅绛夋娴嬩粛鎵ц
    misjudge_wl = cfg.get("misjudge_whitelist") or []
    misjudge_exempt = isinstance(misjudge_wl, list) and str(user_id) in misjudge_wl

    mild_wl = cfg.get("mild_exempt_whitelist") or []
    mild_exempt = isinstance(mild_wl, list) and str(user_id) in mild_wl

    # 銆屽彫鍞ゃ€嶏細鏈満鍣ㄤ汉涓嶅仛浠讳綍鍔ㄤ綔锛岀敱缇ゅ唴鍏朵粬鏈哄櫒浜哄鐞?
    if message.text and message.text.strip() == "鍙敜":
        return

    # 銆屾潈闄愩€嶆煡璇㈠彂濯掍綋杩涘害锛堟媺鍙栨渶鏂板姪鍔涙暟锛?
    if message.text and message.text.strip() == "鏉冮檺":
        await _refresh_user_boosts(group_id, user_id)
        key = _media_key(group_id, user_id)
        count = media_stats["message_counts"].get(key, 0)
        unlocked = media_stats["unlocked"].get(key, False)
        boosts = media_stats["boosts"].get(key, 0)
        need_msg = cfg.get("media_unlock_msg_count", 50)
        need_boosts = cfg.get("media_unlock_boosts", 4)
        if unlocked:
            await message.reply(f"鉁?宸茶В閿佸彂濯掍綋锛堝彂閫佸悎瑙勬秷鎭凡婊?{need_msg} 鏉★級銆?)
            return
        if boosts >= need_boosts:
            await message.reply(f"鉁?宸茶В閿佸彂濯掍綋锛堝凡鍔╁姏 {boosts} 娆★級銆?)
            return
        await message.reply(
            f"馃搳 鍙戝獟浣撹繘搴n"
            f"路 鍙戦€佸悎瑙勬秷鎭細{count}/{need_msg}\n"
            f"路 缇ょ粍鍔╁姏锛歿boosts}/{need_boosts}\n"
            f"锛堝埛灞?閲嶅/鐭秷鎭笉璁″叆锛?
        )
        return

    report_history_whitelist = cfg.get("report_history_whitelist") or []
    report_history_exempt = isinstance(report_history_whitelist, list) and str(user_id) in report_history_whitelist
    reported_count = count_user_reported_messages(user_id, group_id)
    report_threshold = max(1, int(cfg.get("report_history_threshold", 3)))
    if (
        not report_history_exempt
        and reported_count >= report_threshold
        and reported_count > get_report_history_action_count(group_id, user_id)
    ):
        try:
            mute_hours = max(1, int(cfg.get("report_history_mute_hours", 24)))
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
                    can_pin_messages=False,
                ),
                until_date=until_date,
            )
            await _delete_original_and_linked_reply(group_id, message.message_id)
            report_action_state[_report_action_key(group_id, user_id)] = {
                "last_trigger_count": reported_count,
                "last_trigger_at": int(time.time()),
            }
            asyncio.create_task(save_report_action_state())
            display_name = _get_display_name_from_message(message, user_id)
            sent = await bot.send_message(
                group_id,
                (
                    f"🚫 用户 {display_name}\n"
                    f"📍 触发原因：历史累计已有 {reported_count} 条消息被群成员举报。\n"
                    f"🔒 处理结果：已限制发言 {mute_hours} 小时。\n"
                    "📘 说明：同一累计举报数只处罚一次，解禁后如果没有新增举报，不会再次处罚。"
                ),
                reply_markup=build_report_history_exempt_keyboard(group_id, user_id),
            )
            _track_bot_message(group_id, sent.message_id)
            return
        except Exception as e:
            print(f"历史举报处罚失败: {e}")

    triggers = []
    if not misjudge_exempt and cfg.get("check_display_keywords", True):
        display_name = (message.from_user.full_name or "").lower()
        if any(kw.lower() in display_name for kw in cfg.get("display_keywords", [])):
            triggers.append("昵称敏感词")

    if len(triggers) > 0:
        reason = "+".join(triggers)
        display_name = _get_display_name_from_message(message, user_id)
        should_warn = _should_send_warning(group_id, user_id)

        if should_warn:
            warning_text = (
                f"⚠️ 检测到用户 {display_name} 疑似广告，包含 {reason} 内容。\n"
                "请留意该用户，可点击举报或由管理员标记。"
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
                _track_bot_message(group_id, warning.message_id)
            except Exception as e:
                print(f"发送警告失败: {e}")
        else:
            rk = _report_key(group_id, message.message_id)
            async with lock:
                reports[rk] = {
                    "warning_id": 0,
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

    # 5.3 鎸夎Е鍙戝眰鏁板鐞?
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
                f"馃毇 鐢ㄦ埛 {display_name}\n馃搶 瑙﹀彂鍘熷洜锛歿reason}\n馃敀 澶勭悊缁撴灉锛氬凡琚湰缇ゆ案涔呴檺鍒跺彂瑷€銆俓n{MISJUDGE_BOT_MENTION}",
                auto_delete_sec=10)
        except Exception as e:
            print(f"鑷姩灏佺澶辫触: {e}")
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
                        f"鈿狅笍 鐢ㄦ埛 {user_id} 宸茬涓夋瑙﹀彂杞诲害璀﹀憡銆俓n瀹氫綅: {link}",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[[
                                InlineKeyboardButton(text="瀹氫綅鍒版秷鎭?, url=link),
                                InlineKeyboardButton(text="璞佸厤杞诲害", callback_data=f"mild_exempt:{group_id}:{user_id}")
                            ]]
                        ),
                    )
                except Exception:
                    pass
            mild_trigger_entries[mild_key] = [entries[2]]

    # 閲嶅鍙戣█妫€娴嬶紙澶氬眰涔嬪悗鎵ц锛?
    if await handle_repeat_message(message):
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="RULE_ACTION",
            details="閲嶅鍙戣█妫€娴嬪凡瑙﹀彂骞舵墽琛屽缃?鎻愰啋锛堣瑙侀噸澶嶅彂瑷€妯″潡锛?,
        )
        return

    # 璧板埌杩欓噷璇存槑娌℃湁瑙﹀彂浠讳綍澶勭綒鍨嬭鍒?
    if triggers:
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="PASS",
            details="澶氬眰鐩戝惉瑙﹀彂椤? " + "銆?.join(triggers),
        )
    else:
        _push_listen_log(
            group_id=group_id,
            user_id=user_id,
            msg_id=message.message_id,
            text=text,
            verdict="PASS",
            details="鏈懡涓瑼D璇箟搴擄紱澶氬眰鐩戝惉鏃犺Е鍙戦」",
        )


@router.message(Command(commands=["ad", "AD", "Ad"]), F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def cmd_mark_ad(message: Message):
    """绠＄悊鍛樺懡浠わ細/ad锛屽洖澶嶄竴鏉″箍鍛婃秷鎭紝瀛︿範骞跺垹闄よ鐢ㄦ埛鏈€杩戞秷鎭€?""
    try:
        target = message.reply_to_message
        if not target or not target.from_user or target.from_user.is_bot:
            await message.reply("璇峰洖澶嶇湡瀹炵敤鎴风殑骞垮憡娑堟伅浣跨敤 /ad銆?)
            return
        group_id = message.chat.id
        user_id = target.from_user.id
        text = target.text or target.caption or ""
        if text:
            try:
                semantic_ad_detector.add_ad_sample(text)
                await _enable_semantic_detection_for_group(group_id)
            except Exception as e:
                print(f"/ad 瀛︿範骞垮憡鏍锋湰澶辫触: {e}")
        try:
            await _delete_user_recent_and_warnings(group_id, user_id, target.message_id)
        except Exception as e:
            print(f"/ad 鍒犻櫎鐢ㄦ埛娑堟伅澶辫触: {e}")
        await message.reply("鉁?宸插涔犲苟鍒犻櫎璇ョ敤鎴疯繎鏈熷彂瑷€銆?)
    except Exception as e:
        print("/ad 鍛戒护寮傚父:", e)
        await message.reply("鉂?澶辫触", reply_markup=ReplyKeyboardRemove())


@router.message(F.from_user.id.in_(ADMIN_IDS))
async def on_forward_learn_ad(message: Message):
    """
    绠＄悊鍛樿浆鍙戠敤鎴锋秷鎭粰鏈哄櫒浜猴細
    1) 瀛︿範璇ユ潯骞垮憡鏂囨湰
    2) 鏍规嵁鍘熷缇D鍜岀敤鎴稩D锛屽垹闄ゅ叾鏈€杩?4灏忔椂鍐呯殑鍏ㄩ儴娑堟伅鍜岃鍛婏紝骞跺涔犺繖浜涙枃鏈?
    """
    try:
        f_user = getattr(message, "forward_from", None)
        f_chat = getattr(message, "forward_from_chat", None)
        f_origin = getattr(message, "forward_origin", None)

        # 鍏煎鏂扮増 forward_origin锛坲ser/chat/channel锛?
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
                print(f"杞彂瀛︿範骞垮憡鏍锋湰澶辫触: {e}")
        group_id = f_chat.id if f_chat else _get_only_group_id()
        if not group_id:
            if learned:
                await message.reply("鉁?宸插涔犺杞彂娑堟伅鍐呭锛屼絾褰撳墠涓嶆槸鍗曠兢妯″紡锛屼笖杞彂閲屾病鏈夊師缇や俊鎭紝鏃犳硶绮惧噯鍥炵兢鍒犻櫎銆?)
            return
        if group_id not in GROUP_IDS:
            only_gid = _get_only_group_id()
            if only_gid is None:
                if learned:
                    await message.reply("鉁?宸插涔犺杞彂娑堟伅鍐呭锛屼絾杞彂鏉ユ簮缇や笉鍦ㄥ彈鎺х兢鍒楄〃锛屾湭鎵ц鍥炵兢鍒犻櫎銆?)
                return
            group_id = only_gid

        if learned:
            await _enable_semantic_detection_for_group(group_id)

        # 1) 浼樺厛浣跨敤 Telegram 鐩存帴缁欏嚭鐨?uid
        user_id = None
        if f_user:
            user_id = f_user.id
        else:
            user_id = _get_remembered_user_id_by_text(group_id, text)
        if not user_id:
            matched_user_ids = _find_recent_user_ids_by_text(group_id, text, limit=3)
            if len(matched_user_ids) == 1:
                user_id = matched_user_ids[0]
            elif len(matched_user_ids) > 1:
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
                print(f"杞彂瀛︿範鏃跺垹闄ょ敤鎴锋秷鎭け璐? {e}")

        deleted_by_text = 0
        try:
            deleted_by_text = await _delete_recent_messages_by_text(group_id, text)
        except Exception as e:
            print(f"鎸夋枃妗堝洖缇ゅ垹闄ゅけ璐? {e}")

        if memory_changed:
            asyncio.create_task(save_forward_match_memory())

        if learned:
            if deleted_by_user or deleted_by_text:
                scope = f"缇?{group_id}"
                if user_id:
                    await message.reply(f"鉁?宸插涔犲箍鍛婂唴瀹癸紝骞跺凡鍦?{scope} 娓呯悊璇ョ敤鎴疯繎鏈熷彂瑷€锛涘悓鏂囨鍏滃簳鍒犻櫎 {deleted_by_text} 鏉°€?)
                else:
                    await message.reply(f"鉁?宸插涔犲箍鍛婂唴瀹癸紱Telegram 鏈繑鍥炲師鐢ㄦ埛淇℃伅锛屽凡鍦?{scope} 鎸夊悓鏂囨鍏滃簳鍒犻櫎 {deleted_by_text} 鏉°€?)
            else:
                await message.reply(f"鉁?宸插涔犲箍鍛婂唴瀹癸紝浣嗗湪缇?{group_id} 鐨勬渶杩戞秷鎭紦瀛橀噷娌℃壘鍒板彲鍒犻櫎鐨勫悓鏂囨璁板綍銆?)
    except Exception as e:
        print("杞彂瀛︿範鍛戒护寮傚父:", e)


@router.message(F.chat.id.in_(GROUP_IDS), F.left_chat_member)
async def on_member_left(message: Message):
    """鎴愬憳閫€缇わ細鍒犻櫎鍏跺湪鏈兢鐨勬渶杩戞秷鎭拰鍏ㄩ儴璀﹀憡"""
    try:
        if not message.left_chat_member or message.left_chat_member.is_bot:
            return
        group_id = message.chat.id
        user_id = message.left_chat_member.id
        # 鍒╃敤宸叉湁宸ュ叿鍑芥暟锛氬垹闄ゆ渶杩?4灏忔椂鍐呮秷鎭?+ 鎵€鏈夎鍛婅褰?
        await _delete_user_recent_and_warnings(group_id, user_id, orig_msg_id=None)
    except Exception as e:
        print(f"澶勭悊閫€缇ょ敤鎴锋秷鎭竻鐞嗗け璐? {e}")

    # 鍚堣娑堟伅锛氫粎褰?triggers<=1 涓旀湰鏉℃湭鍙椾换浣曞缃氭椂璁″叆
    if len(triggers) <= 1:
        await _try_count_media_and_notify(message, group_id, user_id, cfg)

# 鍏朵粬鍐呭绫诲瀷锛堣创绾?鏂囦欢/鍔ㄧ敾绛夛級锛氫粎鍋氬閮ㄥ紩鐢ㄦ娴嬶紝涓庢枃鏈?濯掍綋涓€鑷村鐞?
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


# 澶栭儴寮曠敤妫€娴嬪凡绉婚櫎锛屼氦鐢卞叾浠栨満鍣ㄤ汉澶勭悊


@router.callback_query(F.data.startswith("admin_ban:"))
async def handle_admin_ban(callback: CallbackQuery):
    """绠＄悊鍛樹竴閿皝绂?""
    try:
        parts = callback.data.split(":")
        group_id = int(parts[1])
        user_id = int(parts[2])
        
        if callback.from_user.id not in ADMIN_IDS:
            await callback.answer("浠呯鐞嗗憳鎿嶄綔", show_alert=True)
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
        await callback.answer("鉁?宸插鐞?)
    except Exception as e:
        print(f"绠＄悊鍛樺皝绂佸け璐? {e}")
        await callback.answer("鉂?澶辫触", show_alert=True)

@router.callback_query(F.data.startswith("report:"))
async def handle_report(callback: CallbackQuery):
    """涓炬姤澶勭悊锛涗粎闈炵鐞嗗憳涓炬姤璁″叆鍘嗗彶闃堝€硷紱浼樺寲鍝嶅簲閫熷害"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("宸茶繃鏈?)
            return
        group_id = int(parts[1])
        msg_id = int(parts[2])
        reporter_id = callback.from_user.id
        rk = _report_key(group_id, msg_id)
        async with lock:
            if rk not in reports:
                await callback.answer("宸茶繃鏈?)
                return
            data = reports[rk]
            if reporter_id in data["reporters"]:
                await callback.answer("宸蹭妇鎶ヨ繃")
                return
            data["reporters"].add(reporter_id)
            count = len(data["reporters"])
            user_id = data["suspect_id"]
            warning_id = data["warning_id"]
            reason = data["reason"]
        
        # 灏芥棭杩斿洖鍝嶅簲锛屽悗缁搷浣滀笉闃诲鐢ㄦ埛
        await callback.answer(f"鉁?涓炬姤({count}浜?")
        
        # 鍚庡彴淇濆瓨
        key = f"{group_id}_{user_id}"
        if key not in user_violations:
            user_violations[key] = {}
        if str(msg_id) not in user_violations[key]:
            user_violations[key][str(msg_id)] = {"time": time.time(), "reporters": set()}
        user_violations[key][str(msg_id)]["reporters"].add(reporter_id)
        asyncio.create_task(save_user_violations())
        
        # 淇敼璀﹀憡娑堟伅 - 鍏抽敭锛氭樉绀轰妇鎶ユ暟 + 鏍规嵁涓炬姤鏁板喅瀹氭寜閽?
        display_name = data.get("suspect_name") or f"ID {user_id}"
        updated_text = (
            "馃毃 宸叉敹鍒扮兢鎴愬憳鐨勪妇鎶n\n"
            f"馃懁 鐢ㄦ埛锛歿display_name}锛圛D: {user_id}锛夝煋?瑙﹀彂鍘熷洜锛歿reason}\n"
            f"馃摚 褰撳墠涓炬姤浜烘暟锛歿count} 浜篭n\n"
            "鈿狅笍 鐤戜技骞垮憡锛岃鍕跨淇¤鐢ㄦ埛锛屽彲缁х画鐐逛妇鎶ャ€?
        )
        kb = build_warning_buttons(group_id, msg_id, count)
        try:
            if warning_id:  # 鍙湁鏈夎鍛婃秷鎭椂鎵嶆洿鏂?
                await bot.edit_message_text(
                    chat_id=group_id,
                    message_id=warning_id,
                    text=updated_text,
                    reply_markup=kb
                )
        except Exception:
            pass
        trigger_count = data.get("trigger_count", 0)
        # 瑙﹀彂2灞傛娴?2浜轰妇鎶?姘稿皝
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
                # 姘稿皝鏃跺垹闄ょ敤鎴峰叏閮ㄦ秷鎭拰鍏ㄩ儴璀﹀憡
                await _delete_user_recent_and_warnings(group_id, user_id, msg_id)
                await _delete_all_banned_warnings(group_id)
                # 鍙戦€佸皝绂侀€氱煡锛?0绉掑悗鑷姩鍒犻櫎锛?
                final_text = (
                    f"馃毇 鐢ㄦ埛 {display_name}\n"
                    f"馃搶 瑙﹀彂鍘熷洜锛歿reason}锛堝凡琚?{count} 浣嶆垚鍛樹妇鎶ワ級\n"
                    f"馃敀 澶勭悊缁撴灉锛氭案涔呯姝㈠湪鏈兢鍙戣█銆俓n{MISJUDGE_BOT_MENTION}"
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
                print("2灞?涓炬姤姘稿皝澶辫触:", e)
        asyncio.create_task(save_data())
    except Exception as e:
        print("涓炬姤寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)

@router.callback_query(F.data.startswith(("ban24h:", "banperm:")))
async def handle_ban(callback: CallbackQuery):
    """灏佺澶勭悊"""
    try:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("宸茶繃鏈?)
            return
        action, group_id_str, msg_id_str = parts[0], parts[1], parts[2]
        group_id = int(group_id_str)
        msg_id = int(msg_id_str)
        caller_id = callback.from_user.id
        rk = _report_key(group_id, msg_id)
        async with lock:
            if rk not in reports:
                await callback.answer("宸茶繃鏈?)
                return
            data = reports[rk]
            user_id = data["suspect_id"]
            warning_id = data["warning_id"]
            reason = data["reason"]
        if caller_id not in ADMIN_IDS:
            await callback.answer("浠呯鐞嗗憳鎿嶄綔", show_alert=True)
            return
        
        # 鎵ц灏佺
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
        
        ban_type = "绂佽█ 24 灏忔椂" if action == "ban24h" else "姘镐箙绂佹鍦ㄦ湰缇ゅ彂瑷€"
        report_count = len(data.get("reporters", set()))
        display_name = data.get("suspect_name") or f"ID {user_id}"
        
        # 姘稿皝鏃跺垹闄ょ敤鎴峰叏閮ㄦ秷鎭拰鍏ㄩ儴璀﹀憡
        if action == "banperm":
            await _delete_user_recent_and_warnings(group_id, user_id, msg_id)
            # 鍒犻櫎鎵€鏈夊凡灏佺鐨勮鍛婃秷鎭?
            await _delete_all_banned_warnings(group_id)
            # 鍒犻櫎褰撳墠璀﹀憡娑堟伅
            if warning_id:
                try:
                    await bot.delete_message(group_id, warning_id)
                except Exception:
                    pass
            # 鍙戦€佸皝绂侀€氱煡锛?0绉掑悗鑷姩鍒犻櫎锛?
            final_text = (
                f"馃毇 鐢ㄦ埛 {display_name}\n"
                f"馃搶 瑙﹀彂鍘熷洜锛歿reason}锛堝凡琚?{report_count} 浣嶆垚鍛樹妇鎶ワ級\n"
                f"馃敀 澶勭悊缁撴灉锛歿ban_type}銆俓n{MISJUDGE_BOT_MENTION}"
            )
            try:
                sent = await bot.send_message(group_id, final_text)
                _track_bot_message(group_id, sent.message_id, 10)  # 10绉掑悗鍒犻櫎
            except Exception:
                pass
        else:
            # 24灏忔椂绂佽█锛氬垹闄ゆ簮娑堟伅锛屾洿鏂拌鍛?            await _delete_original_and_linked_reply(group_id, msg_id)
            final_text = (
                f"馃毇 鐢ㄦ埛 {display_name}\n"
                f"馃搶 瑙﹀彂鍘熷洜锛歿reason}锛堝凡琚?{report_count} 浣嶆垚鍛樹妇鎶ワ級\n"
                f"馃敀 澶勭悊缁撴灉锛歿ban_type}銆俓n{MISJUDGE_BOT_MENTION}"
            )
            if warning_id:
                try:
                    await bot.edit_message_text(
                        chat_id=group_id,
                        message_id=warning_id,
                        text=final_text,
                        reply_markup=None
                    )
                    # 娣诲姞鍒板凡灏佺璀﹀憡鍒楄〃
                    _add_banned_warning(group_id, warning_id)
                except Exception:
                    pass

        # 鍒犻櫎鎵€鏈夊凡灏佺鐨勮鍛婃秷鎭紙鏇挎崲鍘熸潵鐨勫彧鍒犱笂涓€鏉★級
        await _delete_all_banned_warnings(group_id)

        await callback.answer(f"鉁?{ban_type}")
        async with lock:
            reports.pop(rk, None)
        await save_data()
    
    except TelegramBadRequest:
        await callback.answer("鉂?澶辫触", show_alert=True)
    except Exception as e:
        print("灏佺寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)

@router.callback_query(F.data.startswith("exempt:"))
async def handle_exempt(callback: CallbackQuery):
    """璇垽璞佸厤锛氬垹闄よ鍛娿€佺Щ闄ゆ姤鍛婏紝骞跺皢璇ョ敤鎴峰姞鍏ュ灞傛娴嬬櫧鍚嶅崟"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("宸茶繃鏈?)
            return
        group_id = int(parts[1])
        msg_id = int(parts[2])
        caller_id = callback.from_user.id
        rk = _report_key(group_id, msg_id)
        async with lock:
            if rk not in reports:
                await callback.answer("宸茶繃鏈?)
                return
            data = reports[rk]
            warning_id = data["warning_id"]
            suspect_id = data["suspect_id"]
        if caller_id not in ADMIN_IDS:
            await callback.answer("浠呯鐞嗗憳鎿嶄綔", show_alert=True)
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
        await callback.answer("鉁?宸茶眮鍏?)
        async with lock:
            reports.pop(rk, None)
        await save_data()
    except Exception as e:
        print("璞佸厤寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)



@router.callback_query(F.data.startswith("report_history_exempt:"), F.from_user.id.in_(ADMIN_IDS))
async def handle_report_history_exempt(callback: CallbackQuery):
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("鍙傛暟閿欒", show_alert=True)
            return
        group_id = int(parts[1])
        user_id = int(parts[2])
        cfg = get_group_config(group_id)
        wl = cfg.get("report_history_whitelist") or []
        if not isinstance(wl, list):
            wl = []
        sid = str(user_id)
        if sid not in wl:
            wl.append(sid)
            apply_global_config_value("report_history_whitelist", wl)
            await save_config()
        report_action_state[_report_action_key(group_id, user_id)] = {"last_trigger_count": 10 ** 9, "last_trigger_at": int(time.time())}
        asyncio.create_task(save_report_action_state())
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
                    can_pin_messages=False
                )
            )
        except Exception:
            pass
        try:
            await callback.message.edit_text(callback.message.text + "\\n\\n宸插姞鍏ュ巻鍙蹭妇鎶ョ櫧鍚嶅崟銆?, reply_markup=None)
        except Exception:
            pass
        await callback.answer("宸茶眮鍏?, show_alert=True)
    except Exception as e:
        print("鍘嗗彶涓炬姤璞佸厤寮傚父:", e)
        await callback.answer("澶辫触", show_alert=True)

@router.callback_query(F.data.startswith("markad:"), F.from_user.id.in_(ADMIN_IDS))
async def handle_mark_ad(callback: CallbackQuery):
    """鏍囪骞垮憡骞跺垹闄わ細瀛︿範骞垮憡鏍锋湰 + 鍒犻櫎璇ョ敤鎴锋渶杩戞秷鎭拰鍏ㄩ儴璀﹀憡"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("宸茶繃鏈?, show_alert=True)
            return
        group_id = int(parts[1])
        msg_id = int(parts[2])
        rk = _report_key(group_id, msg_id)
        async with lock:
            data = reports.get(rk)
        if not data:
            await callback.answer("璁板綍宸茶繃鏈?, show_alert=True)
            return
        suspect_id = data.get("suspect_id")
        orig_msg_id = data.get("original_message_id")
        orig_text = data.get("original_text") or ""

        # 瀛︿範骞垮憡鏍锋湰锛堜粎浣跨敤褰撳墠瑙﹀彂鐨勫師濮嬫枃鏈級
        if orig_text:
            try:
                semantic_ad_detector.add_ad_sample(orig_text)
                await _enable_semantic_detection_for_group(group_id)
            except Exception as e:
                print(f"瀛︿範骞垮憡鏍锋湰澶辫触: {e}")

        # 鍒犻櫎璇ョ敤鎴锋渶杩戞秷鎭拰鍏ㄩ儴璀﹀憡
        try:
            await _delete_user_recent_and_warnings(group_id, suspect_id, orig_msg_id)
        except Exception as e:
            print(f"鏍囪骞垮憡鏃跺垹闄ゆ秷鎭け璐? {e}")

        async with lock:
            reports.pop(rk, None)
        await save_data()
        # 涓嶅脊绐楋紝浠呴潤榛樼‘璁?
        await callback.answer()
    except Exception as e:
        print("鏍囪骞垮憡寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)


@router.callback_query(F.data.startswith("mild_exempt:"))
async def handle_mild_exempt(callback: CallbackQuery):
    """杞诲害瑙﹀彂璞佸厤锛氫粎鍏抽棴璇ョ敤鎴风殑杞诲害妫€娴嬶紝涓嶅奖鍝嶅叾浠栨娴?""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("宸茶繃鏈?)
            return
        group_id = int(parts[1])
        user_id = int(parts[2])
        caller_id = callback.from_user.id
        if caller_id not in ADMIN_IDS:
            await callback.answer("浠呯鐞嗗憳鎿嶄綔", show_alert=True)
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

        await callback.answer("鉁?宸茶眮鍏嶈鐢ㄦ埛鐨勮交搴︽娴?)
    except Exception as e:
        print("杞诲害璞佸厤寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)

@router.callback_query(F.data.startswith("mr:"))
async def handle_media_report(callback: CallbackQuery):
    """涓炬姤鍎跨鑹叉儏锛氶檺娴侊紙杩炵画涓ゆ潯 cooldown銆佷竴澶╀笂闄愶級浠庣兢閰嶇疆璇?""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("鏃犳晥", show_alert=True)
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
            await callback.answer("浠婃棩涓炬姤娆℃暟宸茶揪涓婇檺锛屽鏈夐棶棰樿鐩存帴鑱旂郴绠＄悊鍛樸€?, show_alert=True)
            return

        if len(media_report_last) >= MEDIA_REPORT_LAST_MAX:
            items = sorted(media_report_last.items(), key=lambda x: x[1][1])[:1000]
            for u, _ in items:
                media_report_last.pop(u, None)
        last = media_report_last.get(uid)
        if last:
            last_mid, last_ts = last
            if last_mid != media_msg_id and (now - last_ts) < cooldown_sec:
                await callback.answer(f"璇峰嬁鍦?{cooldown_sec // 60} 鍒嗛挓鍐呭澶氭潯濯掍綋杩炵画涓炬姤锛岃绋嶅悗鍐嶈瘯銆?, show_alert=True)
                return
        media_report_last[uid] = (media_msg_id, now)
        media_report_day_count[day_key] = day_count + 1

        async with media_reports_lock:
            key = (chat_id, media_msg_id)
            if key not in media_reports:
                await callback.answer("宸茶繃鏈?)
                return
            data = media_reports[key]
            if data["deleted"]:
                await callback.answer("璇ュ獟浣撳凡琚垹闄?)
                return
            if uid in data["reporters"]:
                await callback.answer("宸蹭妇鎶ヨ繃")
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
                    text="鈿狅笍 澶氫汉涓炬姤锛屽凡鍒犻櫎璇ュ獟浣撱€?,
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
                        f"鈿狅笍 缇ゅ唴濯掍綋琚妇鎶ワ紙鍎跨鑹叉儏鐩稿叧锛塡n缇? {chat_id}\n娑堟伅: {link}\n褰撳墠涓炬姤浜烘暟: 2 浜?,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="瀹氫綅鍒版秷鎭?, url=link)]
                        ])
                    )
                except Exception:
                    pass
    except Exception as e:
        print("濯掍綋涓炬姤寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)

@router.callback_query(F.data.startswith("ml:"))
async def handle_media_like(callback: CallbackQuery):
    """鐐硅禐"""
    try:
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("鏃犳晥", show_alert=True)
            return
        chat_id = int(parts[1])
        media_msg_id = int(parts[2])
        uid = callback.from_user.id
        async with media_reports_lock:
            key = (chat_id, media_msg_id)
            if key not in media_reports:
                await callback.answer("宸茶繃鏈?)
                return
            data = media_reports[key]
            if uid in data["likes"]:
                await callback.answer("宸茬偣璧炶繃")
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
        print("濯掍綋鐐硅禐寮傚父:", e)
        await callback.answer("鉂?澶辫触", show_alert=True)

async def cleanup_deleted_messages():
    """姣?10 鍒嗛挓妫€鏌ワ細涓炬姤璁板綍瓒呰繃 24 灏忔椂鏈鐞嗗垯闅愯棌鎸夐挳骞朵粠鍐呭瓨绉婚櫎銆?""
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
    """姣忓皬鏃舵竻鐞嗕竴娆℃満鍣ㄤ汉鍦ㄧ兢閲岀殑寮曠敤鍥炲锛岄伩鍏嶆紡鏉€"""
    while True:
        await asyncio.sleep(3600)
        # 鎷疯礉涓€浠藉綋鍓嶅垪琛紝閬垮厤閬嶅巻鏃朵慨鏀?
        items = list(bot_reply_links.items())
        if not items:
            continue
        for (group_id, bot_msg_id), (orig_msg_id, created_ts) in items:
            try:
                await bot.delete_message(group_id, bot_msg_id)
            except TelegramBadRequest:
                # 宸茬粡琚垹灏卞拷鐣?
                pass
            except Exception:
                # 鍏朵粬閿欒涔熶笉褰卞搷缁х画
                pass
            finally:
                bot_reply_links.pop((group_id, bot_msg_id), None)

async def main():
    print("馃殌 鏈哄櫒浜哄惎鍔?)
    if admin_router is not None:
        try:
            from bot_config import validate_immutable_config
            validate_immutable_config()
        except Exception as e:
            print(f"鈿狅笍 鍏ㄥ眬閰嶇疆鏈姞杞斤紙鍏ㄥ眬绯荤粺閰嶇疆闈㈡澘涓嶅彲鐢級: {e}")
    await load_config()
    for gid in GROUP_IDS:
        get_group_config(gid)
    await save_config()
    await load_data()
    await load_user_violations()
    await load_report_action_state()
    await load_forward_match_memory()
    load_repeat_levels()
    load_link_ref_levels()
    await load_media_stats()
    asyncio.create_task(cleanup_deleted_messages())
    asyncio.create_task(cleanup_orphan_replies())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
