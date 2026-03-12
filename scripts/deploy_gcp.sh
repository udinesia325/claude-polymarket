#!/usr/bin/env bash
# Deploy bot to Google Cloud e2-micro (Ubuntu 22.04)
# Prerequisites: gcloud CLI installed and authenticated
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
INSTANCE_NAME="${GCP_INSTANCE_NAME:-polymarket-bot}"
ZONE="${GCP_ZONE:-us-central1-a}"
MODE="${1:-fake}"   # pass "real" as first arg for production deploy

echo "[INFO] Deploying to GCP | instance=$INSTANCE_NAME | mode=$MODE"

# ── Rsync source code ─────────────────────────────────────────────────────────
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gcloud compute scp --recurse \
  --project "$PROJECT_ID" \
  --zone "$ZONE" \
  "$BOT_DIR" \
  "${INSTANCE_NAME}:~/polymarket-bot" \
  --exclude=".env.*" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  --exclude=".git"

# ── Copy the right env file ───────────────────────────────────────────────────
gcloud compute scp \
  --project "$PROJECT_ID" \
  --zone "$ZONE" \
  "$BOT_DIR/.env.$MODE" \
  "${INSTANCE_NAME}:~/polymarket-bot/.env.$MODE"

# ── Remote setup & restart ────────────────────────────────────────────────────
gcloud compute ssh "$INSTANCE_NAME" \
  --project "$PROJECT_ID" \
  --zone "$ZONE" \
  --command "
    set -e
    cd ~/polymarket-bot

    # Install deps if needed
    if [ ! -d venv ]; then
      python3 -m venv venv
    fi
    source venv/bin/activate
    pip install -q --upgrade pip
    pip install -q -r requirements.txt

    # Install systemd service
    sudo tee /etc/systemd/system/polymarket-bot.service > /dev/null <<'EOF'
[Unit]
Description=Polymarket Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=/home/$(whoami)/polymarket-bot
Environment=TRADING_MODE=$MODE
ExecStart=/home/$(whoami)/polymarket-bot/venv/bin/python main.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable polymarket-bot
    sudo systemctl restart polymarket-bot
    echo '[OK] Bot service started'
    sudo systemctl status polymarket-bot --no-pager
  "

echo "[INFO] Deploy complete. Tail logs with:"
echo "  gcloud compute ssh $INSTANCE_NAME --zone $ZONE --command 'journalctl -u polymarket-bot -f'"
