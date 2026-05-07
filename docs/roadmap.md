# Roadmap

Status as of 2026-05-06; checkboxes reflect what's shipped.

## v1 — Stateless recommender (weekend, ~2 days)

The smallest thing that's already useful. No state, no caps, no logging.

- [x] Repo + dependencies (`python-telegram-bot`, `pyyaml`, `pydantic`, `pytest`)
- [x] `cards.yaml` for 6–8 cards I actually own + popular ones (HSBC Revo, UOB PPV/VS/PRVI, Citi Rewards/PM, DBS Altitude, Amex KF)
- [x] `merchants.yaml` seed list (~50 SG merchants)
- [x] `rule_engine.py` — `recommend()` function with rounding + FCY
- [x] `tests/` — 20+ pytest cases covering edge cases (UOB rounding, FCY fees, MCC exclusions)
- [x] `bot.py` — Telegram webhook, parse `merchant amount [fcy]`, `/cards add/remove/list`
- [x] Deploy to Railway (attach a persistent volume for `users.sqlite`)
- [x] Send to 3 friends, watch them break the parser, iterate

**Ship gate:** Bot returns correct recommendations for 10 hand-crafted scenarios in <500ms.

## v1.5 — Better input parsing (optional, ~1 day)

If friends keep typo-ing, add an LLM input parser as a thin shell:

- [x] Gemma 4 31B via Gemini API with tight JSON schema (`response_schema`)
- [x] Falls back to regex parser on LLM failure; auto-disabled when `GEMINI_API_KEY` is absent
- [x] LLM **only** produces structured input; never decides cards
- [x] Infers `is_fcy` from currency codes (usd, eur, gbp, …) in addition to the explicit `fcy` keyword

## v2 — Logging & cap awareness (week 3–4)

This is when the bot becomes genuinely sticky.

- [x] `/log <card> <merchant> <amount> [fcy] [yyyy-mm-dd]` command + inline 📝 buttons after recommendations
- [x] SQLite tables for transactions, statement closing days, and Lady's chosen category per user
- [x] Recommendation factors in remaining cap (blends bonus/base on overflow), min spend progress
- [x] Bonus output: `⚠ S$80 from UOB VS min spend, 6 days left in statement month`
- [x] `/pools` command — show all card cap states at a glance, grouped by redemption pool
- [x] `/recent` + per-txn 🗑 button + per-confirmation ↩ Undo for correction flows
- [x] `/lady_choice` for UOB Lady's chosen-category UX

**Ship gate:** Recommendations change correctly as caps fill up. Tested across calendar/statement boundaries.

## v3 — Posting date intelligence (month 2)

The unique-vs-HeyMax layer. HeyMax tracks via Visa Offers Platform — transaction-date only, network-locked. This bot can go further: model each issuer's posting lag and warn before period boundaries eat your cap.

- [x] Add `posting_delay_days` to `cards.yaml`: typical lag in days (T+1=HSBC/Citi, T+2=UOB, T+3=Amex) + weekend skip rule
- [x] Tag merchants known to post same-day in `merchants.yaml` (`same_day_posting: true` for Grab, Shopee, NTUC)
- [x] `resolve_posting_date(txn_date, delay_days, same_day_merchant)` — predicts posting date, skips weekends
- [x] Detect period boundary: flag when `txn_date` and predicted `posting_date` land in different cap periods
- [x] Append posting warning to recommendations: `"⚠ Posts Fri 1 May — counts toward May cap, not Apr"`
- [x] End-of-period nudge in `/pools`: "⏰ Last N days for HSBC Revo cap — same-day posters: Grab, Shopee..."
- [x] Per-user posting delay override stored in SQLite (same pattern as statement closing days)
- [x] `anniversary_year` cap period in rule engine: 12-month window from card opening month, resets annually
- [x] Per-user anniversary month stored in SQLite; inline keyboard prompt on `/cards add` for anniversary_year cards

**Architecture:** posting_date threaded into rule engine (Approach B) — cap math is actually correct, not cosmetic. For `tracks_by: posting_date` cards, both `recommend()` and `build_usage()` evaluate period membership against predicted posting date. See [Decision #3](decisions.md#3-approach-b-for-posting-date-threading).

**Ship gate:** Transaction at 11 pm on 30 Apr shows `"Posts Fri 1 May — counts toward May cap, not Apr"` for HSBC Revo. `/pools` on 29 Apr shows nudge for HSBC Revo (1 day left, delay=1, threshold=2).

**Post-v3 UX polish (shipped):**
- Recommendations: MPD-only display (no absolute miles), top-5 cards with medals 🥇🥈🥉4️⃣5️⃣, `_(tied)_` notation for tied ranks
- `/help` split into Simple / Advanced views with ⚙️ toggle
- `/recent` edit mode — ✏️ Enter / ← Done, bulk 🗑 delete without per-action confirmation
- `/pools` progress bars and miles-earned tracking
- `/lady_choice` updated to 7 correct UOB categories with tag-based matching
- Card catalog buttons include `descriptor` value-prop
- Log confirmation shows cap progress bar

## v4 — The Milelion knowledge layer (month 3)

This is where AI earns its keep.

- [ ] Scrape Milelion (with attribution and respectful rate limits)
- [ ] Chunk by article + card, embed, store in vector DB
- [ ] `/ask` command — RAG over Milelion + community Q&A
- [ ] Always cite source articles; never claim authority Milelion has

**Out of scope:** scraping behind paywalls, anything Aaron doesn't want me to.
