from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, TelegramObject

from .calculator import Opportunity, evaluate
from .config import Settings
from .market_data import MarketData, SourceUnavailable
from .store import Store

log = logging.getLogger(__name__)
MAX_ALERTS_PER_MESSAGE = 10
BROADCAST_INTERVAL_SECONDS = 0.05  # 20 messages/second, under Telegram's free broadcast cap.
CHAT_INTERVAL_SECONDS = 1.05  # Small margin below Telegram's 1 message/second per-chat limit.


class CommandThrottle(BaseMiddleware):
    """Suppress repeated commands from one Telegram user; signal delivery is independently paced."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.last_seen: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text and event.text.startswith("/"):
            user = event.from_user
            if user is not None:
                now = time.monotonic()
                previous = self.last_seen.get(user.id, 0.0)
                if now - previous < self.interval:
                    return None
                self.last_seen[user.id] = now
                if len(self.last_seen) > 20_000:
                    cutoff = now - self.interval * 10
                    self.last_seen = {key: stamp for key, stamp in self.last_seen.items() if stamp >= cutoff}
        return await handler(event, data)


def format_signal(items: list[Opportunity]) -> str:
    now = time.time()
    fresh = sorted(items, key=lambda item: item.net_percent, reverse=True)[:MAX_ALERTS_PER_MESSAGE]
    lines = ["<b>Возможные спотовые связки · оценка</b>"]
    for item in fresh:
        symbol = html.quote(item.symbol)
        base = html.quote(item.symbol.removesuffix("USDT"))
        stamp = datetime.fromtimestamp(item.observed_at, tz=timezone.utc).strftime("%H:%M:%S UTC")
        lines.extend([
            f"\n<b>{symbol}: +{item.net_percent:.3f}% оценочно</b>",
            f"Купить {item.buy_exchange}: {item.buy_vwap:.8g} USDT",
            f"Продать {item.sell_exchange}: {item.sell_vwap:.8g} USDT",
            f"Объём оценки: {item.base_amount:.8g} {base} (~{item.buy_quote:.2f} USDT)",
            f"До комиссий: {item.gross_percent:.3f}%; комиссии в модели: "
            f"{item.buy_fee_percent:.3f}% + {item.sell_fee_percent:.3f}%",
            f"Снимки получены: {stamp}",
        ])
    omitted = max(0, len(items) - len(fresh))
    if omitted:
        lines.append(f"\nЕщё связок в этом проходе: {omitted} (не показаны в сообщении).")
    lines.extend([
        "\nИсточник: публичные стаканы Bybit Kazakhstan и Binance Global.",
        "Данные Binance Global не подтверждают цены/доступность локального Binance Kazakhstan.",
        "Расчёт не гарантирует исполнение или доход. Не включены перевод активов и сетевые комиссии; "
        "обычно для арбитража нужен заранее размещённый баланс на обеих площадках.",
        f"Проверено: {datetime.fromtimestamp(now, tz=timezone.utc).strftime('%H:%M:%S UTC')}",
    ])
    return "\n".join(lines)


def signal_key(item: Opportunity) -> tuple[str, str, str]:
    return item.symbol, item.buy_exchange, item.sell_exchange


async def run() -> None:
    settings = Settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty. Copy .env.example to .env and set the token.")

    store = Store(settings.database_path)
    await store.open()
    data = MarketData(settings)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(CommandThrottle())

    last_scan_ok: float | None = None
    source_errors: dict[str, str] = {}
    last_sent: dict[tuple[str, str, str], float] = {}
    pending_keys: set[tuple[str, str, str]] = set()
    alert_queue: asyncio.Queue[list[Opportunity]] = asyncio.Queue(maxsize=1)
    last_chat_delivery: dict[int, float] = {}
    delivery_stats = {"sent": 0, "failed": 0, "expired": 0}
    active_symbols: list[str] = []
    scanner_task: asyncio.Task[None] | None = None
    sender_task: asyncio.Task[None] | None = None

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer("Откройте бота в личном чате и отправьте /start.")
            return
        if message.from_user is None:
            return
        await store.subscribe(message.from_user.id)
        await message.answer(
            "Подписка включена. Для доставки сигналов бот сохраняет ваш Telegram ID; "
            "команда /stop удалит его из списка.\n"
            "Сигналы — оценка публичных котировок, не обещание прибыли.\n"
            "Команды: /help, /settings, /status, /stop"
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Бот сравнивает активные spot-пары USDT на публичных источниках Bybit Kazakhstan и Binance Global.\n"
            "Расчёт использует глубину стакана и заданные комиссии. Binance Global может отличаться от "
            "локального Binance Kazakhstan. Переводы и фактическое исполнение не проверяются.\n"
            "/start — включить сигналы\n/settings — параметры\n/status — состояние\n/stop — удалить подписку"
        )

    @dp.message(Command("settings"))
    async def settings_command(message: Message) -> None:
        await message.answer(
            f"Минимальный оценочный чистый спред: {settings.min_net_spread_percent:.2f}%\n"
            f"Минимальный объём оценки: {settings.min_notional_usdt:.2f} USDT\n"
            f"Максимальная сумма покупки: {settings.max_notional_usdt:.2f} USDT\n"
            f"Активные пары: {', '.join(active_symbols) or 'не найдены'}\n"
            "Комиссии — настройки владельца, они могут отличаться от тарифа пользователя."
        )

    @dp.message(Command("status"))
    async def status_command(message: Message) -> None:
        bybit_state = source_errors.get("Bybit Kazakhstan", "работает" if last_scan_ok else "ожидание проверки")
        binance_state = source_errors.get("Binance Global", "работает" if last_scan_ok else "ожидание проверки")
        scanned = "ещё не было"
        if last_scan_ok:
            scanned = datetime.fromtimestamp(last_scan_ok, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        queue_state = "занята" if not alert_queue.empty() else "пустая"
        await message.answer(
            f"Bybit Kazakhstan API: {bybit_state}\nBinance Global market-data API: {binance_state}\n"
            f"Активные общие пары: {len(active_symbols)}\nПоследний проход: {scanned}\n"
            f"Проверка рынков: {source_errors.get('market validation', 'пройдена' if active_symbols else 'нет общих активных пар')}\n"
            f"Подписчиков: {await store.count()}\nОчередь уведомлений: {queue_state}\n"
            f"Доставлено: {delivery_stats['sent']}; ошибок: {delivery_stats['failed']}; "
            f"пропущено устаревших: {delivery_stats['expired']}"
        )

    @dp.message(Command("stop"))
    async def stop_command(message: Message) -> None:
        if message.from_user is not None:
            await store.delete_subscriber(message.from_user.id)
            last_chat_delivery.pop(message.from_user.id, None)
        await message.answer("Подписка удалена. Чтобы получать сигналы снова, отправьте /start.")

    async def send_one(chat_id: int, text: str, expires_at: float) -> bool | None:
        for attempt in range(2):
            try:
                if time.time() >= expires_at:
                    return None
                await bot.send_message(chat_id, text)
                return True
            except TelegramRetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(min(30.0, max(0.1, float(exc.retry_after))))
                    if time.time() >= expires_at:
                        return None
                else:
                    log.warning("Telegram rate limit persisted for one subscriber")
                    return False
            except TelegramForbiddenError:
                await store.delete_subscriber(chat_id)
                last_chat_delivery.pop(chat_id, None)
                return False
            except TelegramNetworkError as exc:
                if attempt == 0:
                    log.info("Temporary Telegram delivery failure (%s)", type(exc).__name__)
                    await asyncio.sleep(0.5)
                    if time.time() >= expires_at:
                        return None
                else:
                    return False
            except Exception as exc:
                log.warning("Telegram delivery failed (%s)", type(exc).__name__)
                return False
        return False

    async def sender_forever() -> None:
        while True:
            items = await alert_queue.get()
            try:
                represented = sorted(items, key=lambda item: item.net_percent, reverse=True)[:MAX_ALERTS_PER_MESSAGE]
                message_text = format_signal(items)
                subscribers = await store.subscribers()
                delivered = 0
                expired = False
                expires_at = min(item.observed_at for item in items) + settings.max_signal_age_seconds
                for index, user_id in enumerate(subscribers):
                    if not await store.is_subscribed(user_id):
                        continue
                    if time.time() >= expires_at:
                        omitted = len(subscribers) - index
                        delivery_stats["expired"] += omitted
                        expired = True
                        log.warning("Dropped stale alert delivery for %s remaining subscribers", omitted)
                        break
                    wait = CHAT_INTERVAL_SECONDS - (time.monotonic() - last_chat_delivery.get(user_id, 0.0))
                    if wait > 0:
                        await asyncio.sleep(wait)
                    if not await store.is_subscribed(user_id):
                        continue
                    if time.time() >= expires_at:
                        delivery_stats["expired"] += len(subscribers) - index
                        expired = True
                        break
                    delivered_result = await send_one(user_id, message_text, expires_at)
                    if delivered_result is True:
                        delivery_stats["sent"] += 1
                        delivered += 1
                        last_chat_delivery[user_id] = time.monotonic()
                    elif delivered_result is None:
                        delivery_stats["expired"] += len(subscribers) - index
                        expired = True
                        break
                    else:
                        delivery_stats["failed"] += 1
                    if index + 1 < len(subscribers):
                        await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
                if delivered:
                    now = time.time()
                    for item in represented:
                        last_sent[signal_key(item)] = now
                if expired:
                    log.info("Signal broadcast expired before reaching all subscribers")
            except Exception:
                log.exception("Alert delivery batch failed")
            finally:
                pending_keys.difference_update(signal_key(item) for item in items)
                alert_queue.task_done()

    async def enqueue_alerts(items: list[Opportunity]) -> None:
        now = time.time()
        eligible = [
            item for item in items
            if now - last_sent.get(signal_key(item), 0) >= settings.cooldown_seconds
            and signal_key(item) not in pending_keys
        ]
        if not eligible or await store.count() == 0:
            return
        if alert_queue.full():
            old = alert_queue.get_nowait()
            alert_queue.task_done()
            pending_keys.difference_update(signal_key(item) for item in old)
            log.warning("Replaced a queued alert batch with fresher market data")
        eligible = sorted(eligible, key=lambda item: item.net_percent, reverse=True)
        pending_keys.update(signal_key(item) for item in eligible)
        alert_queue.put_nowait(eligible)

    async def scan_forever() -> None:
        nonlocal last_scan_ok
        while True:
            started = time.monotonic()
            opportunities: list[Opportunity] = []
            for symbol in active_symbols:
                try:
                    bybit, binance = await data.pair(symbol)
                    source_errors.pop("Bybit Kazakhstan", None)
                    source_errors.pop("Binance Global", None)
                    now = time.time()
                    for buy, sell, buy_fee, sell_fee in (
                        (bybit, binance, settings.bybit_taker_fee_percent, settings.binance_taker_fee_percent),
                        (binance, bybit, settings.binance_taker_fee_percent, settings.bybit_taker_fee_percent),
                    ):
                        if now - buy.observed_at > settings.max_quote_age_seconds:
                            continue
                        if now - sell.observed_at > settings.max_quote_age_seconds:
                            continue
                        if buy.exchange_timestamp is not None and now - buy.exchange_timestamp > settings.max_quote_age_seconds:
                            continue
                        if abs(buy.observed_at - sell.observed_at) > settings.max_cross_exchange_skew_seconds:
                            continue
                        opportunity = evaluate(
                            buy, sell, buy_fee, sell_fee,
                            settings.max_notional_usdt, settings.min_notional_usdt, now,
                        )
                        if opportunity and opportunity.net_percent >= settings.min_net_spread_percent:
                            opportunities.append(opportunity)
                    last_scan_ok = now
                except SourceUnavailable as exc:
                    source_errors[exc.exchange] = type(exc.cause).__name__
                    log.warning("%s request failed for %s (%s)", exc.exchange, symbol, type(exc.cause).__name__)
                except Exception as exc:
                    log.exception("Scan failed for %s (%s)", symbol, type(exc).__name__)
            if opportunities:
                await enqueue_alerts(opportunities)
            delay = max(0.1, settings.scan_interval_seconds - (time.monotonic() - started))
            await asyncio.sleep(delay)

    async def on_startup() -> None:
        nonlocal active_symbols, scanner_task, sender_task
        active_symbols = await data.validate_markets(settings.symbol_list)
        for symbol, state in data.market_states.items():
            if state != "active on both sources":
                log.warning("Skipping %s: %s", symbol, state)
                if state.startswith("validation error:"):
                    source_errors["market validation"] = state
                    for exchange in ("Bybit Kazakhstan", "Binance Global"):
                        if exchange in state:
                            source_errors[exchange] = state
        if not active_symbols:
            source_errors["market validation"] = "no common active spot symbols"
            log.error("No active, matching spot USDT symbols found on both data sources")
        else:
            source_errors.pop("market validation", None)
        sender_task = asyncio.create_task(sender_forever(), name="telegram-sender")
        scanner_task = asyncio.create_task(scan_forever(), name="market-scanner")
        log.info("Scanner started for %s", ",".join(active_symbols))

    async def on_shutdown() -> None:
        for task in (scanner_task, sender_task):
            if task is not None:
                task.cancel()
        for task in (scanner_task, sender_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await data.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    try:
        await dp.start_polling(bot, close_bot_session=True)
    finally:
        await data.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())

