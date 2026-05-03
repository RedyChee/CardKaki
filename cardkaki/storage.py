"""SQLite-backed user wallet store. Single-writer (the bot process)."""
from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  telegram_user_id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS user_cards (
  telegram_user_id INTEGER NOT NULL,
  card_id TEXT NOT NULL,
  added_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (telegram_user_id, card_id),
  FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.executescript(SCHEMA)
            await db.commit()

    async def upsert_user(self, telegram_user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
                (telegram_user_id,),
            )
            await db.commit()

    async def add_card(self, telegram_user_id: int, card_id: str) -> bool:
        """Returns True if newly added, False if already present."""
        await self.upsert_user(telegram_user_id)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO user_cards (telegram_user_id, card_id) VALUES (?, ?)",
                (telegram_user_id, card_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def remove_card(self, telegram_user_id: int, card_id: str) -> bool:
        """Returns True if removed, False if wasn't there."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM user_cards WHERE telegram_user_id = ? AND card_id = ?",
                (telegram_user_id, card_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_cards(self, telegram_user_id: int) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT card_id FROM user_cards WHERE telegram_user_id = ? ORDER BY added_at",
                (telegram_user_id,),
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows]
