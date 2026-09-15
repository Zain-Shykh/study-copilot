"""Gmail API calls: read/search/summarize, draft, send."""

from googleapiclient.errors import HttpError


def _get_message_metadata(gmail_service, message_id: str) -> dict:
    msg = (
        gmail_service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute(num_retries=3)
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg["id"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
    }


def list_messages(gmail_service, query: str, max_results: int) -> list[dict]:
    response = (
        gmail_service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute(num_retries=3)
    )
    return [_get_message_metadata(gmail_service, ref["id"]) for ref in response.get("messages", [])]


def get_current_history_id(gmail_service) -> str:
    """Current historyId — used both to establish/reset the checkpoint
    baseline and to advance it after a successful poll."""
    return gmail_service.users().getProfile(userId="me").execute(num_retries=3)["historyId"]


def get_new_message_ids(gmail_service, start_history_id: str) -> list[str] | None:
    """Message ids added since start_history_id (deduped — the same
    message can appear in multiple history records), or None if Gmail
    reports 404 (checkpoint too old/expired; caller re-baselines instead
    of treating this as a failure)."""
    message_ids: set[str] = set()
    page_token = None
    try:
        while True:
            response = (
                gmail_service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute(num_retries=3)
            )
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_ids.add(added["message"]["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        if e.resp.status == 404:
            return None
        raise
    return list(message_ids)


def get_messages_by_id(gmail_service, message_ids: list[str]) -> list[dict]:
    return [_get_message_metadata(gmail_service, mid) for mid in message_ids]


def build_query(intent_args: dict, unread_only: bool) -> str:
    parts = []
    if intent_args.get("email_sender"):
        parts.append(f"from:{intent_args['email_sender']}")
    if intent_args.get("email_subject"):
        parts.append(f"subject:{intent_args['email_subject']}")
    if intent_args.get("email_label"):
        parts.append(f"label:{intent_args['email_label']}")
    if not parts and unread_only:
        parts.append("is:unread")
    return " ".join(parts)
