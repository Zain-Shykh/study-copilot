"""LangGraph state schemas for the router, assignment, and email-draft graphs."""

from typing import Any, TypedDict


class RouterState(TypedDict, total=False):
    inbound_text: str
    whatsapp_message_id: str
    sender: str
    reply_to_message_id: str | None  # Meta's context.id for this inbound message, if any
    intent: str  # one of: answer_question | work_on_assignment | respond_to_pending | unrecognized
    intent_args: dict[str, Any]
    reply_text: str | None  # reset to None at the start of every turn by route_entry_node
    pending_question: dict | None  # see router_graph.py; persists via checkpointer
    # pending_question["kind"] is one of:
    #   disambiguate_assignment   — candidates: list[dict] (course_name/title/due/coursework_id/course_id)
    #   confirm_start_assignment  — resolved: dict (course_name/title/due/coursework_id/course_id)
    #   disambiguate_pending_item — candidates: list[dict] (pending_items rows), original_text: str
    #     (the user's original respond_to_pending reply, preserved so resuming after
    #     disambiguation uses their actual approve/revise/reject/confirm content —
    #     not the disambiguation answer itself)
    matched_pending_item: dict | None  # transient, set by route_entry_node for this turn only —
    # the pending_items row (see repo.get_pending_item) that state["reply_to_message_id"]
    # targets, if any; consumed by handle_pending_item_reply
