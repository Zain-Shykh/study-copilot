"""LangGraph state schemas for the router, assignment, and email-draft graphs."""

from typing import Any, TypedDict


class RouterState(TypedDict, total=False):
    inbound_text: str
    whatsapp_message_id: str
    sender: str
    intent: str  # one of: list_courses | whats_due | summarize_emails | search_emails | unrecognized
    intent_args: dict[str, Any]
    reply_text: str
