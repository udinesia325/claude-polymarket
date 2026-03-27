"""
core/market.py
──────────────
Market data layer: fetches open markets, order books, copy-trader positions,
and scrapes news headlines for the hybrid strategy.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from py_clob_client.client import ClobClient

from config.settings import Settings
from services.football_data import FootballDataService
from services.coingecko import CoinGeckoService

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"
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
    # Polymarket event enrichment (populated from events[].description / eventMetadata)
    event_description: str = ""   # resolution criteria text
    event_context: str = ""       # AI-generated market context summary (updated hourly)


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
                # Gamma API returns token IDs in clobTokenIds[] and prices in outcomePrices[]
                # (NOT in tokens[], which is always empty)
                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    prices = json.loads(prices)
                outcomes = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)

                yes_idx = outcomes.index("Yes") if "Yes" in outcomes else 0
                no_idx = outcomes.index("No") if "No" in outcomes else 1

                yes_token_id = clob_ids[yes_idx] if len(clob_ids) > yes_idx else ""
                no_token_id = clob_ids[no_idx] if len(clob_ids) > no_idx else ""
                yes_price = float(prices[yes_idx]) if len(prices) > yes_idx else 0.5
                no_price = float(prices[no_idx]) if len(prices) > no_idx else 0.5

                # Tags come from the nested events[0].tags array
                events = m.get("events", [])
                event_tags: list[str] = []
                event_description = ""
                event_context = ""
                if events:
                    ev = events[0]
                    event_tags = [t.get("label", "") for t in ev.get("tags", [])]
                    event_description = ev.get("description", "")
                    meta = ev.get("eventMetadata") or {}
                    event_context = meta.get("context_description", "")

                snapshot = MarketSnapshot(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    end_date=m.get("endDate", ""),
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume_usdc=float(m.get("volume24hr") or 0),
                    liquidity_usdc=float(m.get("liquidity") or 0),
                    tags=event_tags,
                    event_description=event_description,
                    event_context=event_context,
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
                f"{POLYMARKET_DATA_API}/positions",
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
                f"{POLYMARKET_DATA_API}/activity",
                params={"user": address, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("Failed to fetch copy-trader trades: %s", exc)
            return []

    # ── News ──────────────────────────────────────────────────────────────────

    def scrape_news_for_market(
        self,
        question: str,
        max_items: int = 5,
        event_context: str = "",
    ) -> list[NewsItem]:
        """
        Fetch news relevant to a market question.
        Uses Tavily Search API if configured (returns full content snippets),
        falls back to Google News RSS (headlines only).
        event_context is Polymarket's AI-generated summary — used to build
        a richer, entity-focused search query.
        """
        query = self._build_news_query(question, event_context)
        if self.settings.tavily_api_key:
            return self._tavily_search(query, max_items)
        return self._google_news_rss(query, max_items)

    def _build_news_query(self, question: str, event_context: str) -> str:
        """
        Build a focused news search query by extracting key named entities
        from the Polymarket event context description.
        Falls back to the market question if context is empty.
        """
        if not event_context:
            return question

        # Extract the first sentence of the context as the most current signal
        first_sentence = event_context.split(".")[0].strip()

        # Keep query concise: question keywords + first context sentence (truncated)
        question_words = " ".join(question.split()[:8])
        context_snippet = first_sentence[:120] if len(first_sentence) > 120 else first_sentence

        query = f"{question_words} {context_snippet}"
        logger.debug("News query built from context: %r", query[:100])
        return query

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
        news = self.scrape_news_for_market(
            market.question,
            event_context=market.event_context,
        )
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

        # Polymarket event enrichment: resolution criteria + hourly-updated context
        if market.event_description:
            ctx["resolution_criteria"] = market.event_description[:600]
        if market.event_context:
            ctx["polymarket_context"] = market.event_context[:800]

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

    def _summarise_order_book(self, book) -> dict:
        """Handle both dict and OrderBookSummary object from py-clob-client."""
        if hasattr(book, "bids"):
            # OrderBookSummary object: bids/asks are lists of OrderSummary with .price/.size
            bids = book.bids or []
            asks = book.asks or []
            def _price(x): return float(x.price)
            def _size(x): return float(x.size)
        else:
            bids = book.get("bids", []) if book else []
            asks = book.get("asks", []) if book else []
            def _price(x): return float(x.get("price", 0))
            def _size(x): return float(x.get("size", 0))

        return {
            "best_bid": _price(bids[0]) if bids else None,
            "best_ask": _price(asks[0]) if asks else None,
            "bid_depth_3": sum(_size(b) for b in bids[:3]),
            "ask_depth_3": sum(_size(a) for a in asks[:3]),
        }

    def close(self) -> None:
        self._http.close()
        self._football.close()
        self._coingecko.close()
