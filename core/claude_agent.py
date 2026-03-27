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
Your job is to analyse market data and estimate the TRUE probability of events.

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
3. Recent news articles (title + content snippets) related to the market
4. (Optional) Copy-trader positions and recent trades
5. (Optional) Whale activity (top profitable wallets' positions on this market)
6. Current portfolio state (open positions, exposure, P&L)

## Your output
Return ONLY a valid JSON object — no markdown, no commentary:
{{
  "action": "BUY_YES" | "BUY_NO" | "SKIP",
  "estimated_probability": <float 0.0–1.0, your estimate of the TRUE probability of YES>,
  "confidence": <float 0.0–1.0, how confident you are in your estimate>,
  "price": <float 0.0–1.0, limit price to bid>,
  "reasoning": "<1-3 sentence explanation of your edge, referencing specific news or data>",
  "risk_factors": ["<factor1>", "<factor2>"]
}}

NOTE: You do NOT need to provide size_usdc — position sizing is calculated
automatically using the Kelly Criterion based on your estimated_probability.

## Trading rules
1. Only recommend BUY_YES or BUY_NO when confidence >= {min_confidence}.
   Below that threshold, always return SKIP.
2. Your "estimated_probability" is your genuine assessment of the event's
   true probability, based on ALL available data (news, order book, whale
   activity, copy-traders). This is the most important field.
3. Your "confidence" reflects how sure you are that your probability estimate
   is accurate — NOT the event probability itself.
   - 0.7-0.8: somewhat confident (limited data, ambiguous signals)
   - 0.8-0.9: confident (clear news signals, data supports your view)
   - 0.9-1.0: very confident (overwhelming evidence, multiple confirming sources)
4. Choose BUY_YES when estimated_probability > yes_price (market underprices YES).
   Choose BUY_NO when estimated_probability < yes_price (market overprices YES).
   The bigger the gap, the stronger the edge.
5. Prefer markets with liquidity > $1,000 USDC to ensure fills.
6. Set your limit price between best_bid and best_ask (the spread).
   Never bid more than 5 percentage points above best_ask.
7. If the market resolves within 24 hours and outcome is still uncertain, SKIP —
   resolution risk outweighs potential edge.
8. If we already hold a position in this market, SKIP to avoid doubling exposure.
9. Use copy-trader/whale positions as confirming signals, not the sole reason to trade.
10. If news is stale (>48h) or absent, weigh order book and price action more heavily.
11. Always reference SPECIFIC news or data in your reasoning — never trade on vague intuition.

## Domain expertise
You have deep knowledge in these focus areas. Apply domain-specific reasoning:

### Sports
- Consider team form, injuries, head-to-head records, home/away advantage.
- Recent results (last 5 games) matter more than season averages.
- Pay attention to lineup confirmations and manager statements.
- Betting odds from established bookmakers are strong reference points.

### Crypto / Blockchain
- Token price targets: check recent momentum, whale wallet movements, and exchange flows.
- Regulatory news (SEC, CFTC, EU MiCA) can move markets dramatically.
- Network metrics (TVL, active addresses, fees) provide fundamental signals.
- Be cautious of hype cycles — distinguish genuine adoption from speculation.

### Geopolitics (Iran, Middle East)
- Official government statements vs actual actions — actions matter more.
- Satellite imagery reports and shipping data override rhetoric.
- Consider the source credibility: Reuters/AP > regional media > social media.
- Sanctions, IAEA reports, and diplomatic meetings are leading indicators.
- Multiple conflicting signals = high uncertainty = prefer SKIP.
"""


@dataclass
class TradeDecision:
    action: str             # "BUY_YES" | "BUY_NO" | "SKIP"
    confidence: float
    estimated_probability: float  # Claude's estimate of true probability
    size_usdc: float
    price: float            # limit price (0–1)
    reasoning: str
    risk_factors: list[str]
    market_id: str
    market_question: str
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

        # Polymarket's hourly-updated AI context for this market (resolution criteria + current state)
        resolution_criteria = ctx.get("resolution_criteria", "")
        polymarket_context = ctx.get("polymarket_context", "")
        if resolution_criteria:
            parts.append(f"## Resolution Criteria\n{resolution_criteria}")
        if polymarket_context:
            parts.append(f"## Polymarket Context (updated hourly)\n{polymarket_context}")

        if news:
            news_parts = []
            for n in news[:5]:
                line = f"- [{n['source']}] {n['title']} ({n['published']})"
                if n.get("content"):
                    line += f"\n  > {n['content'][:300]}"
                news_parts.append(line)
            parts.append(f"## Relevant News\n" + "\n".join(news_parts))
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

        # Whale activity
        whale_activity = ctx.get("whale_activity", [])
        if whale_activity:
            whale_lines = "\n".join(
                f"  - {w['address']} | {w['outcome']} | size={w['size']:.1f} | "
                f"avg_price={w['avg_price']:.3f}"
                for w in whale_activity
            )
            parts.append(f"## Whale Activity (Top Traders)\n{whale_lines}")

        # Domain-specific data (football stats, crypto prices, etc.)
        domain_data = ctx.get("domain_data")
        if domain_data:
            domain_type = domain_data.get("match_type") or domain_data.get("market_type", "")
            if domain_type == "football":
                domain_lines = [f"## Football Data"]
                teams = domain_data.get("teams", [])
                if teams:
                    domain_lines.append(f"Match: {teams[0]} vs {teams[1]}")

                for key in ("team_a_form", "team_b_form"):
                    form = domain_data.get(key)
                    if form and "last_5" in form:
                        domain_lines.append(f"\n**{form['team']}** — {form['record']} | {form['goals']}")
                        for r in form["last_5"]:
                            domain_lines.append(f"  {r}")

                h2h = domain_data.get("head_to_head")
                if h2h:
                    domain_lines.append(
                        f"\nHead-to-head ({h2h['total_matches']} matches): "
                        f"Home wins {h2h['home_wins']} | Away wins {h2h['away_wins']} | Draws {h2h['draws']}"
                    )
                parts.append("\n".join(domain_lines))

            elif domain_type == "crypto":
                domain_lines = ["## Crypto Market Data"]
                for token in domain_data.get("tokens", []):
                    domain_lines.append(
                        f"\n**{token['coin']}** ({token['symbol']})"
                        f"\n  Price: ${token['current_price_usd']:,.2f}"
                        f"\n  24h: {token['price_change_24h_pct']:+.1f}% | "
                        f"7d: {token['price_change_7d_pct']:+.1f}% | "
                        f"30d: {token['price_change_30d_pct']:+.1f}%"
                        f"\n  Market Cap: ${token['market_cap_usd']:,.0f}"
                        f"\n  24h Volume: ${token['total_volume_24h_usd']:,.0f}"
                        f"\n  ATH: ${token['ath_usd']:,.2f} ({token['ath_change_pct']:+.1f}% from ATH)"
                    )
                    trend = token.get("trend_7d")
                    if trend:
                        domain_lines.append(
                            f"  7d Trend: ${trend['start']:,.2f} → ${trend['mid']:,.2f} → ${trend['end']:,.2f} ({trend['direction']})"
                        )
                price_target = domain_data.get("price_target")
                if price_target:
                    domain_lines.append(f"\nPrice target in question: ${price_target:,.2f}")
                parts.append("\n".join(domain_lines))

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

    def _kelly_criterion_size(
        self,
        estimated_prob: float,
        market_price: float,
        action: str,
    ) -> float:
        """
        Fractional Kelly Criterion for position sizing.
        f = (p - m) / (1 - m) for BUY_YES
        f = ((1 - p) - (1 - m)) / (1 - (1 - m)) = (m - p) / m for BUY_NO
        Then multiply by KELLY_FRACTION (0.20) for safety.
        """
        KELLY_FRACTION = 0.20  # Use 20% of full Kelly to reduce variance

        if action == "BUY_YES":
            edge = estimated_prob - market_price
            if edge <= 0:
                return 0.0
            kelly_f = edge / (1.0 - market_price) if market_price < 1.0 else 0.0
        elif action == "BUY_NO":
            edge = market_price - estimated_prob
            if edge <= 0:
                return 0.0
            kelly_f = edge / market_price if market_price > 0.0 else 0.0
        else:
            return 0.0

        kelly_f = max(0.0, min(1.0, kelly_f))
        size = self.settings.max_order_size_usdc * kelly_f * KELLY_FRACTION
        return min(size, self.settings.max_order_size_usdc)

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

        confidence = float(data.get("confidence") or 0.0)
        estimated_prob = float(data.get("estimated_probability") or 0.5)
        price = max(0.01, min(0.99, float(data.get("price") or 0.5)))
        market_yes_price = float(market.get("yes_price") or 0.5)

        # Kelly Criterion sizing
        size_usdc = self._kelly_criterion_size(estimated_prob, market_yes_price, action)

        # Scale down by confidence (lower confidence = smaller position)
        size_usdc *= confidence

        # Enforce minimum trade size
        # $0.25 floor: with MAX_ORDER_SIZE=$5 and KELLY_FRACTION=0.20 the theoretical
        # max size is $1.00, so $0.50 would reject most valid trades.
        MIN_TRADE_SIZE = 0.25
        if size_usdc < MIN_TRADE_SIZE:
            if action != "SKIP":
                logger.info(
                    "Kelly size $%.2f too small (< $%.2f), converting to SKIP",
                    size_usdc, MIN_TRADE_SIZE,
                )
                action = "SKIP"
            size_usdc = 0.0

        decision = TradeDecision(
            action=action,
            confidence=confidence,
            estimated_probability=estimated_prob,
            size_usdc=round(size_usdc, 2),
            price=price,
            reasoning=data.get("reasoning", ""),
            risk_factors=data.get("risk_factors", []),
            market_id=market.get("condition_id", ""),
            market_question=market.get("question", ""),
            yes_token_id=market.get("yes_token_id", ""),
            no_token_id=market.get("no_token_id", ""),
        )

        logger.info(
            "Claude decision | action=%s | est_prob=%.2f | confidence=%.2f | "
            "kelly_size=$%.2f | market=%r",
            decision.action,
            decision.estimated_probability,
            decision.confidence,
            decision.size_usdc,
            market.get("question", "")[:50],
        )
        return decision

    def _skip_decision(self, market: dict, reason: str = "") -> TradeDecision:
        return TradeDecision(
            action="SKIP",
            confidence=0.0,
            estimated_probability=0.5,
            size_usdc=0.0,
            price=0.0,
            reasoning=reason,
            risk_factors=[],
            market_id=market.get("condition_id", ""),
            market_question=market.get("question", ""),
            yes_token_id=market.get("yes_token_id", ""),
            no_token_id=market.get("no_token_id", ""),
        )
