"""LangGraph state schemas for the router, assignment, and email-draft graphs."""

from typing import Any, TypedDict


class RouterState(TypedDict, total=False):
    inbound_text: str
    whatsapp_message_id: str
    sender: str
    reply_to_message_id: str | None  # Meta's context.id for this inbound message, if any
    intent: str  # one of: answer_question | work_on_assignment | draft_email | respond_to_pending | unrecognized
    intent_args: dict[str, Any]
    reply_text: str | None  # reset to None at the start of every turn by route_entry_node
    pending_question: dict | None  # see router_graph.py; persists via checkpointer
    # pending_question["kind"] is one of:
    #   disambiguate_assignment   — candidates: list[dict] (course_name/title/due/coursework_id/course_id)
    #   confirm_start_assignment  — resolved: dict (course_name/title/due/coursework_id/course_id)
    #   disambiguate_email        — candidates: list[dict] (gmail message dicts), topic: str
    #   confirm_draft_email       — resolved: dict (mode/recipient_email/recipient_display/original_subject/
    #     gmail_message_id/gmail_thread_id/gmail_message_id_header/topic)
    #   disambiguate_pending_item — candidates: list[dict] (pending_items rows), original_text: str
    #     (the user's original respond_to_pending reply, preserved so resuming after
    #     disambiguation uses their actual approve/revise/reject/confirm content —
    #     not the disambiguation answer itself)
    matched_pending_item: dict | None  # transient, set by route_entry_node for this turn only —
    # the pending_items row (see repo.get_pending_item) that state["reply_to_message_id"]
    # targets, if any; consumed by handle_pending_item_reply


class EmailState(TypedDict, total=False):
    mode: str  # "reply" | "new"
    sender: str  # WhatsApp number to relay results to
    recipient_email: str
    recipient_display: str  # for confirm/relay text — the From header for a reply, the raw address for new
    original_subject: str | None       # only for "reply"
    original_body: str | None          # only for "reply" — fetched once by draft_node, carried across revisions
    gmail_message_id: str | None       # the message being replied to
    gmail_thread_id: str | None
    gmail_message_id_header: str | None  # RFC 5322 Message-ID, for In-Reply-To/References
    topic: str                          # the user's original instruction — preserved across revisions
    subject: str | None                 # current draft subject
    body: str | None                    # current draft body
    review_reply_text: str | None       # raw text from the draft-review interrupt
    review_decision: str | None         # "approve" | "revise" | "reject"
    review_feedback: str | None         # only set when review_decision == "revise"
    failure_text: str | None
    sent: bool
