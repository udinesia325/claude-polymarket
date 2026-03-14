"""
config/settings.py
──────────────────
Single source of truth for all runtime configuration.
Reads from the .env file that is already loaded before import.
"""

import os
from pathlib import Path
from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    # ── Mode ──────────────────────────────────────────────────────────────────
    trading_mode: str = Field(default="fake")
    dry_run: bool = Field(default=True)

    # ── Network ───────────────────────────────────────────────────────────────
    rpc_url: str
    chain_id: int
    polymarket_host: str

    # ── Wallet ────────────────────────────────────────────────────────────────
    wallet_address: str
    wallet_private_key: str

    # ── Polymarket CLOB API ───────────────────────────────────────────────────
    api_key: str
    api_secret: str
    api_passphrase: str

    # ── Claude AI ─────────────────────────────────────────────────────────────
    anthropic_api_key: str
    claude_model: str = Field(default="claude-opus-4-6")
    analyze_interval_minutes: int = Field(default=15)

    # ── Risk Management ───────────────────────────────────────────────────────
    max_order_size_usdc: float = Field(default=10.0)
    max_total_exposure_usdc: float = Field(default=100.0)
    max_positions: int = Field(default=5)
    min_confidence_score: float = Field(default=0.7)

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_chat_id: str
    notify_on_order: bool = Field(default=True)
    notify_on_pnl: bool = Field(default=True)
    pnl_report_interval_hours: int = Field(default=6)

    # ── News / Search ─────────────────────────────────────────────────────────
    tavily_api_key: str = Field(default="")

    # ── Domain Data APIs ────────────────────────────────────────────────────────
    football_data_api_key: str = Field(default="")

    # ── Market Focus ──────────────────────────────────────────────────────────
    # Comma-separated tags/keywords to filter markets (empty = all markets)
    market_focus_tags: str = Field(default="")
    # Comma-separated keywords to match in market question
    market_focus_keywords: str = Field(default="")

    # ── Strategy ──────────────────────────────────────────────────────────────
    strategy: str = Field(default="hybrid")
    copy_trader_address: str = Field(default="")

    @field_validator("trading_mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("fake", "real"):
            raise ValueError(f"TRADING_MODE must be 'fake' or 'real', got: {v!r}")
        return v

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        if v not in ("copy_trader", "news", "hybrid"):
            raise ValueError(f"STRATEGY must be copy_trader|news|hybrid, got: {v!r}")
        return v

    @field_validator("min_confidence_score")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("MIN_CONFIDENCE_SCORE must be between 0.0 and 1.0")
        return v

    @property
    def is_real(self) -> bool:
        return self.trading_mode == "real"

    @property
    def is_fake(self) -> bool:
        return self.trading_mode == "fake"

    model_config = {"extra": "ignore"}


def _bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes")


def load_settings() -> Settings:
    """Build Settings from environment variables (already loaded via dotenv)."""
    return Settings(
        trading_mode=os.environ["TRADING_MODE"],
        dry_run=_bool(os.getenv("DRY_RUN", "true")),
        rpc_url=os.environ["RPC_URL"],
        chain_id=int(os.environ["CHAIN_ID"]),
        polymarket_host=os.environ["POLYMARKET_HOST"],
        wallet_address=os.environ["WALLET_ADDRESS"],
        wallet_private_key=os.environ["WALLET_PRIVATE_KEY"],
        api_key=os.environ["API_KEY"],
        api_secret=os.environ["API_SECRET"],
        api_passphrase=os.environ["API_PASSPHRASE"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-4-6"),
        analyze_interval_minutes=int(os.getenv("ANALYZE_INTERVAL_MINUTES", "15")),
        max_order_size_usdc=float(os.getenv("MAX_ORDER_SIZE_USDC", "10.0")),
        max_total_exposure_usdc=float(os.getenv("MAX_TOTAL_EXPOSURE_USDC", "100.0")),
        max_positions=int(os.getenv("MAX_POSITIONS", "5")),
        min_confidence_score=float(os.getenv("MIN_CONFIDENCE_SCORE", "0.7")),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
        notify_on_order=_bool(os.getenv("NOTIFY_ON_ORDER", "true")),
        notify_on_pnl=_bool(os.getenv("NOTIFY_ON_PNL", "true")),
        pnl_report_interval_hours=int(os.getenv("PNL_REPORT_INTERVAL_HOURS", "6")),
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        football_data_api_key=os.getenv("FOOTBALL_DATA_API_KEY", ""),
        market_focus_tags=os.getenv("MARKET_FOCUS_TAGS", ""),
        market_focus_keywords=os.getenv("MARKET_FOCUS_KEYWORDS", ""),
        strategy=os.getenv("STRATEGY", "hybrid"),
        copy_trader_address=os.getenv("COPY_TRADER_ADDRESS", ""),
    )
