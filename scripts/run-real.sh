#!/bin/bash
# Run the bot in REAL mode (Polygon mainnet, real money)
set -euo pipefail

ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env.real"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found."
  echo "Copy .env.real to the server and fill in your values."
  exit 1
fi

# Safety confirmation
echo "⚠️  You are about to start the bot in REAL mode (Polygon mainnet)."
echo "   This will trade with REAL money."
read -rp "   Type 'yes' to confirm: " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

# Stop and remove existing real container if running
docker rm -f polymarket-real 2>/dev/null || true

docker run -d \
  --name polymarket-real \
  --restart always \
  -e TRADING_MODE=real \
  -v "$ENV_FILE":/app/.env.real:ro \
  polymarket-bot

echo "✅ Bot started in REAL mode."
echo "   Logs: docker logs -f polymarket-real"
