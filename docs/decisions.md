# Decisions

Lightweight architecture decision records. Each entry is numbered for stable cross-references (`docs/architecture.md` links here as `decisions.md#N-slug`). Numbering reflects the order decisions were made, not their importance.

## 1. SQLite as the durable store

**Decision:** A single SQLite file (`users.sqlite`) on a Railway-mounted volume, accessed via `aiosqlite` from a single bot process.

**Why:** Single-writer (only the bot writes), human-scale write rates (a few transactions per active user per day), one server. SQLite handles this workload forever. The file is portable, the format is publicly documented and Library-of-Congress-recommended for archival, and migration to anything else is `pgloader`-trivial when the day comes.

**Consequences:** No multi-region writes, no horizontal scaling, no concurrent writers. Acceptable for this workload. Forces backups to live somewhere else (Railway volume → R2/B2 daily snapshot) — this is a feature, not a bug.

## 2. YAML as card-rule storage

**Decision:** Card rules live in `data/cards.yaml`, not Python.

**Why:** Banks change T&Cs constantly. Encoding a rule as a diff (`cap_sgd: 1500 → 1000`) is reviewable; encoding it as a code change is a refactor. `cards.yaml` is also the artifact community contributors should be able to PR without learning Python internals — they're contributing facts about cards, not behavior.

**Consequences:** The schema must be enforced at startup (see [#8](#8-pydantic-schema-validation-at-startup)) so a typo in `cards.yaml` doesn't silently degrade recommendations. The rule engine becomes a pure interpreter of declarative rules; new card behaviors require either a new field (one place to change) or stretching existing fields.

## 3. Approach B for posting-date threading

**Decision:** When a card has `tracks_by: posting_date`, the predicted posting date is threaded through both `recommend()` and `build_usage()` — period membership is computed against posting date in both the recommendation path and the usage rollup.

**Why:** The alternative (Approach A) would slap a cosmetic `"Posts in May, counts toward May cap"` warning on top of math that still uses the txn date. That makes the warning a lie: the cap arithmetic and the warning would disagree on which period the txn belongs to. Approach B makes them agree by routing the same `posting_date` into both layers.

**Consequences:** `build_usage()` takes `posting_delays` and uses each transaction's predicted posting date when computing per-period spend. `today` passed to `build_usage()` must be the *period anchor* (posting date of the txn being evaluated), not literally today. Tests cover both paths together — a posting-date change must move the txn in both `recommend()`'s gating and `build_usage()`'s totals.

## 4. Pool-keyed cap aggregation

**Decision:** Cards that share a redemption pool (UOB PPV / VS / Lady all earn UNI$) declare `pool: uob_unis` in `cards.yaml`. The bot's `/pools` view aggregates by pool, not by card.

**Why:** UOB's spend tracking is real — UNI$ are awarded by the issuer, not per-card-MCC. From the user's perspective, "how much UNI$ have I earned this statement month" matters more than which specific UOB card touched the swipe. Pool-keyed views also make the recommendation logic honest about competing UOB cards: the engine still ranks per-card, but the user sees one unified bucket.

**Consequences:** The `pool` field is metadata, not a constraint enforced by the rule engine. Per-card cap math still applies for cards inside a pool; the pool is presentation. If UOB ever splits the pool (PPV no longer earns UNI$), the change is one YAML edit.

## 5. Cap-overflow blend (vs hard cutoff)

**Decision:** When a transaction crosses the bonus cap line, miles split: `floor(remaining * bonus_rate) + floor((amount - remaining) * base_rate)`. The bonus rate applies to the part that fits; the base rate applies to the overflow.

**Why:** A hard cutoff ("you've used your cap, this txn earns 0 bonus") would be wrong — the bank actually does pay bonus on the part that fits and base on the rest. The user's mental model is also "I get *some* bonus on this," not "the whole txn dropped to base." Recommendations need to match the math.

**Consequences:** The recommendation reason string explains the blend (`cap S$1500 • S$200 left → 4mpd on S$200, 0.4mpd on S$300`). `select_bonus_for_log` mirrors the same blending so logged-txn miles match what the user was shown. When all bonuses are gated (cap full or min-spend unmet), the engine falls back to the highest base-rate path on the card.

## 6. Per-user overrides for statement-day, posting-delay, anniversary-month

**Decision:** Statement closing day, issuer posting delay, and anniversary month are stored per-(user, card) in SQLite, not as global card defaults.

**Why:** Each user opened their card on a different day, each issuer's posting lag varies by user setup (auto-pay date, time of swipe), and anniversary-year cap windows are by definition per-user. A global default in `cards.yaml` would be wrong for any user who deviates from it.

**Consequences:** Three SQLite tables — `card_statement_days`, `card_posting_delays`, `card_anniversaries` — all keyed by `(telegram_user_id, card_id)`. Recommendations and usage rollups take dicts of overrides. Defaults from `cards.yaml` apply when the user hasn't set one. UX adds inline-keyboard prompts on `/cards add` for cards that *need* an anniversary month.

## 7. Webhook over long-polling on Railway

**Decision:** Telegram delivers updates via webhook to a FastAPI endpoint at `/webhook`. No long-polling in production.

**Why:** Long-polling holds an HTTP connection open while idle, which on Railway's per-second billing burns money 24/7 even when nobody is using the bot. Webhooks are inverted: Railway only spends compute when an actual update arrives.

**Consequences:** Local dev needs an ngrok tunnel (or equivalent) to receive webhook calls. The production webhook URL must be set explicitly via `setWebhook` after deploy. If Railway is unreachable, Telegram queues updates for ~24h and replays them on recovery — operationally fine.

## 8. Pydantic schema validation at startup

**Decision:** `cards.yaml` is loaded into Pydantic models (`Card`, `Bonus`, `Rounding`) with `model_config = ConfigDict(extra="forbid")` at startup. The bot refuses to start on validation failure.

**Why:** Card rules are facts, and facts wrong-typed are worse than facts missing — a typo of `cap_sgd: 150o` (letter O) silently becomes a string and the cap is never enforced. Catching typos at startup is the cheapest possible test.

**Consequences:** Adding a new card field requires updating `models.py` first, not just `cards.yaml`. `extra="forbid"` means a misspelled field name is a hard error, not silently ignored. The startup cost is negligible (~50 cards).

## 9. Pure rule engine (no I/O in `recommend()`)

**Decision:** `recommend()` is a pure function. All inputs come from the caller — cards, merchant categories, amount, today, usage, statement_days, posting_delays, anniversary_months. No file reads, no DB queries, no clock calls except via the optional `today` parameter.

**Why:** The rule engine is the thing that absolutely must be right. Purity makes it ruthlessly testable: every behavior is a unit test that constructs inputs in-memory. It also separates concerns cleanly — the bot layer is responsible for I/O and assembly; the engine is responsible for math.

**Consequences:** The bot has to gather all inputs before calling the engine (storage queries for usage, statement days, etc.), which is more verbose than letting the engine fetch what it needs — but also more honest about the data dependencies. `build_usage()` is similarly pure; storage produces `TxnRow`s, usage rolls them up, the engine consumes the rollup.

## 10. Anniversary-year cap period (12-month sliding window)

**Decision:** Some cards (typically high-tier travel cards) have caps that reset annually on the card's anniversary month, not at the calendar or statement-month boundary. This is a first-class period type in the engine: `cap_period: anniversary_year`, requiring per-user `anniversary_month`.

**Why:** Anniversary-year resets are how PRVI-class cards actually work — encoding them as quarterly approximations would systematically misreport remaining cap. Once `period_bounds()` already supported four other period types, adding a fifth was a natural extension rather than a special case.

**Consequences:** The `card_anniversaries` SQLite table stores per-user anniversary month. `/cards add` prompts for it via inline keyboard for cards declaring `anniversary_year: true`. If a user hasn't set their anniversary month, anniversary-year periods raise — `recommend()` short-circuits with a prompt to set it.
