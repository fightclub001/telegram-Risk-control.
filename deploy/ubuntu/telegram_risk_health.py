#!/usr/bin/env python3
import json
import os
import platform
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import certifi
    _CERTIFI_CAFILE = certifi.where()
except Exception:
    _CERTIFI_CAFILE = None


HOST = os.getenv("HEALTH_LISTEN_HOST", "127.0.0.1")
PORT = int(os.getenv("HEALTH_LISTEN_PORT", "18080"))
READ_TOKEN = os.getenv("HEARTBEAT_READ_TOKEN", "").strip()
ALLOW_ANON_STATUS = (os.getenv("HEALTH_ALLOW_ANON_STATUS", "1").strip().lower() in {"1", "true", "yes", "on"})
_svc = os.getenv("PRIMARY_SERVICE_NAME", "").strip()
if not _svc and platform.system() == 'Darwin':
    _svc = "ai.telegram-risk-control"
SERVICE_NAME = _svc or "telegram-risk-control.service"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_BOT_API_BASE", "https://api.telegram.org").strip().rstrip("/")
NETWORK_TIMEOUT_SEC = max(2, int((os.getenv("HEALTH_NET_TIMEOUT_SEC") or "8").strip()))
NETWORK_CACHE_TTL_SEC = max(2, int((os.getenv("HEALTH_NET_CACHE_TTL_SEC") or "8").strip()))
FAILOVER_GRACE_SEC = max(0, int((os.getenv("HEALTH_FAILOVER_GRACE_SEC") or "300").strip()))
_NETWORK_CACHE_TS = 0.0
_NETWORK_CACHE_OK = False
_LAST_FULLY_HEALTHY_TS = time.time()


def service_is_healthy() -> bool:
    if platform.system() == 'Darwin':
        proc = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True, check=False)
        return proc.returncode == 0
    proc = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() == "active"


def telegram_api_is_healthy() -> bool:
    global _NETWORK_CACHE_TS, _NETWORK_CACHE_OK

    if not BOT_TOKEN:
        return True

    now = time.time()
    if now - _NETWORK_CACHE_TS < NETWORK_CACHE_TTL_SEC:
        return _NETWORK_CACHE_OK

    url = f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getMe"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "telegram-risk-health/1.0")

    ok = False
    try:
        ctx = None
        if _CERTIFI_CAFILE:
            ctx = ssl.create_default_context(cafile=_CERTIFI_CAFILE)
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SEC, context=ctx) as resp:
            ok = resp.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        ok = False

    _NETWORK_CACHE_TS = now
    _NETWORK_CACHE_OK = ok
    return ok


def compute_status() -> dict:
    global _LAST_FULLY_HEALTHY_TS

    service_ok = service_is_healthy()
    network_ok = telegram_api_is_healthy()
    healthy = service_ok and network_ok
    now = time.time()
    if healthy:
        _LAST_FULLY_HEALTHY_TS = now
        unhealthy_for_sec = 0
    else:
        unhealthy_for_sec = max(0, int(now - _LAST_FULLY_HEALTHY_TS))
    failover_allowed = (not healthy) and unhealthy_for_sec >= FAILOVER_GRACE_SEC
    return {
        "healthy": healthy,
        "service_healthy": service_ok,
        "telegram_api_healthy": network_ok,
        "failover_allowed": failover_allowed,
        "failover_grace_sec": FAILOVER_GRACE_SEC,
        "unhealthy_for_sec": unhealthy_for_sec,
        "primary_reachable": True,
    }


class Handler(BaseHTTPRequestHandler):
    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not READ_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {READ_TOKEN}"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            return self._write_json(200, {"ok": True, "service": "telegram-risk-health"})
        if parsed.path != "/status":
            return self._write_json(404, {"ok": False, "error": "not_found"})
        if not self._authorized() and not ALLOW_ANON_STATUS:
            return self._write_json(401, {"ok": False, "error": "unauthorized"})

        query = parse_qs(parsed.query)
        node_id = (query.get("node_id") or [os.getenv("PRIMARY_NODE_ID", "mac-main")])[0]
        status = compute_status()
        return self._write_json(
            200,
            {
                "ok": True,
                "node_id": node_id,
                "service": SERVICE_NAME,
                **status,
            },
        )

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()
