#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/opt/telegram-risk-control/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

BOT_TOKEN="${BOT_TOKEN:-}"
API_BASE="${TELEGRAM_BOT_API_BASE:-https://api.telegram.org}"
NET_TIMEOUT="${HEALTH_NET_TIMEOUT_SEC:-8}"

if [[ -z "$BOT_TOKEN" ]]; then
  exit 0
fi

probe() {
  curl -fsS --max-time "$NET_TIMEOUT" "${API_BASE%/}/bot${BOT_TOKEN}/getMe" >/dev/null 2>&1
}

if probe; then
  exit 0
fi

# Primary egress recovery path: make sure WARP service is running and connected.
systemctl start warp-svc >/dev/null 2>&1 || true
/usr/bin/warp-cli --accept-tos connect >/dev/null 2>&1 || true
sleep 4

if probe; then
  # Refresh bot sockets after link recovery.
  systemctl try-restart telegram-risk-control.service >/dev/null 2>&1 || true
fi

exit 0
