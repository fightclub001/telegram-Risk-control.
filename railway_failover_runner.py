import json
import os
import signal
import subprocess
import sys
import threading
import time
import io
import hashlib
import zipfile
import queue
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAIN_CMD = [sys.executable, "main.py"]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# Safe default avoids accidental always-on standby when Railway env drifts.
STATUS_URL = os.getenv(
    "HEARTBEAT_STATUS_URL",
    "https://telegram-risk-health.telegarmpromax.com/status",
).strip()
STATUS_TOKEN = os.getenv("HEARTBEAT_STATUS_TOKEN", "").strip()
PRIMARY_NODE_ID = os.getenv("PRIMARY_NODE_ID", "ubuntu-main").strip()
MAX_AGE_SEC = max(1, _env_int("HEARTBEAT_MAX_AGE_SEC", 90))
POLL_SEC = max(1, _env_int("HEARTBEAT_POLL_SEC", 15))
RECOVER_CONFIRM_LOOPS = max(1, _env_int("HEARTBEAT_RECOVER_CONFIRM_LOOPS", 1))
FAIL_CONFIRM_LOOPS = max(1, _env_int("HEARTBEAT_FAIL_CONFIRM_LOOPS", 3))
REQUEST_TIMEOUT_SEC = max(1, _env_int("HEARTBEAT_TIMEOUT_SEC", 5))
ALERT_CHAT_ID = int((os.getenv("FAILOVER_ALERT_CHAT_ID") or "827803411").strip() or "0")
ALERT_AFTER_SEC = max(60, _env_int("FAILOVER_ALERT_AFTER_SEC", 600))
ALERT_RETRY_SEC = max(60, _env_int("FAILOVER_ALERT_RETRY_SEC", 300))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_BOT_API_BASE", "https://api.telegram.org").strip().rstrip("/")
STANDBY_CONFLICT_COOLDOWN_SEC = max(
    POLL_SEC * 2,
    _env_int("STANDBY_CONFLICT_COOLDOWN_SEC", 300),
)
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
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
IMAGE_FUZZY_BLOCK_FILE = os.path.join(DATA_DIR, "image_fuzzy_blocks.json")
SEMANTIC_AD_DB_FILE = os.path.join(DATA_DIR, "semantic_ads", "semantic_ads.db")
ADMIN_STATE_FILE_PATHS = {
    "config.json": CONFIG_FILE,
    "image_fuzzy_blocks.json": IMAGE_FUZZY_BLOCK_FILE,
    "semantic_ads/semantic_ads.db": SEMANTIC_AD_DB_FILE,
}
CONFIG_SYNC_TOKEN = (os.getenv("CONFIG_SYNC_TOKEN") or "").strip()
CONFIG_SYNC_PORT = _env_int("CONFIG_SYNC_PORT", _env_int("PORT", 8080))
CONFIG_SYNC_HOST = (os.getenv("CONFIG_SYNC_HOST") or "0.0.0.0").strip() or "0.0.0.0"
CONFIG_SYNC_PATH = (os.getenv("CONFIG_SYNC_PATH") or "/config").strip() or "/config"
IMAGE_FUZZY_SYNC_PATH = (os.getenv("IMAGE_FUZZY_SYNC_PATH") or "/image-fuzzy-blocks").strip() or "/image-fuzzy-blocks"
STATE_SYNC_MANIFEST_PATH = (os.getenv("STATE_SYNC_MANIFEST_PATH") or "/state-manifest").strip() or "/state-manifest"
STATE_SYNC_BUNDLE_PATH = (os.getenv("STATE_SYNC_BUNDLE_PATH") or "/state-bundle").strip() or "/state-bundle"
CONFIG_SYNC_BODY_LIMIT = max(1024, _env_int("CONFIG_SYNC_BODY_LIMIT", 1024 * 1024))
STATE_SYNC_BODY_LIMIT = max(1024 * 1024, _env_int("STATE_SYNC_BODY_LIMIT", _env_int("CONFIG_SYNC_BODY_LIMIT", 16 * 1024 * 1024)))


def log(msg: str) -> None:
    print(f"[failover] {msg}", flush=True)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分钟{secs}秒"
    if minutes > 0:
        return f"{minutes}分钟{secs}秒"
    return f"{secs}秒"


def _format_alert_message(*, detector: str, target: str, abnormal_for_sec: float, details: list[str]) -> str:
    lines = [
        "🚨 <b>Telegram Risk Control 节点异常告警</b>",
        "",
        f"• 检测来源：<b>{detector}</b>",
        f"• 异常节点：<b>{target}</b>",
        f"• 持续时间：<b>{_format_duration(abnormal_for_sec)}</b>",
        f"• 告警阈值：<b>{_format_duration(ALERT_AFTER_SEC)}</b>",
        f"• 发生时间：<code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}</code>",
        "",
        "• 当前情况：",
    ]
    lines.extend(f"  - {item}" for item in details if item)
    return "\n".join(lines)


def _send_alert_message(text: str) -> bool:
    if ALERT_CHAT_ID <= 0 or not BOT_TOKEN:
        log("skip alert: missing FAILOVER_ALERT_CHAT_ID or BOT_TOKEN")
        return False
    payload = json.dumps(
        {
            "chat_id": ALERT_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=max(REQUEST_TIMEOUT_SEC, 10)) as resp:
            _ = resp.read()
        log(f"alert sent to chat_id={ALERT_CHAT_ID}")
        return True
    except Exception as exc:
        log(f"alert send failed: {exc}")
        return False


def _is_primary_probe_healthy(data: dict) -> bool:
    return bool(data.get("healthy", False))


def _is_failover_allowed(data: dict) -> bool:
    return bool(data.get("failover_allowed", False))


def _write_bytes_atomic(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_file = f"{path}.tmp"
    with open(temp_file, "wb") as f:
        f.write(payload)
    os.replace(temp_file, path)


def _build_admin_state_manifest() -> dict:
    files: dict[str, dict] = {}
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
        except Exception as exc:
            log(f"state manifest skip {rel_path}: {exc}")
    return {
        "schema": 1,
        "generated_at_ns": time.time_ns(),
        "files": files,
    }


def _extract_manifest_files(raw: dict | None) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    files = raw.get("files")
    if not isinstance(files, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for rel_path, meta in files.items():
        if rel_path not in ADMIN_STATE_FILE_PATHS or not isinstance(meta, dict):
            continue
        cleaned[rel_path] = {
            "size": int(meta.get("size", 0) or 0),
            "mtime_ns": int(meta.get("mtime_ns", 0) or 0),
            "sha256": str(meta.get("sha256", "") or ""),
        }
    return cleaned


def _build_admin_state_bundle() -> bytes:
    manifest = _build_admin_state_manifest()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for rel_path in sorted(_extract_manifest_files(manifest).keys()):
            abs_path = ADMIN_STATE_FILE_PATHS[rel_path]
            if os.path.exists(abs_path):
                zf.write(abs_path, arcname=rel_path)
    return buffer.getvalue()


def _apply_admin_state_bundle(bundle: bytes) -> list[str]:
    applied: list[str] = []
    with zipfile.ZipFile(io.BytesIO(bundle), "r") as zf:
        try:
            manifest_raw = json.loads(zf.read("_manifest.json").decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid_state_bundle_manifest:{exc}") from exc
        manifest_files = _extract_manifest_files(manifest_raw)
        for rel_path in sorted(manifest_files.keys()):
            if rel_path not in zf.namelist():
                continue
            target_path = ADMIN_STATE_FILE_PATHS[rel_path]
            _write_bytes_atomic(target_path, zf.read(rel_path))
            mtime_ns = int(manifest_files[rel_path].get("mtime_ns", 0) or 0)
            if mtime_ns > 0:
                try:
                    os.utime(target_path, ns=(mtime_ns, mtime_ns))
                except Exception:
                    pass
            applied.append(rel_path)
    return applied


class _ConfigSyncHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not CONFIG_SYNC_TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {CONFIG_SYNC_TOKEN}"

    def _read_payload(self) -> tuple[dict | list | None, dict | None]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            return None, {"ok": False, "error": "invalid_content_length"}

        if length <= 0 or length > CONFIG_SYNC_BODY_LIMIT:
            return None, {"ok": False, "error": "payload_too_large"}

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            return payload, None
        except json.JSONDecodeError:
            return None, {"ok": False, "error": "invalid_json"}
        except Exception as exc:
            return None, {"ok": False, "error": f"read_failed:{exc}"}

    def _read_binary_payload(self) -> tuple[bytes | None, dict | None]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            return None, {"ok": False, "error": "invalid_content_length"}

        if length <= 0 or length > STATE_SYNC_BODY_LIMIT:
            return None, {"ok": False, "error": "payload_too_large"}

        try:
            return self.rfile.read(length), None
        except Exception as exc:
            return None, {"ok": False, "error": f"read_failed:{exc}"}

    def _read_json_file(self, path: str, expect_type: type[dict] | type[list], not_found: str, invalid: str) -> tuple[int, dict]:
        if not os.path.exists(path):
            return 404, {"ok": False, "error": not_found}
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, expect_type):
                return 500, {"ok": False, "error": invalid}
            return 200, payload
        except Exception as exc:
            return 500, {"ok": False, "error": f"read_failed:{exc}"}

    def _write_json_file(self, path: str, payload: dict | list) -> tuple[int, dict]:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            temp_file = f"{path}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, path)
            return 200, {"ok": True, "saved": True}
        except Exception as exc:
            return 500, {"ok": False, "error": f"write_failed:{exc}"}

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._write_json(200, {"ok": True})
            return

        if self.path not in {CONFIG_SYNC_PATH, IMAGE_FUZZY_SYNC_PATH, STATE_SYNC_MANIFEST_PATH, STATE_SYNC_BUNDLE_PATH}:
            self._write_json(404, {"ok": False, "error": "not_found"})
            return

        if not self._authorized():
            self._write_json(401, {"ok": False, "error": "unauthorized"})
            return

        if self.path == STATE_SYNC_MANIFEST_PATH:
            self._write_json(200, _build_admin_state_manifest())
            return

        if self.path == STATE_SYNC_BUNDLE_PATH:
            try:
                self._write_bytes(200, _build_admin_state_bundle(), "application/zip")
            except Exception as exc:
                self._write_json(500, {"ok": False, "error": f"bundle_build_failed:{exc}"})
            return

        if self.path == CONFIG_SYNC_PATH:
            status, payload = self._read_json_file(
                CONFIG_FILE,
                dict,
                "config_not_found",
                "invalid_config",
            )
            self._write_json(status, payload)
            return

        status, payload = self._read_json_file(
            IMAGE_FUZZY_BLOCK_FILE,
            list,
            "image_fuzzy_blocks_not_found",
            "invalid_image_fuzzy_blocks",
        )
        self._write_json(status, payload)

    def do_PUT(self) -> None:
        if self.path not in {CONFIG_SYNC_PATH, IMAGE_FUZZY_SYNC_PATH, STATE_SYNC_BUNDLE_PATH}:
            self._write_json(404, {"ok": False, "error": "not_found"})
            return

        if not self._authorized():
            self._write_json(401, {"ok": False, "error": "unauthorized"})
            return

        if self.path == STATE_SYNC_BUNDLE_PATH:
            payload, err = self._read_binary_payload()
            if err is not None:
                status = 413 if err.get("error") == "payload_too_large" else 400
                self._write_json(status, err)
                return
            try:
                applied = _apply_admin_state_bundle(payload or b"")
                self._write_json(200, {"ok": True, "saved": True, "applied": applied})
            except Exception as exc:
                self._write_json(400, {"ok": False, "error": f"invalid_bundle:{exc}"})
            return

        payload, err = self._read_payload()
        if err is not None:
            status = 413 if err.get("error") == "payload_too_large" else 400
            self._write_json(status, err)
            return

        if self.path == CONFIG_SYNC_PATH:
            if not isinstance(payload, dict):
                self._write_json(400, {"ok": False, "error": "payload_must_be_object"})
                return
            status, body = self._write_json_file(CONFIG_FILE, payload)
            self._write_json(status, body)
            return

        if not isinstance(payload, list):
            self._write_json(400, {"ok": False, "error": "payload_must_be_array"})
            return
        sanitized = [item for item in payload if isinstance(item, dict)]
        status, body = self._write_json_file(IMAGE_FUZZY_BLOCK_FILE, sanitized)
        self._write_json(status, body)


def start_config_sync_server_if_enabled() -> None:
    log(f"runtime data dir: {DATA_DIR} (source: {DATA_DIR_SOURCE})")
    if not CONFIG_SYNC_TOKEN:
        log("config sync server disabled (CONFIG_SYNC_TOKEN not set)")
        return
    try:
        server = ThreadingHTTPServer((CONFIG_SYNC_HOST, CONFIG_SYNC_PORT), _ConfigSyncHandler)
    except Exception as exc:
        log(f"config sync server failed to start: {exc}")
        return

    thread = threading.Thread(target=server.serve_forever, name="config-sync-server", daemon=True)
    thread.start()
    log(
        "config sync server listening on "
        f"{CONFIG_SYNC_HOST}:{CONFIG_SYNC_PORT}"
        f"{CONFIG_SYNC_PATH}, {IMAGE_FUZZY_SYNC_PATH}, "
        f"{STATE_SYNC_MANIFEST_PATH}, {STATE_SYNC_BUNDLE_PATH}"
    )


def query_primary_alive() -> bool:
    if not STATUS_URL:
        return False
    url = f"{STATUS_URL}?node_id={PRIMARY_NODE_ID}&max_age={MAX_AGE_SEC}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "curl/8.6.0")
    if STATUS_TOKEN:
        req.add_header("Authorization", f"Bearer {STATUS_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)
            healthy = _is_primary_probe_healthy(data)
            failover_allowed = _is_failover_allowed(data)
            if healthy:
                return True
            if failover_allowed:
                log(f"primary reported unhealthy and takeover allowed: {body}")
                return False
            log(f"primary reachable but sticky-primary mode blocks takeover: {body}")
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        log(f"primary probe failed: {exc}")
        return False


def start_bot() -> subprocess.Popen:
    log("starting standby bot process")
    return subprocess.Popen(
        MAIN_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def stop_bot(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    log("primary recovered; stopping standby bot process")
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=12)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_plain_main() -> int:
    log("no HEARTBEAT_STATUS_URL configured; running main.py directly")
    return subprocess.call(MAIN_CMD)


def _bot_log_reader(proc: subprocess.Popen, lines: "queue.Queue[str]") -> None:
    stream = proc.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            lines.put(line.rstrip("\r\n"))
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _drain_bot_log_lines(
    lines: "queue.Queue[str]",
    *,
    conflict_seen: bool = False,
) -> bool:
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            break
        if line:
            print(line, flush=True)
            if (
                "TelegramConflictError" in line
                or "Conflict: terminated by other getUpdates request" in line
            ):
                conflict_seen = True
    return conflict_seen


def main() -> int:
    start_config_sync_server_if_enabled()

    if not STATUS_URL:
        return run_plain_main()

    bot_proc: subprocess.Popen | None = None
    bot_log_lines: "queue.Queue[str]" = queue.Queue()
    bot_log_thread: threading.Thread | None = None
    standby_conflict_seen = False
    standby_conflict_cooldown_until = 0.0
    recover_streak = 0
    fail_streak = 0
    primary_down_since_ts = 0.0
    primary_down_alert_sent = False
    primary_down_last_alert_attempt_ts = 0.0
    log(
        "failover controller active, "
        f"node={PRIMARY_NODE_ID}, poll={POLL_SEC}s, max_age={MAX_AGE_SEC}s, "
        f"fail_confirm={FAIL_CONFIRM_LOOPS}, recover_confirm={RECOVER_CONFIRM_LOOPS}, "
        f"alert_after={ALERT_AFTER_SEC}s, "
        f"conflict_cooldown={STANDBY_CONFLICT_COOLDOWN_SEC}s"
    )

    while True:
        standby_conflict_seen = _drain_bot_log_lines(
            bot_log_lines,
            conflict_seen=standby_conflict_seen,
        )

        if bot_proc is not None and standby_conflict_seen:
            standby_conflict_cooldown_until = time.time() + STANDBY_CONFLICT_COOLDOWN_SEC
            log(
                "standby detected Telegram conflict; "
                f"yielding to primary for {STANDBY_CONFLICT_COOLDOWN_SEC}s"
            )
            stop_bot(bot_proc)
            bot_proc = None
            standby_conflict_seen = False

        primary_alive = query_primary_alive()

        if primary_alive:
            fail_streak = 0
            recover_streak += 1
            primary_down_since_ts = 0.0
            primary_down_alert_sent = False
            primary_down_last_alert_attempt_ts = 0.0
            if bot_proc is not None and recover_streak >= RECOVER_CONFIRM_LOOPS:
                stop_bot(bot_proc)
                bot_proc = None
        else:
            recover_streak = 0
            fail_streak += 1
            now = time.time()
            if primary_down_since_ts <= 0:
                primary_down_since_ts = now
            cooldown_remaining = standby_conflict_cooldown_until - time.time()
            if cooldown_remaining > 0:
                log(
                    "primary probe unhealthy but standby conflict cooldown is active; "
                    f"skip takeover for {int(cooldown_remaining)}s"
                )
            # Guard against transient network jitter: only fail over after consecutive failures.
            if (
                cooldown_remaining <= 0
                and fail_streak >= FAIL_CONFIRM_LOOPS
                and (bot_proc is None or bot_proc.poll() is not None)
            ):
                log(f"primary unhealthy streak={fail_streak}; entering standby")
                bot_proc = start_bot()
                bot_log_lines = queue.Queue()
                bot_log_thread = threading.Thread(
                    target=_bot_log_reader,
                    args=(bot_proc, bot_log_lines),
                    name="standby-bot-log-reader",
                    daemon=True,
                )
                bot_log_thread.start()
                standby_conflict_seen = False
            abnormal_for_sec = now - primary_down_since_ts
            if (
                abnormal_for_sec >= ALERT_AFTER_SEC
                and not primary_down_alert_sent
                and (now - primary_down_last_alert_attempt_ts) >= ALERT_RETRY_SEC
            ):
                primary_down_last_alert_attempt_ts = now
                if _send_alert_message(
                    _format_alert_message(
                        detector="Railway 兜底节点",
                        target="Ubuntu 主节点",
                        abnormal_for_sec=abnormal_for_sec,
                        details=[
                            f"健康探测地址：{STATUS_URL}",
                            f"连续失败次数：{fail_streak}",
                            f"Railway 待机 bot：{'已启动' if bot_proc is not None and bot_proc.poll() is None else '未启动'}",
                            f"主节点 ID：{PRIMARY_NODE_ID}",
                        ],
                    )
                ):
                    primary_down_alert_sent = True

        if bot_proc is not None and bot_proc.poll() is not None:
            standby_conflict_seen = _drain_bot_log_lines(
                bot_log_lines,
                conflict_seen=standby_conflict_seen,
            )
            code = bot_proc.returncode
            log(f"standby bot exited with code={code}; waiting for next loop")
            bot_proc = None
            bot_log_thread = None

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
