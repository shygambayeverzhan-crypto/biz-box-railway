from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    database_path: str = "./data/bot.sqlite3"
    # Bybit's regional Kazakhstan host; public endpoints do not need an API key.
    bybit_base_url: str = "https://api.bybit.kz"
    ataix_base_url: str = "https://api.ataix.kz"
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"
    scan_interval_seconds: float = Field(default=5, ge=1, le=60)
    orderbook_depth: int = Field(default=50, ge=1, le=50)
    max_notional_usdt: float = Field(default=100, gt=0)
    min_notional_usdt: float = Field(default=10, gt=0)
    min_net_spread_percent: float = Field(default=0.50, ge=0, le=100)
    max_quote_age_seconds: float = Field(default=4, ge=0.5, le=60)
    max_cross_exchange_skew_seconds: float = Field(default=2, ge=0.1, le=30)
    max_signal_age_seconds: float = Field(default=20, ge=2, le=300)
    bybit_taker_fee_percent: float = Field(default=0.10, ge=0, le=10)
    ataix_taker_fee_percent: float = Field(default=0.20, ge=0, le=10)
    cooldown_seconds: int = Field(default=300, ge=10, le=86400)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: str) -> str:
        symbols = [part.strip().upper() for part in value.split(",") if part.strip()]
        if not symbols:
            raise ValueError("SYMBOLS must contain at least one market")
        if any(re.fullmatch(r"[A-Z0-9]{5,30}", symbol) is None for symbol in symbols):
            raise ValueError("SYMBOLS must contain exchange symbols such as BTCUSDT, separated by commas")
        return ",".join(dict.fromkeys(symbols))

    @property
    def symbol_list(self) -> list[str]:
        return self.symbols.split(",")

    @model_validator(mode="after")
    def validate_amount_bounds(self) -> "Settings":
        if self.min_notional_usdt > self.max_notional_usdt:
            raise ValueError("MIN_NOTIONAL_USDT cannot exceed MAX_NOTIONAL_USDT")
        return self
