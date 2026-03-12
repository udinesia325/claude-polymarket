"""
core/executor.py
────────────────
Order execution layer.
Validates decisions against risk limits, then submits limit orders via
py-clob-client. Supports DRY_RUN mode (logs only, no real orders).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

from config.settings import Settings
from core.claude_agent import TradeDecision
from services.portfolio import PortfolioService

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    market_id: str
    action: str
    token_id: str
    size_usdc: float
    price: float
    dry_run: bool
    error: Optional[str] = None
    placed_at: str = ""

    def __post_init__(self):
        if not self.placed_at:
            self.placed_at = datetime.now(timezone.utc).isoformat()


class Executor:
    def __init__(
        self,
        settings: Settings,
        clob_client: ClobClient,
        portfolio: PortfolioService,
    ) -> None:
        self.settings = settings
        self.clob = clob_client
        self.portfolio = portfolio

    def execute(self, decision: TradeDecision) -> Optional[OrderResult]:
        """
        Validate and execute a TradeDecision.
        Returns OrderResult or None if the decision was SKIP / blocked.
        """
        if decision.is_skip:
            logger.debug("Decision is SKIP — nothing to execute")
            return None

        # ── Confidence gate ───────────────────────────────────────────────────
        if decision.confidence < self.settings.min_confidence_score:
            logger.info(
                "Skipping order — confidence %.2f < threshold %.2f",
                decision.confidence,
                self.settings.min_confidence_score,
            )
            return None

        # ── Risk gate ─────────────────────────────────────────────────────────
        allowed, reason = self.portfolio.can_open_position(decision.size_usdc)
        if not allowed:
            logger.warning("Order blocked by risk manager: %s", reason)
            return OrderResult(
                success=False,
                order_id=None,
                market_id=decision.market_id,
                action=decision.action,
                token_id=decision.token_id,
                size_usdc=decision.size_usdc,
                price=decision.price,
                dry_run=self.settings.dry_run,
                error=f"Risk limit: {reason}",
            )

        # ── DRY RUN ───────────────────────────────────────────────────────────
        if self.settings.dry_run:
            logger.info(
                "[DRY_RUN] Would place %s | token=%s | size=%.2f USDC | price=%.4f",
                decision.action,
                decision.token_id[:12],
                decision.size_usdc,
                decision.price,
            )
            return OrderResult(
                success=True,
                order_id="DRY_RUN_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                market_id=decision.market_id,
                action=decision.action,
                token_id=decision.token_id,
                size_usdc=decision.size_usdc,
                price=decision.price,
                dry_run=True,
            )

        # ── Live order ────────────────────────────────────────────────────────
        return self._place_order(decision)

    def _place_order(self, decision: TradeDecision) -> OrderResult:
        """Submit a limit GTC order to the Polymarket CLOB."""
        # Convert USDC size → share quantity at the given price
        # shares = usdc / price  (each share costs `price` USDC)
        if decision.price <= 0:
            return OrderResult(
                success=False,
                order_id=None,
                market_id=decision.market_id,
                action=decision.action,
                token_id=decision.token_id,
                size_usdc=decision.size_usdc,
                price=decision.price,
                dry_run=False,
                error="Invalid price (≤ 0)",
            )

        size_shares = round(decision.size_usdc / decision.price, 4)

        order_args = OrderArgs(
            token_id=decision.token_id,
            price=decision.price,
            size=size_shares,
            side="BUY",
        )

        try:
            resp = self.clob.create_and_post_order(order_args)
            order_id = resp.get("orderID") or resp.get("order_id") or str(resp)
            logger.info(
                "Order placed | id=%s | %s | token=%s | %.4f shares @ %.4f",
                order_id,
                decision.action,
                decision.token_id[:12],
                size_shares,
                decision.price,
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                market_id=decision.market_id,
                action=decision.action,
                token_id=decision.token_id,
                size_usdc=decision.size_usdc,
                price=decision.price,
                dry_run=False,
            )
        except Exception as exc:
            logger.error("Order submission failed: %s", exc)
            return OrderResult(
                success=False,
                order_id=None,
                market_id=decision.market_id,
                action=decision.action,
                token_id=decision.token_id,
                size_usdc=decision.size_usdc,
                price=decision.price,
                dry_run=False,
                error=str(exc),
            )

    def cancel_all_open_orders(self) -> bool:
        """Cancel all open orders. Used on shutdown or error recovery."""
        if self.settings.dry_run:
            logger.info("[DRY_RUN] Would cancel all open orders")
            return True
        try:
            self.clob.cancel_all()
            logger.info("All open orders cancelled")
            return True
        except Exception as exc:
            logger.error("Failed to cancel orders: %s", exc)
            return False
