"""Telegram handlers, factored as pure helpers + thin PTB wiring.

Helpers (`format_*`, `handle_*`) take primitives and return reply strings,
which is what `tests/test_bot_handlers.py` exercises. The PTB application
factory at the bottom wires those helpers to commands/messages.
"""
from __future__ import annotations

import logging
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

from .models import Card, Recommendation
from .parser import parse
from .rule_engine import recommend
from .storage import Storage

log = logging.getLogger(__name__)


WELCOME = (
    "👋 *CardKaki* — tells you which SG card to use at checkout.\n\n"
    "*Add a card:* `/cards add <id>`  e.g. `/cards add hsbc_revo`\n"
    "*List your cards:* `/cards list`\n"
    "*Browse available cards:* `/cards catalog`\n"
    "*Remove:* `/cards remove <id>`\n\n"
    "Then send: `<merchant> <amount> [fcy]`\n"
    "e.g. `cold storage 45`  ·  `klook 320 fcy`\n\n"
    "⚠️ Recommendations are best-effort, based on Milelion-cited rules. "
    "Verify against your statement. Caps and min-spend are *informational* in v1."
)


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
            "Try `/cards add hsbc_revo` (see /start for the format)."
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
    # If the top two tie on miles, drop medals to "=" markers so the tie is visible.
    rendered_lines: list[str] = []
    if len(top) >= 2 and top[0].miles == top[1].miles and top[0].miles > 0:
        rendered_lines.append(_render_tied(top))
    else:
        for i, r in enumerate(top):
            rendered_lines.append(f"{medals[i]} {_render_one(r)}")

    return "\n".join(header_bits + rendered_lines)


def _render_one(r: Recommendation) -> str:
    head = f"*{r.card_name}*: {r.miles} mi ({r.effective_mpd:.1f} mpd)"
    top_reason = r.reasons[0] if r.reasons else ""
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

    rows = [
        [InlineKeyboardButton(
            f"🗑️ {catalog[cid].name if cid in catalog else cid}",
            callback_data=f"ck:rm:{cid}",
        )]
        for cid in card_ids
    ]
    rows.append([
        InlineKeyboardButton("🔍 Browse & Add", callback_data="ck:catalog"),
        InlineKeyboardButton("← Back", callback_data="ck:menu"),
    ])
    return text, InlineKeyboardMarkup(rows)


def format_catalog_keyboard(
    catalog: dict[str, Card], owned_ids: list[str]
) -> tuple[str, InlineKeyboardMarkup]:
    owned = set(owned_ids)
    rows = []
    for cid in sorted(catalog):
        name = catalog[cid].name
        if cid in owned:
            rows.append([InlineKeyboardButton(f"✓ {name}", callback_data=f"ck:own:{cid}")])
        else:
            rows.append([InlineKeyboardButton(f"+ {name}", callback_data=f"ck:add:{cid}")])
    rows.append([
        InlineKeyboardButton("📋 My Cards", callback_data="ck:wallet"),
        InlineKeyboardButton("← Back", callback_data="ck:menu"),
    ])
    return "Available cards — tap to add:", InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Pure handlers (storage + catalog in, reply string out)
# ---------------------------------------------------------------------------

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


async def handle_text_message(
    text: str,
    user_id: int,
    storage: Storage,
    catalog: dict[str, Card],
    merchants: dict[str, list[str]],
) -> str:
    try:
        parsed = parse(text)
    except ValueError as e:
        return str(e)

    owned_ids = await storage.list_cards(user_id)
    owned_cards = [catalog[cid] for cid in owned_ids if cid in catalog]
    if not owned_cards:
        return (
            "Your wallet is empty — add cards first.\n"
            "Try `/cards add hsbc_revo`."
        )

    categories = merchants.get(parsed.merchant, [])
    unknown = parsed.merchant not in merchants

    recs = recommend(
        owned_cards,
        categories,
        parsed.amount_sgd,
        is_fcy=parsed.is_fcy,
    )

    # All excluded = no card got a bonus despite categories suggesting one
    # (i.e. there's at least one bonus-shaped exclusion in merchant categories
    # and every rec fell to base or excluded).
    all_excluded = bool(categories) and all(
        not any(reason.startswith("✓") for reason in r.reasons) for r in recs
    )

    return format_recommendations(
        recs,
        merchant=parsed.merchant,
        amount_sgd=parsed.amount_sgd,
        is_fcy=parsed.is_fcy,
        unknown_merchant=unknown,
        all_excluded=all_excluded,
    )


# ---------------------------------------------------------------------------
# PTB wiring
# ---------------------------------------------------------------------------

async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def _cards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text, kb = format_menu_keyboard()
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
        await storage.add_card(user_id, cid)
        owned = await storage.list_cards(user_id)
        text, kb = format_catalog_keyboard(catalog, owned)
    elif data.startswith("ck:rm:"):
        cid = data[6:]
        await storage.remove_card(user_id, cid)
        owned = await storage.list_cards(user_id)
        text, kb = format_wallet_keyboard(owned, catalog)
    elif data.startswith("ck:own:"):
        await query.answer("Already in your wallet", show_alert=True)
        return
    else:
        return

    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def _text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.message.text:
        return
    storage: Storage = context.application.bot_data["storage"]
    catalog: dict[str, Card] = context.application.bot_data["cards"]
    merchants: dict[str, list[str]] = context.application.bot_data["merchants"]
    reply = await handle_text_message(
        update.message.text,
        update.effective_user.id,
        storage,
        catalog,
        merchants,
    )
    await update.message.reply_text(reply, parse_mode="Markdown")


BOT_COMMANDS = [
    BotCommand("start", "Set up your wallet and get started"),
    BotCommand("help", "Show help and available commands"),
    BotCommand("cards", "Manage your cards (add, list, remove, catalog)"),
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
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("help", _start))
    app.add_handler(CommandHandler("cards", _cards))
    app.add_handler(CallbackQueryHandler(_cards_callback, pattern=r"^ck:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _text))
    return app
