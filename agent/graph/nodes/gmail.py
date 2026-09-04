"""Gmail API calls: read/search/summarize, draft, send."""

from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig

from agent import google_auth
from agent.graph.state import RouterState
from agent.llm import summarize_emails

_FILTER_ARGS = ("email_sender", "email_subject", "email_label")


def list_messages(gmail_service, query: str, max_results: int) -> list[dict]:
    response = (
        gmail_service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute(num_retries=3)
    )
    message_refs = response.get("messages", [])

    emails = []
    for ref in message_refs:
        msg = (
            gmail_service.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute(num_retries=3)
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        emails.append(
            {
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            }
        )
    return emails


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


def _describe_filter(intent_args: dict) -> str:
    if intent_args.get("email_sender"):
        return f"Emails from {intent_args['email_sender']}"
    if intent_args.get("email_subject"):
        return f'Emails about "{intent_args["email_subject"]}"'
    if intent_args.get("email_label"):
        return f"Emails labeled {intent_args['email_label']}"
    return f"Your {intent_args.get('email_count', 10)} most recent unread emails"


def _format_email_list(emails: list[dict]) -> str:
    lines = []
    for email in emails:
        lines.append(
            f"- {email['from']} — {email['subject']} ({email['date']})\n  {email['snippet']}"
        )
    return "\n".join(lines)


def gmail_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pool = configurable["pool"]
    genai_client = configurable["genai_client"]
    gemini_model = configurable["gemini_model"]

    intent = state["intent"]
    intent_args = state.get("intent_args", {})
    max_results = intent_args.get("email_count", 10)
    is_default_filter = not any(intent_args.get(k) for k in _FILTER_ARGS)

    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"reply_text": clients}
    gmail_service, _ = clients

    try:
        query = build_query(intent_args, unread_only=(intent == "summarize_emails"))
        emails = list_messages(gmail_service, query, max_results)
    except HttpError as e:
        return {"reply_text": f"Couldn't reach Gmail right now: {e}"}

    if intent == "summarize_emails":
        if not emails:
            return {"reply_text": "No unread emails found."}
        try:
            summary = summarize_emails(genai_client, gemini_model, emails)
        except Exception:
            return {"reply_text": "Couldn't summarize those emails right now — please try again."}
        prefix = (
            f"Here are your {max_results} most recent unread emails:"
            if is_default_filter
            else f"{_describe_filter(intent_args)}:"
        )
        return {"reply_text": f"{prefix}\n\n{summary}"}

    else:  # search_emails
        if not emails:
            return {"reply_text": "No emails found matching that."}
        return {"reply_text": _format_email_list(emails)}
