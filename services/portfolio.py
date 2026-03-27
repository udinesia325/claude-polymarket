"""
services/portfolio.py
─────────────────────
Tracks open positions, realised / unrealised P&L, and enforces risk limits.
Uses py-clob-client to fetch live position data from Polymarket.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from py_clob_client.client import ClobClient

from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class Position:
    market_id: str          # condition_id or market slug
    question: str           # human-readable question
    outcome: str            # "YES" or "NO"
    token_id: str           # Polymarket outcome token id
    size: Decimal           # number of shares held
    avg_entry_price: Decimal
    current_price: Decimal = Decimal("0")
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def notional(self) -> Decimal:
        return self.size * self.avg_entry_price

    @property
    def unrealised_pnl(self) -> Decimal:
        return self.size * (self.current_price - self.avg_entry_price)

    @property
    def unrealised_pnl_pct(self) -> float:
        if self.avg_entry_price == 0:
            return 0.0
        return float((self.current_price - self.avg_entry_price) / self.avg_entry_price * 100)


class PortfolioService:
    def __init__(self, settings: Settings, clob_client: ClobClient) -> None:
        self.settings = settings
        self.clob = clob_client
        self._positions: dict[str, Position] = {}   # token_id → Position
        self._virtual_positions: dict[str, Position] = {}  # dry-run positions
        self._realised_pnl: Decimal = Decimal("0")
        self._session_start = datetime.now(timezone.utc)

    # ── Position management ───────────────────────────────────────────────────

    def refresh(self) -> None:
        """Pull latest positions + prices from Polymarket CLOB."""
        try:
            trades = self.clob.get_trades(
                maker_address=self.settings.wallet_address
            )
        except Exception as exc:
            logger.warning("Failed to refresh portfolio: %s", exc)
            return

        # Rebuild positions from trade history (simplified – net size per token)
        net: dict[str, dict] = {}
        for t in trades:
            tid = t.get("asset_id", "")
            if not tid:
                continue
            side_sign = Decimal("1") if t.get("side") == "BUY" else Decimal("-1")
            size = Decimal(str(t.get("size", 0)))
            price = Decimal(str(t.get("price", 0)))

            if tid not in net:
                net[tid] = {
                    "market_id": t.get("market", ""),
                    "question": t.get("question", ""),
                    "outcome": t.get("outcome", ""),
                    "token_id": tid,
                    "total_size": Decimal("0"),
                    "total_cost": Decimal("0"),
                }
            net[tid]["total_size"] += side_sign * size
            if side_sign > 0:
                net[tid]["total_cost"] += size * price

        new_positions: dict[str, Position] = {}
        for tid, data in net.items():
            if data["total_size"] <= Decimal("0.001"):
                continue  # effectively closed
            avg = (
                data["total_cost"] / data["total_size"]
                if data["total_size"] > 0
                else Decimal("0")
            )
            pos = Position(
                market_id=data["market_id"],
                question=data["question"],
                outcome=data["outcome"],
                token_id=tid,
                size=data["total_size"],
                avg_entry_price=avg,
            )
            # Preserve opened_at from previous snapshot if same position
            if tid in self._positions:
                pos.opened_at = self._positions[tid].opened_at
            new_positions[tid] = pos

        self._positions = new_positions
        # Merge virtual (dry-run) positions into real positions
        for tid, vpos in self._virtual_positions.items():
            if tid not in self._positions:
                self._positions[tid] = vpos
        self._update_current_prices()
        logger.info(
            "Portfolio refreshed | real=%d | virtual=%d | total=%d",
            len(new_positions), len(self._virtual_positions), len(self._positions),
        )

    def _update_current_prices(self) -> None:
        for tid, pos in self._positions.items():
            try:
                book = self.clob.get_order_book(token_id=tid)
                # Handle both OrderBookSummary object and plain dict
                if hasattr(book, "bids"):
                    bids = book.bids or []
                    best_bid = bids[0].price if bids else None
                else:
                    bids = book.get("bids", []) if book else []
                    best_bid = bids[0].get("price") if bids else None
                if best_bid is not None:
                    pos.current_price = Decimal(str(best_bid))
            except Exception as exc:
                logger.debug("Could not update price for %s: %s", tid, exc)

    # ── Risk checks ───────────────────────────────────────────────────────────

    def can_open_position(self, order_size_usdc: float) -> tuple[bool, str]:
        """Return (allowed, reason). Called before placing a new order."""
        if len(self._positions) >= self.settings.max_positions:
            return False, f"Max positions reached ({self.settings.max_positions})"

        total_exposure = sum(float(p.notional) for p in self._positions.values())
        if total_exposure + order_size_usdc > self.settings.max_total_exposure_usdc:
            return False, (
                f"Total exposure limit would be exceeded "
                f"(current={total_exposure:.2f}, limit={self.settings.max_total_exposure_usdc})"
            )

        if order_size_usdc > self.settings.max_order_size_usdc:
            return False, (
                f"Order size {order_size_usdc} > max {self.settings.max_order_size_usdc}"
            )

        return True, "ok"

    # ── P&L summary ───────────────────────────────────────────────────────────

    def get_pnl_summary(self) -> dict:
        unrealised = sum(float(p.unrealised_pnl) for p in self._positions.values())
        total_notional = sum(float(p.notional) for p in self._positions.values())
        positions_list = [
            {
                "market_id": p.market_id,
                "market_question": p.question[:60],
                "side": p.outcome,
                "size": float(p.size),
                "size_usdc": float(p.notional),
                "entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealised_pnl": float(p.unrealised_pnl),
                "unrealised_pnl_pct": p.unrealised_pnl_pct,
            }
            for p in self._positions.values()
        ]
        return {
            "session_start": self._session_start.isoformat(),
            "open_positions": len(self._positions),
            "total_notional_usdc": round(total_notional, 4),
            "unrealised_pnl_usdc": round(unrealised, 4),
            "realised_pnl_usdc": float(self._realised_pnl),
            "total_pnl_usdc": round(unrealised + float(self._realised_pnl), 4),
            "positions": positions_list,
        }

    def add_virtual_position(
        self,
        market_id: str,
        question: str,
        outcome: str,
        token_id: str,
        size_usdc: float,
        price: float,
    ) -> None:
        """Register a dry-run position so deduplication and risk limits work."""
        size = Decimal(str(size_usdc)) / Decimal(str(price)) if price > 0 else Decimal("0")
        pos = Position(
            market_id=market_id,
            question=question,
            outcome=outcome,
            token_id=token_id,
            size=size,
            avg_entry_price=Decimal(str(price)),
            current_price=Decimal(str(price)),
        )
        self._virtual_positions[token_id] = pos
        # Also add to live positions immediately
        self._positions[token_id] = pos
        logger.info(
            "Virtual position added | market=%s | %s | $%.2f",
            question[:40], outcome, size_usdc,
        )

    def remove_virtual_position(self, token_id: str) -> None:
        """Remove a virtual position (e.g. when closing a dry-run position)."""
        self._virtual_positions.pop(token_id, None)
        self._positions.pop(token_id, None)

    def add_realised_pnl(self, amount: Decimal) -> None:
        self._realised_pnl += amount

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions
