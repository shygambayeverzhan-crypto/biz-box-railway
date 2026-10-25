from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings


class SourceUnavailable(RuntimeError):
    def __init__(self, exchange: str, cause: Exception) -> None:
        super().__init__(f"{exchange} market data unavailable ({type(cause).__name__})")
        self.exchange = exchange
        self.cause = cause


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
    probe_required: bool = False
    probe_in_progress: bool = False

    def check(self, now: float) -> None:
        if now < self.opened_until:
            raise CircuitOpen("circuit breaker open")
        if self.probe_required:
            if self.probe_in_progress:
                raise CircuitOpen("circuit breaker probe already running")
            self.probe_in_progress = True

    def success(self) -> None:
        self.failures = 0
        self.opened_until = 0.0
        self.probe_required = False
        self.probe_in_progress = False

    def failure(self) -> None:
        if self.probe_in_progress:
            self.opened_until = time.monotonic() + 30.0
            self.probe_in_progress = False
            self.probe_required = True
            self.failures = 0
            return
        self.failures += 1
        if self.failures >= 3:
            self.opened_until = time.monotonic() + 30.0
            self.failures = 0
            self.probe_required = True


class MarketData:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=3.0),
            headers={"User-Agent": "arbscanner-mvp/0.1"},
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        self._circuits = {"Bybit Kazakhstan": Circuit(), "Binance Global": Circuit()}
        self._locks = {key: asyncio.Lock() for key in self._circuits}
        self._last_request: dict[str, float] = {key: 0.0 for key in self._circuits}
        self._market_states: dict[str, str] = {}

    async def close(self) -> None:
        await self.client.aclose()

    @property
    def market_states(self) -> dict[str, str]:
        return dict(self._market_states)

    async def _get_json(self, exchange: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
        circuit = self._circuits[exchange]
        async with self._locks[exchange]:
            circuit.check(time.monotonic())
            for attempt in range(3):
                now = time.monotonic()
                delay = 0.15 - (now - self._last_request[exchange])
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request[exchange] = time.monotonic()
                try:
                    response = await self.client.get(url, params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            await asyncio.sleep(_retry_delay(response, attempt))
                            continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("Unexpected JSON response")
                    if exchange == "Bybit Kazakhstan" and payload.get("retCode") == 10006:
                        if attempt < 2:
                            await asyncio.sleep(_retry_delay(response, attempt))
                            continue
                        circuit.failure()
                        raise RuntimeError("Bybit rate limit retCode 10006")
                    circuit.success()
                    return payload
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (2**attempt) + random.uniform(0.05, 0.3))
                        continue
                    circuit.failure()
                    raise exc
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (418, 429) or status >= 500:
                        if attempt < 2:
                            if status != 418:
                                await asyncio.sleep(_retry_delay(exc.response, attempt))
                                continue
                        if status in (418, 429) or status >= 500:
                            circuit.failure()
                        raise
                    else:
                        # A permanent 4xx indicates an invalid request, not provider downtime.
                        circuit.success()
                    raise
                except ValueError:
                    circuit.failure()
                    raise
        raise RuntimeError("Request retries exhausted")

    async def validate_markets(self, symbols: list[str]) -> list[str]:
        self._market_states.clear()
        valid: list[str] = []
        results = await asyncio.gather(
            *(self._validate_one(symbol) for symbol in symbols), return_exceptions=True
        )
        for symbol, result in zip(symbols, results, strict=True):
            if result is True:
                self._market_states[symbol] = "active on both sources"
                valid.append(symbol)
            elif isinstance(result, SourceUnavailable):
                self._market_states[symbol] = (
                    f"validation error: {result.exchange} ({type(result.cause).__name__})"
                )
            elif isinstance(result, Exception):
                self._market_states[symbol] = f"validation error: {type(result).__name__}"
            else:
                self._market_states[symbol] = "not active or not matched on both sources"
        return valid

    async def _validate_one(self, symbol: str) -> bool:
        bybit_url = f"{self.settings.bybit_base_url.rstrip('/')}/v5/market/instruments-info"
        binance_url = f"{self.settings.binance_base_url.rstrip('/')}/api/v3/exchangeInfo"

        async def capture(exchange: str, request):
            try:
                return await request
            except Exception as exc:
                raise SourceUnavailable(exchange, exc) from exc

        bybit, binance = await asyncio.gather(
            capture(
                "Bybit Kazakhstan",
                self._get_json("Bybit Kazakhstan", bybit_url, {"category": "spot", "symbol": symbol}),
            ),
            capture(
                "Binance Global",
                self._get_json("Binance Global", binance_url, {"symbol": symbol}),
            ),
        )
        if bybit.get("retCode") != 0:
            raise ValueError(f"Bybit returned {bybit.get('retCode')}")
        b_list = bybit.get("result", {}).get("list", [])
        n_list = binance.get("symbols", [])
        b_market = next((item for item in b_list if item.get("symbol") == symbol), None)
        n_market = next((item for item in n_list if item.get("symbol") == symbol), None)
        expected_quote = "USDT"
        expected_base = symbol.removesuffix(expected_quote) if symbol.endswith(expected_quote) else ""
        if not expected_base or not b_market or not n_market:
            return False
        return (
            b_market.get("status") == "Trading"
            and b_market.get("baseCoin") == expected_base
            and b_market.get("quoteCoin") == expected_quote
            and b_market.get("symbolType", "") != "xstocks"
            and n_market.get("status") == "TRADING"
            and n_market.get("baseAsset") == expected_base
            and n_market.get("quoteAsset") == expected_quote
            and n_market.get("isSpotTradingAllowed", True) is not False
        )

    async def bybit(self, symbol: str) -> Book:
        data = await self._get_json(
            "Bybit Kazakhstan",
            f"{self.settings.bybit_base_url.rstrip('/')}/v5/market/orderbook",
            {"category": "spot", "symbol": symbol, "limit": self.settings.orderbook_depth},
        )
        if data.get("retCode") != 0:
            raise ValueError(f"Bybit returned {data.get('retCode')}")
        result = data.get("result", {})
        if result.get("s") != symbol:
            raise ValueError("Bybit returned a different market")
        observed_at = time.time()
        exchange_timestamp = _to_epoch(result.get("ts"))
        server_time = _to_epoch(data.get("time"))
        if exchange_timestamp is not None and server_time is not None:
            # Convert the exchange clock timestamp to local time using this response's server-time sample.
            exchange_timestamp -= server_time - observed_at
        return Book(
            "Bybit Kazakhstan", symbol,
            _levels(result.get("b", [])), _levels(result.get("a", [])),
            observed_at=observed_at,
            exchange_timestamp=exchange_timestamp,
        )

    async def binance(self, symbol: str) -> Book:
        data = await self._get_json(
            "Binance Global",
            f"{self.settings.binance_base_url.rstrip('/')}/api/v3/depth",
            {"symbol": symbol, "limit": min(self.settings.orderbook_depth, 1000)},
        )
        return Book(
            "Binance Global", symbol,
            _levels(data.get("bids", [])), _levels(data.get("asks", [])),
            observed_at=time.time(), exchange_timestamp=None,
        )

    async def pair(self, symbol: str) -> tuple[Book, Book]:
        async def capture(exchange: str, request):
            try:
                return await request(symbol)
            except Exception as exc:
                raise SourceUnavailable(exchange, exc) from exc

        bybit, binance = await asyncio.gather(
            capture("Bybit Kazakhstan", self.bybit),
            capture("Binance Global", self.binance),
        )
        return bybit, binance


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(30.0, max(0.1, float(retry_after)))
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return min(30.0, max(0.1, (retry_time - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    reset_timestamp = response.headers.get("X-Bapi-Limit-Reset-Timestamp")
    if reset_timestamp:
        try:
            return min(30.0, max(0.1, int(reset_timestamp) / 1000 - time.time()))
        except (TypeError, ValueError, OverflowError):
            pass
    return 0.25 * (2**attempt) + random.uniform(0.05, 0.3)


def _to_epoch(value: Any) -> float | None:
    try:
        return int(value) / 1000
    except (TypeError, ValueError):
        return None


def _levels(raw: list[list[str]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for level in raw:
        try:
            price, amount = float(level[0]), float(level[1])
            if price > 0 and amount > 0:
                result.append((price, amount))
        except (TypeError, ValueError, IndexError):
            continue
    return result

