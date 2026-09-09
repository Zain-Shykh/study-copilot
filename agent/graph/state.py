"""LangGraph state schemas for the router, assignment, and email-draft graphs."""

from typing import Any, TypedDict


class RouterState(TypedDict, total=False):
    inbound_text: str
    whatsapp_message_id: str
    sender: str
    reply_to_message_id: str | None  # Meta's context.id for this inbound message, if any
    intent: str  # one of: list_courses | whats_due | summarize_emails | search_emails | work_on_assignment | unrecognized
    intent_args: dict[str, Any]
    reply_text: str
    pending_question: dict | None  # see router_graph.py for the two shapes; persists via checkpointer
