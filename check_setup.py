"""
check_setup.py
──────────────
Validasi semua koneksi sebelum menjalankan bot.
Jalankan dengan:
    TRADING_MODE=fake python check_setup.py
"""

import os
import sys
from pathlib import Path

# ── Load env file ──────────────────────────────────────────────────────────────
_mode = os.environ.get("TRADING_MODE", "fake")
_env_file = Path(__file__).parent / f".env.{_mode}"
if not _env_file.exists():
    print(f"[FAIL] Env file tidak ditemukan: {_env_file}")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv(_env_file, override=True)

# ── Helpers ────────────────────────────────────────────────────────────────────
PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
INFO = "\033[94m[INFO]\033[0m"

errors = 0

def ok(label, detail=""):
    print(f"{PASS} {label}" + (f" — {detail}" if detail else ""))

def fail(label, detail=""):
    global errors
    errors += 1
    print(f"{FAIL} {label}" + (f" — {detail}" if detail else ""))

def warn(label, detail=""):
    print(f"{WARN} {label}" + (f" — {detail}" if detail else ""))

def info(label, detail=""):
    print(f"{INFO} {label}" + (f" — {detail}" if detail else ""))

print(f"\n{'='*55}")
print(f"  Polymarket Bot — Setup Check  (mode: {_mode})")
print(f"{'='*55}\n")

# ── 1. Settings / env validation ───────────────────────────────────────────────
print("[ 1 ] Settings")
try:
    from config.settings import load_settings
    settings = load_settings()
    ok("Semua env variable terbaca oleh pydantic")
    info("TRADING_MODE", settings.trading_mode)
    info("DRY_RUN", str(settings.dry_run))
    info("STRATEGY", settings.strategy)
    info("CLAUDE_MODEL", settings.claude_model)
except Exception as e:
    fail("Settings gagal load", str(e))
    print("\n[ABORT] Tidak bisa lanjut tanpa settings yang valid.")
    sys.exit(1)

# ── 2. Wallet / private key ────────────────────────────────────────────────────
print("\n[ 2 ] Wallet")
try:
    from services.wallet import WalletService
    wallet = WalletService(settings)
    ok("Private key valid dan cocok dengan WALLET_ADDRESS")
    summary = wallet.get_summary()
    info("Address", summary["address"])
    info("MATIC balance", f"{summary['matic_balance']:.6f}")
    info("USDC balance", f"{summary['usdc_balance']:.2f}")
    info("USDC allowance", f"{summary['usdc_allowance']:.2f}")

    if summary["matic_balance"] < 0.001:
        warn("MATIC sangat rendah — mungkin tidak cukup untuk gas fee")
    else:
        ok("MATIC cukup untuk gas")

    if summary["usdc_balance"] < 1:
        warn("USDC balance < 1 — tidak ada modal untuk trading")
    else:
        ok("USDC balance tersedia", f"${summary['usdc_balance']:.2f}")

except AssertionError as e:
    fail("WALLET_ADDRESS tidak cocok dengan private key", str(e))
except Exception as e:
    fail("Wallet gagal diinisialisasi", str(e))

# ── 3. Polymarket CLOB API ────────────────────────────────────────────────────
print("\n[ 3 ] Polymarket CLOB API")
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        api_passphrase=settings.api_passphrase,
    )
    clob = ClobClient(
        host=settings.polymarket_host,
        chain_id=settings.chain_id,
        key=settings.wallet_private_key,
        creds=creds,
    )
    markets = clob.get_markets()
    count = len(markets.get("data", []) if isinstance(markets, dict) else markets)
    ok("Koneksi ke CLOB API berhasil", f"{count} markets diterima")
except Exception as e:
    fail("CLOB API gagal", str(e))

# ── 4. Anthropic API key ──────────────────────────────────────────────────────
print("\n[ 4 ] Anthropic API (Claude)")
try:
    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=10,
        messages=[{"role": "user", "content": "ping"}],
    )
    ok("Anthropic API key valid", f"model: {settings.claude_model}")
except Exception as e:
    fail("Anthropic API gagal", str(e))

# ── 5. Telegram ───────────────────────────────────────────────────────────────
print("\n[ 5 ] Telegram")
try:
    import asyncio
    import telegram

    async def _check_telegram():
        async with telegram.Bot(token=settings.telegram_bot_token) as bot:
            bot_info = await bot.get_me()
            ok("Telegram bot token valid", f"@{bot_info.username}")
            try:
                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text="✅ *Polymarket Bot* — setup check berhasil!",
                    parse_mode="Markdown",
                )
                ok("Pesan test berhasil dikirim ke chat_id", settings.telegram_chat_id)
            except Exception as e:
                fail("Gagal kirim pesan ke TELEGRAM_CHAT_ID", str(e))
                warn("Pastikan kamu sudah pernah mengirim pesan ke bot terlebih dahulu")

    asyncio.run(_check_telegram())
except Exception as e:
    fail("Telegram bot token tidak valid", str(e))

# ── 6. Copy trader address (jika hybrid/copy_trader) ─────────────────────────
print("\n[ 6 ] Copy Trader")
if settings.strategy in ("copy_trader", "hybrid"):
    if not settings.copy_trader_address or settings.copy_trader_address.startswith("0xTarget"):
        fail("COPY_TRADER_ADDRESS belum diisi", f"strategy={settings.strategy} membutuhkan ini")
    else:
        ok("COPY_TRADER_ADDRESS terisi", settings.copy_trader_address[:20] + "…")
else:
    info("Dilewati", f"strategy={settings.strategy} tidak butuh copy trader")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*55}")
if errors == 0:
    print(f"\033[92m  SEMUA CHECK PASSED — Bot siap dijalankan!\033[0m")
else:
    print(f"\033[91m  {errors} CHECK GAGAL — Perbaiki masalah di atas sebelum menjalankan bot.\033[0m")
print(f"{'='*55}\n")

sys.exit(0 if errors == 0 else 1)
