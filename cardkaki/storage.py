"""SQLite-backed user wallet store. Single-writer (the bot process)."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path

import aiosqlite

from .models import TxnRow

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
CREATE TABLE IF NOT EXISTS transactions (
  tx_id TEXT PRIMARY KEY,
  telegram_user_id INTEGER NOT NULL,
  card_id TEXT NOT NULL,
  bonus_idx INTEGER,
  bonus_label TEXT,
  merchant TEXT NOT NULL,
  amount_sgd REAL NOT NULL,
  is_fcy INTEGER NOT NULL DEFAULT 0,
  miles_earned INTEGER NOT NULL,
  txn_date TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_txns_user_date
  ON transactions (telegram_user_id, txn_date);
CREATE INDEX IF NOT EXISTS idx_txns_user_card_bonus_date
  ON transactions (telegram_user_id, card_id, bonus_idx, txn_date);
CREATE TABLE IF NOT EXISTS card_statement_days (
  telegram_user_id INTEGER NOT NULL,
  card_id TEXT NOT NULL,
  statement_day INTEGER NOT NULL CHECK (statement_day BETWEEN 1 AND 28),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (telegram_user_id, card_id),
  FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS lady_choices (
  telegram_user_id INTEGER NOT NULL,
  category TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  PRIMARY KEY (telegram_user_id, effective_from),
  FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS card_posting_delays (
  telegram_user_id INTEGER NOT NULL,
  card_id TEXT NOT NULL,
  delay_days INTEGER NOT NULL CHECK (delay_days BETWEEN 0 AND 7),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (telegram_user_id, card_id),
  FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS card_anniversaries (
  telegram_user_id INTEGER NOT NULL,
  card_id TEXT NOT NULL,
  anniversary_month INTEGER NOT NULL CHECK (anniversary_month BETWEEN 1 AND 12),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (telegram_user_id, card_id),
  FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id) ON DELETE CASCADE
);
"""


def _row_to_txn(row) -> TxnRow:
    return TxnRow(
        tx_id=row[0],
        telegram_user_id=row[1],
        card_id=row[2],
        bonus_idx=row[3],
        bonus_label=row[4],
        merchant=row[5],
        amount_sgd=row[6],
        is_fcy=bool(row[7]),
        miles_earned=row[8],
        txn_date=date.fromisoformat(row[9]),
        created_at=datetime.fromisoformat(row[10]),
    )


_TXN_COLS = (
    "tx_id, telegram_user_id, card_id, bonus_idx, bonus_label, "
    "merchant, amount_sgd, is_fcy, miles_earned, txn_date, created_at"
)


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

    # ------------------------------------------------------------------
    # Transactions (v2)
    # ------------------------------------------------------------------

    async def log_transaction(
        self,
        *,
        telegram_user_id: int,
        card_id: str,
        bonus_idx: int | None,
        bonus_label: str | None,
        merchant: str,
        amount_sgd: float,
        is_fcy: bool,
        miles_earned: int,
        txn_date: date,
    ) -> str:
        """Insert a new transaction; returns the generated tx_id."""
        await self.upsert_user(telegram_user_id)
        tx_id = uuid.uuid4().hex
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                INSERT INTO transactions
                  (tx_id, telegram_user_id, card_id, bonus_idx, bonus_label,
                   merchant, amount_sgd, is_fcy, miles_earned, txn_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_id,
                    telegram_user_id,
                    card_id,
                    bonus_idx,
                    bonus_label,
                    merchant,
                    float(amount_sgd),
                    1 if is_fcy else 0,
                    int(miles_earned),
                    txn_date.isoformat(),
                ),
            )
            await db.commit()
        return tx_id

    async def get_transaction(self, telegram_user_id: int, tx_id: str) -> TxnRow | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT {_TXN_COLS} FROM transactions WHERE tx_id = ? AND telegram_user_id = ?",
                (tx_id, telegram_user_id),
            )
            row = await cur.fetchone()
        return _row_to_txn(row) if row else None

    async def delete_transaction(self, telegram_user_id: int, tx_id: str) -> bool:
        """Returns True if a row was deleted; False if no such row for this user."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM transactions WHERE tx_id = ? AND telegram_user_id = ?",
                (tx_id, telegram_user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def restore_transaction(self, txn: TxnRow) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                INSERT OR IGNORE INTO transactions
                  (tx_id, telegram_user_id, card_id, bonus_idx, bonus_label,
                   merchant, amount_sgd, is_fcy, miles_earned, txn_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    txn.tx_id,
                    txn.telegram_user_id,
                    txn.card_id,
                    txn.bonus_idx,
                    txn.bonus_label,
                    txn.merchant,
                    float(txn.amount_sgd),
                    1 if txn.is_fcy else 0,
                    int(txn.miles_earned),
                    txn.txn_date.isoformat(),
                ),
            )
            await db.commit()

    async def list_transactions_since(
        self, telegram_user_id: int, since: date
    ) -> list[TxnRow]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT {_TXN_COLS} FROM transactions "
                "WHERE telegram_user_id = ? AND txn_date >= ? "
                "ORDER BY txn_date, created_at",
                (telegram_user_id, since.isoformat()),
            )
            rows = await cur.fetchall()
        return [_row_to_txn(r) for r in rows]

    async def recent_transactions(
        self, telegram_user_id: int, limit: int = 10
    ) -> list[TxnRow]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                f"SELECT {_TXN_COLS} FROM transactions "
                "WHERE telegram_user_id = ? "
                "ORDER BY txn_date DESC, created_at DESC "
                "LIMIT ?",
                (telegram_user_id, int(limit)),
            )
            rows = await cur.fetchall()
        return [_row_to_txn(r) for r in rows]

    # ------------------------------------------------------------------
    # Statement days (v2)
    # ------------------------------------------------------------------

    async def set_statement_day(
        self, telegram_user_id: int, card_id: str, statement_day: int
    ) -> None:
        if not (1 <= int(statement_day) <= 28):
            raise ValueError("statement_day must be between 1 and 28")
        await self.upsert_user(telegram_user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO card_statement_days (telegram_user_id, card_id, statement_day)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id, card_id) DO UPDATE SET
                  statement_day = excluded.statement_day,
                  updated_at = datetime('now')
                """,
                (telegram_user_id, card_id, int(statement_day)),
            )
            await db.commit()

    async def get_statement_days(self, telegram_user_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT card_id, statement_day FROM card_statement_days "
                "WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            rows = await cur.fetchall()
        return {cid: int(d) for cid, d in rows}

    # ------------------------------------------------------------------
    # Lady's Card chosen category (v2)
    # ------------------------------------------------------------------

    async def set_lady_choice(
        self, telegram_user_id: int, category: str, effective_from: date
    ) -> None:
        await self.upsert_user(telegram_user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO lady_choices (telegram_user_id, category, effective_from)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id, effective_from) DO UPDATE SET
                  category = excluded.category
                """,
                (telegram_user_id, category, effective_from.isoformat()),
            )
            await db.commit()

    async def get_lady_choice(
        self, telegram_user_id: int, today: date
    ) -> str | None:
        """Returns the most recent category whose effective_from is on or before `today`."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT category FROM lady_choices "
                "WHERE telegram_user_id = ? AND effective_from <= ? "
                "ORDER BY effective_from DESC LIMIT 1",
                (telegram_user_id, today.isoformat()),
            )
            row = await cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # Posting delays (v3)
    # ------------------------------------------------------------------

    async def set_posting_delay(
        self, telegram_user_id: int, card_id: str, delay_days: int
    ) -> None:
        if not (0 <= int(delay_days) <= 7):
            raise ValueError("delay_days must be between 0 and 7")
        await self.upsert_user(telegram_user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO card_posting_delays (telegram_user_id, card_id, delay_days)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id, card_id) DO UPDATE SET
                  delay_days = excluded.delay_days,
                  updated_at = datetime('now')
                """,
                (telegram_user_id, card_id, int(delay_days)),
            )
            await db.commit()

    async def get_posting_delays(self, telegram_user_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT card_id, delay_days FROM card_posting_delays "
                "WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            rows = await cur.fetchall()
        return {cid: int(d) for cid, d in rows}

    # ------------------------------------------------------------------
    # Card anniversaries (v3)
    # ------------------------------------------------------------------

    async def set_anniversary(
        self, telegram_user_id: int, card_id: str, anniversary_month: int
    ) -> None:
        if not (1 <= int(anniversary_month) <= 12):
            raise ValueError("anniversary_month must be between 1 and 12")
        await self.upsert_user(telegram_user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO card_anniversaries (telegram_user_id, card_id, anniversary_month)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id, card_id) DO UPDATE SET
                  anniversary_month = excluded.anniversary_month,
                  updated_at = datetime('now')
                """,
                (telegram_user_id, card_id, int(anniversary_month)),
            )
            await db.commit()

    async def get_anniversaries(self, telegram_user_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT card_id, anniversary_month FROM card_anniversaries "
                "WHERE telegram_user_id = ?",
                (telegram_user_id,),
            )
            rows = await cur.fetchall()
        return {cid: int(m) for cid, m in rows}
