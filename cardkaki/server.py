"""FastAPI app: Telegram webhook + healthz + daily backup scheduler.

Run via `python -m cardkaki.server` (the form the README uses).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from telegram import Update

from .backup import run_backup
from .bot import build_application
from .data import load_cards, load_merchants
from .storage import Storage

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cardkaki")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    db_path = Path(os.environ.get("DB_PATH", "./data/users.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    storage = Storage(db_path)
    await storage.init()
    cards = load_cards(data_dir / "cards.yaml")
    merchants = load_merchants(data_dir / "merchants.yaml")

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    secret = os.environ["WEBHOOK_SECRET"]
    application = build_application(token, storage, cards, merchants)
    await application.initialize()

    base = os.environ.get("WEBHOOK_BASE_URL")
    if base:
        webhook_url = f"{base.rstrip('/')}/webhook"
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("webhook set to %s", webhook_url)
    else:
        log.info("WEBHOOK_BASE_URL unset — skipping set_webhook (local mode)")

    await application.start()

    scheduler = AsyncIOScheduler()
    if os.environ.get("BACKUP_ENABLED", "false").lower() == "true":
        backup_dir = Path(os.environ.get("BACKUP_DIR", "./backups"))
        # 18:00 UTC = 02:00 SGT next day
        scheduler.add_job(
            run_backup,
            CronTrigger(hour=18, minute=0),
            args=[db_path, backup_dir],
            id="daily_backup",
        )
        scheduler.start()
        log.info("daily backup scheduled at 18:00 UTC")

    app.state.app_telegram = application
    app.state.secret = secret
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await application.stop()
        await application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request) -> dict:
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header_secret != app.state.secret:
        raise HTTPException(status_code=403, detail="bad secret token")
    try:
        data = await request.json()
        update = Update.de_json(data, app.state.app_telegram.bot)
    except Exception as e:  # noqa: BLE001 — Telegram payload errors come in many shapes
        log.warning("rejecting invalid webhook payload: %s", e)
        raise HTTPException(status_code=400, detail="invalid update payload") from None
    if update is None:
        raise HTTPException(status_code=400, detail="invalid update payload")
    await app.state.app_telegram.update_queue.put(update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "cardkaki.server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )
