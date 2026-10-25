from __future__ import annotations

from dataclasses import dataclass

from .market_data import Book


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_vwap: float
    sell_vwap: float
    base_amount: float
    buy_quote: float
    sell_quote: float
    gross_percent: float
    net_percent: float
    buy_fee_percent: float
    sell_fee_percent: float
    observed_at: float


def _quote_for(levels: list[tuple[float, float]], amount: float) -> float | None:
    remaining = amount
    quote = 0.0
    for price, available in levels:
        take = min(remaining, available)
        quote += take * price
        remaining -= take
        if remaining <= 1e-12:
            return quote
    return None


def evaluate(
    buy_book: Book,
    sell_book: Book,
    buy_fee_percent: float,
    sell_fee_percent: float,
    max_notional: float,
    min_notional: float,
    now: float,
) -> Opportunity | None:
    if not buy_book.asks or not sell_book.bids or buy_book.symbol != sell_book.symbol:
        return None
    upper = min(sum(qty for _, qty in buy_book.asks), sum(qty for _, qty in sell_book.bids))
    upper = min(upper, max_notional / buy_book.asks[0][0])
    if upper <= 0:
        return None

    # Binary-search the largest base quantity whose actual buy-side VWAP stays under the cap.
    low, high = 0.0, upper
    for _ in range(36):
        mid = (low + high) / 2
        cost = _quote_for(buy_book.asks, mid)
        if cost is not None and cost <= max_notional:
            low = mid
        else:
            high = mid
    amount = low
    if amount <= 1e-10:
        return None

    buy_quote = _quote_for(buy_book.asks, amount)
    sell_quote = _quote_for(sell_book.bids, amount)
    if buy_quote is None or sell_quote is None or buy_quote < min_notional:
        return None

    buy_vwap, sell_vwap = buy_quote / amount, sell_quote / amount
    gross = (sell_quote - buy_quote) / buy_quote * 100
    net_revenue = sell_quote * (1 - sell_fee_percent / 100)
    net_cost = buy_quote * (1 + buy_fee_percent / 100)
    net = (net_revenue - net_cost) / net_cost * 100
    return Opportunity(
        symbol=buy_book.symbol,
        buy_exchange=buy_book.exchange,
        sell_exchange=sell_book.exchange,
        buy_vwap=buy_vwap,
        sell_vwap=sell_vwap,
        base_amount=amount,
        buy_quote=buy_quote,
        sell_quote=sell_quote,
        gross_percent=gross,
        net_percent=net,
        buy_fee_percent=buy_fee_percent,
        sell_fee_percent=sell_fee_percent,
        observed_at=now,
    )

