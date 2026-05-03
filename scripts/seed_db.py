"""Seed a Telegram user's wallet from the CLI.

Usage:
    uv run python scripts/seed_db.py <telegram_user_id> <card_id> [<card_id> ...]

Useful for friend onboarding ("get your TG user id from @userinfobot, paste
it here") or for resetting a known wallet during development.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Allow running as a plain script as well as via `python -m scripts.seed_db`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cardkaki.data import load_cards  # noqa: E402
from cardkaki.storage import Storage  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("DB_PATH", ROOT / "data" / "users.sqlite"))
DEFAULT_CARDS = ROOT / "data" / "cards.yaml"


async def main(uid: int, card_ids: list[str]) -> int:
    cards = load_cards(DEFAULT_CARDS)
    bad = [c for c in card_ids if c not in cards]
    if bad:
        print(f"unknown card ids: {bad}", file=sys.stderr)
        print(f"valid: {sorted(cards)}", file=sys.stderr)
        return 2
    store = Storage(DEFAULT_DB)
    await store.init()
    for cid in card_ids:
        added = await store.add_card(uid, cid)
        marker = "+" if added else "·"
        print(f"  {marker} {cid} — {cards[cid].name}")
    print(f"seeded user {uid} with {len(card_ids)} card(s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    code = asyncio.run(main(int(sys.argv[1]), sys.argv[2:]))
    sys.exit(code)
