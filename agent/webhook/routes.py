"""Verifies Meta's webhook signature, parses the inbound payload, and invokes
the LangGraph app.

Acks Meta immediately after signature verification + a fast idempotency
check, then runs the actual graph turn as a background task. A tool-calling
turn can take long enough that Meta decides the webhook timed out and
redelivers the same message; running it inline used to mean every
redelivery re-ran the whole turn and sent an independent (differently
worded) reply. The idempotency check against processed_messages is
defense-in-depth for redeliveries that happen for other reasons."""

import asyncio
import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from agent.db import repo
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


def _log_if_failed(task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background inbound-message task raised an uncaught exception", exc_info=exc)


async def _process_text_message(
    request: Request, text: str, msg_id: str, sender: str, reply_to_message_id: str | None
) -> None:
    """Runs the actual graph turn — dispatched as a background task so a
    slow tool-calling loop can't make the webhook handler itself slow
    enough for Meta to treat it as timed out and redeliver the message."""
    settings = request.app.state.settings
    try:
        graph = request.app.state.graph
        await graph.ainvoke(
            {
                "inbound_text": text,
                "whatsapp_message_id": msg_id,
                "sender": sender,
                "reply_to_message_id": reply_to_message_id,
            },
            config={
                "configurable": {
                    "thread_id": f"user:{sender}",
                    "pool": request.app.state.pool,
                    "genai_client": request.app.state.genai_client,
                    "gemini_model": settings.gemini_model,
                    "whatsapp_access_token": settings.meta_whatsapp_access_token,
                    "whatsapp_phone_number_id": settings.meta_whatsapp_phone_number_id,
                    "assignment_graph": request.app.state.assignment_graph,
                    "background_tasks": request.app.state.background_tasks,
                }
            },
        )
    except Exception:
        # Not a modeled failure (those are handled inside the graph's own
        # nodes) — an actual bug.
        logger.exception("Unhandled error processing inbound WhatsApp message")
        await _send_reply(request, sender, "Something went wrong handling that — please try again.")


def _dispatch_background(request: Request, coro) -> None:
    background_tasks = request.app.state.background_tasks
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))


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

    msg_id = message["id"]
    with request.app.state.pool.connection() as conn:
        is_new = repo.mark_message_processed_if_new(conn, msg_id)
    if not is_new:
        # A redelivery of a message we already handled — ack and do
        # nothing further, rather than re-running the turn and sending a
        # second, independently-worded reply.
        return Response(status_code=200)

    if message.get("type") != "text":
        _dispatch_background(request, _send_reply(request, sender, "I can only read text messages right now."))
    else:
        text = message["text"]["body"]
        reply_to_message_id = message.get("context", {}).get("id")
        _dispatch_background(request, _process_text_message(request, text, msg_id, sender, reply_to_message_id))

    # Ack Meta immediately — the actual reply is sent from the background
    # task above via the WhatsApp send API directly, not via this response.
    return Response(status_code=200)
