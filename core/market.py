"""
core/market.py
──────────────
Market data layer: fetches open markets, order books, copy-trader positions,
and scrapes news headlines for the hybrid strategy.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from py_clob_client.client import ClobClient

from config.settings import Settings

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
GNEWS_RSS = "https://gnews.io/rss/search?q={query}&lang=en&max=5"


@dataclass
class MarketSnapshot:
    condition_id: str
    question: str
    slug: str
    end_date: str
    yes_token_id: str
    no_token_id: str
    yes_price: float    # current best-bid price for YES (0–1)
    no_price: float
    volume_usdc: float
    liquidity_usdc: float
    tags: list[str]


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published: str


class MarketService:
    def __init__(self, settings: Settings, clob_client: ClobClient) -> None:
        self.settings = settings
        self.clob = clob_client
        self._http = httpx.Client(timeout=15)

    # ── Markets ───────────────────────────────────────────────────────────────

    def get_active_markets(self, limit: int = 20) -> list[MarketSnapshot]:
        """Fetch top active markets from Polymarket Gamma API + enrich with CLOB prices."""
        try:
            resp = self._http.get(
                f"{POLYMARKET_GAMMA_API}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            logger.error("Failed to fetch markets from Gamma API: %s", exc)
            return []

        markets: list[MarketSnapshot] = []
        for m in raw:
            try:
                tokens = m.get("tokens", [])
                yes_token = next((t for t in tokens if t.get("outcome") == "Yes"), {})
                no_token = next((t for t in tokens if t.get("outcome") == "No"), {})

                snapshot = MarketSnapshot(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    end_date=m.get("endDate", ""),
                    yes_token_id=yes_token.get("tokenId", ""),
                    no_token_id=no_token.get("tokenId", ""),
                    yes_price=float(yes_token.get("price", 0.5)),
                    no_price=float(no_token.get("price", 0.5)),
                    volume_usdc=float(m.get("volume24hr", 0)),
                    liquidity_usdc=float(m.get("liquidity", 0)),
                    tags=[t.get("label", "") for t in m.get("tags", [])],
                )
                markets.append(snapshot)
            except Exception as exc:
                logger.debug("Skipping malformed market: %s", exc)

        logger.info("Fetched %d active markets", len(markets))
        return markets

    def get_order_book(self, token_id: str) -> dict:
        """Fetch order book from CLOB for a given outcome token."""
        try:
            return self.clob.get_order_book(token_id=token_id)
        except Exception as exc:
            logger.warning("Order book fetch failed for %s: %s", token_id, exc)
            return {}

    # ── Copy-trader ───────────────────────────────────────────────────────────

    def get_copy_trader_positions(self) -> list[dict]:
        """
        Fetch open positions of the configured COPY_TRADER_ADDRESS via
        Polymarket data API.
        Returns a list of position dicts.
        """
        address = self.settings.copy_trader_address
        if not address:
            logger.warning("COPY_TRADER_ADDRESS not set")
            return []

        try:
            resp = self._http.get(
                f"{POLYMARKET_GAMMA_API}/positions",
                params={"user": address, "sizeThreshold": "0.01"},
            )
            resp.raise_for_status()
            positions = resp.json()
            logger.info(
                "Copy-trader %s has %d positions", address[:10], len(positions)
            )
            return positions
        except Exception as exc:
            logger.error("Failed to fetch copy-trader positions: %s", exc)
            return []

    def get_copy_trader_recent_trades(self, limit: int = 10) -> list[dict]:
        """Fetch the most recent trades by the copy-trader address."""
        address = self.settings.copy_trader_address
        if not address:
            return []

        try:
            resp = self._http.get(
                f"{POLYMARKET_GAMMA_API}/activity",
                params={"user": address, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("Failed to fetch copy-trader trades: %s", exc)
            return []

    # ── News ──────────────────────────────────────────────────────────────────

    def scrape_news_for_market(self, question: str, max_items: int = 5) -> list[NewsItem]:
        """
        Scrape recent news headlines relevant to a market question.
        Uses GNews RSS (no API key needed for basic use).
        """
        # Extract key terms from the question for the search query
        query = " ".join(question.split()[:6])
        url = f"https://news.google.com/rss/search?q={httpx.URL(query)}&hl=en-US&gl=US&ceid=US:en"

        try:
            resp = self._http.get(url, follow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "xml")
            items = soup.find_all("item")[:max_items]

            return [
                NewsItem(
                    title=item.find("title").text if item.find("title") else "",
                    url=item.find("link").text if item.find("link") else "",
                    source=item.find("source").text if item.find("source") else "Unknown",
                    published=item.find("pubDate").text if item.find("pubDate") else "",
                )
                for item in items
            ]
        except Exception as exc:
            logger.debug("News scrape failed for %r: %s", query, exc)
            return []

    def get_market_context(self, market: MarketSnapshot) -> dict:
        """
        Build a full context dict for a single market, combining:
        - market metadata + prices
        - order book depth
        - relevant news headlines
        - copy-trader activity (if enabled)
        """
        news = self.scrape_news_for_market(market.question)
        order_book = self.get_order_book(market.yes_token_id)

        ctx: dict = {
            "market": {
                "condition_id": market.condition_id,
                "question": market.question,
                "end_date": market.end_date,
                "yes_token_id": market.yes_token_id,
                "no_token_id": market.no_token_id,
                "yes_price": market.yes_price,
                "no_price": market.no_price,
                "volume_24h_usdc": market.volume_usdc,
                "liquidity_usdc": market.liquidity_usdc,
                "tags": market.tags,
            },
            "order_book_summary": self._summarise_order_book(order_book),
            "news": [
                {"title": n.title, "source": n.source, "published": n.published}
                for n in news
            ],
        }

        if self.settings.strategy in ("copy_trader", "hybrid"):
            ctx["copy_trader_positions"] = self.get_copy_trader_positions()
            ctx["copy_trader_recent_trades"] = self.get_copy_trader_recent_trades()

        return ctx

    def _summarise_order_book(self, book: dict) -> dict:
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        return {
            "best_bid": float(bids[0]["price"]) if bids else None,
            "best_ask": float(asks[0]["price"]) if asks else None,
            "bid_depth_3": sum(float(b.get("size", 0)) for b in bids[:3]),
            "ask_depth_3": sum(float(a.get("size", 0)) for a in asks[:3]),
        }

    def close(self) -> None:
        self._http.close()
