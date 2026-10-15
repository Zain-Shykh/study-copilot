"""Composes and sends messages via the Meta Graph API."""

import logging
import mimetypes
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"


def _log_if_error(response: httpx.Response, step: str) -> None:
    """Meta's error JSON explains *why* a send failed; raise_for_status()
    alone discards it. Contains no secrets."""
    if response.is_error:
        logger.error("whatsapp %s failed status=%s body=%s", step, response.status_code, response.text[:500])


async def send_whatsapp_message(
    access_token: str, phone_number_id: str, to: str, body: str
) -> str:
    """Sends a WhatsApp text message. Returns the sent message's id.

    Raises httpx.HTTPStatusError on a non-2xx response — the caller decides
    how to handle a failed send (there's no way to notify the user over
    WhatsApp if WhatsApp itself is the thing failing).
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
            timeout=10,
        )
    _log_if_error(response, "send")
    response.raise_for_status()
    message_id = response.json()["messages"][0]["id"]
    logger.info("whatsapp text sent to=***%s chars=%d id=%s", to[-4:], len(body), message_id)
    return message_id


async def send_whatsapp_document(
    access_token: str,
    phone_number_id: str,
    to: str,
    file_path: Path,
    caption: str | None = None,
    filename: str | None = None,
) -> str:
    """Uploads file_path as media, then sends it as a WhatsApp document
    message. Returns the sent message's id.

    filename overrides the name shown to the user (defaults to
    file_path.name) — used when the same on-disk basename could collide
    across sibling subfolders (see relay_node's submission/ flattening).

    Raises httpx.HTTPStatusError on either step's failure.
    """
    display_name = filename or file_path.name
    mime_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"

    async with httpx.AsyncClient() as client:
        upload_response = await client.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/media",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (display_name, file_path.read_bytes(), mime_type)},
            timeout=30,
        )
        _log_if_error(upload_response, "media upload")
        upload_response.raise_for_status()
        media_id = upload_response.json()["id"]

        document: dict = {"id": media_id, "filename": display_name}
        if caption:
            document["caption"] = caption

        send_response = await client.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": document,
            },
            timeout=10,
        )
    _log_if_error(send_response, "document send")
    send_response.raise_for_status()
    message_id = send_response.json()["messages"][0]["id"]
    logger.info("whatsapp document sent to=***%s filename=%s id=%s", to[-4:], display_name, message_id)
    return message_id
