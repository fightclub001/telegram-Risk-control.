import json
import os
import socket
import time
import urllib.error
import urllib.request


WRITE_URL = os.getenv("HEARTBEAT_WRITE_URL", "").strip()
WRITE_TOKEN = os.getenv("HEARTBEAT_WRITE_TOKEN", "").strip()
PRIMARY_NODE_ID = os.getenv("PRIMARY_NODE_ID", "ubuntu-main").strip()
INTERVAL_SEC = int(os.getenv("HEARTBEAT_INTERVAL_SEC", "15"))
TIMEOUT_SEC = int(os.getenv("HEARTBEAT_TIMEOUT_SEC", "5"))


def log(msg: str) -> None:
    print(f"[heartbeat] {msg}", flush=True)


def build_payload() -> bytes:
    body = {
        "node_id": PRIMARY_NODE_ID,
        "ts": int(time.time()),
        "hostname": socket.gethostname(),
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def send_once() -> bool:
    if not WRITE_URL:
        log("HEARTBEAT_WRITE_URL is empty")
        return False
    req = urllib.request.Request(WRITE_URL, data=build_payload(), method="POST")
    req.add_header("Content-Type", "application/json")
    if WRITE_TOKEN:
        req.add_header("Authorization", f"Bearer {WRITE_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            ok = 200 <= int(resp.status) < 300
            if not ok:
                log(f"unexpected status: {resp.status}")
            return ok
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        log(f"post failed: {exc}")
        return False


def main() -> int:
    log(f"sender started: node={PRIMARY_NODE_ID}, interval={INTERVAL_SEC}s")
    while True:
        ok = send_once()
        if ok:
            log("heartbeat sent")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())

