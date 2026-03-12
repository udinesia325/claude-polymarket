#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-vps.sh
# One-time setup script for a fresh Google Cloud Debian/Ubuntu VM.
# Run once as root (or with sudo).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "==> Installing Docker..."
apt-get update -q
apt-get install -y docker.io
systemctl enable --now docker

echo "==> Creating app directory..."
mkdir -p /opt/polymarket
cd /opt/polymarket

echo ""
echo "✅ Docker installed."
echo ""
echo "Next steps:"
echo "  1. Copy your source code to this server:"
echo "     rsync -av --exclude='.env.*' ./ user@YOUR_IP:/opt/polymarket/"
echo ""
echo "  2. Create your env files on the server:"
echo "     nano /opt/polymarket/.env.fake   # for testnet"
echo "     nano /opt/polymarket/.env.real   # for mainnet"
echo ""
echo "  3. Build the Docker image (run from /opt/polymarket):"
echo "     docker build -t polymarket-bot ."
echo ""
echo "  4. Start the bot (choose fake or real):"
echo "     bash scripts/run-fake.sh"
echo "     bash scripts/run-real.sh"
