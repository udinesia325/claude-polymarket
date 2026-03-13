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

## Deploy ke VPS (Docker)

### Langkah 1 — Setup VPS (sekali saja)

```bash
# SSH ke server, lalu jalankan:
sudo bash scripts/deploy-vps.sh
```

### Langkah 2 — Upload kode (dari laptop)

File `.env.*` **tidak ikut di-upload** karena berisi private key.

```bash
rsync -av --exclude='.env.*' --exclude='.git' \
  ./ user@IP_SERVER:/opt/polymarket/
```

### Langkah 3 — Upload env file (dari laptop)

Lebih aman copy langsung dari laptop daripada mengetik ulang di server:

```bash
scp .env.fake user@IP_SERVER:/opt/polymarket/.env.fake
scp .env.real user@IP_SERVER:/opt/polymarket/.env.real   # jika pakai real mode
```

> File ini tidak pernah masuk ke Docker image — hanya ada di server dan di-mount
> saat container run (`-v .env.fake:/app/.env.fake:ro`).

### Langkah 4 — Build image (di server)

Hanya perlu dijalankan sekali, atau setiap kali **kode** berubah.
Jika hanya mengubah nilai di `.env.*`, **tidak perlu rebuild**.

```bash
cd /opt/polymarket
docker build -t polymarket-bot .
```

### Langkah 5 — Jalankan bot

```bash
bash scripts/run-fake.sh   # fake mode: DRY_RUN=true, tidak ada order real
bash scripts/run-real.sh   # real mode: ada konfirmasi ketik "yes" dulu
```

### Update nilai env tanpa rebuild

```bash
# 1. Edit .env.fake di laptop
# 2. Upload ulang ke server
scp .env.fake user@IP_SERVER:/opt/polymarket/.env.fake
# 3. Restart container (tidak perlu rebuild image)
ssh user@IP_SERVER 'bash /opt/polymarket/scripts/run-fake.sh'
```

### Update kode

```bash
# 1. Upload kode terbaru
rsync -av --exclude='.env.*' --exclude='.git' ./ user@IP_SERVER:/opt/polymarket/
# 2. Rebuild image dan restart
ssh user@IP_SERVER 'cd /opt/polymarket && docker build -t polymarket-bot . && bash scripts/run-fake.sh'
```

### Perintah Berguna

```bash
docker logs -f polymarket-fake     # lihat log realtime (fake mode)
docker logs -f polymarket-real     # lihat log realtime (real mode)

docker stop polymarket-fake        # stop bot fake
docker stop polymarket-real        # stop bot real

docker restart polymarket-fake     # restart bot fake
```

### Keamanan

| Aspek | Status |
|---|---|
| Secret di dalam image | Tidak pernah |
| Secret di-mount read-only | Ya (`:ro`) |
| Auto-restart kalau crash | Ya (`--restart always`) |
| Konfirmasi sebelum run real | Ya (ketik `yes`) |