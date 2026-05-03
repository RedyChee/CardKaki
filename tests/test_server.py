"""Integration tests for the FastAPI app.

The lifespan requires a real Telegram bot token + RPC, which we don't
want in unit tests. Instead we mount the routes against ASGITransport
with lifespan disabled and stub `app.state` directly. /healthz works
standalone; /webhook auth is tested by stubbing the PTB application.
"""
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
async def client():
    from cardkaki.server import app

    # Stub the bits the webhook handler reaches into.
    app.state.ready = True
    app.state.secret = "test-secret"
    app.state.app_telegram = SimpleNamespace(
        bot=SimpleNamespace(),
        update_queue=_FakeQueue(),
    )

    # lifespan disabled → no PTB initialize, no startup hooks
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class _FakeQueue:
    def __init__(self):
        self.items = []

    async def put(self, item):
        self.items.append(item)


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


async def test_webhook_rejects_missing_header(client):
    resp = await client.post("/webhook", json={"update_id": 1})
    assert resp.status_code == 403


async def test_webhook_rejects_wrong_secret(client):
    resp = await client.post(
        "/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403


async def test_webhook_rejects_invalid_payload(client):
    # Empty payload — Update.de_json returns None, we reject with 400
    resp = await client.post(
        "/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
    )
    assert resp.status_code == 400
