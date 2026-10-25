from __future__ import annotations

from pathlib import Path

import aiosqlite


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._legacy_active_column = False

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=10000")
            cur = await db.execute("PRAGMA table_info(subscribers)")
            columns = {str(row[1]) for row in await cur.fetchall()}
            self._legacy_active_column = "active" in columns
            await db.execute("""CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS ai_usage (
                user_id INTEGER NOT NULL,
                usage_day TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_day)
            )""")
            await db.commit()

    async def consume_ai_quota(self, user_id: int, user_limit: int, daily_limit: int) -> bool:
        """Atomically charge one daily AI request before sending it to the paid API."""
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=10000")
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT requests FROM ai_usage WHERE user_id=? AND usage_day=date('now')",
                (user_id,),
            )
            row = await cur.fetchone()
            user_used = int(row[0]) if row else 0
            cur = await db.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM ai_usage WHERE usage_day=date('now')"
            )
            total_row = await cur.fetchone()
            total_used = int(total_row[0] if total_row else 0)
            if user_used >= user_limit or total_used >= daily_limit:
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO ai_usage(user_id, usage_day, requests) VALUES (?, date('now'), 1) "
                "ON CONFLICT(user_id, usage_day) DO UPDATE SET requests=requests+1",
                (user_id,),
            )
            await db.commit()
            return True

    async def subscribe(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=10000")
            if self._legacy_active_column:
                await db.execute(
                    "INSERT INTO subscribers(user_id, active) VALUES (?, 1) "
                    "ON CONFLICT(user_id) DO UPDATE SET active=1", (user_id,)
                )
            else:
                await db.execute("INSERT OR IGNORE INTO subscribers(user_id) VALUES (?)", (user_id,))
            await db.commit()

    async def delete_subscriber(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=10000")
            await db.execute("DELETE FROM subscribers WHERE user_id=?", (user_id,))
            await db.commit()

    async def subscribers(self) -> list[int]:
        async with aiosqlite.connect(self.path, timeout=10) as db:
            query = "SELECT user_id FROM subscribers"
            if self._legacy_active_column:
                query += " WHERE active=1"
            cur = await db.execute(query + " ORDER BY created_at")
            return [int(row[0]) for row in await cur.fetchall()]

    async def count(self) -> int:
        async with aiosqlite.connect(self.path, timeout=10) as db:
            query = "SELECT COUNT(*) FROM subscribers"
            if self._legacy_active_column:
                query += " WHERE active=1"
            cur = await db.execute(query)
            row = await cur.fetchone()
            return int(row[0] if row else 0)

    async def is_subscribed(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path, timeout=10) as db:
            query = "SELECT 1 FROM subscribers WHERE user_id=?"
            if self._legacy_active_column:
                query += " AND active=1"
            cur = await db.execute(query, (user_id,))
            return await cur.fetchone() is not None
