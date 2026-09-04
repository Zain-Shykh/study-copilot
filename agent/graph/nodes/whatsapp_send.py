"""Composes and sends messages via the Meta Graph API."""

import httpx

GRAPH_API_VERSION = "v21.0"


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
    response.raise_for_status()
    return response.json()["messages"][0]["id"]
