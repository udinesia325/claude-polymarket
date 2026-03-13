"""
notifications/telegram.py
─────────────────────────
Telegram notification service.
Sends alerts for order placements and periodic P&L reports.
Uses python-telegram-bot in sync (non-async) mode via the Bot.send_message
convenience wrapper so it works cleanly inside APScheduler jobs.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import telegram  # python-telegram-bot

from config.settings import Settings
from core.executor import OrderResult

logger = logging.getLogger(__name__)

# Emoji shortcuts
_MODE = {"fake": "🧪 FAKE", "real": "💰 REAL"}
_STATUS = {True: "✅", False: "❌"}


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._chat_id = settings.telegram_chat_id
        logger.info("TelegramNotifier ready | chat_id=%s", self._chat_id)

    # ── Public helpers ────────────────────────────────────────────────────────

    def send_startup(self) -> None:
        mode_label = _MODE.get(self.settings.trading_mode, self.settings.trading_mode.upper())
        dry = " | DRY_RUN=ON" if self.settings.dry_run else ""
        self._send(
            f"🤖 *Polymarket Bot Started*\n"
            f"Mode: {mode_label}{dry}\n"
            f"Strategy: `{self.settings.strategy}`\n"
            f"Interval: every {self.settings.analyze_interval_minutes} min\n"
            f"Max order: ${self.settings.max_order_size_usdc} USDC\n"
            f"Max exposure: ${self.settings.max_total_exposure_usdc} USDC"
        )

    def send_order_notification(self, result: OrderResult, reasoning: str = "") -> None:
        if not self.settings.notify_on_order:
            return

        status_icon = _STATUS[result.success]
        dry_tag = " `[DRY RUN]`" if result.dry_run else ""

        lines = [
            f"{status_icon} *Order {result.action}*{dry_tag}",
            f"Market: `{result.market_id[:16]}…`",
            f"Token: `{result.token_id[:16]}…`",
            f"Size: `${result.size_usdc:.2f} USDC`",
            f"Price: `{result.price:.4f}`",
        ]
        if result.order_id:
            lines.append(f"Order ID: `{result.order_id[:20]}`")
        if not result.success and result.error:
            lines.append(f"Error: _{result.error}_")
        if reasoning:
            lines.append(f"\n📝 _{reasoning[:200]}_")

        self._send("\n".join(lines))

    def send_pnl_report(self, summary: dict) -> None:
        if not self.settings.notify_on_pnl:
            return

        pnl = summary.get("total_pnl_usdc", 0)
        unrealised = summary.get("unrealised_pnl_usdc", 0)
        realised = summary.get("realised_pnl_usdc", 0)
        pnl_icon = "📈" if pnl >= 0 else "📉"

        lines = [
            f"{pnl_icon} *P&L Report* — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"Open positions: `{summary.get('open_positions', 0)}`",
            f"Total exposure: `${summary.get('total_notional_usdc', 0):.2f} USDC`",
            f"Unrealised P&L: `${unrealised:+.4f} USDC`",
            f"Realised P&L: `${realised:+.4f} USDC`",
            f"*Total P&L: `${pnl:+.4f} USDC`*",
        ]

        positions = summary.get("positions", [])
        if positions:
            lines.append("\n*Open positions:*")
            for p in positions[:5]:
                pnl_pct = p.get("unrealised_pnl_pct", 0)
                icon = "🟢" if pnl_pct >= 0 else "🔴"
                lines.append(
                    f"{icon} `{p.get('market_question', p.get('question', '?'))[:40]}` "
                    f"({p.get('side', p.get('outcome', '?'))}) → `{pnl_pct:+.1f}%`"
                )

        self._send("\n".join(lines))

    def send_position_close(self, result: dict) -> None:
        """Notify when the position monitor closes a position."""
        urgency = result.get("urgency", 0)
        icon = "🔴" if urgency >= 0.9 else "🟠"
        lines = [
            f"{icon} *Position Closed*",
            f"Market: _{result.get('question', 'Unknown')[:60]}_",
            f"Urgency: `{urgency:.2f}`",
            f"P&L: `{result.get('unrealised_pnl_pct', 0):+.1f}%`",
            f"Reason: _{result.get('reasoning', '')[:200]}_",
        ]
        self._send("\n".join(lines))

    def send_error(self, context: str, error: str) -> None:
        self._send(f"⚠️ *Error in {context}*\n`{error[:300]}`")

    def send_shutdown(self, reason: str = "manual") -> None:
        self._send(f"🛑 *Bot stopped* — reason: {reason}")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        async def _do_send():
            async with telegram.Bot(token=self.settings.telegram_bot_token) as bot:
                await bot.send_message(
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode="Markdown",
                )
        try:
            asyncio.run(_do_send())
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
