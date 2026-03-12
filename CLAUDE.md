# CLAUDE.md — Polymarket Trading Bot

Panduan konteks untuk Claude agar memahami arsitektur, keputusan desain, dan
hal-hal penting yang perlu diketahui sebelum menyentuh kode ini.

---

## Ringkasan Proyek

Bot trading otomatis untuk Polymarket (prediction market).
Claude AI (claude-opus-4-6) bertindak sebagai decision engine — menganalisis
pasar, berita, dan data copy-trader untuk memutuskan apakah akan buy YES, buy NO,
atau skip. Eksekusi order dilakukan via Polymarket CLOB API.

---

## Struktur File

```
main.py                     Entry point, scheduler, TradingBot class
config/settings.py          Semua konfigurasi dari env vars (via pydantic)
core/claude_agent.py        Claude AI decision engine + TradeDecision dataclass
core/executor.py            Validasi risk + submit order ke CLOB
core/market.py              Fetch markets, order book, copy-trader, scrape news
core/position_monitor.py    Monitor posisi terbuka, auto-close jika adverse
services/portfolio.py       Track posisi, hitung P&L, enforce risk limits
services/wallet.py          USDC balance, allowance, signing via web3
notifications/telegram.py   Kirim notifikasi order/P&L/error ke Telegram
```

---

## Alur Kerja Utama

```
Setiap N menit (ANALYZE_INTERVAL_MINUTES):
  1. portfolio.refresh()          — ambil posisi terbaru dari CLOB
  2. market_svc.get_active_markets() — top 20 pasar by volume 24h
  3. Filter pasar yang sudah dipegang
  4. Untuk setiap kandidat (max 5):
       market_svc.get_market_context() → news + order book + copy-trader
       agent.analyse_market()          → Claude returns TradeDecision JSON
       executor.execute()              → risk check → submit order / dry run
       telegram.send_order_notification()

Setiap N/3 menit (position monitor):
  1. portfolio.refresh()
  2. Untuk setiap posisi terbuka:
       scrape news terbaru + order book
       agent.analyse_position_risk()  → Claude: CLOSE atau HOLD + urgency
       Jika urgency >= 0.7 → close position (sell at best bid)
       telegram.send_position_close() jika dieksekusi
```

---

## Konfigurasi Penting (Settings)

| Variable | Default | Keterangan |
|---|---|---|
| `TRADING_MODE` | `fake` | `fake` = Mumbai testnet, `real` = Polygon mainnet |
| `DRY_RUN` | `true` | Jika true, log order tanpa submit ke CLOB |
| `CLAUDE_MODEL` | `claude-opus-4-6` | Model Anthropic yang digunakan |
| `ANALYZE_INTERVAL_MINUTES` | `15` | Frekuensi siklus analisis |
| `MIN_CONFIDENCE_SCORE` | `0.7` | Threshold confidence Claude untuk trade |
| `MAX_ORDER_SIZE_USDC` | `10.0` | Ukuran order maksimal per trade |
| `MAX_TOTAL_EXPOSURE_USDC` | `100.0` | Total eksposur maksimal semua posisi |
| `MAX_POSITIONS` | `5` | Jumlah posisi terbuka maksimal |
| `STRATEGY` | `hybrid` | `copy_trader` / `news` / `hybrid` |

Dua env file: `.env.fake` (testnet) dan `.env.real` (mainnet).
**Jangan pernah commit file `.env.*` ke git.**

---

## Claude Agent — Hal Penting

### System Prompt (`SYSTEM_PROMPT_TEMPLATE`)
- Diinjeksi dengan tanggal hari ini (`today`) dan `min_confidence` dari settings
  saat runtime — bukan hardcode.
- Menjelaskan bahwa YES price = implied probability (bukan hanya angka harga).
- Memandu position sizing proporsional terhadap edge (kecil/sedang/kuat).
- Rule: SKIP jika market resolves dalam 24 jam dan outcome masih tidak pasti.
- Rule: SKIP jika kita sudah punya posisi di market yang sama.

### `analyse_market()` — parameter Claude API
- `temperature=0` — penting untuk JSON output yang konsisten
- `max_tokens=1024` — cukup untuk reasoning + JSON tanpa terpotong
- Response di-parse dengan `_strip_code_fences()` menggunakan regex (bukan split)

### `analyse_position_risk()` — untuk position monitor
- Mengembalikan `{"action": "CLOSE"|"HOLD", "reasoning": "...", "urgency": 0.0-1.0}`
- `urgency >= 0.7` akan memicu eksekusi close

---

## Market Context (`get_market_context`)

Dict yang dikirim ke Claude berisi:

```python
{
  "market": {
    "condition_id", "question", "end_date",
    "yes_token_id", "no_token_id",   # ← wajib ada untuk eksekusi order
    "yes_price", "no_price",
    "volume_24h_usdc", "liquidity_usdc", "tags"
  },
  "order_book_summary": {"best_bid", "best_ask", "bid_depth_3", "ask_depth_3"},
  "news": [{"title", "source", "published"}],
  "copy_trader_positions": [...],   # hanya jika strategy == copy_trader/hybrid
  "copy_trader_recent_trades": [...]
}
```

> **Bug yang sudah diperbaiki:** Sebelumnya `yes_token_id` dan `no_token_id`
> tidak dimasukkan ke dalam market context dict, sehingga token selalu kosong
> dan order tidak bisa dieksekusi. Sekarang sudah ada di `market.py:200-201`.

---

## Portfolio Summary — Field Names

`portfolio.get_pnl_summary()` mengembalikan `positions` list dengan field:

```python
{
  "market_id", "market_question", "side",          # bukan "question"/"outcome"
  "size", "size_usdc", "entry_price", "current_price",
  "unrealised_pnl", "unrealised_pnl_pct"
}
```

`_build_user_message` di claude_agent.py membaca field `market_question`, `side`,
`size_usdc`, `entry_price`, `current_price`, `unrealised_pnl`. Jika mengubah
portfolio summary, update juga bagian ini.

---

## Risk Gates (Urutan Eksekusi di `executor.py`)

1. `decision.is_skip` → return None
2. `confidence < min_confidence_score` → return None
3. `portfolio.can_open_position(size)` → cek max_positions & max_exposure
4. `dry_run == True` → log saja, kembalikan OrderResult sukses palsu
5. `_place_order()` → submit ke CLOB

---

## Position Monitor (`core/position_monitor.py`)

- Dijalankan setiap `max(5, analyze_interval // 3)` menit.
- `URGENCY_THRESHOLD = 0.7` — konstanta di file ini, bisa disesuaikan.
- Sell order ditempatkan di **best bid** (bukan market order).
- Setelah close, langsung memanggil `portfolio.add_realised_pnl()`.
- Dry run: hanya log, tidak submit order.

---

## Deployment (Docker di Google Cloud VPS)

```bash
# Setup VPS sekali saja (dari laptop):
ssh user@IP 'bash -s' < scripts/deploy-vps.sh

# Upload kode:
rsync -av --exclude='.env.*' --exclude='.git' ./ user@IP:/opt/polymarket/

# Di server — buat env file, lalu:
docker build -t polymarket-bot .
bash scripts/run-fake.sh   # testnet
bash scripts/run-real.sh   # mainnet (ada konfirmasi)
```

Env file **tidak masuk ke Docker image** (dikecualikan di `.dockerignore`).
Di-mount saat runtime: `-v /path/.env.fake:/app/.env.fake:ro`.

---

## Hal yang Perlu Diperhatikan

- `structlog` di-import di `main.py` tapi belum digunakan secara aktif —
  logging masih pakai `logging` standar.
- News scraping pakai Google News RSS (tidak butuh API key), tapi bisa
  flaky jika Google membatasi request.
- Copy-trader data diambil dari Gamma API (`gamma-api.polymarket.com`) —
  bukan endpoint resmi yang dijamin stabil.
- `portfolio.refresh()` merekonstruksi posisi dari trade history (bukan
  snapshot langsung) — bisa tidak akurat jika ada order yang partial fill.
