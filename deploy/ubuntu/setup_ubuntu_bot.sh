#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/opt/telegram-risk-control"
APP_DIR="$APP_ROOT/app"
DATA_DIR="$APP_ROOT/data"
BACKUP_DIR="$DATA_DIR/backups/config"
VENV_DIR="$APP_ROOT/venv"
SERVICE_DIR="/etc/systemd/system"

if [[ ! -f "$APP_DIR/main.py" ]]; then
  echo "main.py not found under $APP_DIR" >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip rsync curl cloudflare-warp

sudo mkdir -p "$APP_ROOT" "$DATA_DIR" "$BACKUP_DIR"
sudo chown -R fightclub:fightclub "$APP_ROOT"

if [[ ! -d "$VENV_DIR" ]]; then
  sudo -u fightclub python3 -m venv "$VENV_DIR"
fi

sudo -u fightclub "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u fightclub "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo cp "$APP_DIR/deploy/ubuntu/telegram-risk-control.service" "$SERVICE_DIR/telegram-risk-control.service"
sudo cp "$APP_DIR/deploy/ubuntu/telegram-risk-health.service" "$SERVICE_DIR/telegram-risk-health.service"
sudo cp "$APP_DIR/deploy/ubuntu/telegram-risk-net-guard.service" "$SERVICE_DIR/telegram-risk-net-guard.service"
sudo cp "$APP_DIR/deploy/ubuntu/telegram-risk-net-guard.timer" "$SERVICE_DIR/telegram-risk-net-guard.timer"
sudo cp "$APP_DIR/deploy/ubuntu/warp-connect.service" "$SERVICE_DIR/warp-connect.service"
sudo install -m 755 "$APP_DIR/deploy/ubuntu/telegram-risk-net-guard.sh" /usr/local/sbin/telegram-risk-net-guard.sh

sudo systemctl daemon-reload
sudo systemctl enable telegram-risk-control.service
sudo systemctl enable telegram-risk-health.service
sudo systemctl enable telegram-risk-net-guard.timer
sudo systemctl enable warp-svc.service
sudo systemctl enable warp-connect.service
sudo systemctl restart telegram-risk-control.service
sudo systemctl restart telegram-risk-health.service
sudo systemctl restart telegram-risk-net-guard.timer
sudo systemctl restart warp-svc.service
sudo systemctl restart warp-connect.service

sudo systemctl --no-pager --full status \
  telegram-risk-control.service \
  telegram-risk-health.service \
  telegram-risk-net-guard.timer \
  warp-svc.service \
  warp-connect.service
