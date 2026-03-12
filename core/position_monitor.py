"""
core/position_monitor.py
────────────────────────
Periodic position health checker.
Scrapes fresh market data for every open position and asks Claude whether
any position should be closed (e.g. adverse news, collapsing probability).
"""

import logging
from decimal import Decimal

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

from config.settings import Settings
from core.claude_agent import ClaudeAgent
from core.market import MarketService
from services.portfolio import PortfolioService, Position

logger = logging.getLogger(__name__)

# If Claude says urgency >= this threshold, we close immediately
URGENCY_THRESHOLD = 0.7


class PositionMonitor:
    def __init__(
        self,
        settings: Settings,
        clob_client: ClobClient,
        portfolio: PortfolioService,
        market_svc: MarketService,
        agent: ClaudeAgent,
    ) -> None:
        self.settings = settings
        self.clob = clob_client
        self.portfolio = portfolio
        self.market_svc = market_svc
        self.agent = agent

    def check_positions(self) -> list[dict]:
        """
        Iterate over all open positions, gather fresh market data,
        and ask Claude if any should be closed.

        Returns a list of action summaries for logging / notifications.
        """
        self.portfolio.refresh()
        positions = self.portfolio.positions

        if not positions:
            logger.debug("No open positions to monitor")
            return []

        results = []
        for token_id, pos in positions.items():
            try:
                result = self._evaluate_position(pos)
                results.append(result)
            except Exception as exc:
                logger.error(
                    "Error evaluating position %s (%s): %s",
                    token_id[:12], pos.question[:40], exc,
                )
                results.append({
                    "token_id": token_id,
                    "question": pos.question,
                    "action": "HOLD",
                    "reasoning": f"Evaluation error: {exc}",
                    "urgency": 0.0,
                    "executed": False,
                })

        closed = [r for r in results if r.get("action") == "CLOSE" and r.get("executed")]
        if closed:
            logger.info("Position monitor closed %d position(s)", len(closed))

        return results

    def _evaluate_position(self, pos: Position) -> dict:
        """Gather market context for a position and ask Claude for risk assessment."""
        # Build position summary for Claude
        position_data = {
            "market_id": pos.market_id,
            "question": pos.question,
            "our_side": pos.outcome,
            "token_id": pos.token_id,
            "shares": float(pos.size),
            "entry_price": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "unrealised_pnl_usdc": float(pos.unrealised_pnl),
            "unrealised_pnl_pct": pos.unrealised_pnl_pct,
            "opened_at": pos.opened_at.isoformat(),
        }

        # Gather fresh market context (news, order book)
        market_context = self._get_position_market_context(pos)

        # Ask Claude
        assessment = self.agent.analyse_position_risk(position_data, market_context)

        action = assessment.get("action", "HOLD")
        urgency = float(assessment.get("urgency", 0.0))
        reasoning = assessment.get("reasoning", "")

        logger.info(
            "Position check | %s | %s | urgency=%.2f | pnl=%.2f%% | %s",
            pos.question[:40],
            action,
            urgency,
            pos.unrealised_pnl_pct,
            reasoning[:80],
        )

        executed = False
        if action == "CLOSE" and urgency >= URGENCY_THRESHOLD:
            executed = self._close_position(pos)

        return {
            "token_id": pos.token_id,
            "question": pos.question,
            "action": action,
            "reasoning": reasoning,
            "urgency": urgency,
            "unrealised_pnl_pct": pos.unrealised_pnl_pct,
            "executed": executed,
        }

    def _get_position_market_context(self, pos: Position) -> dict:
        """Build a lightweight market context for an existing position."""
        order_book = {}
        try:
            order_book = self.clob.get_order_book(token_id=pos.token_id)
        except Exception as exc:
            logger.debug("Could not fetch order book for %s: %s", pos.token_id[:12], exc)

        book_summary = self.market_svc._summarise_order_book(order_book)

        news = self.market_svc.scrape_news_for_market(pos.question)
        news_data = [
            {"title": n.title, "source": n.source, "published": n.published}
            for n in news
        ]

        return {
            "order_book": book_summary,
            "news": news_data if news_data else [{"title": "No recent news", "source": "-", "published": "-"}],
        }

    def _close_position(self, pos: Position) -> bool:
        """
        Sell/close a position by placing a sell order at best bid.
        Returns True if the order was placed successfully.
        """
        if self.settings.dry_run:
            logger.info(
                "[DRY_RUN] Would close position: %s | %s | %.4f shares",
                pos.question[:40], pos.outcome, float(pos.size),
            )
            return True

        # Get best bid to sell at
        try:
            book = self.clob.get_order_book(token_id=pos.token_id)
            bids = book.get("bids", [])
            if not bids:
                logger.warning("No bids available to close position %s", pos.token_id[:12])
                return False
            sell_price = float(bids[0]["price"])
        except Exception as exc:
            logger.error("Failed to get order book for close: %s", exc)
            return False

        order_args = OrderArgs(
            token_id=pos.token_id,
            price=sell_price,
            size=float(pos.size),
            side="SELL",
        )

        try:
            resp = self.clob.create_and_post_order(order_args)
            order_id = resp.get("orderID") or resp.get("order_id") or str(resp)
            logger.info(
                "Position closed | order_id=%s | %s | %s @ %.4f",
                order_id, pos.question[:40], pos.outcome, sell_price,
            )

            # Track realised P&L
            pnl = pos.size * (Decimal(str(sell_price)) - pos.avg_entry_price)
            self.portfolio.add_realised_pnl(pnl)

            return True
        except Exception as exc:
            logger.error("Failed to close position %s: %s", pos.token_id[:12], exc)
            return False
