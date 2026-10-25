# Архитектура MVP

## Допущения

- Аудитория — пользователи Telegram в Казахстане.
- Сигналы отправляются только в личный чат после `/start`; MVP не проводит торговых операций.
- Источники данных: публичные spot API Bybit Kazakhstan (`api.bybit.kz`) и ATAIX Kazakhstan (`api.ataix.kz`). Binance Global не используется как замена локальному Binance Kazakhstan.
- Сканируются пользовательские символы с котируемым активом USDT. На старте доступны только рынки, которые API обоих источников сообщает как активные и совпадающие по base/quote.
- Комиссии — задаваемые владельцем оценки. Балансы, индивидуальные тарифы, переводы и сети не запрашиваются.
- Запуск — Python 3.12+, Telegram long polling; постоянный публичный IP, домен и webhook не нужны. Для 24/7 нужен постоянно работающий хост.

## Компоненты и поток данных

```text
Telegram command -> aiogram dispatcher -> command throttle -> SQLite subscription

startup -> validate instruments on both public APIs -> active common symbols
scanner -> Bybit + ATAIX spot books -> freshness/skew/liquidity checks
        -> two-direction VWAP + estimated fees -> threshold/cooldown
        -> one-slot coalescing alert queue -> paced Telegram sender -> opted-in DMs
Telegram text /ask -> quota check -> OpenAI Responses API (store=false) -> short reply
Telegram /settings -> per-user pair and threshold preferences -> filtered signal DM
Telegram /diagnostics and /history -> venue health and recent saved estimates
```

| Файл | Роль |
|---|---|
| `src/arbscanner/config.py` | Переменные окружения и проверка конфигурации |
| `src/arbscanner/market_data.py` | Bybit/ATAIX REST adapters, активность рынков, retries, rate spacing, circuit breakers, данные стаканов |
| `src/arbscanner/ai_chat.py` | Отдельный платный ИИ-чат через OpenAI Responses API; краткая история в памяти, лимиты ответов |
| `src/arbscanner/calculator.py` | Объём, VWAP и оценка результата после условных комиссий |
| `src/arbscanner/store.py` | SQLite подписчиков и удаление подписки |
| `src/arbscanner/bot.py` | Telegram-команды, throttling, scanner loop, очередь/pacing доставки, статусы |
| `src/arbscanner/__main__.py` | Запуск приложения |
| `pyproject.toml` | Python package и зафиксированные верхнеуровневые версии библиотек |
| `.env.example` | Пустой шаблон конфигурации |
| `Dockerfile`, `docker-compose.yml` | Контейнерный запуск и постоянный volume |
| `README.md` | Локальный/VPS запуск, эксплуатация и ограничения |

## Команды Telegram

| Команда | Действие |
|---|---|
| `/start` | Добавляет пользователя в список уведомлений и сообщает, что его Telegram ID хранится до `/stop` |
| `/help` | Кратко объясняет источники данных и границы сигнала |
| `/settings` | Показывает активные пары и параметры расчёта |
| `/status` | Показывает доступность API, активные рынки, очередь и счётчики доставки |
| `/diagnostics` | Показывает состояние API, рынков и причину отсутствия расчётов |
| `/history` | Последние 10 расчётных возможностей; хранится максимум 1000 |
| `/ask вопрос` | Задаёт вопрос ИИ; обычный текст тоже направляется в чат |
| `/stop` | Удаляет Telegram ID из SQLite; повторно включить можно `/start` |

Административных команд в MVP нет. Поэтому admin allowlist пока не нужен; если появятся закрытые действия, их надо добавить отдельным маршрутом с allowlist Telegram ID.

## Формула

Для базового количества `q` расчёт проходит по asks рынка покупки и bids рынка продажи до указанного количества. Если глубины одной стороны недостаточно, объём ограничивается меньшей стороной. Количество дополнительно выбирается так, чтобы сумма покупки не превышала `MAX_NOTIONAL_USDT`. По исполненным уровням считаются quote-обороты и VWAP.

`gross_pct = (sell_quote - buy_quote) / buy_quote × 100`

`net_pct = (sell_quote × (1 - sell_fee) - buy_quote × (1 + buy_fee)) / (buy_quote × (1 + buy_fee)) × 100`

Значения `buy_fee` / `sell_fee` переводятся из процентов в доли. Сигнал отправляется, если расчётная сумма не меньше `MIN_NOTIONAL_USDT`, чистый результат не ниже `MIN_NET_SPREAD_PERCENT`, снимки свежие и их время получения не слишком разнесено. На каждый рынок считаются оба направления.

## Надёжность и лимиты

- Каждая площадка имеет независимую очередь запросов с минимальным интервалом, таймауты и до трёх попыток.
- 429 и 5xx повторяются с учётом `Retry-After`, если оно присутствует; сетевые повторы используют exponential backoff + jitter.
- После трёх подряд неудачных циклов запросов circuit breaker открывается на 30 секунд. Следующая попытка после паузы служит проверочной.
- Сообщения рассылки ограничены 20/сек глобально (предел ниже бесплатного лимита массовой рассылки Telegram); дополнительно соблюдается минимум 1 секунда между сообщениями в одном личном чате. В одной DM уходит не больше одного агрегированного сообщения за проход сканера.
- Очередь хранит один пакет; более свежие сигналы могут заменить ожидающий пакет. Через `MAX_SIGNAL_AGE_SECONDS` недоставленная часть отбрасывается и учитывается в `/status`.
- Эти ограничения делают MVP подходящим для небольшой аудитории. Большая аудитория требует Telegram-канала как fan-out или другой архитектуры; частная рассылка не обеспечивает мгновенную доставку всем пользователям.
- SQLite подходит для одного процесса/узла. Горизонтально масштабировать несколько экземпляров с одной SQLite-базой не следует.
- `user_settings` хранит индивидуальный выбор пар/порога. `/settings` меняет их inline-кнопками, а отправитель фильтрует сигналы по настройкам каждого подписчика.
- `signal_history` хранит до 1000 последних оценок. Состояние API проверяется при проходе сканера и повторной проверке каталогов каждую минуту; смена состояния отправляется подписчикам.
- Для ответов ИИ текст пользователя передаётся в OpenAI API; ответ использует `store=false`. Полная переписка не сохраняется в SQLite, краткая история существует только в RAM процесса. SQLite содержит дневные счётчики квот. `/stop` очищает историю в RAM.
- ИИ не получает стаканы, не рассчитывает сигналы, не открывает веб-поиск и не торгует. API usage имеет отдельную оплату и дневные ограничения: 25 вызовов на весь бот и 5 на пользователя по умолчанию; `AI_DAILY_LIMIT=0` полностью отключает запросы.

## Проверенные официальные документы

- Bybit regional API hosts и `api.bybit.kz`: [Integration Guidance](https://bybit-exchange.github.io/docs/v5/guide).
- Bybit REST IP guardrail — до 600 запросов за окно 5 секунд на IP, включая локальные хосты; данный MVP ограничивает частоту адаптера существенно ниже этого значения: [Rate Limit Rules](https://bybit-exchange.github.io/docs/v5/rate-limit).
- Bybit spot orderbook endpoint, поля и порядок bids/asks: [Get Orderbook](https://bybit-exchange.github.io/docs/v5/market/orderbook).
- Bybit spot instrument status/base/quote: [Get Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument).
- ATAIX public symbols and order book formats: [market symbols](https://api.ataix.kz/api/symbols) and [sample order book](https://api.ataix.kz/api/book/BTC-USDT).
- OpenAI Responses API Python SDK quickstart and request format: [OpenAI Docs](https://developers.openai.com/api/docs/quickstart); `store=false` disables response storage: [Responses API reference](https://developers.openai.com/api/reference/python/resources/responses/methods/create).
- OpenAI GPT-6 Luna price: [model page](https://developers.openai.com/api/docs/models/gpt-6-luna). The configured call cap limits volume but is not a fixed dollar billing cap.
- aiogram async API и polling: [aiogram documentation](https://docs.aiogram.dev/en/latest/).
- Telegram long polling и broadcast pacing: [Bots FAQ](https://core.telegram.org/bots/faq).

## Ограничения данных

- Ни Bybit, ни ATAIX response не дают сопоставимый timestamp снимков; записывается локальное время получения ответа, поэтому skew — приближённый фильтр.
- Данные являются snapshots и могут устареть после отправки. Частые стратегии требуют WebSocket snapshot/delta sync и локальной книги стакана.
- Не учитываются фактические размеры лота, точный персональный fee asset, account balance, сетевой ввод/вывод, лимиты переводов, проскальзывание после snapshot, частичное исполнение, комиссии перевода или регулирование.
- Railway US West отдавал HTTP 403 от `api.bybit.kz` при проверке, поэтому размещение должно быть в регионе, который не блокируется API; этот региональный сбой не исправляется OpenAI API key.


