"""Phase 0 verification: sends one WhatsApp text message via the Meta Graph
API to prove the app/test-number/recipient setup works. Disposable — not
imported by agent/.
"""

import os
import sys

import dotenv
import httpx

GRAPH_API_VERSION = "v21.0"


def send_test_message(access_token: str, phone_number_id: str, to: str) -> str:
    """POSTs a text message via the Graph API; returns the sent message's id."""
    response = httpx.post(
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": "Phase 0 verification: WhatsApp send is working."},
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["messages"][0]["id"]


def main() -> None:
    dotenv.load_dotenv()

    access_token = os.environ.get("META_WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = os.environ.get("META_WHATSAPP_PHONE_NUMBER_ID", "")
    to = os.environ.get("MY_WHATSAPP_NUMBER", "")

    for name, value in [
        ("META_WHATSAPP_ACCESS_TOKEN", access_token),
        ("META_WHATSAPP_PHONE_NUMBER_ID", phone_number_id),
        ("MY_WHATSAPP_NUMBER", to),
    ]:
        if not value:
            print(f"Missing required env var: {name}", file=sys.stderr)
            sys.exit(1)

    try:
        message_id = send_test_message(access_token, phone_number_id, to)
    except httpx.HTTPStatusError as e:
        print(
            f"WhatsApp send failed: {e.response.status_code} {e.response.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Sent, message id: {message_id}")


if __name__ == "__main__":
    main()
