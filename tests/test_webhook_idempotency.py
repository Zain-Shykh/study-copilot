"""Integration tests for the webhook-duplicate-reply fix: the handler must
ack immediately and process the actual turn in the background, and a
redelivered message id must be a no-op rather than re-running the turn."""

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import FastAPI

from agent.webhook import routes as routes_module

APP_SECRET = "test-secret"
MY_NUMBER = "923115224115"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(routes_module.router)
    app.state.settings = SimpleNamespace(
        meta_webhook_verify_token="verify-me",
        meta_app_secret=APP_SECRET,
        my_whatsapp_number=MY_NUMBER,
        gemini_model="gemini-x",
        meta_whatsapp_access_token="token",
        meta_whatsapp_phone_number_id="phone123",
    )
    app.state.pool = MagicMock()
    app.state.graph = MagicMock()
    app.state.graph.ainvoke = AsyncMock()
    app.state.genai_client = MagicMock()
    app.state.assignment_graph = MagicMock()
    app.state.background_tasks = set()
    return app


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _text_message_payload(text: str, msg_id: str = "wamid.IN1", sender: str = MY_NUMBER) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": msg_id, "from": sender, "type": "text", "text": {"body": text}}
                            ]
                        }
                    }
                ]
            }
        ]
    }


async def _post(app: FastAPI, payload: dict) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(body)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook", content=body, headers=headers)


async def _post_and_drain(app: FastAPI, payload: dict) -> httpx.Response:
    """Posts, then awaits any background task(s) the handler dispatched —
    within the same event loop, since asyncio.create_task requires the
    loop it was scheduled on to still be running to complete."""
    response = await _post(app, payload)
    if app.state.background_tasks:
        await asyncio.gather(*app.state.background_tasks)
    return response


def test_first_delivery_acks_and_dispatches_graph_once(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(routes_module.repo, "mark_message_processed_if_new", lambda conn, mid: True)

    response = asyncio.run(_post_and_drain(app, _text_message_payload("how many assignments?")))

    assert response.status_code == 200
    app.state.graph.ainvoke.assert_awaited_once()


def test_duplicate_delivery_is_a_no_op(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(routes_module.repo, "mark_message_processed_if_new", lambda conn, mid: False)

    response = asyncio.run(_post_and_drain(app, _text_message_payload("how many assignments?")))

    assert response.status_code == 200
    app.state.graph.ainvoke.assert_not_awaited()


def test_non_text_message_reply_is_backgrounded(monkeypatch):
    app = _build_app()
    monkeypatch.setattr(routes_module.repo, "mark_message_processed_if_new", lambda conn, mid: True)
    send_mock = AsyncMock(return_value="wamid.OUT1")
    monkeypatch.setattr(routes_module, "send_whatsapp_message", send_mock)

    payload = {
        "entry": [
            {
                "changes": [
                    {"value": {"messages": [{"id": "wamid.IMG1", "from": MY_NUMBER, "type": "image", "image": {}}]}}
                ]
            }
        ]
    }

    response = asyncio.run(_post_and_drain(app, payload))

    assert response.status_code == 200
    send_mock.assert_awaited_once()
    assert send_mock.call_args[0][3] == "I can only read text messages right now."


def test_message_from_other_sender_ignored_without_dedup_check(monkeypatch):
    app = _build_app()
    mark_mock = MagicMock()
    monkeypatch.setattr(routes_module.repo, "mark_message_processed_if_new", mark_mock)

    response = asyncio.run(_post(app, _text_message_payload("hi", sender="10000000000")))

    assert response.status_code == 200
    mark_mock.assert_not_called()
    app.state.graph.ainvoke.assert_not_awaited()


def test_bad_signature_returns_403():
    app = _build_app()

    async def run():
        body = json.dumps(_text_message_payload("hi")).encode()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})

    response = asyncio.run(run())

    assert response.status_code == 403
