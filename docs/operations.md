# Operations

Running CardKaki in production: deploy to Railway, keep backups, and stay portable.

## Deploying to Railway

```bash
railway login
railway init
railway volume add --mount-path /data    # SQLite lives here, survives redeploys
railway variables set TELEGRAM_BOT_TOKEN=...
railway up
```

Two things to get right:

1. **Persistent volume for SQLite.** Railway's filesystem is ephemeral — if `users.sqlite` lives in the app directory, it gets wiped on every redeploy. Mount a volume at `/data` and point `DB_PATH=/data/users.sqlite` so user wallets and pool state survive deploys.
2. **Webhook, not long-polling.** Set the Telegram webhook to `https://<your-railway-domain>/webhook`. Long-polling works locally but burns Railway's per-second billing while idle.

## Backups & data portability

The data layer is a single SQLite file (`users.sqlite`) on the Railway volume. This is deliberate — it's the most portable durable storage there is. The file format is publicly documented, has 20+ years of backward compatibility, and is explicitly recommended by the Library of Congress as a long-term archival format. Whatever you build into this bot, the user data will outlive your hosting choices.

**Backup strategy.** Run a daily snapshot from inside the bot. Costs ~$0.01/month and serves three purposes: disaster recovery, exit insurance, and time-travel debugging.

```python
# cardkaki/backup.py — runs daily via APScheduler
import sqlite3
from datetime import datetime, timezone

def backup_db():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup_path = f"/tmp/users-{ts}.sqlite"

    # .backup() produces a consistent snapshot without locking the live DB
    src = sqlite3.connect(os.environ["DB_PATH"])
    dst = sqlite3.connect(backup_path)
    src.backup(dst)
    src.close(); dst.close()

    upload_to_r2(backup_path, f"backups/{ts}.sqlite")
    os.remove(backup_path)
```

Use `sqlite3.Connection.backup()` rather than `cp` — the latter can produce a corrupted snapshot if the bot is mid-write.

## Leaving Railway

When/if that day comes, migration is trivial because the data is just a file:

```bash
# Pull the live DB off Railway
railway run sqlite3 /data/users.sqlite ".backup /tmp/exit.sqlite"
railway run cat /tmp/exit.sqlite > users.sqlite

# Move to wherever — VPS, Pi, laptop, anywhere SQLite runs
scp users.sqlite user@new-host:/data/users.sqlite
```

Then update `DB_PATH`, `WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN` env vars on the new host, point the Telegram webhook at the new URL, done. Realistic downtime: a couple of minutes while DNS/webhook propagates. Telegram queues messages during the gap.

## Keeping exit cheap

Two habits make migration painless if you ever need it:

1. **Everything platform-specific lives in env vars.** `DB_PATH`, `PORT`, `WEBHOOK_URL` — never hardcode Railway-isms in code.
2. **The daily R2/B2 backup means you can leave even if Railway is unreachable.** Worst case you restore from yesterday's snapshot; you lose a day, not the project.

## Outgrowing SQLite

If you ever do (you probably won't for this workload — see "Risks & mitigations" in [`README.md`](../README.md)), `pgloader` migrates SQLite → Postgres in one command. The schema is simple enough that this is a half-day job, not a project.
