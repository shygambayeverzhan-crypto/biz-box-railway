from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from aiogram import Bot, Dispatcher, html
from aiogram.client.default import DefaultBotProperties
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, MenuButtonCommands, Message, TelegramObject

from .ai_chat import AIChat
from .calculator import Opportunity, evaluate
from .config import Settings
from .market_data import MarketData
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
        "\nИсточники: публичные стаканы выбранных бирж.",
        "Котировки — справочные; доступность API/рынков не подтверждает доступность торговли для вашего аккаунта или региона.",
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
    ai_chat = AIChat(settings.openai_api_key, settings.openai_model, settings.ai_max_output_tokens)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(CommandThrottle())

    last_scan_ok: float | None = None
    last_scan_qualified = 0
    last_scan_max_net: float | None = None
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
            "Привет! Я АрбиСкан Бот 👋\n\n"
            "Слежу за спотовыми рынками и пришлю сообщение, если найду заметное расхождение цен. "
            "Сейчас подписка включена.\n\n"
            "Это оценка по публичным стаканам: комиссии примерные, а перевод и исполнение я не проверяю. "
            "Для доставки сохраняю ваш Telegram ID; остановить сообщения можно командой /stop.\n\n"
            "Можешь спросить меня обычным сообщением или написать /ask. "
            "/help, /settings, /status, /stop"
        )

    @dp.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Я АрбиСкан Бот. Сравниваю спотовые пары к USDT на доступных публичных API Bybit Kazakhstan и ATAIX Kazakhstan, если у обеих есть активная пара. Учитываю глубину стаканов и модельные комиссии.\n\n"
            "Доступность этих API не означает, что торговля на каждой площадке разрешена или доступна именно вам. "
            "Переводы, комиссии сетей и исполнение сделки я не проверяю. "
            "Любая связка — только оценка, не обещание дохода.\n\n"
            "Можешь задать обычный вопрос ИИ-чату или написать /ask вопрос. "
            "Текст вопроса отправляется в OpenAI; не присылай пароли, API-ключи или личные данные.\n\n"
            "/start — получать сигналы\n/settings — параметры сканирования\n"
            "/status — как идут проверки\n/stop — отключить сигналы"
        )

    @dp.message(Command("settings"))
    async def settings_command(message: Message) -> None:
        await message.answer(
            "Вот с какими параметрами я сейчас сканирую:\n"
            f"• Минимальная чистая разница: {settings.min_net_spread_percent:.2f}%\n"
            f"• Размер оценки: от {settings.min_notional_usdt:.2f} до {settings.max_notional_usdt:.2f} USDT\n"
            f"• Пары, активные на обеих площадках: {', '.join(active_symbols) or 'пока не нашлись'}\n"
            f"• Модель taker-комиссий: Bybit {settings.bybit_taker_fee_percent:.3f}%, "
            f"ATAIX {settings.ataix_taker_fee_percent:.3f}%\n\n"
            "Комиссии заданы владельцем бота и могут отличаться от ваших. Параметры пока общие для всех."
        )

    @dp.message(Command("status"))
    async def status_command(message: Message) -> None:
        states = data.source_states
        scanned = "ещё не было"
        if last_scan_ok:
            scanned = datetime.fromtimestamp(last_scan_ok, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        queue_state = "есть сообщение к отправке" if not alert_queue.empty() else "свободна"
        await message.answer(
            "Я на связи — вот как идут проверки:\n"
            + "\n".join(f"• {name}: {state}" for name, state in states.items()) + "\n"
            f"• Общих активных пар: {len(active_symbols)}\n• Подключённых API: {len(data.active_venues)} из {len(states)}\n• Последняя проверка: {scanned}\n"
            f"• Рынки: {source_errors.get('market validation', 'пары проверены' if active_symbols else 'пока нет общих активных пар')}\n"
            f"• Подписчиков: {await store.count()}\n• Очередь сигналов: {queue_state}\n"
            f"• Связок выше порога в последнем проходе: {last_scan_qualified}; "
            f"максимум после комиссий: {f'{last_scan_max_net:.3f}%' if last_scan_max_net is not None else 'пока нет расчёта'}\n"
            f"• Отправлено: {delivery_stats['sent']}; не доставлено: {delivery_stats['failed']}; "
            f"устарело до отправки: {delivery_stats['expired']}"
        )

    @dp.message(Command("stop"))
    async def stop_command(message: Message) -> None:
        if message.from_user is not None:
            await store.delete_subscriber(message.from_user.id)
            last_chat_delivery.pop(message.from_user.id, None)
            ai_chat.clear_user(message.from_user.id)
        await message.answer("Готово, сигналы отключил. Если захотите вернуться — просто отправьте /start 🙂")

    async def handle_ai_question(message: Message, prompt: str) -> None:
        if message.chat.type != "private" or message.from_user is None:
            return
        user_id = message.from_user.id
        if not await store.is_subscribed(user_id):
            await message.answer("Сначала открой бота в личке и нажми /start, тогда смогу отвечать 🙂")
            return
        if not settings.openai_api_key:
            await message.answer("ИИ-чат пока не подключён. Сигналы и команды /status работают отдельно.")
            return
        answer, error = await ai_chat.ask(
            user_id,
            prompt,
            lambda: store.consume_ai_quota(
                user_id, settings.ai_user_daily_limit, settings.ai_daily_limit
            ),
        )
        await message.answer(html.quote(answer or error or "Не получилось ответить."))

    @dp.message(Command("ask"))
    async def ask_command(message: Message) -> None:
        prompt = (message.text or "").partition(" ")[2].strip()
        if not prompt:
            await message.answer("Напиши вопрос после команды, например: /ask что такое спред?")
            return
        await handle_ai_question(message, prompt)

    @dp.message()
    async def ai_chat_message(message: Message) -> None:
        if message.text and not message.text.startswith("/"):
            await handle_ai_question(message, message.text)

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
        nonlocal last_scan_ok, last_scan_qualified, last_scan_max_net
        while True:
            started = time.monotonic()
            opportunities: list[Opportunity] = []
            max_net: float | None = None
            for symbol in active_symbols:
                try:
                    books = await data.books(symbol)
                    now = time.time()
                    fees = {
                        "Bybit Kazakhstan": settings.bybit_taker_fee_percent,
                        "ATAIX Kazakhstan": settings.ataix_taker_fee_percent,
                    }
                    for left, right in combinations(books, 2):
                        for buy, sell in ((left, right), (right, left)):
                            if now - buy.observed_at > settings.max_quote_age_seconds or now - sell.observed_at > settings.max_quote_age_seconds:
                                continue
                            if buy.exchange_timestamp is not None and now - buy.exchange_timestamp > settings.max_quote_age_seconds:
                                continue
                            if sell.exchange_timestamp is not None and now - sell.exchange_timestamp > settings.max_quote_age_seconds:
                                continue
                            if abs(buy.observed_at - sell.observed_at) > settings.max_cross_exchange_skew_seconds:
                                continue
                            opportunity = evaluate(
                                buy, sell, fees[buy.exchange], fees[sell.exchange],
                                settings.max_notional_usdt, settings.min_notional_usdt, now,
                            )
                            if opportunity and (max_net is None or opportunity.net_percent > max_net):
                                max_net = opportunity.net_percent
                            if opportunity and opportunity.net_percent >= settings.min_net_spread_percent:
                                opportunities.append(opportunity)
                    if books:
                        last_scan_ok = now
                except Exception as exc:
                    log.exception("Scan failed for %s (%s)", symbol, type(exc).__name__)
            last_scan_qualified = len(opportunities)
            last_scan_max_net = max_net
            if opportunities:
                await enqueue_alerts(opportunities)
            delay = max(0.1, settings.scan_interval_seconds - (time.monotonic() - started))
            await asyncio.sleep(delay)

    async def on_startup() -> None:
        nonlocal active_symbols, scanner_task, sender_task
        try:
            await bot.set_my_name("АрбиСкан Бот", language_code="ru")
            await bot.set_my_description(
                "Сканирую спотовые рынки и присылаю оценку межбиржевых ценовых расхождений. "
                "Сигналы не гарантируют исполнение или доход.", language_code="ru"
            )
            await bot.set_my_short_description(
                "Спотовый сканер ценовых расхождений. Только оценки, без торговли.", language_code="ru"
            )
            await bot.set_my_commands([
                BotCommand(command="start", description="Включить сигналы"),
                BotCommand(command="help", description="Как работает сканер"),
                BotCommand(command="settings", description="Параметры сканирования"),
                BotCommand(command="status", description="Состояние проверок"),
                BotCommand(command="ask", description="Задать вопрос ИИ"),
                BotCommand(command="stop", description="Отключить сигналы"),
            ], language_code="ru")
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception as exc:
            log.warning("Could not update Telegram bot profile (%s)", type(exc).__name__)
        active_symbols = await data.validate_markets(settings.symbol_list)
        for symbol, state in data.market_states.items():
            log.info("Market %s available on: %s", symbol, state)
        if not active_symbols:
            source_errors["market validation"] = "нет пары из SYMBOLS, активной одновременно на Bybit.kz и ATAIX.kz"
            log.error("No active, matching spot USDT symbols found on at least two data sources")
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
        await ai_chat.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    try:
        await dp.start_polling(bot, close_bot_session=True)
    finally:
        await data.close()
        await ai_chat.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run())
