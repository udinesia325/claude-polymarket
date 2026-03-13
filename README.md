# Polymarket Trading Bot

AI-powered prediction market trading bot using Claude as the decision engine.

## Architecture

```
1 codebase → 2 deployments
  .env.fake  →  TRADING_MODE=fake  →  Mumbai testnet  (no real money)
  .env.real  →  TRADING_MODE=real  →  Polygon mainnet (real wallet)
```

## Project Structure

```
claude-polymarket/
├── main.py                    # Entry point, scheduler, wiring
├── requirements.txt
├── .env.example               # Template (copy → .env.fake / .env.real)
├── .env.fake                  # Testnet config  ← fill this
├── .env.real                  # Mainnet config  ← fill this
├── config/
│   └── settings.py            # Pydantic settings model
├── core/
│   ├── claude_agent.py        # Claude AI decision engine
│   ├── market.py              # Market data + news scraping
│   └── executor.py            # Order placement + risk gates
├── services/
│   ├── wallet.py              # web3.py wallet + USDC balance/approval
│   └── portfolio.py           # Position tracking + P&L
├── notifications/
│   └── telegram.py            # Telegram alerts
└── scripts/
    ├── run_fake.sh             # Local testnet run
    ├── run_real.sh             # Local mainnet run (safety prompt)
    └── deploy_gcp.sh           # Deploy to GCP e2-micro
```

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env.fake
# Edit .env.fake with your testnet wallet, API keys, Telegram token
```

### 3. Run in fake mode (testnet)

```bash
chmod +x scripts/run_fake.sh
./scripts/run_fake.sh
# or directly:
TRADING_MODE=fake python main.py
```

### 4. Run in real mode (mainnet)

```bash
cp .env.example .env.real
# Edit .env.real — set DRY_RUN=false, real wallet, real API keys
./scripts/run_real.sh
```

## Strategy: Hybrid (copy_trader + news)

Each 15-minute cycle:
1. Refresh portfolio positions from Polymarket CLOB
2. Fetch top 20 active markets by 24h volume
3. For each candidate (not already held), build context:
   - Market metadata (prices, liquidity, volume)
   - Order book depth
   - Google News headlines for the market question
   - Copy-trader's open positions + recent trades
4. Send context to Claude → receive JSON decision
5. Apply confidence gate (`MIN_CONFIDENCE_SCORE`)
6. Apply risk gates (max order size, max exposure, max positions)
7. Submit limit order via CLOB SDK
8. Send Telegram notification

## Risk Controls

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MIN_CONFIDENCE_SCORE` | 0.7 | Claude must be ≥70% confident |
| `MAX_ORDER_SIZE_USDC` | 10 | Max single order |
| `MAX_TOTAL_EXPOSURE_USDC` | 100 | Max total open exposure |
| `MAX_POSITIONS` | 5 | Max concurrent positions |
| `DRY_RUN` | true | Log orders without submitting |

## Deploy to GCP e2-micro

```bash
export GCP_PROJECT_ID=your-project-id
export GCP_INSTANCE_NAME=claude-polymarket
export GCP_ZONE=us-central1-a

# Deploy fake mode
./scripts/deploy_gcp.sh fake

# Deploy real mode
./scripts/deploy_gcp.sh real
```

Tail logs after deploy:
```bash
gcloud compute ssh claude-polymarket --zone us-central1-a \
  --command 'journalctl -u claude-polymarket -f'
```

## Environment Variables

See `.env.example` for full documentation of every variable.

## Security Notes

- **Never commit** `.env.fake` or `.env.real` — they contain private keys
- The `.gitignore` excludes them by default
- On GCP, the deploy script copies env files over SCP (never in source code)
- For production, consider using GCP Secret Manager instead of env files

Cara Deploy ke Google Cloud VPS
Langkah 1 — Setup VPS (sekali saja)

# SSH ke server, lalu:
sudo bash scripts/deploy-vps.sh
Langkah 2 — Upload kode

# Dari laptop, jalankan ini:
rsync -av --exclude='.env.*' --exclude='.git' \
  ./ user@IP_SERVER:/opt/polymarket/
Langkah 3 — Buat env file di server

# Di server:
nano /opt/polymarket/.env.fake   # isi semua nilai
nano /opt/polymarket/.env.real   # isi semua nilai
File ini tidak pernah masuk ke image — hanya ada di server, di-mount saat container run.

Langkah 4 — Build image

cd /opt/polymarket
docker build -t polymarket-bot .
Langkah 5 — Jalankan

bash scripts/run-fake.sh   # testnet
bash scripts/run-real.sh   # mainnet (ada konfirmasi dulu)
Perintah Berguna

docker logs -f polymarket-fake     # lihat log realtime
docker logs -f polymarket-real

docker stop polymarket-real        # stop bot
docker restart polymarket-fake     # restart bot

# Update kode (setelah rsync ulang):
docker build -t polymarket-bot . && bash scripts/run-fake.sh
Keamanan
Status
Secret di dalam image	Tidak pernah
Secret di-mount read-only	Ya (:ro)
Auto-restart kalau crash	Ya (--restart always)
Konfirmasi sebelum run real	Ya (ketik yes)



Next steps:
  1. Copy your source code to this server:
     rsync -av --exclude='.env.*' ./ user@YOUR_IP:/opt/polymarket/

  2. Create your env files on the server:
     nano /opt/polymarket/.env.fake   # for testnet
     nano /opt/polymarket/.env.real   # for mainnet

  3. Build the Docker image (run from /opt/polymarket):
     docker build -t polymarket-bot .

  4. Start the bot (choose fake or real):
     bash scripts/run-fake.sh
     bash scripts/run-real.sh