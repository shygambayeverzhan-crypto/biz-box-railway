from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — АрбиСкан Бот, дружелюбный русскоязычный помощник в Telegram. "
    "Отвечай естественно, коротко и по делу, обычно в 2–5 предложениях. "
    "Помогай разобраться в командах бота и общих понятиях крипторынка и спотового арбитража. "
    "Не выдумывай текущие цены, листинги, комиссии, доступность бирж или расчёты связок: "
    "у тебя нет доступа к живым данным; для них направляй пользователя к /status и сигналам. "
    "Не обещай доход, не давай персональных рекомендаций купить или продать и не предлагай обходить "
    "правила площадок. АрбиСкан только показывает оценки и не выставляет ордера. "
    "Если вопрос неясен, задай один короткий уточняющий вопрос."
)


class AIChat:
    def __init__(self, api_key: str, model: str, max_output_tokens: int) -> None:
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=5.0),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        ) if api_key else None
        self._history: dict[int, list[dict[str, str]]] = {}
        self._last_request: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    def clear_user(self, user_id: int) -> None:
        self._history.pop(user_id, None)
        self._last_request.pop(user_id, None)
        self._locks.pop(user_id, None)

    async def ask(
        self,
        user_id: int,
        prompt: str,
        consume_quota: Callable[[], Awaitable[bool]],
    ) -> tuple[str | None, str | None]:
        if not self.client:
            return None, "Пока не подключил ИИ-чат. Я умею принимать /start, /help, /settings и /status."
        prompt = prompt.strip()
        if not prompt:
            return None, "Напиши вопрос обычным сообщением или после команды /ask."
        if len(prompt) > 1000:
            return None, "Сообщение длинновато — сократи вопрос до 1000 символов, пожалуйста."

        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            remaining = 4.0 - (now - self._last_request.get(user_id, 0.0))
            if remaining > 0:
                return None, "Дай мне пару секунд между вопросами 🙂"
            if not await consume_quota():
                return None, "На сегодня лимит вопросов исчерпан. Попробуй завтра 🙂"
            self._last_request[user_id] = now
            history = self._history.get(user_id, [])
            input_messages = [*history, {"role": "user", "content": prompt}]
            try:
                response = await self.client.post(
                    "https://api.openai.com/v1/responses",
                    json={
                        "model": self.model,
                        "instructions": SYSTEM_PROMPT,
                        "input": input_messages,
                        "max_output_tokens": self.max_output_tokens,
                        "reasoning": {"effort": "none"},
                        "store": False,
                    },
                )
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                answer = _output_text(payload)
                if not answer:
                    return None, "Не смог собрать ответ. Попробуй задать вопрос немного иначе."
                self._history[user_id] = [
                    *input_messages,
                    {"role": "assistant", "content": answer},
                ][-6:]
                return answer, None
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                log.warning("OpenAI chat request failed (HTTP %s)", status)
                if status in (401, 403):
                    return None, "Не получилось подключиться к ИИ. Проверьте OPENAI_API_KEY в Railway."
                if status == 429:
                    return None, "OpenAI сейчас ограничил запросы или закончился доступный баланс. Попробуй позже."
                if status == 404:
                    return None, "Выбранная модель OpenAI недоступна для этого API-аккаунта. Проверьте OPENAI_MODEL."
                return None, "Ии-чат временно не отвечает. Сканер сигналов продолжает работать отдельно."
            except (httpx.TimeoutException, httpx.NetworkError):
                log.warning("OpenAI chat request timed out or had a network error")
                return None, "Не дождался ответа ИИ. Попробуй ещё раз чуть позже."
            except (ValueError, KeyError, TypeError):
                log.exception("Could not parse OpenAI response")
                return None, "Получил неожиданный ответ от ИИ. Попробуй позже."


def _output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts).strip()
