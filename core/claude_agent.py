"""
core/claude_agent.py
────────────────────
Claude AI decision engine.
Given market context (prices, news, copy-trader data), Claude returns
a structured trading decision with confidence score and reasoning.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import anthropic

from config.settings import Settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """\
You are an expert prediction market trader on Polymarket.
Your job is to analyse market data and decide whether to place a trade.

Today's date: {today}

## How Polymarket works
- Each market is a binary question that resolves YES or NO.
- YES price = implied probability the event happens (e.g. YES @ $0.70 = 70% chance).
- NO price = 1 − YES price.
- You profit when you buy a side at a price LOWER than the true probability.
  Example: if you believe the true probability is 85% and YES is trading at $0.70,
  buying YES is +EV because you pay $0.70 for something worth ~$0.85.

## What you receive
1. Market metadata (question, end date, prices, liquidity, 24h volume)
2. Order book depth summary (best bid/ask, depth at 3 levels)
3. Recent news headlines related to the market
4. (Optional) Copy-trader positions and recent trades
5. Current portfolio state (open positions, exposure, P&L)

## Your output
Return ONLY a valid JSON object — no markdown, no commentary:
{{
  "action": "BUY_YES" | "BUY_NO" | "SKIP",
  "confidence": <float 0.0–1.0>,
  "size_usdc": <float, suggested order size in USDC>,
  "price": <float 0.0–1.0, limit price to bid>,
  "reasoning": "<1-3 sentence explanation of your edge>",
  "risk_factors": ["<factor1>", "<factor2>"]
}}

## Trading rules
1. Only recommend BUY_YES or BUY_NO when confidence >= {min_confidence}.
   Below that threshold, always return SKIP.
2. Your "confidence" reflects how sure you are that the TRUE probability
   differs meaningfully from the current market price — not just your
   estimate of the event probability itself.
3. Size your position proportionally to your edge:
   - Small edge (confidence 0.7–0.8): use 30–50% of max_order_size.
   - Medium edge (0.8–0.9): use 50–75%.
   - Strong edge (>0.9): up to 100% of max_order_size.
4. Never suggest size_usdc above the max_order_size you are given.
5. Prefer markets with liquidity > $1,000 USDC to ensure fills.
6. Set your limit price between best_bid and best_ask (the spread).
   Never bid more than 5 percentage points above best_ask.
7. If the market resolves within 24 hours and outcome is still uncertain, SKIP —
   resolution risk outweighs potential edge.
8. If we already hold a position in this market, SKIP to avoid doubling exposure.
9. Use copy-trader positions as a confirming signal, not the sole reason to trade.
10. If news is stale (>48h) or absent, weigh order book and price action more heavily.
"""


@dataclass
class TradeDecision:
    action: str             # "BUY_YES" | "BUY_NO" | "SKIP"
    confidence: float
    size_usdc: float
    price: float            # limit price (0–1)
    reasoning: str
    risk_factors: list[str]
    market_id: str
    yes_token_id: str
    no_token_id: str

    @property
    def is_skip(self) -> bool:
        return self.action == "SKIP"

    @property
    def token_id(self) -> str:
        """Return the token_id for the chosen side."""
        if self.action == "BUY_YES":
            return self.yes_token_id
        if self.action == "BUY_NO":
            return self.no_token_id
        return ""

    @property
    def side(self) -> str:
        return "BUY" if not self.is_skip else "SKIP"


class ClaudeAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        logger.info("ClaudeAgent initialised | model=%s", settings.claude_model)

    def _build_system_prompt(self) -> str:
        return SYSTEM_PROMPT_TEMPLATE.format(
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            min_confidence=self.settings.min_confidence_score,
        )

    def analyse_market(
        self,
        market_context: dict,
        portfolio_summary: dict,
    ) -> TradeDecision:
        """
        Send market context to Claude and parse a TradeDecision.

        market_context: dict built by MarketService.get_market_context()
        portfolio_summary: dict from PortfolioService.get_pnl_summary()
        """
        market = market_context["market"]

        user_message = self._build_user_message(market_context, portfolio_summary)

        logger.debug("Sending market %r to Claude", market["question"][:60])

        try:
            response = self.client.messages.create(
                model=self.settings.claude_model,
                max_tokens=1024,
                temperature=0,
                system=self._build_system_prompt(),
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = response.content[0].text.strip()
        except Exception as exc:
            logger.error("Claude API call failed: %s", exc)
            return self._skip_decision(market, reason=f"Claude API error: {exc}")

        return self._parse_response(raw_text, market)

    def analyse_position_risk(
        self,
        position: dict,
        market_context: dict,
    ) -> dict:
        """
        Ask Claude whether an existing position should be closed.
        Returns {"action": "CLOSE" | "HOLD", "reasoning": "...", "urgency": 0.0-1.0}
        """
        prompt = (
            "You are monitoring an open prediction market position.\n"
            "Given the position details and current market data, decide if "
            "the position should be CLOSED (sold) or HELD.\n\n"
            "Return ONLY a JSON object:\n"
            '{"action": "CLOSE" | "HOLD", "reasoning": "<1-2 sentences>", '
            '"urgency": <float 0.0-1.0>}\n\n'
            "CLOSE when:\n"
            "- New information significantly reduces the probability of our side winning\n"
            "- The position has lost >30% of its value and trend is worsening\n"
            "- Market is about to resolve and our side is clearly losing\n"
            "- Breaking news contradicts our position thesis\n\n"
            "HOLD when:\n"
            "- Fundamentals still support our position\n"
            "- Price dip is temporary / noise\n"
            "- No significant new information\n\n"
            f"## Position\n{json.dumps(position, indent=2)}\n\n"
            f"## Current Market Data\n{json.dumps(market_context, indent=2)}"
        )

        try:
            response = self.client.messages.create(
                model=self.settings.claude_model,
                max_tokens=512,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = self._strip_code_fences(response.content[0].text.strip())
            return json.loads(raw)
        except Exception as exc:
            logger.error("Position risk analysis failed: %s", exc)
            return {"action": "HOLD", "reasoning": f"Analysis error: {exc}", "urgency": 0.0}

    def _build_user_message(self, ctx: dict, portfolio: dict) -> str:
        market = ctx["market"]
        order_book = ctx.get("order_book_summary", {})
        news = ctx.get("news", [])
        copy_positions = ctx.get("copy_trader_positions", [])
        copy_trades = ctx.get("copy_trader_recent_trades", [])

        parts = [
            f"## Market\n{json.dumps(market, indent=2)}",
            f"## Order Book Summary\n{json.dumps(order_book, indent=2)}",
        ]

        if news:
            news_lines = "\n".join(
                f"- [{n['source']}] {n['title']} ({n['published']})" for n in news[:5]
            )
            parts.append(f"## Relevant News\n{news_lines}")
        else:
            parts.append("## Relevant News\nNo recent news found.")

        if copy_positions:
            parts.append(
                f"## Copy-Trader Open Positions (top 5)\n"
                + json.dumps(copy_positions[:5], indent=2)
            )

        if copy_trades:
            parts.append(
                f"## Copy-Trader Recent Trades (last 5)\n"
                + json.dumps(copy_trades[:5], indent=2)
            )

        # Detailed portfolio with per-position breakdown
        position_details = portfolio.get("positions", [])
        held_markets = []
        for pos in position_details:
            held_markets.append(
                f"  - {pos.get('market_question', 'Unknown')[:60]} | "
                f"side={pos.get('side', '?')} | "
                f"size=${pos.get('size_usdc', 0):.2f} | "
                f"entry={pos.get('entry_price', 0):.3f} | "
                f"current={pos.get('current_price', 0):.3f} | "
                f"pnl=${pos.get('unrealised_pnl', 0):.2f}"
            )

        positions_block = "\n".join(held_markets) if held_markets else "  (none)"

        parts.append(
            f"## Current Portfolio State\n"
            f"Open positions: {portfolio.get('open_positions', 0)}\n"
            f"Total exposure: ${portfolio.get('total_notional_usdc', 0):.2f} USDC\n"
            f"Unrealised P&L: ${portfolio.get('unrealised_pnl_usdc', 0):.2f} USDC\n"
            f"Max order size: ${self.settings.max_order_size_usdc:.2f} USDC\n"
            f"Max total exposure: ${self.settings.max_total_exposure_usdc:.2f} USDC\n"
            f"Held positions:\n{positions_block}"
        )

        return "\n\n".join(parts)

    @staticmethod
    def _strip_code_fences(raw: str) -> str:
        """Remove markdown code fences from Claude's response."""
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
        if match:
            return match.group(1).strip()
        return raw.strip()

    def _parse_response(self, raw: str, market: dict) -> TradeDecision:
        raw = self._strip_code_fences(raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("Could not parse Claude response as JSON: %s\n%s", exc, raw)
            return self._skip_decision(market, reason="JSON parse error")

        action = data.get("action", "SKIP")
        if action not in ("BUY_YES", "BUY_NO", "SKIP"):
            logger.warning("Unknown action %r from Claude, defaulting to SKIP", action)
            action = "SKIP"

        confidence = float(data.get("confidence", 0.0))
        size_usdc = min(
            float(data.get("size_usdc", self.settings.max_order_size_usdc)),
            self.settings.max_order_size_usdc,
        )
        price = max(0.01, min(0.99, float(data.get("price", 0.5))))

        decision = TradeDecision(
            action=action,
            confidence=confidence,
            size_usdc=size_usdc,
            price=price,
            reasoning=data.get("reasoning", ""),
            risk_factors=data.get("risk_factors", []),
            market_id=market.get("condition_id", ""),
            yes_token_id=market.get("yes_token_id", ""),
            no_token_id=market.get("no_token_id", ""),
        )

        logger.info(
            "Claude decision | action=%s | confidence=%.2f | size=%.2f | market=%r",
            decision.action,
            decision.confidence,
            decision.size_usdc,
            market.get("question", "")[:50],
        )
        return decision

    def _skip_decision(self, market: dict, reason: str = "") -> TradeDecision:
        return TradeDecision(
            action="SKIP",
            confidence=0.0,
            size_usdc=0.0,
            price=0.0,
            reasoning=reason,
            risk_factors=[],
            market_id=market.get("condition_id", ""),
            yes_token_id=market.get("yes_token_id", ""),
            no_token_id=market.get("no_token_id", ""),
        )
