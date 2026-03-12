"""
main.py
───────
Entry point for the Polymarket trading bot.

Usage:
    # Load the correct env file first, then run:
    TRADING_MODE=fake python main.py
    # Or use the helper scripts:
    ./scripts/run_fake.sh
    ./scripts/run_real.sh
"""

import logging
import os
import sys
from pathlib import Path

import structlog
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

# ── Load env file based on TRADING_MODE ──────────────────────────────────────
_mode = os.environ.get("TRADING_MODE", "fake")
_env_file = Path(__file__).parent / f".env.{_mode}"
if not _env_file.exists():
    print(f"[ERROR] Env file not found: {_env_file}", file=sys.stderr)
    sys.exit(1)
load_dotenv(_env_file, override=True)

# ── Now safe to import config (env is loaded) ─────────────────────────────────
from config.settings import load_settings
from core.claude_agent import ClaudeAgent
from core.executor import Executor
from core.market import MarketService
from core.position_monitor import PositionMonitor
from notifications.telegram import TelegramNotifier
from services.portfolio import PortfolioService
from services.wallet import WalletService

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot.main")

# Quieten noisy libraries
for _lib in ("httpx", "httpcore", "web3", "urllib3"):
    logging.getLogger(_lib).setLevel(logging.WARNING)


def build_clob_client(settings) -> ClobClient:
    creds = ApiCreds(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        api_passphrase=settings.api_passphrase,
    )
    client = ClobClient(
        host=settings.polymarket_host,
        chain_id=settings.chain_id,
        key=settings.wallet_private_key,
        creds=creds,
    )
    return client


class TradingBot:
    def __init__(self) -> None:
        self.settings = load_settings()
        logger.info(
            "Starting bot | mode=%s | dry_run=%s | strategy=%s",
            self.settings.trading_mode,
            self.settings.dry_run,
            self.settings.strategy,
        )

        # ── Services ──────────────────────────────────────────────────────────
        self.clob = build_clob_client(self.settings)
        self.wallet = WalletService(self.settings)
        self.portfolio = PortfolioService(self.settings, self.clob)
        self.market_svc = MarketService(self.settings, self.clob)
        self.agent = ClaudeAgent(self.settings)
        self.executor = Executor(self.settings, self.clob, self.portfolio)
        self.telegram = TelegramNotifier(self.settings)
        self.position_monitor = PositionMonitor(
            self.settings, self.clob, self.portfolio, self.market_svc, self.agent,
        )

        # ── Scheduler ─────────────────────────────────────────────────────────
        self.scheduler = BlockingScheduler(timezone="UTC")

    # ── Main analysis loop ────────────────────────────────────────────────────

    def run_analysis_cycle(self) -> None:
        logger.info("── Analysis cycle start ─────────────────────────────────")

        # 1. Refresh portfolio state
        self.portfolio.refresh()
        portfolio_summary = self.portfolio.get_pnl_summary()

        # 2. Fetch candidate markets
        markets = self.market_svc.get_active_markets(limit=20)
        if not markets:
            logger.warning("No markets returned — skipping cycle")
            return

        # 3. Filter: skip markets we already hold a position in
        held_ids = {p.market_id for p in self.portfolio.positions.values()}
        candidates = [m for m in markets if m.condition_id not in held_ids]

        logger.info(
            "Markets: total=%d | held=%d | candidates=%d",
            len(markets), len(held_ids), len(candidates),
        )

        # 4. Analyse top candidates (limit Claude calls to avoid high cost)
        max_to_analyse = min(5, len(candidates))
        for market in candidates[:max_to_analyse]:
            try:
                ctx = self.market_svc.get_market_context(market)
                decision = self.agent.analyse_market(ctx, portfolio_summary)

                result = self.executor.execute(decision)
                if result and result.success:
                    self.telegram.send_order_notification(result, decision.reasoning)
                elif result and not result.success:
                    logger.warning("Order failed: %s", result.error)

            except Exception as exc:
                logger.exception("Error analysing market %s: %s", market.condition_id, exc)
                self.telegram.send_error("analysis loop", str(exc))

        logger.info("── Analysis cycle end ───────────────────────────────────")

    def run_position_monitor(self) -> None:
        """Check open positions for adverse conditions and close if needed."""
        logger.info("── Position monitor start ──────────────────────────────")
        try:
            results = self.position_monitor.check_positions()
            for r in results:
                if r.get("action") == "CLOSE" and r.get("executed"):
                    self.telegram.send_position_close(r)
                elif r.get("action") == "CLOSE" and not r.get("executed"):
                    logger.warning(
                        "Claude recommended CLOSE but urgency %.2f < threshold for: %s",
                        r.get("urgency", 0), r.get("question", "")[:40],
                    )
        except Exception as exc:
            logger.exception("Position monitor error: %s", exc)
            self.telegram.send_error("position_monitor", str(exc))
        logger.info("── Position monitor end ────────────────────────────────")

    def run_pnl_report(self) -> None:
        self.portfolio.refresh()
        summary = self.portfolio.get_pnl_summary()
        self.telegram.send_pnl_report(summary)

    # ── Startup ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        # Wallet sanity check
        wallet_info = self.wallet.get_summary()
        logger.info("Wallet: %s", wallet_info)

        # Ensure USDC is approved for trading
        self.wallet.ensure_usdc_approval()

        # Send startup notification
        self.telegram.send_startup()

        # Schedule analysis loop
        self.scheduler.add_job(
            self.run_analysis_cycle,
            trigger=IntervalTrigger(minutes=self.settings.analyze_interval_minutes),
            id="analysis",
            name="Market analysis",
            replace_existing=True,
        )

        # Schedule position monitor (runs more frequently than analysis)
        monitor_interval = max(5, self.settings.analyze_interval_minutes // 3)
        self.scheduler.add_job(
            self.run_position_monitor,
            trigger=IntervalTrigger(minutes=monitor_interval),
            id="position_monitor",
            name="Position monitor",
            replace_existing=True,
        )

        # Schedule P&L report
        self.scheduler.add_job(
            self.run_pnl_report,
            trigger=IntervalTrigger(hours=self.settings.pnl_report_interval_hours),
            id="pnl_report",
            name="P&L report",
            replace_existing=True,
        )

        logger.info(
            "Scheduler started | analysis every %d min | monitor every %d min | P&L every %d h",
            self.settings.analyze_interval_minutes,
            monitor_interval,
            self.settings.pnl_report_interval_hours,
        )

        # Run once immediately before entering the blocking scheduler loop
        self.run_analysis_cycle()

        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received")
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        logger.info("Shutting down…")
        self.executor.cancel_all_open_orders()
        self.market_svc.close()
        self.telegram.send_shutdown("user interrupt / process stop")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
