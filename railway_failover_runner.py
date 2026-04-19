import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


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


def log(msg: str) -> None:
    print(f"[failover] {msg}", flush=True)


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
