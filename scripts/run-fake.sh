#!/bin/bash
# Run the bot in FAKE mode (Mumbai testnet, dry_run=true)
set -euo pipefail

ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env.fake"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found."
  echo "Copy .env.fake to the server and fill in your values."
  exit 1
fi

# Stop and remove existing fake container if running
docker rm -f polymarket-fake 2>/dev/null || true

docker run -d \
  --name polymarket-fake \
  --restart always \
  -e TRADING_MODE=fake \
  -v "$ENV_FILE":/app/.env.fake:ro \
  polymarket-bot

echo "✅ Bot started in FAKE mode."
echo "   Logs: docker logs -f polymarket-fake"
