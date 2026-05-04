"""Telegram handlers, factored as pure helpers + thin PTB wiring.

Helpers (`format_*`, `handle_*`) take primitives and return reply strings,
which is what `tests/test_bot_handlers.py` exercises. The PTB application
factory at the bottom wires those helpers to commands/messages.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Awaitable, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .models import Card, Recommendation, TxnRow
from .parser import parse
from .periods import days_left, period_bounds, period_label
from .rule_engine import recommend, select_bonus_for_log
from .storage import Storage
from .usage import build_usage

log = logging.getLogger(__name__)


def _md(s: str) -> str:
    """Escape Telegram Markdown V1 special chars in dynamic content."""
    return s.replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


WELCOME = (
    "👋 *CardKaki* — tells you which SG card to use at checkout.\n\n"
    "*Manage your wallet:* /cards\n"
    "*Log a purchase:* `/log <card> <merchant> <amount> [fcy] [yyyy-mm-dd]`\n"
    "*See cap usage:* /pools\n"
    "*Recent transactions:* /recent\n"
    "*Lady's chosen category:* /lady\\_choice\n\n"
    "Then send: `<merchant> <amount> [fcy]`\n"
    "e.g. `cold storage 45`  ·  `klook 320 fcy`\n\n"
    "v2 enforces caps and min-spend. Tap 📝 after any recommendation to log.\n\n"
    "⚠️ Best-effort recommendations based on Milelion-cited rules. Verify "
    "against your statement. Statement-month caps use your set closing day "
    "(or fall back to calendar month)."
)


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

    fcy_str = " fcy" if is_fcy else ""
    header_bits.append(f"`{merchant} {amount_sgd:g}{fcy_str}`")

    top = recs[:3]
    medals = ["🥇", "🥈", "🥉"]
    rendered_lines: list[str] = []
    if len(top) >= 2 and top[0].miles == top[1].miles and top[0].miles > 0:
        rendered_lines.append(_render_tied(top))
    else:
        for i, r in enumerate(top):
            rendered_lines.append(f"{medals[i]} {_render_one(r)}")
        # Surface secondary reasons (cap/min-spend warnings) on a continuation line.
        for i, r in enumerate(top):
            if len(r.reasons) > 1:
                tail = " · ".join(r.reasons[1:])
                # Don't repeat FCY-fee chatter on every card; only include
                # reasons that look like warnings or cap/blend info.
                relevant = [
                    x for x in r.reasons[1:]
                    if x.startswith("⚠") or x.startswith("cap ")
                ]
                if relevant:
                    rendered_lines.append(f"   {' · '.join(_md(x) for x in relevant)}")

    return "\n".join(header_bits + rendered_lines)


def _render_one(r: Recommendation) -> str:
    head = f"*{_md(r.card_name)}*: {r.miles} mi ({r.effective_mpd:.1f} mpd)"
    top_reason = _md(r.reasons[0]) if r.reasons else ""
    return f"{head}  {top_reason}".rstrip()


def _render_tied(top: list[Recommendation]) -> str:
    lines = []
    tie_value = top[0].miles
    for r in top:
        marker = "=" if r.miles == tie_value else "🥉"
        lines.append(f"{marker} {_render_one(r)}")
    return "\n".join(lines)


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
    else:
        lines = [f"Your wallet ({len(card_ids)} card{'s' if len(card_ids) != 1 else ''}):"]
        for cid in card_ids:
            c = catalog.get(cid)
            lines.append(f"• {c.name if c else cid}")
        text = "\n".join(lines)

    rows = [[
        InlineKeyboardButton("🔍 Browse & Add", callback_data="ck:catalog"),
        InlineKeyboardButton("← Back", callback_data="ck:menu"),
    ]]
    return text, InlineKeyboardMarkup(rows)


def format_catalog_keyboard(
    catalog: dict[str, Card], owned_ids: list[str]
) -> tuple[str, InlineKeyboardMarkup]:
    owned = set(owned_ids)
    rows = []
    for cid in sorted(catalog):
        name = catalog[cid].name
        if cid in owned:
            rows.append([InlineKeyboardButton(f"✓ {name}", callback_data=f"ck:rm:{cid}")])
        else:
            rows.append([InlineKeyboardButton(f"+ {name}", callback_data=f"ck:add:{cid}")])
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
    """Append [📝 Log <name>] buttons for the top-3 recs.

    Each button stores its (card_id, merchant, amount, is_fcy) under a
    short token in `pending_logs` so the callback handler can recover the
    txn details without exceeding Telegram's 64-byte callback_data limit.
    """
    if not recs:
        return None
    top = recs[:3]
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
    return InlineKeyboardMarkup([rows])


def format_log_confirmation(
    txn: TxnRow, card: Card, usage_after: dict[tuple[str, int], object] | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Reply text + buttons after logging a transaction."""
    lines = [f"📝 Logged: *{_md(card.name)}*"]
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
                lines.append(
                    f"This period on {_md(card.name)} {_md(bonus.label or '')}: "
                    f"S${u.spend_sgd:g} / S${bonus.cap_sgd:g} ({pct}%)"
                )

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
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Per-card cap progress for /pools."""
    if not owned_cards:
        return ("Your wallet is empty — add cards first via /cards.", None)

    lines = [f"📊 *Your caps* — {today.strftime('%b %Y')}", ""]
    sday_buttons: list[InlineKeyboardButton] = []
    pool_groups: dict[str, list[str]] = {}

    for card in owned_cards:
        if card.pool:
            pool_groups.setdefault(card.pool, []).append(card.name)
        s_day = statement_days.get(card.id)
        needs_statement = any(
            (b.cap_period or "").startswith("statement")
            or (b.min_spend_period or "").startswith("statement")
            for b in card.bonus
        )

        header = f"*{_md(card.name)}*"
        if card.pool:
            header += f"  _({_md(_pool_pretty(card.pool))})_"
        if needs_statement and s_day is None:
            header += "  ⚠ statement day not set"
            sday_buttons.append(
                InlineKeyboardButton(
                    f"Set statement day — {card.name}",
                    callback_data=f"sday:{card.id}:prompt",
                )
            )
        lines.append(header)

        if not card.bonus:
            lines.append("  no bonus categories — base rate only")
            lines.append("")
            continue

        for idx, bonus in enumerate(card.bonus):
            label = bonus.label or "bonus"
            u = usage.get((card.id, idx))
            spend = u.spend_sgd if u is not None else 0.0
            min_used = u.min_spend_sgd if u is not None else 0.0
            line_parts = [f"  {_md(label)}:"]
            cap_period = bonus.cap_period or "calendar_month"
            cycle = period_label(cap_period, today, s_day)
            if bonus.cap_sgd is not None:
                pct = int(round(spend / bonus.cap_sgd * 100)) if bonus.cap_sgd else 0
                if spend >= bonus.cap_sgd:
                    line_parts.append(
                        f"S${spend:g} / S${bonus.cap_sgd:g}  ⚠ cap reached"
                    )
                else:
                    line_parts.append(
                        f"S${spend:g} / S${bonus.cap_sgd:g}  ({pct}%)  •  {cycle}"
                    )
            else:
                line_parts.append(f"S${spend:g} spent  •  no cap  •  {cycle}")
            lines.append(" ".join(line_parts))

            if bonus.min_spend_sgd is not None:
                ms_period = bonus.min_spend_period or "calendar_month"
                if min_used >= bonus.min_spend_sgd:
                    lines.append(
                        f"    ✓ min spend met (S${min_used:g} / S${bonus.min_spend_sgd:g})"
                    )
                else:
                    gap = bonus.min_spend_sgd - min_used
                    n = days_left(ms_period, today, s_day)
                    day_word = "day" if n == 1 else "days"
                    lines.append(
                        f"    ⚠ S${gap:g} from min spend, {n} {day_word} left"
                    )
        lines.append("")

    shared_pools = [pool for pool, names in pool_groups.items() if len(names) > 1]
    if shared_pools:
        lines.append("_Cards sharing a redemption pool:_")
        for p in shared_pools:
            names_str = ", ".join(_md(n) for n in pool_groups[p])
            lines.append(f"  • {_md(_pool_pretty(p))}: {names_str}")

    kb = None
    if sday_buttons:
        # one button per row for legibility
        kb = InlineKeyboardMarkup([[b] for b in sday_buttons])
    return "\n".join(lines).rstrip(), kb


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
    txns: list[TxnRow], catalog: dict[str, Card]
) -> tuple[str, InlineKeyboardMarkup | None]:
    if not txns:
        return ("🧾 No transactions logged yet.\nLog with /log or tap 📝 after a recommendation.", None)

    lines = ["🧾 *Recent transactions*", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for i, t in enumerate(txns, start=1):
        c = catalog.get(t.card_id)
        cname = c.name if c else t.card_id
        fcy = " fcy" if t.is_fcy else ""
        line = (
            f"{i}. {t.txn_date.strftime('%b %d')}  *{_md(cname)}*  "
            f"`{t.merchant} S${t.amount_sgd:g}{fcy}`  →  {t.miles_earned} mi"
        )
        lines.append(line)
        rows.append([
            InlineKeyboardButton(f"🗑 #{i}", callback_data=f"del:{t.tx_id}"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


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


def format_lady_choice_keyboard(
    current: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    options = [
        ("Dining (local)", "dining_local"),
        ("Online shopping", "online_shopping"),
        ("Beauty & wellness", "beauty"),
        ("Entertainment", "entertainment"),
        ("Transport", "transport"),
        ("Family", "family"),
    ]
    text = "👜 *UOB Lady's Card* — pick your bonus category for this period:"
    if current:
        text += f"\nCurrent pick: `{current}`"
    rows = []
    for i in range(0, len(options), 2):
        rows.append([
            InlineKeyboardButton(label, callback_data=f"lc:{cat}")
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
    merchants: dict[str, list[str]],
    today: date | None = None,
) -> RecommendationPayload:
    today = today or date.today()
    try:
        parsed = parse(text)
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

    base_categories = list(merchants.get(parsed.merchant, []))
    unknown = parsed.merchant not in merchants

    # Inject lady_chosen if the user owns Lady's and their chosen category
    # matches this merchant.
    categories = list(base_categories)
    if any(c.id == "uob_lady" for c in owned_cards):
        choice = await storage.get_lady_choice(user_id, today=today)
        if choice and choice in base_categories:
            categories.append("lady_chosen")

    # Materialize usage from txn history.
    statement_days = await storage.get_statement_days(user_id)
    earliest_period_start = _earliest_period_start(owned_cards, today, statement_days)
    txns = await storage.list_transactions_since(user_id, since=earliest_period_start)
    usage = build_usage(txns, owned_cards, today, statement_days)

    recs = recommend(
        owned_cards,
        categories,
        parsed.amount_sgd,
        is_fcy=parsed.is_fcy,
        today=today,
        usage=usage,
        statement_days=statement_days,
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
    merchants: dict[str, list[str]],
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
                f"Unknown card id `{cid}`.\n"
                f"Valid: {', '.join(f'`{x}`' for x in sorted(catalog))}"
            )
        added = await storage.add_card(user_id, cid)
        if added:
            return f"✅ Added *{catalog[cid].name}* to your wallet."
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
            return f"🗑️ Removed *{name}* from your wallet."
        return f"`{cid}` wasn't in your wallet."

    return format_cards_help(catalog)


async def handle_log_command(
    args: list[str],
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    merchants: dict[str, list[str]],
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
        return (f"Unknown card id `{card_id}`. Try /cards for valid ids.", None)

    owned = await storage.list_cards(user_id)
    if card_id not in owned:
        return (
            f"`{card_id}` isn't in your wallet — add it first via /cards.",
            None,
        )

    merchant_raw = args[1]
    merchant = merchant_raw.lower().replace(" ", "_")
    try:
        amount = float(args[2])
    except ValueError:
        return (f"Invalid amount: `{args[2]}`. Use a number like 45 or 9.99.", None)
    if amount <= 0:
        return ("Amount must be greater than 0.", None)

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
                return (f"Couldn't parse `{extra}` — expected `fcy`/`sgd` or `yyyy-mm-dd`.", None)

    card = catalog[card_id]
    base_categories = list(merchants.get(merchant, []))

    # Lady's chosen-category injection at log time too.
    categories = list(base_categories)
    if card_id == "uob_lady":
        choice = await storage.get_lady_choice(user_id, today=txn_date)
        if choice and choice in base_categories:
            categories.append("lady_chosen")

    statement_days = await storage.get_statement_days(user_id)
    owned_cards = [catalog[c] for c in owned if c in catalog]
    earliest = _earliest_period_start(owned_cards, txn_date, statement_days)
    txns = await storage.list_transactions_since(user_id, since=earliest)
    usage = build_usage(txns, owned_cards, txn_date, statement_days)

    bonus_idx, bonus_label, miles = select_bonus_for_log(
        card, categories, amount, is_fcy, txn_date, usage, statement_days
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
    usage_after = build_usage(txns_after, owned_cards, txn_date, statement_days)
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
    return format_log_confirmation(txn, card, usage_after=usage_after)


async def handle_pools_command(
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    today: date | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    today = today or date.today()
    owned = await storage.list_cards(user_id)
    owned_cards = [catalog[c] for c in owned if c in catalog]
    statement_days = await storage.get_statement_days(user_id)
    earliest = _earliest_period_start(owned_cards, today, statement_days) if owned_cards else today
    txns = await storage.list_transactions_since(user_id, since=earliest)
    usage = build_usage(txns, owned_cards, today, statement_days)
    return format_pools(owned_cards, usage, statement_days, today)


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
        return (f"✅ Set Lady's category to `{category}` (effective {today.isoformat()}).", None)
    current = await storage.get_lady_choice(user_id, today=today)
    return format_lady_choice_keyboard(current)


# ---------------------------------------------------------------------------
# PTB wiring
# ---------------------------------------------------------------------------

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong processing your request. Check the server logs."
            )
        except Exception:
            pass


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME, parse_mode="Markdown")


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
    else:
        return

    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    if added_card is not None:
        await _maybe_prompt_statement_day(update, context, added_card, user_id)


async def _text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.text:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, list[str]] = context.application.bot_data["merchants"]
    pending_logs: dict[str, dict] = context.application.bot_data.setdefault(
        "pending_logs", {}
    )

    payload = await compute_recommendation_payload(
        update.message.text, update.effective_user.id, storage, catalog, merchants
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
    merchants: dict[str, list[str]] = context.application.bot_data["merchants"]
    args = (update.message.text or "").split()[1:]
    text, kb = await handle_log_command(
        args, update.effective_user.id, storage, catalog, merchants
    )
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _pools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    text, kb = await handle_pools_command(update.effective_user.id, storage, catalog)
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
    merchants: dict[str, list[str]] = context.application.bot_data["merchants"]
    args = [info["card_id"], info["merchant"], str(info["amount_sgd"])]
    if info["is_fcy"]:
        args.append("fcy")
    text, kb = await handle_log_command(
        args, update.effective_user.id, storage, catalog, merchants
    )
    if query.message:
        await query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def _undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    tx_id = query.data[len("undo:"):]
    deleted = await storage.delete_transaction(update.effective_user.id, tx_id)
    msg = "↩ Undone." if deleted else "Already removed."
    await query.edit_message_text(msg)


async def _del_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    tx_id = query.data[len("del:"):]
    await storage.delete_transaction(update.effective_user.id, tx_id)
    text, kb = await handle_recent_command(update.effective_user.id, storage, catalog)
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def _pools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    text, kb = await handle_pools_command(update.effective_user.id, storage, catalog)
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
            "You can change later via /pools.",
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
        f"✅ *{_md(card.name)}* statement closing day set to {day}.",
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
    await query.edit_message_text(
        f"✅ Lady's category set to `{category}` (effective {today.isoformat()}).",
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
]


def build_application(
    token: str,
    storage: Storage,
    cards: dict[str, Card],
    merchants: dict[str, list[str]],
) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["storage"] = storage
    app.bot_data["cards"] = cards
    app.bot_data["merchants"] = merchants
    app.bot_data["pending_logs"] = {}
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _start))
    app.add_handler(CommandHandler("cards", _cards))
    app.add_handler(CommandHandler("log", _log))
    app.add_handler(CommandHandler("pools", _pools))
    app.add_handler(CommandHandler("recent", _recent))
    app.add_handler(CommandHandler("lady_choice", _lady_choice))
    app.add_handler(CallbackQueryHandler(_cards_callback, pattern=r"^ck:"))
    app.add_handler(CallbackQueryHandler(_log_callback, pattern=r"^log:"))
    app.add_handler(CallbackQueryHandler(_undo_callback, pattern=r"^undo:"))
    app.add_handler(CallbackQueryHandler(_del_callback, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(_pools_callback, pattern=r"^pools$"))
    app.add_handler(CallbackQueryHandler(_sday_callback, pattern=r"^sday:"))
    app.add_handler(CallbackQueryHandler(_lc_callback, pattern=r"^lc:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text))
    app.add_error_handler(_error_handler)
    return app
