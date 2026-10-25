from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


class CircuitOpen(RuntimeError):
    pass


@dataclass(frozen=True)
class Book:
    exchange: str
    symbol: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    observed_at: float
    exchange_timestamp: float | None = None


@dataclass
class Circuit:
    failures: int = 0
    opened_until: float = 0.0

    def check(self) -> None:
        if time.monotonic() < self.opened_until:
            raise CircuitOpen("circuit breaker open")

    def success(self) -> None:
        self.failures = 0
        self.opened_until = 0.0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= 3:
            self.opened_until = time.monotonic() + 30.0
            self.failures = 0


VENUES = {
    "Bybit Kazakhstan": {"key": "bybit", "base": "https://api.bybit.kz"},
    "ATAIX Kazakhstan": {"key": "ataix", "base": "https://api.ataix.kz"},
}


class MarketData:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.venue_bases = {"Bybit Kazakhstan": settings.bybit_base_url, "ATAIX Kazakhstan": settings.ataix_base_url}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(7.0, connect=4.0),
            headers={"User-Agent": "arbiscan-bot/0.3"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._circuits = {name: Circuit() for name in VENUES}
        self._locks = {name: asyncio.Lock() for name in VENUES}
        self._last_request = {name: 0.0 for name in VENUES}
        self._market_states: dict[str, str] = {}
        self._markets: dict[str, dict[str, str]] = {}
        self._source_states = {name: "проверяю" for name in VENUES}

    async def close(self) -> None:
        await self.client.aclose()

    @property
    def market_states(self) -> dict[str, str]:
        return dict(self._market_states)

    @property
    def source_states(self) -> dict[str, str]:
        return dict(self._source_states)

    @property
    def active_venues(self) -> list[str]:
        return sorted({venue for venues in self._markets.values() for venue in venues})

    async def _get_json(self, exchange: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        circuit = self._circuits[exchange]
        async with self._locks[exchange]:
            circuit.check()
            for attempt in range(3):
                gap = 0.20 - (time.monotonic() - self._last_request[exchange])
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_request[exchange] = time.monotonic()
                try:
                    response = await self.client.get(self.venue_bases[exchange] + path, params=params)
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            wait = float(retry_after) if retry_after else 0.3 * (2**attempt)
                        except ValueError:
                            wait = 0.3 * (2**attempt)
                        await asyncio.sleep(min(5, wait) + random.uniform(0.05, 0.35))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, (dict, list)):
                        raise ValueError("Unexpected JSON response")
                    circuit.success()
                    self._source_states[exchange] = "на связи"
                    return payload
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (2**attempt) + random.uniform(0.05, 0.35))
                        continue
                    circuit.failure()
                    self._source_states[exchange] = _friendly_error(exc)
                    raise
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (418, 429) or exc.response.status_code >= 500:
                        circuit.failure()
                    self._source_states[exchange] = _friendly_error(exc)
                    raise
                except ValueError as exc:
                    circuit.failure()
                    self._source_states[exchange] = "ответ API не распознан"
                    raise
        raise RuntimeError("Request retries exhausted")

    async def validate_markets(self, symbols: list[str]) -> list[str]:
        self._market_states.clear()
        self._markets.clear()
        results = await asyncio.gather(*(self._catalog(name) for name in VENUES), return_exceptions=True)
        catalogs: dict[str, dict[str, str]] = {}
        for (name, _), result in zip(VENUES.items(), results, strict=True):
            if isinstance(result, Exception):
                self._source_states[name] = _friendly_error(result)
            else:
                catalogs[name] = result
                self._source_states[name] = f"на связи · рынков USDT: {len(result)}"
        active: list[str] = []
        for symbol in symbols:
            venues = {name: catalog[symbol] for name, catalog in catalogs.items() if symbol in catalog}
            self._markets[symbol] = venues
            self._market_states[symbol] = ", ".join(venues) if venues else "не найден на обеих площадках"
            if len(venues) >= 2:
                active.append(symbol)
        self._markets = {symbol: venues for symbol, venues in self._markets.items() if len(venues) >= 2}
        return active

    async def _catalog(self, exchange: str) -> dict[str, str]:
        if exchange == "Bybit Kazakhstan":
            data = await self._get_json(exchange, "/v5/market/instruments-info", {"category": "spot", "limit": 1000})
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit retCode={data.get('retCode')}")
            rows = data.get("result", {}).get("list", [])
            return {r["symbol"]: r["symbol"] for r in rows if r.get("status") == "Trading" and r.get("quoteCoin") == "USDT" and r.get("symbolType") != "xstocks"}
        data = await self._get_json(exchange, "/api/symbols")
        if data.get("status") is not True:
            raise RuntimeError("ATAIX symbols status is not true")
        rows = data.get("result", [])
        return {
            str(row["base"]).upper() + str(row["quote"]).upper(): f"{row['base']}-{row['quote']}"
            for row in rows
            if row.get("isActive") is True and not row.get("isHidden", False) and str(row.get("quote", "")).upper() == "USDT"
        }

    async def books(self, symbol: str) -> list[Book]:
        venues = self._markets.get(symbol, {})
        results = await asyncio.gather(*(self._book(name, venue_symbol) for name, venue_symbol in venues.items()), return_exceptions=True)
        books: list[Book] = []
        for (name, _), result in zip(venues.items(), results, strict=True):
            if isinstance(result, Exception):
                self._source_states[name] = _friendly_error(result)
            else:
                books.append(result)
        return books

    async def _book(self, exchange: str, venue_symbol: str) -> Book:
        if exchange == "Bybit Kazakhstan":
            data = await self._get_json(exchange, "/v5/market/orderbook", {"category": "spot", "symbol": venue_symbol, "limit": min(self.settings.orderbook_depth, 50)})
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit retCode={data.get('retCode')}")
            row = data["result"]
            return _book(exchange, venue_symbol, row.get("b", []), row.get("a", []), row.get("ts"))
        data = await self._get_json(exchange, f"/api/book/{venue_symbol}")
        if data.get("status") is not True:
            raise RuntimeError("ATAIX orderbook status is not true")
        row = data.get("result", {})
        return _book(exchange, symbol=venue_symbol.replace("-", ""), bids=row.get("buy", []), asks=row.get("sell", []), timestamp=None)


def _book(exchange: str, symbol: str, bids: list, asks: list, timestamp: Any = None) -> Book:
    observed = time.time()
    try:
        exchange_ts = float(timestamp) / (1000 if float(timestamp) > 10_000_000_000 else 1)
    except (TypeError, ValueError):
        exchange_ts = None
    return Book(exchange, symbol.replace("-", ""), _levels(bids), _levels(asks), observed, exchange_ts)


def _friendly_error(error: Exception) -> str:
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 451:
        return "API недоступен из региона сервера (HTTP 451)"
    if status == 403:
        return "API отклонил запрос (HTTP 403)"
    if status == 429:
        return "лимит API (HTTP 429), повторю позже"
    if isinstance(status, int):
        return f"ошибка API (HTTP {status})"
    if isinstance(error, CircuitOpen):
        return "пауза после повторных ошибок"
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
        return "нет ответа API / сетевая ошибка"
    return f"временная ошибка ({type(error).__name__})"


def _levels(raw: list) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for level in raw:
        try:
            if isinstance(level, dict):
                price, amount = float(level["price"]), float(level["quantity"])
            else:
                price, amount = float(level[0]), float(level[1])
            if price > 0 and amount > 0:
                result.append((price, amount))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return result
