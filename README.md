# MilesBot 🛫

> A Telegram bot that tells you which Singapore credit card to use at checkout — without the manual gymnastics of cross-referencing Milelion articles, MCC lists, and HeyMax.

## The problem

Earning miles in Singapore is a research project disguised as a payment. To pick the optimal card for a single transaction, you need to mentally juggle:

- **Merchant Category Code (MCC)** — does this merchant qualify for the bonus?
- **Caps & sub-caps** — how much of HSBC Revo's S$1,500/month bonus pool have I used?
- **Minimum spend** — is UOB Visa Signature's S$1,000 threshold met yet?
- **Calendar vs statement month** — which clock is this card on?
- **Transaction date vs posting date** — will this transaction post in time?
- **Rounding rules** — UOB rounds down to nearest S$5, killing small transactions
- **FCY fees** — 3.25% can wipe out bonus rates
- **Card pooling** — UOB PPV/VS/Lady's share UNI$; which one to feed?

Existing tools have gaps:

| Tool | Limitation |
|------|------------|
| HeyMax Card Maximiser | Auto-tracking is Visa-only (Visa Offers Platform API dependency); Mastercard/Amex tracking is structurally impossible until those networks expose equivalents |
| Milelion | Authoritative knowledge buried in long-form articles |
| Bank apps | Track per-card on statement basis; no cross-card view |
| Mental math | You will forget the UOB rounding rule. Every time. |

## What this is

A **deterministic rule engine** wrapped in a **Telegram bot**. You message it `cold storage 45`, it tells you the optimal card from *your* wallet, with reasoning.

```
You: cold storage 45
Bot: 🥇 HSBC Revo: 180 mi (4.0 mpd) ✓ groceries
     🥈 UOB PPV:    140 mi (3.1 mpd) ⚠ in-store, not contactless?
     🥉 Citi Rewards: 18 mi (0.4 mpd)

You: klook 320
Bot: ⚠️  MCC 4722 (travel) — most 4mpd cards exclude this
     🥇 UOB PRVI: 448 mi (1.4 mpd) base
     Use UOB PRVI or any general-spend card.
```

## Design principles

1. **Deterministic core, AI at the edges.** The rule engine is pure Python. LLMs only touch input parsing (v1.5) and the `/ask` knowledge layer (v3+). Miles math never runs through an LLM.
2. **Sub-second response.** If it's slower than glancing at your wallet, nobody uses it.
3. **Card rules as data, not code.** Every bank changes T&Cs constantly. Encode in YAML so updates are diffs, not refactors.
4. **Honest about uncertainty.** "Best-guess based on typical MCC. Verify against your statement." Set expectations, earn trust.
5. **Privacy by default.** No card numbers, no bank linkage, no PCI scope. Just merchant names and amounts.

## Architecture

```mermaid
flowchart LR
    User([Telegram user]) -->|message| Bot
    Bot[FastAPI webhook<br/>+ python-telegram-bot] -->|parsed input| Engine
    Engine[Rule Engine<br/>pure function] -->|ranked recs| Bot
    Bot -->|reply| User

    Engine -.reads.-> Cards[(cards.yaml<br/>card rules)]
    Engine -.reads.-> Merchants[(merchants.yaml<br/>merchant → categories)]
    Engine -.reads/writes.-> DB[(users.sqlite<br/>wallets + pool state)]

    classDef store fill:#fef3c7,stroke:#92400e,color:#92400e
    classDef core fill:#dbeafe,stroke:#1e40af,color:#1e40af
    classDef edge fill:#f3f4f6,stroke:#374151,color:#374151
    class Cards,Merchants,DB store
    class Engine core
    class Bot,User edge
```

**Reading the diagram:** the rule engine is the brain — it's a pure function with no I/O. The bot is a thin shell that handles Telegram plumbing and feeds structured input into the engine. The three data stores are deliberately separate concerns: card rules (rarely change), merchant mappings (community-growable), and user state (per-user, mutable).

### The rule engine

A pure function. No I/O. Fully unit-testable.

```python
def recommend(
    user_cards: list[Card],
    merchant_categories: list[str],
    amount_sgd: float,
    is_fcy: bool = False,
    today: date = date.today(),
) -> list[Recommendation]:
    """Returns ranked list of (card, miles, effective_mpd, reasons)."""
```

Always returns **effective mpd** (after rounding + FCY fee), not nominal. UOB PRVI's nominal 1.4 mpd becomes 0.6 mpd effective on a S$9.99 transaction. Surfacing that is half the value of the project.

### Data model

**`cards.yaml`** — encode each card's full ruleset:

```yaml
- id: hsbc_revo
  name: HSBC Revolution
  issuer: hsbc
  network: visa
  base_rate_mpd: 0.4
  bonus:
    - rate_mpd: 4.0
      categories: [online_shopping, dining_local, contactless]
      excluded_mccs: [4722, 4511, 6540]   # travel, airlines, wallets
      cap_sgd: 1500
      cap_period: calendar_month
      min_spend_sgd: null
  pool: hsbc_rewards
  tracks_by: posting_date
  fcy_fee: 0.0325
  notes: "MCC-driven; mobile wallet top-ups excluded"

- id: uob_ppv
  name: UOB Preferred Platinum Visa
  issuer: uob
  network: visa
  base_rate_mpd: 0.4
  rounding: { method: floor_sgd_5, unis_per_block: 3.5 }
  bonus:
    - rate_mpd: 4.0
      categories: [mobile_contactless, online_shopping, entertainment]
      cap_sgd: 1000
      cap_period: calendar_month
  pool: uob_unis   # shared with uob_lady, uob_vs
  tracks_by: posting_date
  fcy_fee: 0.0325
```

**`merchants.yaml`** — merchant → internal categories. Doesn't need to be MCC-perfect; just consistent with the bonus rule labels.

```yaml
cold_storage:    [groceries, contactless]
shopee:          [online_shopping]
grab_ride:       [transport, online_shopping]
grab_food:       [dining_delivery, online_shopping]
klook:           [travel_excluded]
spc:             [petrol, uob_excluded]
simplygo:        [transport, public_transit]
```

**`users.sqlite`** — minimal: `(telegram_user_id, card_ids[], created_at)`. Pool state added in v2.

## Roadmap

### v1 — Stateless recommender (weekend, ~2 days)

The smallest thing that's already useful. No state, no caps, no logging.

- [ ] Repo + dependencies (`python-telegram-bot`, `pyyaml`, `pydantic`, `pytest`)
- [ ] `cards.yaml` for 6–8 cards I actually own + popular ones (HSBC Revo, UOB PPV/VS/PRVI, Citi Rewards/PM, DBS Altitude, Amex KF)
- [ ] `merchants.yaml` seed list (~50 SG merchants)
- [ ] `rule_engine.py` — `recommend()` function with rounding + FCY
- [ ] `tests/` — 20+ pytest cases covering edge cases (UOB rounding, FCY fees, MCC exclusions)
- [ ] `bot.py` — Telegram webhook, parse `merchant amount [fcy]`, `/cards add/remove/list`
- [ ] Deploy to Fly.io free tier (or wherever)
- [ ] Send to 3 friends, watch them break the parser, iterate

**Ship gate:** Bot returns correct recommendations for 10 hand-crafted scenarios in <500ms.

### v1.5 — Better input parsing (optional, ~1 day)

If friends keep typo-ing, add an LLM input parser as a thin shell:

- [ ] Haiku / gpt-4o-mini call with tight JSON schema
- [ ] Falls back to regex parser on LLM failure
- [ ] LLM **only** produces structured input; never decides cards

Skip if the strict format works for users.

### v2 — Logging & cap awareness (week 3–4)

This is when the bot becomes genuinely sticky.

- [ ] `log <card> <merchant> <amount>` command
- [ ] SQLite tables for transactions and pool state per user
- [ ] Recommendation factors in remaining cap, min spend progress, pool balance
- [ ] Bonus output: `⚠️ You're S$80 from UOB VS min spend, 6 days left in statement month`
- [ ] `/pools` command — show all card pool states at a glance
- [ ] Reset/correct flows for transactions logged wrong

**Ship gate:** Recommendations change correctly as caps fill up. Tested across calendar/statement boundaries.

### v3 — Posting date intelligence (month 2)

The unique-vs-HeyMax layer.

- [ ] Encode each issuer's typical posting behavior (T+1/T+2/T+3, weekend handling)
- [ ] "This will post Mon 5 May, after your Apr statement closes" warnings
- [ ] End-of-period nudges: "Last 2 days for HSBC Revo cap — same-day posters: Grab, Shopee, NTUC"
- [ ] Membership-year tracking for cards like KrisFlyer UOB

### v4 — The Milelion knowledge layer (month 3)

This is where AI earns its keep.

- [ ] Scrape Milelion (with attribution and respectful rate limits)
- [ ] Chunk by article + card, embed, store in vector DB
- [ ] `/ask` command — RAG over Milelion + community Q&A
- [ ] Always cite source articles; never claim authority Milelion has

**Out of scope:** scraping behind paywalls, anything Aaron doesn't want me to.

## Tech stack

Boring on purpose.

- **Python 3.11+**
- **`python-telegram-bot`** — handles Telegram plumbing
- **FastAPI** — webhook receiver (lighter than long-polling for shared use)
- **Pydantic** — schema validation for `cards.yaml` (catch typos at startup)
- **SQLite** — the long-term answer for this project, not a stepping stone. Single-writer (the bot process), human-scale write rates, one server. SQLite handles this forever.
- **pytest** — the rule engine MUST be ruthlessly tested
- **Fly.io free tier** (or a $4/mo Hetzner VPS, or a Raspberry Pi at home) — Railway is no longer free; avoid
- **Optional v1.5+:** Anthropic API (Claude Haiku) for parsing
- **Optional v4:** any vector DB (Chroma local is fine), Claude/GPT for `/ask`

## Project layout

```
milesbot/
├── README.md
├── pyproject.toml
├── .env.example
├── data/
│   ├── cards.yaml            # the product, basically
│   └── merchants.yaml        # community-growable
├── milesbot/
│   ├── __init__.py
│   ├── models.py             # pydantic schemas
│   ├── rule_engine.py        # pure functions
│   ├── parser.py             # text → structured input
│   ├── bot.py                # telegram handlers
│   ├── storage.py            # sqlite wrapper
│   └── server.py             # FastAPI webhook
├── tests/
│   ├── test_rule_engine.py   # the most important file in the repo
│   ├── test_parser.py
│   └── fixtures/
└── scripts/
    └── seed_db.py
```

## Things I will *not* build (and why)

- ❌ **Bank API integrations** — regulatory hell, SG banks largely don't expose them
- ❌ **PDF statement parsing in v1** — looks impressive, takes forever, manual log is fine
- ❌ **Optimal redemption calculator** — Milelion already does this well; reinventing is a trap
- ❌ **AI agent for the recommendation flow** — deterministic problem, deterministic solution
- ❌ **100% accuracy promises** — banks change rules, MCCs surprise, set expectations honestly
- ❌ **Public bot from day 1** — invite codes only until v2; spam will burn me out otherwise
- ❌ **All 40+ SG miles cards on day 1** — start with the 8 most-used, expand on demand

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Bank changes T&Cs, my data goes stale | Daily automated monitoring of Milelion RSS, HeyMax announcements, and r/singaporefi for change signals. LLM proposes patches to `cards.yaml`; human reviews before merging. *Don't* scrape bank T&C pages directly — the actual rules live in Milelion's testing, not in marketing copy. |
| User loses miles trusting wrong rec | Disclaimer in `/start`, `⚠️` markers when uncertain, easy `/correct` flow |
| Merchant DB never gets accurate enough | `/correct` command lets users teach the bot; review submissions weekly |
| Telegram bot gets spammed | Invite-only via `/start <code>`; rate-limit per user |
| I lose interest | Code as if I will. Tests, README, deploy script. Future-me thanks present-me. |
| Milelion or HeyMax send angry email | Be a good citizen — attribution, no scraping behind logins, link out generously |

## Success metrics

For me, not for VCs:

- Do **I** use it weekly? (If not, kill the project.)
- Do 5 friends use it weekly after onboarding? (Real signal.)
- Does the merchant DB grow via `/correct` without my intervention? (Network effect working.)
- Number of "huh, didn't know that" reactions to a recommendation explanation. (Educational value.)

## Getting started

```bash
git clone <repo>
cd milesbot
cp .env.example .env             # add TELEGRAM_BOT_TOKEN
uv sync                          # or pip install -e .
pytest                           # all green before you do anything else
python -m milesbot.server        # local dev with ngrok
```

## Inspirations & credits

- **The MileLion** (milelion.com) — the source of truth for SG miles knowledge. This bot stands on Aaron's shoulders.
- **HeyMax** — proved auto-tracking is valuable. Their Visa-only Card Maximiser is structurally locked to that network (Visa Offers Platform); manual logging is the only path to multi-network coverage, and that's the gap this project fills.
- **r/singaporefi** — community-maintained MCC observations.

## License

MIT. The card rules in `cards.yaml` are facts, not code; treat as public-domain reference.

---

*Built because picking a credit card shouldn't require a spreadsheet.*