import json
import os
import signal
import subprocess
import sys
import threading
import time
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
MAX_AGE_SEC = _env_int("HEARTBEAT_MAX_AGE_SEC", 90)
POLL_SEC = _env_int("HEARTBEAT_POLL_SEC", 15)
RECOVER_CONFIRM_LOOPS = _env_int("HEARTBEAT_RECOVER_CONFIRM_LOOPS", 3)
REQUEST_TIMEOUT_SEC = _env_int("HEARTBEAT_TIMEOUT_SEC", 5)
_RUNTIME_STATE_FILES = (
    "config.json",
    "reports.json",
    "media_stats.json",
    "repeat_levels.json",
    "forward_match_memory.json",
    "recent_messages.json",
    "recent_messages.db",
    "image_fuzzy_blocks.json",
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
    for value in (explicit_data_dir, legacy_config_dir, "/data", "/app/data"):
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
    return "/data", "default"


DATA_DIR, DATA_DIR_SOURCE = _resolve_data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
IMAGE_FUZZY_BLOCK_FILE = os.path.join(DATA_DIR, "image_fuzzy_blocks.json")
CONFIG_SYNC_TOKEN = (os.getenv("CONFIG_SYNC_TOKEN") or "").strip()
CONFIG_SYNC_PORT = _env_int("CONFIG_SYNC_PORT", _env_int("PORT", 8080))
CONFIG_SYNC_HOST = (os.getenv("CONFIG_SYNC_HOST") or "0.0.0.0").strip() or "0.0.0.0"
CONFIG_SYNC_PATH = (os.getenv("CONFIG_SYNC_PATH") or "/config").strip() or "/config"
IMAGE_FUZZY_SYNC_PATH = (os.getenv("IMAGE_FUZZY_SYNC_PATH") or "/image-fuzzy-blocks").strip() or "/image-fuzzy-blocks"
CONFIG_SYNC_BODY_LIMIT = max(1024, _env_int("CONFIG_SYNC_BODY_LIMIT", 1024 * 1024))


def log(msg: str) -> None:
    print(f"[failover] {msg}", flush=True)


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

        if self.path not in {CONFIG_SYNC_PATH, IMAGE_FUZZY_SYNC_PATH}:
            self._write_json(404, {"ok": False, "error": "not_found"})
            return

        if not self._authorized():
            self._write_json(401, {"ok": False, "error": "unauthorized"})
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
        if self.path not in {CONFIG_SYNC_PATH, IMAGE_FUZZY_SYNC_PATH}:
            self._write_json(404, {"ok": False, "error": "not_found"})
            return

        if not self._authorized():
            self._write_json(401, {"ok": False, "error": "unauthorized"})
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
        f"{CONFIG_SYNC_PATH} and {IMAGE_FUZZY_SYNC_PATH}"
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
            healthy = bool(data.get("healthy", False))
            if not healthy:
                log(f"primary reported unhealthy: {body}")
            return healthy
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        log(f"primary probe failed: {exc}")
        return False


def start_bot() -> subprocess.Popen:
    log("starting standby bot process")
    return subprocess.Popen(MAIN_CMD)


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


def main() -> int:
    start_config_sync_server_if_enabled()

    if not STATUS_URL:
        return run_plain_main()

    bot_proc: subprocess.Popen | None = None
    recover_streak = 0
    log(
        f"failover controller active, node={PRIMARY_NODE_ID}, poll={POLL_SEC}s, max_age={MAX_AGE_SEC}s"
    )

    while True:
        primary_alive = query_primary_alive()

        if primary_alive:
            recover_streak += 1
            if bot_proc is not None and recover_streak >= RECOVER_CONFIRM_LOOPS:
                stop_bot(bot_proc)
                bot_proc = None
        else:
            recover_streak = 0
            if bot_proc is None or bot_proc.poll() is not None:
                bot_proc = start_bot()

        if bot_proc is not None and bot_proc.poll() is not None:
            code = bot_proc.returncode
            log(f"standby bot exited with code={code}; waiting for next loop")
            bot_proc = None

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
