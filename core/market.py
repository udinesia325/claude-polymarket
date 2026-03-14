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
from services.football_data import FootballDataService
from services.coingecko import CoinGeckoService

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


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
    content: str = ""  # article snippet (populated by Tavily)


class MarketService:
    def __init__(self, settings: Settings, clob_client: ClobClient) -> None:
        self.settings = settings
        self.clob = clob_client
        self._http = httpx.Client(timeout=15)
        self._football = FootballDataService(settings)
        self._coingecko = CoinGeckoService()

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
                    yes_price=float(yes_token.get("price") or 0.5),
                    no_price=float(no_token.get("price") or 0.5),
                    volume_usdc=float(m.get("volume24hr") or 0),
                    liquidity_usdc=float(m.get("liquidity") or 0),
                    tags=[t.get("label", "") for t in m.get("tags", [])],
                )
                markets.append(snapshot)
            except Exception as exc:
                logger.debug("Skipping malformed market: %s", exc)

        # Apply focus filters if configured
        filtered = self._apply_focus_filter(markets)
        logger.info("Fetched %d active markets | after focus filter: %d", len(markets), len(filtered))
        return filtered

    def _apply_focus_filter(self, markets: list[MarketSnapshot]) -> list[MarketSnapshot]:
        """Filter markets by configured tags and keywords."""
        focus_tags = [
            t.strip().lower()
            for t in self.settings.market_focus_tags.split(",")
            if t.strip()
        ]
        focus_keywords = [
            k.strip().lower()
            for k in self.settings.market_focus_keywords.split(",")
            if k.strip()
        ]

        if not focus_tags and not focus_keywords:
            return markets  # No filter configured

        result = []
        for m in markets:
            # Check tags
            market_tags = [t.lower() for t in m.tags]
            if focus_tags and any(ft in tag for ft in focus_tags for tag in market_tags):
                result.append(m)
                continue

            # Check keywords in question
            question_lower = m.question.lower()
            if focus_keywords and any(kw in question_lower for kw in focus_keywords):
                result.append(m)
                continue

        return result

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
        Fetch news relevant to a market question.
        Uses Tavily Search API if configured (returns full content snippets),
        falls back to Google News RSS (headlines only).
        """
        if self.settings.tavily_api_key:
            return self._tavily_search(question, max_items)
        return self._google_news_rss(question, max_items)

    def _tavily_search(self, question: str, max_items: int = 5) -> list[NewsItem]:
        """Search via Tavily API — returns title + content snippets."""
        try:
            resp = self._http.post(
                TAVILY_SEARCH_URL,
                json={
                    "api_key": self.settings.tavily_api_key,
                    "query": question,
                    "search_depth": "basic",
                    "include_answer": False,
                    "max_results": max_items,
                    "topic": "news",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = []
            for r in data.get("results", [])[:max_items]:
                domain = r.get("url", "").split("/")[2] if r.get("url") else "Unknown"
                results.append(NewsItem(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    source=domain,
                    published=r.get("published_date", ""),
                    content=r.get("content", ""),
                ))
            logger.info("Tavily returned %d results for %r", len(results), question[:40])
            return results
        except Exception as exc:
            logger.warning("Tavily search failed: %s — falling back to Google RSS", exc)
            return self._google_news_rss(question, max_items)

    def _google_news_rss(self, question: str, max_items: int = 5) -> list[NewsItem]:
        """Fallback: Google News RSS (headlines only, no content)."""
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
            logger.debug("Google News RSS failed for %r: %s", query, exc)
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
                {
                    "title": n.title,
                    "source": n.source,
                    "published": n.published,
                    **({"content": n.content[:500]} if n.content else {}),
                }
                for n in news
            ],
        }

        if self.settings.strategy in ("copy_trader", "hybrid"):
            ctx["copy_trader_positions"] = self.get_copy_trader_positions()
            ctx["copy_trader_recent_trades"] = self.get_copy_trader_recent_trades()

        # Whale activity for this specific market
        whale_data = self.get_whale_activity(market.condition_id)
        if whale_data:
            ctx["whale_activity"] = whale_data

        # Domain-specific data enrichment
        football_ctx = self._football.get_football_context(market.question)
        if football_ctx:
            ctx["domain_data"] = football_ctx

        crypto_ctx = self._coingecko.get_crypto_context(market.question)
        if crypto_ctx:
            ctx["domain_data"] = crypto_ctx

        return ctx

    # ── Whale tracking ─────────────────────────────────────────────────────

    def get_top_traders(self, limit: int = 20) -> list[str]:
        """Fetch top profitable trader addresses from Gamma API leaderboard."""
        try:
            resp = self._http.get(
                f"{POLYMARKET_GAMMA_API}/leaderboard",
                params={"limit": limit, "window": "volume"},
            )
            resp.raise_for_status()
            data = resp.json()
            addresses = [entry.get("address", "") for entry in data if entry.get("address")]
            logger.info("Fetched %d top trader addresses", len(addresses))
            return addresses
        except Exception as exc:
            logger.debug("Leaderboard fetch failed: %s", exc)
            return []

    def get_whale_activity(self, condition_id: str) -> list[dict]:
        """
        Check if top traders have positions in a specific market.
        Returns a list of whale positions for the given market.
        """
        if not hasattr(self, "_cached_whales"):
            self._cached_whales = self.get_top_traders(limit=20)
            self._whale_cache_time = __import__("time").time()

        # Refresh whale list every 30 minutes
        if __import__("time").time() - getattr(self, "_whale_cache_time", 0) > 1800:
            self._cached_whales = self.get_top_traders(limit=20)
            self._whale_cache_time = __import__("time").time()

        whale_positions = []
        for address in self._cached_whales[:10]:  # Check top 10 to limit API calls
            try:
                resp = self._http.get(
                    f"{POLYMARKET_GAMMA_API}/positions",
                    params={"user": address, "market": condition_id},
                    timeout=5,
                )
                resp.raise_for_status()
                positions = resp.json()
                for p in positions:
                    size = float(p.get("size", 0))
                    if size > 0:
                        whale_positions.append({
                            "address": address[:10] + "...",
                            "outcome": p.get("outcome", ""),
                            "size": size,
                            "avg_price": float(p.get("avgPrice", 0)),
                        })
            except Exception:
                continue

        if whale_positions:
            logger.info(
                "Found %d whale positions for market %s",
                len(whale_positions), condition_id[:12],
            )
        return whale_positions

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
        self._football.close()
        self._coingecko.close()
