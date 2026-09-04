"""Verifies Meta's webhook signature, parses the inbound payload, and invokes the LangGraph app."""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from agent.graph.nodes.whatsapp_send import send_whatsapp_message

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/webhook")
def verify_webhook(request: Request) -> PlainTextResponse:
    mode = request.query_params.get("hub.mode")
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    settings = request.app.state.settings
    if mode == "subscribe" and verify_token == settings.meta_webhook_verify_token:
        return PlainTextResponse(challenge, status_code=200)
    return PlainTextResponse("", status_code=403)


def _verify_signature(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    if not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = header_value.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


async def _send_reply(request: Request, to: str, body: str) -> None:
    settings = request.app.state.settings
    try:
        await send_whatsapp_message(
            settings.meta_whatsapp_access_token,
            settings.meta_whatsapp_phone_number_id,
            to,
            body,
        )
    except Exception:
        logger.exception("Failed to send WhatsApp reply to %s", to)


@router.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    settings = request.app.state.settings
    raw_body = await request.body()

    signature = request.headers.get("X-Hub-Signature-256")
    if not _verify_signature(settings.meta_app_secret, raw_body, signature):
        return Response(status_code=403)

    payload = await request.json()

    try:
        messages = payload["entry"][0]["changes"][0]["value"].get("messages")
    except (KeyError, IndexError):
        messages = None

    if not messages:
        # A status-update callback (delivery/read receipts), not an inbound
        # message — expected, not an error.
        return Response(status_code=200)

    message = messages[0]
    sender = message["from"]

    if sender != settings.my_whatsapp_number:
        # Single-user product — Meta's test number isn't public, so a
        # message from anyone else is silently ignored, not an error.
        return Response(status_code=200)

    if message.get("type") != "text":
        await _send_reply(request, sender, "I can only read text messages right now.")
        return Response(status_code=200)

    text = message["text"]["body"]
    msg_id = message["id"]

    try:
        graph = request.app.state.graph
        await graph.ainvoke(
            {"inbound_text": text, "whatsapp_message_id": msg_id, "sender": sender},
            config={
                "configurable": {
                    "thread_id": f"user:{sender}",
                    "pool": request.app.state.pool,
                    "genai_client": request.app.state.genai_client,
                    "gemini_model": settings.gemini_model,
                    "whatsapp_access_token": settings.meta_whatsapp_access_token,
                    "whatsapp_phone_number_id": settings.meta_whatsapp_phone_number_id,
                }
            },
        )
    except Exception:
        # Not a modeled failure (those are handled inside the graph's own
        # nodes) — an actual bug. Meta still gets a 2xx so it doesn't
        # retry-storm the same webhook event.
        logger.exception("Unhandled error processing inbound WhatsApp message")
        await _send_reply(request, sender, "Something went wrong handling that — please try again.")

    return Response(status_code=200)
