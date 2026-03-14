"""
notifications/telegram.py
─────────────────────────
Telegram bot with interactive commands + push notifications.
Uses python-telegram-bot Application for command handling and notifications.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config.settings import Settings
from core.executor import OrderResult

logger = logging.getLogger(__name__)

# ── Formatting helpers ──────────────────────────────────────────────────────

_MODE = {"fake": "🧪 TESTNET", "real": "💰 MAINNET"}
_STATUS = {True: "✅", False: "❌"}


def _esc(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    special = r"_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


def _mono(text) -> str:
    """Wrap text in monospace for MarkdownV2."""
    return f"`{_esc(str(text))}`"


def _bold(text) -> str:
    return f"*{_esc(str(text))}*"


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._chat_id = settings.telegram_chat_id
        self._bot_token = settings.telegram_bot_token

        # Callbacks — will be set by TradingBot after init
        self._on_scan: Optional[Callable] = None
        self._on_positions: Optional[Callable] = None
        self._on_pnl: Optional[Callable] = None
        self._on_wallet: Optional[Callable] = None

        # Application (built later when start_polling is called)
        self._app: Optional[Application] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        logger.info("TelegramNotifier ready | chat_id=%s", self._chat_id)

    # ── Register callbacks from TradingBot ──────────────────────────────────

    def register_callbacks(
        self,
        on_scan: Callable,
        on_positions: Callable,
        on_pnl: Callable,
        on_wallet: Callable,
    ) -> None:
        self._on_scan = on_scan
        self._on_positions = on_positions
        self._on_pnl = on_pnl
        self._on_wallet = on_wallet

    # ── Telegram Bot Polling (runs in background thread) ────────────────────

    def start_polling(self) -> None:
        """Start the Telegram bot polling in a background thread."""
        self._thread = threading.Thread(target=self._run_polling, daemon=True)
        self._thread.start()
        logger.info("Telegram bot polling started in background thread")

    def _run_polling(self) -> None:
        """Entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._app = (
            Application.builder()
            .token(self._bot_token)
            .build()
        )

        # Register command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("help", self._cmd_help))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self._app.add_handler(CommandHandler("scan", self._cmd_scan))
        self._app.add_handler(CommandHandler("wallet", self._cmd_wallet))

        self._loop.run_until_complete(self._app.initialize())
        self._loop.run_until_complete(self._app.start())
        self._loop.run_until_complete(
            self._app.updater.start_polling(drop_pending_updates=True)
        )
        logger.info("Telegram bot is now accepting commands")
        self._loop.run_forever()

    def stop_polling(self) -> None:
        """Gracefully stop the Telegram bot polling."""
        if self._app and self._loop:
            async def _stop():
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()

            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_stop())
            )
            time.sleep(1)
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Command Handlers ────────────────────────────────────────────────────

    async def _check_auth(self, update: Update) -> bool:
        """Only respond to the configured chat_id."""
        if str(update.effective_chat.id) != str(self._chat_id):
            await update.message.reply_text("⛔ Unauthorized.")
            return False
        return True

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        await self._cmd_help(update, context)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        mode = _MODE.get(self.settings.trading_mode, "UNKNOWN")
        dry = " \\| DRY RUN" if self.settings.dry_run else ""
        text = (
            f"🤖 *Polymarket Trading Bot*\n"
            f"{_esc(mode)}{dry}\n\n"
            f"📋 *Commands:*\n"
            f"/status \\- Bot status & config\n"
            f"/wallet \\- Wallet balances\n"
            f"/positions \\- Open positions\n"
            f"/pnl \\- P&L report\n"
            f"/scan \\- Force market scan now\n"
            f"/help \\- Show this menu"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        mode = _MODE.get(self.settings.trading_mode, "UNKNOWN")
        dry_label = "ON" if self.settings.dry_run else "OFF"
        interval = self.settings.analyze_interval_minutes
        model = self.settings.claude_model
        max_ord = self.settings.max_order_size_usdc
        max_exp = self.settings.max_total_exposure_usdc
        max_pos = self.settings.max_positions
        min_conf = self.settings.min_confidence_score
        lines = [
            f"📊 *Bot Status*\n",
            f"Mode: {_esc(mode)}",
            f"Dry Run: {_mono(dry_label)}",
            f"Strategy: {_mono(self.settings.strategy)}",
            f"Interval: {_mono(f'{interval} min')}",
            f"Model: {_mono(model)}",
            "",
            "⚙️ *Risk Limits*",
            f"Max Order: {_mono(f'${max_ord} USDC')}",
            f"Max Exposure: {_mono(f'${max_exp} USDC')}",
            f"Max Positions: {_mono(max_pos)}",
            f"Min Confidence: {_mono(f'{min_conf:.0%}')}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")

    async def _cmd_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        if not self._on_wallet:
            await update.message.reply_text("⚠️ Wallet service not connected.")
            return

        await update.message.reply_text("🔍 Checking wallet...")
        try:
            info = self._on_wallet()
            addr = info["address"]
            chain_id = info["chain_id"]
            matic = info["matic_balance"]
            usdc = info["usdc_balance"]
            allowance = info["usdc_allowance"]
            lines = [
                "💳 *Wallet*\n",
                f"Address: {_mono(addr)}",
                f"Chain: {_mono(f'Polygon ({chain_id})')}",
                "",
                "💰 *Balances*",
                f"MATIC: {_mono(f'{matic:.4f}')}",
                f"USDC\\.e: {_mono(f'${usdc:.4f}')}",
                f"USDC Allowance: {_mono(f'${allowance:.4f}')}",
            ]
            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        except Exception as exc:
            await update.message.reply_text(f"❌ Error: {exc}")

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        if not self._on_positions:
            await update.message.reply_text("⚠️ Portfolio not connected.")
            return

        await update.message.reply_text("🔍 Fetching positions...")
        try:
            summary = self._on_positions()
            positions = summary.get("positions", [])

            if not positions:
                await update.message.reply_text("📭 No open positions.")
                return

            total_notional = summary.get("total_notional_usdc", 0)
            total_unrealised = summary.get("unrealised_pnl_usdc", 0)
            lines = [
                f"📂 *Open Positions* \\({_esc(str(len(positions)))}\\)\n",
                f"Total Exposure: {_mono(f'${total_notional:.2f} USDC')}",
                f"Unrealised P&L: {_mono(f'${total_unrealised:+.4f} USDC')}",
                "",
            ]

            for i, p in enumerate(positions, 1):
                pnl_pct = p.get("unrealised_pnl_pct", 0)
                icon = "🟢" if pnl_pct >= 0 else "🔴"
                question = _esc(p.get("market_question", "?")[:50])
                side = p.get("side", "?")
                entry = p.get("entry_price", 0)
                current = p.get("current_price", 0)
                size_usdc = p.get("size_usdc", 0)

                lines.append(f"{icon} *{_esc(str(i))}\\. {question}*")
                lines.append(
                    f"   {_esc(side)} \\| "
                    f"Entry: {_mono(f'{entry:.3f}')} → Now: {_mono(f'{current:.3f}')}"
                )
                lines.append(
                    f"   Size: {_mono(f'${size_usdc:.2f}')} \\| "
                    f"P&L: {_mono(f'{pnl_pct:+.1f}%')}"
                )
                lines.append("")

            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        except Exception as exc:
            logger.exception("Error in /positions: %s", exc)
            await update.message.reply_text(f"❌ Error: {exc}")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        if not self._on_pnl:
            await update.message.reply_text("⚠️ Portfolio not connected.")
            return

        try:
            summary = self._on_pnl()
            pnl = summary.get("total_pnl_usdc", 0)
            unrealised = summary.get("unrealised_pnl_usdc", 0)
            realised = summary.get("realised_pnl_usdc", 0)
            open_pos = summary.get("open_positions", 0)
            total_notional = summary.get("total_notional_usdc", 0)
            pnl_icon = "📈" if pnl >= 0 else "📉"
            now = _esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

            lines = [
                f"{pnl_icon} *P&L Report* — {now}\n",
                f"Open Positions: {_mono(open_pos)}",
                f"Total Exposure: {_mono(f'${total_notional:.2f} USDC')}",
                "",
                f"💵 *Unrealised:* {_mono(f'${unrealised:+.4f} USDC')}",
                f"💵 *Realised:* {_mono(f'${realised:+.4f} USDC')}",
                f"{pnl_icon} *Total P&L:* {_mono(f'${pnl:+.4f} USDC')}",
            ]

            positions = summary.get("positions", [])
            if positions:
                lines.append("\n📋 *Positions:*")
                for p in positions[:5]:
                    pnl_pct = p.get("unrealised_pnl_pct", 0)
                    icon = "🟢" if pnl_pct >= 0 else "🔴"
                    q = _esc(p.get("market_question", "?")[:45])
                    side = _esc(p.get("side", "?"))
                    lines.append(f"{icon} {q} \\({side}\\) → {_mono(f'{pnl_pct:+.1f}%')}")

            await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        except Exception as exc:
            logger.exception("Error in /pnl: %s", exc)
            await update.message.reply_text(f"❌ Error: {exc}")

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_auth(update):
            return
        if not self._on_scan:
            await update.message.reply_text("⚠️ Scanner not connected.")
            return

        await update.message.reply_text(
            "🔎 *Scanning markets now\\.\\.\\.*\nThis may take a few minutes\\.",
            parse_mode="MarkdownV2",
        )

        # Run the scan in a thread to avoid blocking the bot
        def _do_scan():
            try:
                self._on_scan()
            except Exception as exc:
                logger.exception("Force scan failed: %s", exc)
                err_text = _mono(str(exc)[:200])
                self._send(f"❌ *Scan failed*\n{err_text}")

        thread = threading.Thread(target=_do_scan, daemon=True)
        thread.start()

    # ── Push Notifications (called from TradingBot / scheduler) ─────────────

    def send_startup(self) -> None:
        mode = _MODE.get(self.settings.trading_mode, self.settings.trading_mode.upper())
        dry = " \\| DRY RUN" if self.settings.dry_run else ""
        interval = self.settings.analyze_interval_minutes
        max_ord = self.settings.max_order_size_usdc
        max_exp = self.settings.max_total_exposure_usdc
        self._send(
            f"🤖 *Bot Started*\n\n"
            f"Mode: {_esc(mode)}{dry}\n"
            f"Strategy: {_mono(self.settings.strategy)}\n"
            f"Interval: {_mono(f'{interval} min')}\n"
            f"Max Order: {_mono(f'${max_ord} USDC')}\n"
            f"Max Exposure: {_mono(f'${max_exp} USDC')}\n\n"
            f"💡 Send /help for commands"
        )

    def send_order_notification(
        self,
        result: OrderResult,
        reasoning: str = "",
        estimated_prob: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        if not self.settings.notify_on_order:
            return

        status_icon = _STATUS[result.success]
        dry_tag = " \\[DRY RUN\\]" if result.dry_run else ""
        action_label = _esc(result.action.replace("BUY_", "BUY "))

        lines = [
            f"{status_icon} *Order: {action_label}*{dry_tag}\n",
        ]

        if result.market_id:
            lines.append(f"Market: {_mono(result.market_id[:20])}")
        lines.append(f"Size: {_mono(f'${result.size_usdc:.2f} USDC')}")
        lines.append(f"Price: {_mono(f'{result.price:.4f}')}")

        if estimated_prob > 0:
            prob_str = f"{estimated_prob:.1%}"
            lines.append(f"Est\\. Prob: {_mono(prob_str)}")
        if confidence > 0:
            conf_str = f"{confidence:.1%}"
            lines.append(f"Confidence: {_mono(conf_str)}")
        if result.order_id:
            lines.append(f"Order ID: {_mono(result.order_id[:24])}")
        if not result.success and result.error:
            lines.append(f"\n⚠️ Error: {_esc(result.error[:200])}")
        if reasoning:
            lines.append(f"\n📝 {_esc(reasoning[:300])}")

        self._send("\n".join(lines))

    def send_pnl_report(self, summary: dict) -> None:
        if not self.settings.notify_on_pnl:
            return

        pnl = summary.get("total_pnl_usdc", 0)
        unrealised = summary.get("unrealised_pnl_usdc", 0)
        realised = summary.get("realised_pnl_usdc", 0)
        open_pos = summary.get("open_positions", 0)
        total_notional = summary.get("total_notional_usdc", 0)
        pnl_icon = "📈" if pnl >= 0 else "📉"
        now = _esc(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

        lines = [
            f"{pnl_icon} *P&L Report* — {now}\n",
            f"Open Positions: {_mono(open_pos)}",
            f"Total Exposure: {_mono(f'${total_notional:.2f} USDC')}",
            f"Unrealised: {_mono(f'${unrealised:+.4f} USDC')}",
            f"Realised: {_mono(f'${realised:+.4f} USDC')}",
            f"*Total P&L: {_mono(f'${pnl:+.4f} USDC')}*",
        ]

        positions = summary.get("positions", [])
        if positions:
            lines.append("\n📋 *Positions:*")
            for p in positions[:5]:
                pnl_pct = p.get("unrealised_pnl_pct", 0)
                icon = "🟢" if pnl_pct >= 0 else "🔴"
                q = _esc(p.get("market_question", p.get("question", "?"))[:45])
                side = _esc(p.get("side", p.get("outcome", "?")))
                lines.append(f"{icon} {q} \\({side}\\) → {_mono(f'{pnl_pct:+.1f}%')}")

        self._send("\n".join(lines))

    def send_position_close(self, result: dict) -> None:
        urgency = result.get("urgency", 0)
        icon = "🔴" if urgency >= 0.9 else "🟠"
        question = _esc(result.get("question", "Unknown")[:60])
        reasoning = _esc(result.get("reasoning", "")[:250])
        pnl_pct = result.get("unrealised_pnl_pct", 0)

        lines = [
            f"{icon} *Position Closed*\n",
            f"Market: {question}",
            f"Urgency: {_mono(f'{urgency:.2f}')}",
            f"P&L: {_mono(f'{pnl_pct:+.1f}%')}",
            f"\n📝 {reasoning}",
        ]
        self._send("\n".join(lines))

    def send_error(self, context: str, error: str) -> None:
        self._send(
            f"⚠️ *Error in {_esc(context)}*\n"
            f"{_mono(error[:300])}"
        )

    def send_shutdown(self, reason: str = "manual") -> None:
        self._send(f"🛑 *Bot stopped* — {_esc(reason)}")

    def send_scan_result(self, analysed: int, traded: int) -> None:
        """Notify after a scan cycle completes."""
        self._send(
            f"📊 *Scan Complete*\n"
            f"Markets analysed: {_mono(analysed)}\n"
            f"Orders placed: {_mono(traded)}"
        )

    # ── Internal send (thread-safe, works from any thread) ──────────────────

    def _send(self, text: str) -> None:
        """Send a MarkdownV2 message. Works from any thread."""
        async def _do_send():
            async with Bot(token=self._bot_token) as bot:
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode="MarkdownV2",
                )
        try:
            asyncio.run(_do_send())
        except RuntimeError:
            # Inside an existing event loop — create a new one
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_do_send())
            loop.close()
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
