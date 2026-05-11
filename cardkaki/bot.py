"""Telegram handlers, factored as pure helpers + thin PTB wiring.

Helpers (`format_*`, `handle_*`) take primitives and return reply strings,
which is what `tests/test_bot_handlers.py` exercises. The PTB application
factory at the bottom wires those helpers to commands/messages.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .data import MerchantEntry
from .models import Card, Recommendation, TxnRow
from .parser import parse_async
from .periods import days_left, period_bounds, period_label
from .posting import posting_period_warning, resolve_posting_date
from .rule_engine import recommend, select_bonus_for_log
from .storage import Storage
from .usage import build_usage


def _resolve_merchant(
    merchants: dict[str, MerchantEntry], key: str
) -> tuple[list[str], bool]:
    """Return (categories, same_day_posting) for a merchant key."""
    entry = merchants.get(key)
    if entry is None:
        return [], False
    return entry.categories, entry.same_day_posting

log = logging.getLogger(__name__)


def _md(s: str) -> str:
    """Escape Telegram Markdown V1 special chars in dynamic content."""
    return s.replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


WELCOME = (
    "👋 *CardKaki* — your SG miles co-pilot.\n\n"
    "Just type a merchant and amount:\n"
    "`cold storage 45`  ·  `klook 320 fcy`\n\n"
    "After a recommendation, tap the card button to log it.\n"
    "Use /cards to manage your wallet.\n\n"
    "_Rates are best-effort (Milelion). Verify against your statement._"
)

HELP_SIMPLE = (
    "*CardKaki* — SG miles co-pilot\n\n"
    "*Quick query*\n"
    "Send `merchant amount` to get a recommendation.\n"
    "e.g. `cold storage 45`  ·  `klook 320 fcy`\n\n"
    "*Log a transaction*\n"
    "After a recommendation, tap the card button to log it.\n"
    "Or use /log to pick a card and date interactively.\n\n"
    "*Manage your wallet*\n"
    "Use /cards to add or remove cards and set your statement closing day.\n\n"
    "Other commands: /pools · /recent · /lady\\_choice\n\n"
    "_Rates are best-effort (Milelion). Verify against your statement._"
)

HELP_ADVANCED = (
    "*CardKaki* — command reference\n\n"
    "`/log <merchant> <amount> [fcy]`\n"
    "`/log <card\\_id> <merchant> <amount> [fcy] [yyyy-mm-dd]`\n"
    "`/cards` — interactive wallet manager\n"
    "`/pools` — cap usage by period\n"
    "`/recent` — last transactions\n"
    "`/lady\\_choice [category]` — set UOB Lady's bonus\n"
    "`/hsbc\\_tier <regular|enhanced>` — pick HSBC Revo earn tier "
    "(enhanced = 8mpd needs S$50K+ in HSBC Everyday Global Account)\n\n"
    "_Rates are best-effort (Milelion). Verify against your statement._"
)

_HELP_SIMPLE_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("⚙️ Advanced syntax", callback_data="help:advanced"),
]])
_HELP_ADVANCED_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("← Simple guide", callback_data="help:simple"),
]])


LADY_CATEGORY_TAGS: dict[str, set[str]] = {
    "dining":        {"dining_local", "dining_delivery"},
    "beauty":        {"beauty"},
    "entertainment": {"entertainment"},
    "fashion":       {"fashion", "dept_store"},
    "family":        {"groceries"},
    "transport":     {"transport"},
    "travel":        {"travel"},
}

_LADY_CATEGORY_LABELS: dict[str, str] = {
    "dining":        "Dining",
    "beauty":        "Beauty & Wellness",
    "entertainment": "Entertainment",
    "fashion":       "Fashion",
    "family":        "Family",
    "transport":     "Transport",
    "travel":        "Travel",
}

# Cards whose bonuses use statement_month/statement_quarter periods.
# Computed at module load when the catalog is passed in; see _needs_statement_day.


# ---------------------------------------------------------------------------
# Pure formatters
# ---------------------------------------------------------------------------

def format_recommendations(
    recs: list[Recommendation],
    *,
    merchant: str,
    amount_sgd: float,
    is_fcy: bool,
    unknown_merchant: bool = False,
    all_excluded: bool = False,
    lady_choice: str | None = None,
) -> str:
    """Format the bot's reply for a transaction lookup."""
    if not recs:
        return (
            "Your wallet is empty — add cards first.\n"
            "Try /cards to add some."
        )

    header_bits: list[str] = []
    if unknown_merchant:
        header_bits.append(
            f"⚠️ Unknown merchant `{merchant}` — assuming generic spend "
            f"(no category bonus)."
        )
    if all_excluded:
        header_bits.append(
            "⚠️ Most 4mpd cards exclude this category — falling back to base rates."
        )
    if is_fcy:
        header_bits.append(f"💱 FCY — fee adds ~3.25% to your cost.")

    top = recs[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    rendered_lines: list[str] = []
    tie_miles = top[0].miles if (len(top) >= 2 and top[0].miles == top[1].miles and top[0].miles > 0) else None
    for i, r in enumerate(top):
        tie_note = "  _(tied)_" if tie_miles is not None and r.miles == tie_miles and i > 0 else ""
        reason = r.reasons[0] if r.reasons else ""
        if lady_choice and r.card_id == "uob_lady" and reason == "✓ chosen category":
            reason = f"✓ {_LADY_CATEGORY_LABELS.get(lady_choice, lady_choice.capitalize())}"
        icon = "⭐" if reason.startswith("✓") else "♾ "
        rendered_lines.append(f"{medals[i]} *{_md(r.card_name)}* {icon} {r.effective_mpd:.1f} mpd{tie_note}")
        # Primary reason on its own indented line(s) — only for bonus/excluded cards
        if reason and reason != "generic spend":
            parts = _split_label_parts(reason)
            for part in parts:
                if part.startswith("✓") or part.startswith("⚠") or part.startswith("—"):
                    rendered_lines.append(f"   {_md(part)}")
                else:
                    rendered_lines.append(f"   ✓ {_md(part)}")
        # Secondary cap/posting warnings inline under the card
        for extra in r.reasons[1:]:
            if extra.startswith("⚠") or extra.startswith("cap "):
                rendered_lines.append(f"   {_md(extra)}")
        if r.posting_warning:
            rendered_lines.append(f"   ⚠ {_md(r.posting_warning)}")
        rendered_lines.append("")

    return "\n".join(header_bits + rendered_lines).rstrip()


def format_card_list(card_ids: list[str], catalog: dict[str, Card]) -> str:
    if not card_ids:
        return (
            "You haven't added any cards yet.\n"
            "Try `/cards add hsbc_revo` (see /start for valid ids)."
        )
    lines = ["Your wallet:"]
    for cid in card_ids:
        c = catalog.get(cid)
        name = c.name if c else cid
        lines.append(f"• `{cid}` — {name}")
    return "\n".join(lines)


def format_catalog(catalog: dict[str, Card]) -> str:
    lines = ["Available cards:"]
    for cid in sorted(catalog):
        lines.append(f"• `{cid}` — {catalog[cid].name}")
    return "\n".join(lines)


def format_cards_help(catalog: dict[str, Card]) -> str:
    valid = ", ".join(f"`{cid}`" for cid in sorted(catalog))
    return (
        "Usage:\n"
        "  `/cards list` — show your wallet\n"
        "  `/cards add <id>` — add a card\n"
        "  `/cards remove <id>` — drop a card\n"
        "  `/cards catalog` — list all available card ids\n\n"
        f"Valid ids: {valid}"
    )


def format_menu_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 My Cards", callback_data="ck:wallet"),
        InlineKeyboardButton("🔍 Browse & Add", callback_data="ck:catalog"),
    ]])
    return "📋 *CardKaki Wallet*\nManage your cards:", kb


def format_wallet_keyboard(
    card_ids: list[str], catalog: dict[str, Card]
) -> tuple[str, InlineKeyboardMarkup]:
    if not card_ids:
        text = "Your wallet is empty — tap *Browse & Add* to add cards."
        rows: list[list[InlineKeyboardButton]] = []
    else:
        text = f"Your wallet ({len(card_ids)} card{'s' if len(card_ids) != 1 else ''}):"
        rows = [
            [InlineKeyboardButton(catalog[cid].name if cid in catalog else cid, callback_data=f"ck:card:{cid}")]
            for cid in card_ids
        ]

    rows.append([
        InlineKeyboardButton("🔍 Browse & Add", callback_data="ck:catalog"),
        InlineKeyboardButton("← Back", callback_data="ck:menu"),
    ])
    return text, InlineKeyboardMarkup(rows)


def format_card_detail_keyboard(
    card: Card, statement_day: int | None
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"*{_md(card.name)}*"]
    needs_statement = _card_uses_statement_period(card)
    if needs_statement:
        if statement_day is not None:
            lines.append(f"Statement closing day: {statement_day}")
        else:
            lines.append("⚠ Statement closing day not set")

    rows: list[list[InlineKeyboardButton]] = []
    if needs_statement:
        label = "📅 Change Statement Day" if statement_day is not None else "📅 Set Statement Day"
        rows.append([InlineKeyboardButton(label, callback_data=f"sday:{card.id}:prompt")])
    rows.append([
        InlineKeyboardButton("🗑️ Remove Card", callback_data=f"ck:rm_detail:{card.id}"),
        InlineKeyboardButton("← Back", callback_data="ck:wallet"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def format_catalog_keyboard(
    catalog: dict[str, Card], owned_ids: list[str]
) -> tuple[str, InlineKeyboardMarkup]:
    owned = set(owned_ids)
    rows = []
    for cid in sorted(catalog):
        card = catalog[cid]
        prefix = "✓" if cid in owned else "+"
        label_base = f"{prefix} {card.name}"
        if card.descriptor:
            full = f"{label_base} · {card.descriptor}"
            label = full if len(full) <= 38 else label_base
        else:
            label = label_base
        cb = f"ck:rm:{cid}" if cid in owned else f"ck:add:{cid}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([
        InlineKeyboardButton("📋 My Cards", callback_data="ck:wallet"),
        InlineKeyboardButton("← Back", callback_data="ck:menu"),
    ])
    return "Available cards — tap to add:", InlineKeyboardMarkup(rows)


def format_log_buttons(
    recs: list[Recommendation],
    pending_logs: dict[str, dict],
    *,
    merchant: str,
    amount_sgd: float,
    is_fcy: bool,
) -> InlineKeyboardMarkup | None:
    """Append [📝 Log <name>] buttons for the top-5 recs (two rows: 3+2).

    Each button stores its (card_id, merchant, amount, is_fcy) under a
    short token in `pending_logs` so the callback handler can recover the
    txn details without exceeding Telegram's 64-byte callback_data limit.
    """
    if not recs:
        return None
    top = recs[:5]
    rows = []
    for r in top:
        token = uuid.uuid4().hex[:10]
        pending_logs[token] = {
            "card_id": r.card_id,
            "merchant": merchant,
            "amount_sgd": amount_sgd,
            "is_fcy": is_fcy,
        }
        # Truncate long names so all 3 buttons fit in one row visually.
        name = r.card_name if len(r.card_name) <= 18 else r.card_name[:17] + "…"
        rows.append(InlineKeyboardButton(f"📝 {name}", callback_data=f"log:{token}"))
    return InlineKeyboardMarkup([rows[:3], rows[3:]])


def format_card_picker_buttons(cards: list[Card], token: str) -> InlineKeyboardMarkup:
    """One button per owned card for the /log card-picker flow."""
    rows = []
    for card in cards:
        name = card.name if len(card.name) <= 22 else card.name[:21] + "…"
        rows.append([InlineKeyboardButton(name, callback_data=f"lcard:{token}:{card.id}")])
    return InlineKeyboardMarkup(rows)


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_friendly_date(text: str, today: date) -> date | None:
    """Parse '3 May', '30/4', 'dd-mm', or 'yyyy-mm-dd'. Returns None on failure."""
    text = text.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})$", text)
    if m:
        try:
            return date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})\s+([a-zA-Z]+)$", text)
    if m:
        month = _MONTHS.get(m.group(2).lower()[:3])
        if month:
            try:
                return date(today.year, month, int(m.group(1)))
            except ValueError:
                pass
    return None


def format_log_confirmation(
    txn: TxnRow, card: Card, usage_after: dict[tuple[str, int], object] | None = None,
    posting_warning: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Reply text + buttons after logging a transaction."""
    lines = [f"✨ Logged on *{_md(card.name)}*"]
    bonus_part = f" ({_md(txn.bonus_label)})" if txn.bonus_label else " (base rate)"
    fcy_part = " fcy" if txn.is_fcy else ""
    lines.append(
        f"`{txn.merchant} S${txn.amount_sgd:g}{fcy_part}` → {txn.miles_earned} mi{bonus_part}"
    )
    if txn.bonus_idx is not None and usage_after is not None:
        u = usage_after.get((txn.card_id, txn.bonus_idx))
        if u is not None:
            bonus = card.bonus[txn.bonus_idx]
            if bonus.cap_sgd is not None:
                pct = int(round(u.spend_sgd / bonus.cap_sgd * 100))
                remaining = bonus.cap_sgd - u.spend_sgd
                if remaining <= 0:
                    cap_note = "  ⚠ cap reached"
                elif pct >= 80:
                    cap_note = f"  ⚠ S${remaining:g} remaining"
                else:
                    cap_note = ""
                bar = _progress_bar(pct)
                label_str = _md(bonus.label or "bonus")
                lines.append(
                    f"{label_str} this period: {bar}  S${u.spend_sgd:g} / S${bonus.cap_sgd:g}{cap_note}"
                )

    if posting_warning:
        lines.append(f"⚠ {posting_warning}")

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩ Undo", callback_data=f"undo:{txn.tx_id}"),
        InlineKeyboardButton("📊 Pools", callback_data="pools"),
    ]])
    return "\n".join(lines), kb


def format_pools(
    owned_cards: list[Card],
    usage: dict[tuple[str, int], object],
    statement_days: dict[str, int],
    today: date,
    posting_delays: dict[str, int] | None = None,
    same_day_merchants: set[str] | None = None,
    lady_choice: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Per-card cap progress for /pools."""
    if not owned_cards:
        return ("Your wallet is empty — add cards first via /cards.", None)

    grand_total = 0
    for card in owned_cards:
        for idx, bonus in enumerate(card.bonus):
            u = usage.get((card.id, idx))
            spend = u.spend_sgd if u is not None else 0.0
            grand_total += round(spend * bonus.rate_mpd)

    lines = [
        f"📊 *Your caps* — {today.strftime('%b %Y')}",
        f"🎉 Total: {grand_total:,} mi",
        "",
    ]
    sday_buttons: list[InlineKeyboardButton] = []
    pool_groups: dict[str, list[str]] = {}
    nudge_lines: list[str] = []
    _pd = posting_delays or {}

    for card in owned_cards:
        if card.pool:
            pool_groups.setdefault(card.pool, []).append(card.short_name or card.name)
        s_day = statement_days.get(card.id)
        needs_statement = any(
            (b.cap_period or "").startswith("statement")
            or (b.min_spend_period or "").startswith("statement")
            for b in card.bonus
        )

        lines.append(f"*{_md(card.short_name or card.name)}* ♾ {card.base_rate_mpd:g} mpd")

        if needs_statement and s_day is None:
            lines.append("  ⚠ statement day not set")
            sday_buttons.append(
                InlineKeyboardButton(
                    f"Set statement day — {card.name}",
                    callback_data=f"sday:{card.id}:prompt",
                )
            )

        if not card.bonus:
            lines.append("  no bonus categories — base rate only")
            lines.append("")
            continue

        card_period = next((b.cap_period for b in card.bonus if b.cap_period), "calendar_month")
        delay = _pd.get(card.id, card.posting_delay_days)
        num_caps = sum(1 for b in card.bonus if b.cap_sgd is not None)

        if card.pool:
            lines.append(f"  💎 {_md(_pool_pretty(card.pool))}")
        lines.append(f"  📅 {period_label(card_period, today, s_day)}")

        card_total_miles = 0
        card_total_spend = 0.0
        min_spend_lines: list[str] = []

        # Precompute per-bonus data for two-pass rendering
        bonus_rows: list[tuple] = []  # (bonus, label, spend, min_used, cap_period, miles)
        for idx, bonus in enumerate(card.bonus):
            raw_label = bonus.label or "bonus"
            label = (
                lady_choice.capitalize()
                if card.id == "uob_lady" and raw_label == "chosen category" and lady_choice
                else raw_label
            )
            u = usage.get((card.id, idx))
            spend = u.spend_sgd if u is not None else 0.0
            min_used = u.min_spend_sgd if u is not None else 0.0
            cap_period = bonus.cap_period or "calendar_month"
            miles_so_far = round(spend * bonus.rate_mpd)

            card_total_miles += miles_so_far
            card_total_spend += spend
            bonus_rows.append((bonus, label, spend, min_used, cap_period, miles_so_far))

            if bonus.min_spend_sgd is not None:
                ms_period = bonus.min_spend_period or "calendar_month"
                if min_used >= bonus.min_spend_sgd:
                    min_spend_lines.append("  ✓ min spend met")
                else:
                    gap = bonus.min_spend_sgd - min_used
                    n = days_left(ms_period, today, s_day)
                    day_word = "day" if n == 1 else "days"
                    min_spend_lines.append(f"  ⚠ S${gap:g} min · {n} {day_word} left")

            if (
                card.tracks_by == "posting_date"
                and posting_delays is not None
                and bonus.cap_sgd is not None
                and spend < bonus.cap_sgd
            ):
                n = days_left(cap_period, today, s_day)
                if n <= delay + 1:
                    day_word = "day" if n == 1 else "days"
                    nudge = f"⏰ Last {n} {day_word} for {card.name} {label} cap"
                    if same_day_merchants:
                        names = ", ".join(
                            m.replace("_", " ").title()
                            for m in sorted(same_day_merchants)
                        )
                        nudge += f" — same-day posters: {names}"
                    nudge_lines.append(nudge)

        # Pass 1: all categories
        for bonus, label, spend, min_used, cap_period, miles_so_far in bonus_rows:
            for part in _split_label_parts(label):
                lines.append(f"  ✅ {_md(part)} ⭐ {bonus.rate_mpd:g} mpd")

        # Pass 2: all progress bars (capped blocks only)
        for bonus, label, spend, min_used, cap_period, miles_so_far in bonus_rows:
            if bonus.cap_sgd is not None:
                pct = int(round(spend / bonus.cap_sgd * 100))
                bar = _progress_bar(pct)
                cap_suffix = "  ⚠ cap reached" if spend >= bonus.cap_sgd else ""
                cap_label = f"  ({_first_label_part(label)})" if num_caps > 1 else ""
                lines.append(f"  {bar}  S${spend:g} / S${bonus.cap_sgd:g}{cap_label}{cap_suffix}")

        lines.append(f"  Total: S${card_total_spend:g} →  {card_total_miles:,} mi")
        lines.extend(min_spend_lines)
        if card.pool_note:
            lines.append(f"  ⚠ {_md(card.pool_note)}")
        lines.append("")

    if nudge_lines:
        lines.extend(nudge_lines)
        lines.append("")

    shared_pools = [pool for pool, names in pool_groups.items() if len(names) > 1]
    if shared_pools:
        lines.append("_Cards sharing a redemption pool:_")
        for p in shared_pools:
            names_str = ", ".join(_md(n) for n in pool_groups[p])
            lines.append(f"  • {_md(_pool_pretty(p))}: {names_str}")

    kb = None
    if sday_buttons:
        kb = InlineKeyboardMarkup([[b] for b in sday_buttons])
    return "\n".join(lines).rstrip(), kb


def _progress_bar(pct: int, width: int = 9) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _split_label_parts(label: str) -> list[str]:
    """Split 'a + b/c + d (note)' into individual point-form lines."""
    parts = [p.strip() for p in label.split(' + ')]
    result = []
    for part in parts:
        depth = 0
        slash_pos: list[int] = []
        for i, c in enumerate(part):
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            elif c == '/' and depth == 0:
                slash_pos.append(i)
        if slash_pos:
            pieces, prev = [], 0
            for pos in slash_pos:
                pieces.append(part[prev:pos].strip())
                prev = pos + 1
            pieces.append(part[prev:].strip())
            result.extend(p for p in pieces if p)
        else:
            result.append(part)
    return result


def _first_label_part(label: str) -> str:
    """Return first segment of a label, stripping parenthetical notes."""
    first = label.split(" + ")[0].strip()
    paren_idx = first.find("(")
    if paren_idx > 0:
        first = first[:paren_idx].strip()
    return first.split("/")[0].strip()


def _pool_pretty(pool: str) -> str:
    return {
        "uob_unis": "UOB UNI$",
        "hsbc_rewards": "HSBC Rewards",
        "citi_thankyou": "Citi ThankYou",
        "citi_miles": "Citi Miles",
        "dbs_points": "DBS Points",
        "direct_kf": "direct KrisFlyer",
        "maybank_treats": "Maybank TREATS",
    }.get(pool, pool.replace("_", " "))


def format_recent(
    txns: list[TxnRow], catalog: dict[str, Card],
) -> tuple[str, InlineKeyboardMarkup | None]:
    if not txns:
        return ("🧾 No transactions logged yet.\nLog with /log or tap 📝 after a recommendation.", None)

    rows: list[list[InlineKeyboardButton]] = []
    for t in txns:
        c = catalog.get(t.card_id)
        short = (c.short_name or c.name) if c else t.card_id
        fcy = " fcy" if t.is_fcy else ""
        label = (
            f"{t.txn_date.strftime('%b %d')}  {short}  "
            f"{t.merchant} S${t.amount_sgd:g}{fcy}  →  {t.miles_earned} mi  🗑"
        )
        rows.append([InlineKeyboardButton(label, callback_data=f"del:{t.tx_id}")])

    return "🧾 *Recent transactions*", InlineKeyboardMarkup(rows)


def format_statement_day_prompt(card: Card) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"📅 *{_md(card.name)}* uses statement-month tracking.\n"
        "Tap your statement closing day (1–28). "
        "Skip to use calendar-month approximation."
    )
    rows = []
    days = list(range(1, 29))
    # 4 buttons per row
    for i in range(0, len(days), 4):
        rows.append([
            InlineKeyboardButton(f"{d}", callback_data=f"sday:{card.id}:{d}")
            for d in days[i : i + 4]
        ])
    rows.append([InlineKeyboardButton("Skip", callback_data=f"sday:{card.id}:skip")])
    return text, InlineKeyboardMarkup(rows)


def format_anniversary_prompt(card: Card) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🗓 *{_md(card.name)}* earns miles on a membership-year cycle.\n"
        "Which month did you open the card? Tap a month or skip."
    )
    months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    rows = []
    for i in range(0, 12, 4):
        rows.append([
            InlineKeyboardButton(months[m], callback_data=f"ann:{card.id}:{m + 1}")
            for m in range(i, min(i + 4, 12))
        ])
    rows.append([InlineKeyboardButton("Skip", callback_data=f"ann:{card.id}:skip")])
    return text, InlineKeyboardMarkup(rows)


def format_lady_choice_keyboard(
    current: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    options = [
        ("Dining", "dining"),
        ("Beauty & Wellness", "beauty"),
        ("Entertainment", "entertainment"),
        ("Fashion", "fashion"),
        ("Family", "family"),
        ("Transport", "transport"),
        ("Travel", "travel"),
    ]
    text = "👜 *UOB Lady's Card* — pick your bonus category for this period:"
    if current:
        text += f"\nCurrent pick: `{current}`"
    rows = []
    for i in range(0, len(options), 2):
        rows.append([
            InlineKeyboardButton(
                f"✓ {label}" if current == cat else label,
                callback_data=f"lc:{cat}",
            )
            for label, cat in options[i : i + 2]
        ])
    return text, InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Pure handlers (storage + catalog in, reply string out)
# ---------------------------------------------------------------------------

def _card_uses_statement_period(card: Card) -> bool:
    return any(
        (b.cap_period or "").startswith("statement")
        or (b.min_spend_period or "").startswith("statement")
        for b in card.bonus
    )


@dataclass
class RecommendationPayload:
    text: str
    recs: list[Recommendation] = field(default_factory=list)
    parsed_merchant: str = ""
    parsed_amount: float = 0.0
    parsed_is_fcy: bool = False


async def compute_recommendation_payload(
    text: str,
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    merchants: dict[str, MerchantEntry],
    today: date | None = None,
) -> RecommendationPayload:
    today = today or date.today()
    try:
        parsed = await parse_async(text)
    except ValueError as e:
        return RecommendationPayload(text=str(e))

    owned_ids = await storage.list_cards(user_id)
    owned_cards = [catalog[cid] for cid in owned_ids if cid in catalog]
    if not owned_cards:
        return RecommendationPayload(
            text=(
                "Your wallet is empty — add cards first.\n"
                "Try /cards to add some."
            )
        )

    base_categories, same_day_merchant = _resolve_merchant(merchants, parsed.merchant)
    unknown = parsed.merchant not in merchants

    # Inject lady_chosen if the user owns Lady's and their chosen category
    # matches this merchant.
    categories = list(base_categories)
    # LLM parser may surface extra category tags (e.g. seasia_fcy for IDR/MYR/THB/VND).
    for tag in parsed.extra_categories:
        if tag and tag not in categories:
            categories.append(tag)
    choice: str | None = None
    if any(c.id == "uob_lady" for c in owned_cards):
        choice = await storage.get_lady_choice(user_id, today=today)
        if choice and any(t in base_categories for t in LADY_CATEGORY_TAGS.get(choice, {choice})):
            categories.append("lady_chosen")

    # Materialize usage from txn history.
    statement_days = await storage.get_statement_days(user_id)
    posting_delays = await storage.get_posting_delays(user_id)
    anniversary_months = await storage.get_anniversaries(user_id)
    card_tiers = await storage.get_card_tiers(user_id)
    earliest_period_start = _earliest_period_start(owned_cards, today, statement_days)
    txns = await storage.list_transactions_since(user_id, since=earliest_period_start)
    usage = build_usage(txns, owned_cards, today, statement_days, posting_delays, anniversary_months)

    recs = recommend(
        owned_cards,
        categories,
        parsed.amount_sgd,
        is_fcy=parsed.is_fcy,
        today=today,
        usage=usage,
        statement_days=statement_days,
        posting_delays=posting_delays,
        same_day_merchant=same_day_merchant,
        anniversary_months=anniversary_months or None,
        card_tiers=card_tiers or None,
    )

    all_excluded = bool(categories) and all(
        not any(reason.startswith("✓") for reason in r.reasons) for r in recs
    )

    text_out = format_recommendations(
        recs,
        merchant=parsed.merchant,
        amount_sgd=parsed.amount_sgd,
        is_fcy=parsed.is_fcy,
        unknown_merchant=unknown,
        all_excluded=all_excluded,
        lady_choice=choice,
    )
    return RecommendationPayload(
        text=text_out,
        recs=recs,
        parsed_merchant=parsed.merchant,
        parsed_amount=parsed.amount_sgd,
        parsed_is_fcy=parsed.is_fcy,
    )


def _earliest_period_start(
    cards: list[Card], today: date, statement_days: dict[str, int]
) -> date:
    """Earliest period start across all owned cards' bonuses. Used to
    bound the txn fetch window."""
    earliest = today
    for card in cards:
        s_day = statement_days.get(card.id)
        for bonus in card.bonus:
            for period in (bonus.cap_period, bonus.min_spend_period):
                if period is None:
                    continue
                start, _ = period_bounds(period, today, s_day)
                if start < earliest:
                    earliest = start
    return earliest


async def handle_text_message(
    text: str,
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    merchants: dict[str, MerchantEntry],
    today: date | None = None,
) -> str:
    payload = await compute_recommendation_payload(
        text, user_id, storage, catalog, merchants, today
    )
    return payload.text


async def handle_cards_command(
    args: list[str],
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
) -> str:
    if not args or args[0] == "list":
        owned = await storage.list_cards(user_id)
        return format_card_list(owned, catalog)

    sub = args[0].lower()
    if sub == "add":
        if len(args) < 2:
            return "Usage: `/cards add <id>`. Try `/cards` for valid ids."
        cid = args[1].lower()
        if cid not in catalog:
            return (
                f"❌ Unknown card id `{cid}`.\n"
                f"Valid: {', '.join(f'`{x}`' for x in sorted(catalog))}"
            )
        added = await storage.add_card(user_id, cid)
        if added:
            return f"✅ Added *{catalog[cid].name}* — welcome to the wallet!"
        return f"Already in your wallet: *{catalog[cid].name}*."

    if sub == "catalog":
        return format_catalog(catalog)

    if sub == "remove":
        if len(args) < 2:
            return "Usage: `/cards remove <id>`."
        cid = args[1].lower()
        removed = await storage.remove_card(user_id, cid)
        if removed:
            name = catalog[cid].name if cid in catalog else cid
            return f"🗑️ Removed *{name}*. Your wallet is updated."
        return f"`{cid}` wasn't in your wallet."

    return format_cards_help(catalog)


async def handle_log_command(
    args: list[str],
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    merchants: dict[str, MerchantEntry],
    today: date | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """`/log <card> <merchant> <amount> [fcy] [yyyy-mm-dd]`"""
    today = today or date.today()
    if len(args) < 3:
        return (
            "Usage: `/log <card> <merchant> <amount> [fcy] [yyyy-mm-dd]`\n"
            "e.g. `/log uob_ppv shopee 45`",
            None,
        )

    card_id = args[0].lower()
    if card_id not in catalog:
        return (f"❌ Unknown card id `{card_id}`. Try /cards for valid ids.", None)

    owned = await storage.list_cards(user_id)
    if card_id not in owned:
        return (
            f"❌ `{card_id}` isn't in your wallet — add it first via /cards.",
            None,
        )

    merchant_raw = args[1]
    merchant = merchant_raw.lower().replace(" ", "_")
    try:
        amount = float(args[2])
    except ValueError:
        return (f"❌ Invalid amount: `{args[2]}`. Use a number like 45 or 9.99.", None)
    if amount <= 0:
        return ("❌ Amount must be greater than 0.", None)

    is_fcy = False
    txn_date = today
    for extra in args[3:]:
        e = extra.lower()
        if e in ("fcy", "sgd"):
            is_fcy = e == "fcy"
        else:
            try:
                txn_date = date.fromisoformat(e)
            except ValueError:
                return (f"❌ Couldn't parse `{extra}` — expected `fcy`/`sgd` or `yyyy-mm-dd`.", None)

    card = catalog[card_id]
    base_categories, same_day_merchant = _resolve_merchant(merchants, merchant)

    # Lady's chosen-category injection at log time too.
    categories = list(base_categories)
    if card_id == "uob_lady":
        choice = await storage.get_lady_choice(user_id, today=txn_date)
        if choice and any(t in base_categories for t in LADY_CATEGORY_TAGS.get(choice, {choice})):
            categories.append("lady_chosen")

    statement_days = await storage.get_statement_days(user_id)
    posting_delays = await storage.get_posting_delays(user_id)
    anniversary_months = await storage.get_anniversaries(user_id)
    card_tiers = await storage.get_card_tiers(user_id)
    owned_cards = [catalog[c] for c in owned if c in catalog]
    earliest = _earliest_period_start(owned_cards, txn_date, statement_days)
    txns = await storage.list_transactions_since(user_id, since=earliest)
    usage = build_usage(txns, owned_cards, txn_date, statement_days, posting_delays, anniversary_months)

    bonus_idx, bonus_label, miles = select_bonus_for_log(
        card, categories, amount, is_fcy, txn_date, usage, statement_days,
        tier=card_tiers.get(card_id),
    )

    posting_warning: str | None = None
    if card.tracks_by == "posting_date" and bonus_idx is not None:
        delay = posting_delays.get(card.id, card.posting_delay_days)
        p_date = resolve_posting_date(txn_date, delay, same_day_merchant)
        if p_date != txn_date:
            cap_period = card.bonus[bonus_idx].cap_period or "calendar_month"
            posting_warning = posting_period_warning(
                txn_date=txn_date,
                posting_date=p_date,
                period=cap_period,
                statement_day=statement_days.get(card.id),
                anniversary_month=anniversary_months.get(card.id),
            )

    tx_id = await storage.log_transaction(
        telegram_user_id=user_id,
        card_id=card_id,
        bonus_idx=bonus_idx,
        bonus_label=bonus_label,
        merchant=merchant,
        amount_sgd=amount,
        is_fcy=is_fcy,
        miles_earned=miles,
        txn_date=txn_date,
    )

    # Re-build usage including the just-logged txn for the confirmation footer.
    txns_after = await storage.list_transactions_since(user_id, since=earliest)
    usage_after = build_usage(txns_after, owned_cards, txn_date, statement_days, posting_delays, anniversary_months)
    txn = TxnRow(
        tx_id=tx_id,
        telegram_user_id=user_id,
        card_id=card_id,
        bonus_idx=bonus_idx,
        bonus_label=bonus_label,
        merchant=merchant,
        amount_sgd=amount,
        is_fcy=is_fcy,
        miles_earned=miles,
        txn_date=txn_date,
        created_at=__import__("datetime").datetime.now(),
    )
    return format_log_confirmation(txn, card, usage_after=usage_after, posting_warning=posting_warning)


async def handle_pools_command(
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    today: date | None = None,
    merchants: dict[str, MerchantEntry] | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    today = today or date.today()
    owned = await storage.list_cards(user_id)
    owned_cards = [catalog[c] for c in owned if c in catalog]
    statement_days = await storage.get_statement_days(user_id)
    posting_delays = await storage.get_posting_delays(user_id)
    anniversary_months = await storage.get_anniversaries(user_id)
    earliest = _earliest_period_start(owned_cards, today, statement_days) if owned_cards else today
    txns = await storage.list_transactions_since(user_id, since=earliest)
    usage = build_usage(txns, owned_cards, today, statement_days, posting_delays, anniversary_months)
    same_day = (
        {k for k, v in merchants.items() if v.same_day_posting}
        if merchants else None
    )
    lady_choice = await storage.get_lady_choice(user_id, today=today)
    return format_pools(owned_cards, usage, statement_days, today, posting_delays or None, same_day, lady_choice)


async def handle_recent_command(
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    limit: int = 10,
) -> tuple[str, InlineKeyboardMarkup | None]:
    txns = await storage.recent_transactions(user_id, limit=limit)
    return format_recent(txns, catalog)


async def handle_lady_choice_command(
    args: list[str],
    user_id: int,
    storage: Storage,
    today: date | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    today = today or date.today()
    if args:
        category = args[0].lower()
        await storage.set_lady_choice(user_id, category, effective_from=today)
        return (f"✅ Set Lady's category to `{category}` — enjoy the bonus!", None)
    current = await storage.get_lady_choice(user_id, today=today)
    return format_lady_choice_keyboard(current)


async def handle_hsbc_tier_command(
    args: list[str],
    user_id: int,
    storage: Storage,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Set HSBC Revolution earn tier: regular (4mpd cap S$1k) or
    enhanced (8mpd cap S$1.2k, requires S$50K+ ADB in HSBC Everyday Global Account).
    """
    if not args:
        tiers = await storage.get_card_tiers(user_id)
        current = tiers.get("hsbc_revo", "regular")
        return (
            f"HSBC Revolution earn tier: *{current}*\n\n"
            "Use `/hsbc_tier regular` for 4mpd cap S$1k/mo (default).\n"
            "Use `/hsbc_tier enhanced` for 8mpd cap S$1.2k/mo "
            "(needs S$50K+ ADB in HSBC Everyday Global Account).",
            None,
        )
    tier = args[0].lower()
    if tier not in ("regular", "enhanced"):
        return (
            "❌ Tier must be `regular` or `enhanced`. "
            "Run `/hsbc_tier` for details.",
            None,
        )
    await storage.set_card_tier(user_id, "hsbc_revo", tier)
    if tier == "enhanced":
        return ("✅ HSBC Revolution set to *enhanced* (8mpd, cap S$1.2k/mo).", None)
    return ("✅ HSBC Revolution set to *regular* (4mpd, cap S$1k/mo).", None)


# ---------------------------------------------------------------------------
# PTB wiring
# ---------------------------------------------------------------------------

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Something went wrong — try your message again, or use /help."
            )
        except Exception:
            pass


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def _help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            HELP_SIMPLE, parse_mode="Markdown", reply_markup=_HELP_SIMPLE_KB
        )


async def _help_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    if query.data == "help:advanced":
        await query.edit_message_text(HELP_ADVANCED, parse_mode="Markdown", reply_markup=_HELP_ADVANCED_KB)
    else:
        await query.edit_message_text(HELP_SIMPLE, parse_mode="Markdown", reply_markup=_HELP_SIMPLE_KB)


async def _cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text, kb = format_menu_keyboard()
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _maybe_prompt_statement_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    card: Card,
    user_id: int,
) -> None:
    """Send a statement-day prompt for the freshly-added card if it
    needs one and the user hasn't set one yet."""
    if not _card_uses_statement_period(card):
        return
    storage: Storage = context.application.bot_data["storage"]
    days = await storage.get_statement_days(user_id)
    if card.id in days:
        return
    text, kb = format_statement_day_prompt(card)
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _maybe_prompt_anniversary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    card: Card,
    user_id: int,
) -> None:
    """Send an anniversary-month prompt for a freshly-added card if it
    uses anniversary_year periods and the user hasn't set one yet."""
    if not card.anniversary_year:
        return
    storage: Storage = context.application.bot_data["storage"]
    months = await storage.get_anniversaries(user_id)
    if card.id in months:
        return
    text, kb = format_anniversary_prompt(card)
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(
            text, reply_markup=kb, parse_mode="Markdown"
        )
    elif update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _cards_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    user_id = update.effective_user.id
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    data = query.data or ""

    added_card: Card | None = None
    if data == "ck:menu":
        text, kb = format_menu_keyboard()
    elif data == "ck:wallet":
        owned = await storage.list_cards(user_id)
        text, kb = format_wallet_keyboard(owned, catalog)
    elif data == "ck:catalog":
        owned = await storage.list_cards(user_id)
        text, kb = format_catalog_keyboard(catalog, owned)
    elif data.startswith("ck:add:"):
        cid = data[7:]
        was_added = await storage.add_card(user_id, cid)
        if was_added and cid in catalog:
            added_card = catalog[cid]
        owned = await storage.list_cards(user_id)
        text, kb = format_catalog_keyboard(catalog, owned)
    elif data.startswith("ck:rm:"):
        cid = data[6:]
        await storage.remove_card(user_id, cid)
        owned = await storage.list_cards(user_id)
        text, kb = format_catalog_keyboard(catalog, owned)
    elif data.startswith("ck:card:"):
        cid = data[8:]
        card = catalog.get(cid)
        if card is None:
            return
        s_days = await storage.get_statement_days(user_id)
        text, kb = format_card_detail_keyboard(card, s_days.get(cid))
    elif data.startswith("ck:rm_detail:"):
        cid = data[13:]
        await storage.remove_card(user_id, cid)
        owned = await storage.list_cards(user_id)
        text, kb = format_wallet_keyboard(owned, catalog)
    else:
        return

    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    if added_card is not None:
        await _maybe_prompt_statement_day(update, context, added_card, user_id)
        await _maybe_prompt_anniversary(update, context, added_card, user_id)


async def _text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.text:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, MerchantEntry] = context.application.bot_data["merchants"]
    user_id = update.effective_user.id

    pending_date_input: dict[int, str] = context.application.bot_data.setdefault("pending_date_input", {})
    token = pending_date_input.get(user_id)
    if token:
        pending_logs: dict[str, dict] = context.application.bot_data.setdefault("pending_logs", {})
        info = pending_logs.get(token)
        if info is not None:
            today = date.today()
            txn_date = _parse_friendly_date(update.message.text.strip(), today)
            if txn_date is None:
                await update.message.reply_text(
                    f"Couldn't parse `{_md(update.message.text.strip())}` — try `3 May`, `30/4`, or `2026-04-30`.",
                    parse_mode="Markdown",
                )
                return
            args = [info["card_id"], info["merchant"], str(info["amount_sgd"])]
            if info["is_fcy"]:
                args.append("fcy")
            args.append(txn_date.isoformat())
            text, kb = await handle_log_command(args, user_id, storage, catalog, merchants)
            pending_date_input.pop(user_id, None)
            pending_logs.pop(token, None)
            await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        else:
            pending_date_input.pop(user_id, None)

    pending_logs = context.application.bot_data.setdefault("pending_logs", {})
    payload = await compute_recommendation_payload(
        update.message.text, user_id, storage, catalog, merchants
    )
    kb = format_log_buttons(
        payload.recs,
        pending_logs,
        merchant=payload.parsed_merchant,
        amount_sgd=payload.parsed_amount,
        is_fcy=payload.parsed_is_fcy,
    ) if payload.recs else None
    await update.message.reply_text(payload.text, reply_markup=kb, parse_mode="Markdown")


async def _log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, MerchantEntry] = context.application.bot_data["merchants"]
    user_id = update.effective_user.id
    args = (update.message.text or "").split()[1:]

    # Power-user path: card_id provided as first arg — log directly.
    if args and args[0].lower() in catalog:
        text, kb = await handle_log_command(args, user_id, storage, catalog, merchants)
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    # Interactive path: show card picker.
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/log <merchant> <amount> [fcy]`\ne.g. `/log shopee 45`\n\n"
            "Or skip the picker: `/log uob_ppv shopee 45`",
            parse_mode="Markdown",
        )
        return

    merchant_raw = args[0]
    merchant = merchant_raw.lower().replace(" ", "_")
    try:
        amount = float(args[1])
    except ValueError:
        await update.message.reply_text(
            f"❌ Invalid amount: `{_md(args[1])}`. Use a number like `45` or `9.99`.",
            parse_mode="Markdown",
        )
        return
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.", parse_mode="Markdown")
        return

    is_fcy = any(a.lower() == "fcy" for a in args[2:])

    owned = await storage.list_cards(user_id)
    owned_cards = [catalog[c] for c in owned if c in catalog]
    if not owned_cards:
        await update.message.reply_text(
            "Your wallet is empty — add cards first via /cards.", parse_mode="Markdown"
        )
        return

    pending_logs: dict[str, dict] = context.application.bot_data.setdefault("pending_logs", {})
    token = uuid.uuid4().hex[:10]
    pending_logs[token] = {"merchant": merchant, "amount_sgd": amount, "is_fcy": is_fcy}

    fcy_note = " FCY" if is_fcy else ""
    kb = format_card_picker_buttons(owned_cards, token)
    await update.message.reply_text(
        f"Which card for `{_md(merchant_raw)} S${amount:g}{fcy_note}`?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def _pools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, MerchantEntry] = context.application.bot_data["merchants"]
    text, kb = await handle_pools_command(update.effective_user.id, storage, catalog, merchants=merchants)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    text, kb = await handle_recent_command(update.effective_user.id, storage, catalog)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _lady_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    storage: Storage = context.application.bot_data["storage"]
    args = (update.message.text or "").split()[1:]
    text, kb = await handle_lady_choice_command(args, update.effective_user.id, storage)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _hsbc_tier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    storage: Storage = context.application.bot_data["storage"]
    args = (update.message.text or "").split()[1:]
    text, kb = await handle_hsbc_tier_command(args, update.effective_user.id, storage)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    pending_logs: dict[str, dict] = context.application.bot_data.setdefault(
        "pending_logs", {}
    )
    token = query.data[len("log:"):]
    info = pending_logs.get(token)
    if info is None:
        await query.edit_message_text("This recommendation expired — send the merchant/amount again to log.")
        return

    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, MerchantEntry] = context.application.bot_data["merchants"]
    args = [info["card_id"], info["merchant"], str(info["amount_sgd"])]
    if info["is_fcy"]:
        args.append("fcy")
    text, kb = await handle_log_command(
        args, update.effective_user.id, storage, catalog, merchants
    )
    if query.message:
        await query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _log_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked a card from the /log card picker — show date picker."""
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()

    rest = query.data[len("lcard:"):]
    token, card_id = rest.split(":", 1)

    pending_logs: dict[str, dict] = context.application.bot_data.setdefault("pending_logs", {})
    info = pending_logs.get(token)
    if info is None:
        await query.edit_message_text("This log request expired — try /log again.")
        return

    info["card_id"] = card_id
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    card = catalog.get(card_id)
    card_name = card.name if card else card_id

    today = date.today()
    today_label = today.strftime("%-d %b")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"📅 Today ({today_label})", callback_data=f"ldate:{token}:today"),
        InlineKeyboardButton("✏️ Different date", callback_data=f"ldate:{token}:custom"),
    ]])
    merchant_display = info["merchant"].replace("_", " ")
    fcy_note = " FCY" if info["is_fcy"] else ""
    await query.edit_message_text(
        f"*{_md(card_name)}* — `{_md(merchant_display)} S${info['amount_sgd']:g}{fcy_note}`\n"
        "When was this transaction?",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def _log_date_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked today or custom date from the /log date picker."""
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()

    rest = query.data[len("ldate:"):]
    token, choice = rest.rsplit(":", 1)

    pending_logs: dict[str, dict] = context.application.bot_data.setdefault("pending_logs", {})
    info = pending_logs.get(token)
    if info is None:
        if query.message:
            await query.message.reply_text("This log request expired — try /log again.")
        return

    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, MerchantEntry] = context.application.bot_data["merchants"]
    user_id = update.effective_user.id

    if choice == "today":
        args = [info["card_id"], info["merchant"], str(info["amount_sgd"])]
        if info["is_fcy"]:
            args.append("fcy")
        text, kb = await handle_log_command(args, user_id, storage, catalog, merchants)
        pending_logs.pop(token, None)
        if query.message:
            await query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        pending_date_input: dict[int, str] = context.application.bot_data.setdefault("pending_date_input", {})
        pending_date_input[user_id] = token
        if query.message:
            await query.message.reply_text(
                "Reply with the date — e.g. `3 May` or `30/4`",
                parse_mode="Markdown",
            )


async def _undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    tx_id = query.data[len("undo:"):]
    deleted = await storage.delete_transaction(update.effective_user.id, tx_id)
    msg = "↩ Undone — transaction removed." if deleted else "Already removed."
    await query.edit_message_text(msg)


async def _del_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    user_id = update.effective_user.id
    tx_id = query.data[len("del:"):]
    txn = await storage.get_transaction(user_id, tx_id)
    await storage.delete_transaction(user_id, tx_id)
    if txn:
        context.user_data["pending_undo"] = txn
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩ Undo", callback_data=f"restore:{tx_id}")]])
        await query.edit_message_text("🗑 Deleted.", reply_markup=kb)
    else:
        catalog: dict[str, Card] = context.application.bot_data["cards"]
        text, kb = await handle_recent_command(user_id, storage, catalog)
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")




async def _restore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    user_id = update.effective_user.id
    txn = context.user_data.pop("pending_undo", None)
    if txn:
        await storage.restore_transaction(txn)
    text, kb = await handle_recent_command(user_id, storage, catalog)
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def _pools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, MerchantEntry] = context.application.bot_data["merchants"]
    text, kb = await handle_pools_command(update.effective_user.id, storage, catalog, merchants=merchants)
    if query.message:
        await query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _sday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _, card_id, action = parts
    card = catalog.get(card_id)
    if card is None:
        return

    if action == "prompt":
        text, kb = format_statement_day_prompt(card)
        if query.message:
            await query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        return
    if action == "skip":
        await query.edit_message_text(
            f"Using calendar-month approximation for *{_md(card.name)}*. "
            "You can change later via /cards.",
            parse_mode="Markdown",
        )
        return
    try:
        day = int(action)
    except ValueError:
        return
    try:
        await storage.set_statement_day(update.effective_user.id, card_id, day)
    except ValueError as e:
        await query.edit_message_text(f"⚠️ {e}")
        return
    await query.edit_message_text(
        f"✅ Got it — statement closes on day {day} for *{_md(card.name)}*.",
        parse_mode="Markdown",
    )


async def _lc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    category = query.data[len("lc:"):]
    today = date.today()
    await storage.set_lady_choice(update.effective_user.id, category, effective_from=today)
    label = _LADY_CATEGORY_LABELS.get(category, category)
    await query.edit_message_text(
        f"✅ Lady's category set to *{_md(label)}* — enjoy the bonus!",
        parse_mode="Markdown",
    )


async def _ann_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        return
    _, card_id, action = parts
    card = catalog.get(card_id)
    if card is None:
        return

    if action == "skip":
        await query.edit_message_text(
            f"Using card-level default for *{_md(card.name)}*. "
            "You can set it later via /cards.",
            parse_mode="Markdown",
        )
        return
    try:
        month = int(action)
    except ValueError:
        return
    try:
        await storage.set_anniversary(update.effective_user.id, card_id, month)
    except ValueError as e:
        await query.edit_message_text(f"⚠️ {e}")
        return
    month_name = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ][month - 1]
    await query.edit_message_text(
        f"✅ *{_md(card.name)}* anniversary month set to {month_name}.",
        parse_mode="Markdown",
    )


BOT_COMMANDS = [
    BotCommand("start", "Set up your wallet and get started"),
    BotCommand("help", "Show help and available commands"),
    BotCommand("cards", "Manage your cards (add, list, remove, catalog)"),
    BotCommand("log", "Log a purchase to track caps"),
    BotCommand("pools", "Show your cap usage across all cards"),
    BotCommand("recent", "Show recent logged transactions"),
    BotCommand("lady_choice", "Pick UOB Lady's bonus category"),
    BotCommand("hsbc_tier", "Set HSBC Revolution earn tier (regular/enhanced)"),
]


def build_application(
    token: str,
    storage: Storage,
    cards: dict[str, Card],
    merchants: dict[str, MerchantEntry],
) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["storage"] = storage
    app.bot_data["cards"] = cards
    app.bot_data["merchants"] = merchants
    app.bot_data["pending_logs"] = {}
    app.bot_data["pending_date_input"] = {}
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _help))
    app.add_handler(CommandHandler("cards", _cards))
    app.add_handler(CommandHandler("log", _log))
    app.add_handler(CommandHandler("pools", _pools))
    app.add_handler(CommandHandler("recent", _recent))
    app.add_handler(CommandHandler("lady_choice", _lady_choice))
    app.add_handler(CommandHandler("hsbc_tier", _hsbc_tier))
    app.add_handler(CallbackQueryHandler(_cards_callback, pattern=r"^ck:"))
    app.add_handler(CallbackQueryHandler(_log_callback, pattern=r"^log:"))
    app.add_handler(CallbackQueryHandler(_log_card_callback, pattern=r"^lcard:"))
    app.add_handler(CallbackQueryHandler(_log_date_callback, pattern=r"^ldate:"))
    app.add_handler(CallbackQueryHandler(_undo_callback, pattern=r"^undo:"))
    app.add_handler(CallbackQueryHandler(_del_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(_restore_callback, pattern=r"^restore:"))
    app.add_handler(CallbackQueryHandler(_help_toggle_callback, pattern=r"^help:"))
    app.add_handler(CallbackQueryHandler(_pools_callback, pattern=r"^pools$"))
    app.add_handler(CallbackQueryHandler(_sday_callback, pattern=r"^sday:"))
    app.add_handler(CallbackQueryHandler(_lc_callback, pattern=r"^lc:"))
    app.add_handler(CallbackQueryHandler(_ann_callback, pattern=r"^ann:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text))
    app.add_error_handler(_error_handler)
    return app
