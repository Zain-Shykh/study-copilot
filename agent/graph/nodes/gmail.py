"""Gmail API calls: read/search/summarize, draft, send."""

import base64
from datetime import datetime
from email.mime.text import MIMEText

from googleapiclient.errors import HttpError

from agent.config import USER_TIMEZONE


def _get_message_metadata(gmail_service, message_id: str) -> dict:
    msg = (
        gmail_service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date", "Message-ID"],
        )
        .execute(num_retries=3)
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "message_id_header": headers.get("Message-ID", ""),
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


def _date_to_epoch_seconds(date_str: str) -> int:
    """Converts a "YYYY-MM-DD" calendar date to the Unix timestamp of that
    date's midnight in USER_TIMEZONE. Gmail's after:/before: search
    operators accept either a YYYY/MM/DD date (whose timezone handling is
    undocumented) or a Unix timestamp (exact, timezone-unambiguous) — we
    use the latter so a "day" always means a day in the user's own
    timezone, not wherever Gmail's servers assume."""
    local_midnight = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=USER_TIMEZONE)
    return int(local_midnight.timestamp())


def build_query(intent_args: dict, unread_only: bool) -> str:
    parts = []
    if intent_args.get("email_sender"):
        parts.append(f"from:{intent_args['email_sender']}")
    if intent_args.get("email_subject"):
        parts.append(f"subject:{intent_args['email_subject']}")
    if intent_args.get("email_label"):
        parts.append(f"label:{intent_args['email_label']}")
    if intent_args.get("after_date"):
        parts.append(f"after:{_date_to_epoch_seconds(intent_args['after_date'])}")
    if intent_args.get("before_date"):
        parts.append(f"before:{_date_to_epoch_seconds(intent_args['before_date'])}")
    if not parts and unread_only:
        parts.append("is:unread")
    return " ".join(parts)


def get_message_body(gmail_service, message_id: str) -> str:
    """Returns the plain-text body of one message, or "" if it has no
    text/plain part (e.g. an HTML-only email) — callers treat that as
    "body unavailable" rather than attempting a lossy HTML-to-text
    conversion, consistent with "never fabricate"."""
    msg = (
        gmail_service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute(num_retries=3)
    )

    def _find_text_plain(payload: dict) -> str | None:
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            found = _find_text_plain(part)
            if found is not None:
                return found
        return None

    return _find_text_plain(msg["payload"]) or ""


def send_message(
    gmail_service,
    to: str,
    subject: str,
    body: str,
    *,
    in_reply_to_header: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Sends a new message, or a reply when in_reply_to_header/thread_id
    are given (sets In-Reply-To/References for correct threading in email
    clients, and Gmail's own threadId for correct threading in the Gmail
    UI). Returns the sent message's Gmail id. Raises HttpError on failure —
    the caller decides how to report/retry."""
    msg = MIMEText(body)
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to_header:
        msg["In-Reply-To"] = in_reply_to_header
        msg["References"] = in_reply_to_header

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body_payload: dict = {"raw": raw}
    if thread_id:
        body_payload["threadId"] = thread_id

    sent = gmail_service.users().messages().send(userId="me", body=body_payload).execute(num_retries=3)
    return sent["id"]
