"""
services/coingecko.py
─────────────────────
Crypto market data enrichment using CoinGecko free API (no key required).
Extracts token/coin names from Polymarket questions and fetches:
- Current price and 24h change
- Market cap and volume
- 7-day price trend
- Key metrics (ATH, ATL, circulating supply)
"""

import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"

# Map common Polymarket token references to CoinGecko IDs
TOKEN_ALIASES = {
    "bitcoin": "bitcoin",
    "btc": "bitcoin",
    "ethereum": "ethereum",
    "eth": "ethereum",
    "solana": "solana",
    "sol": "solana",
    "xrp": "ripple",
    "ripple": "ripple",
    "cardano": "cardano",
    "ada": "cardano",
    "dogecoin": "dogecoin",
    "doge": "dogecoin",
    "polygon": "matic-network",
    "matic": "matic-network",
    "pol": "matic-network",
    "avalanche": "avalanche-2",
    "avax": "avalanche-2",
    "chainlink": "chainlink",
    "link": "chainlink",
    "polkadot": "polkadot",
    "dot": "polkadot",
    "litecoin": "litecoin",
    "ltc": "litecoin",
    "uniswap": "uniswap",
    "uni": "uniswap",
    "aave": "aave",
    "bnb": "binancecoin",
    "binance": "binancecoin",
    "tron": "tron",
    "trx": "tron",
    "near": "near",
    "near protocol": "near",
    "sui": "sui",
    "aptos": "aptos",
    "apt": "aptos",
    "arbitrum": "arbitrum",
    "arb": "arbitrum",
    "optimism": "optimism",
    "op": "optimism",
    "cosmos": "cosmos",
    "atom": "cosmos",
    "pepe": "pepe",
    "shiba": "shiba-inu",
    "shib": "shiba-inu",
    "bonk": "bonk",
}

# Price threshold patterns in market questions
PRICE_PATTERN = re.compile(
    r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)?\s*(?:by|before|above|below|over|under|reach|hit)",
    re.IGNORECASE,
)


class CoinGeckoService:
    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=10,
            headers={"Accept": "application/json"},
        )
        self._coin_cache: dict[str, dict] = {}

    def get_crypto_context(self, question: str) -> Optional[dict]:
        """
        Given a Polymarket market question about crypto,
        extract token names and return enriched context.
        Returns None if not a crypto question.
        """
        tokens = self._extract_tokens(question)
        if not tokens:
            return None

        context = {
            "market_type": "crypto",
            "tokens": [],
        }

        for token_id in tokens:
            data = self._get_coin_data(token_id)
            if data:
                context["tokens"].append(data)

        # Extract price target from question if present
        price_match = PRICE_PATTERN.search(question)
        if price_match:
            target = price_match.group(1).replace(",", "")
            if "k" in question[price_match.start():price_match.end()].lower():
                target = str(float(target) * 1000)
            context["price_target"] = float(target)

        return context if context["tokens"] else None

    def _extract_tokens(self, question: str) -> list[str]:
        """Extract crypto token CoinGecko IDs from a market question."""
        q_lower = question.lower()
        found = []
        seen_ids = set()

        # Sort aliases by length (longest first) to match "near protocol" before "near"
        sorted_aliases = sorted(TOKEN_ALIASES.items(), key=lambda x: len(x[0]), reverse=True)

        for alias, coin_id in sorted_aliases:
            # Use word boundary matching to avoid false positives
            pattern = r'\b' + re.escape(alias) + r'\b'
            if re.search(pattern, q_lower) and coin_id not in seen_ids:
                found.append(coin_id)
                seen_ids.add(coin_id)

        return found[:3]  # Max 3 tokens per question

    def _get_coin_data(self, coin_id: str) -> Optional[dict]:
        """Fetch current market data for a coin from CoinGecko."""
        if coin_id in self._coin_cache:
            return self._coin_cache[coin_id]

        try:
            resp = self._http.get(
                f"{BASE_URL}/coins/{coin_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "true",
                },
            )
            if resp.status_code != 200:
                logger.debug("CoinGecko API error for %s: %d", coin_id, resp.status_code)
                return None

            data = resp.json()
            market_data = data.get("market_data", {})

            result = {
                "coin": data.get("name", coin_id),
                "symbol": data.get("symbol", "").upper(),
                "current_price_usd": market_data.get("current_price", {}).get("usd", 0),
                "price_change_24h_pct": market_data.get("price_change_percentage_24h", 0),
                "price_change_7d_pct": market_data.get("price_change_percentage_7d", 0),
                "price_change_30d_pct": market_data.get("price_change_percentage_30d", 0),
                "market_cap_usd": market_data.get("market_cap", {}).get("usd", 0),
                "total_volume_24h_usd": market_data.get("total_volume", {}).get("usd", 0),
                "ath_usd": market_data.get("ath", {}).get("usd", 0),
                "ath_change_pct": market_data.get("ath_change_percentage", {}).get("usd", 0),
                "atl_usd": market_data.get("atl", {}).get("usd", 0),
                "circulating_supply": market_data.get("circulating_supply", 0),
                "max_supply": market_data.get("max_supply"),
            }

            # 7-day sparkline trend
            sparkline = market_data.get("sparkline_7d", {}).get("price", [])
            if sparkline and len(sparkline) >= 2:
                start = sparkline[0]
                end = sparkline[-1]
                mid = sparkline[len(sparkline) // 2]
                result["trend_7d"] = {
                    "start": round(start, 2),
                    "mid": round(mid, 2),
                    "end": round(end, 2),
                    "direction": "up" if end > start else "down",
                }

            self._coin_cache[coin_id] = result
            logger.info("CoinGecko data fetched for %s: $%.2f", coin_id, result["current_price_usd"])
            return result

        except Exception as exc:
            logger.debug("CoinGecko fetch failed for %s: %s", coin_id, exc)
            return None

    def close(self) -> None:
        self._http.close()
