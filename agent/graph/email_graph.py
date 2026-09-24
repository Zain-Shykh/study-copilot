"""Per-email-draft graph: draft -> interrupt -> revise/send on approval."""

import logging

import httpx
from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from agent import google_auth, llm
from agent.db import repo
from agent.graph.nodes import gmail
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.graph.state import EmailState

logger = logging.getLogger(__name__)


async def draft_node(state: EmailState, config: RunnableConfig) -> dict:
    original_body = state.get("original_body")
    if state["mode"] == "reply" and original_body is None:
        pool = config["configurable"]["pool"]
        with pool.connection() as conn:
            clients = google_auth.load_google_clients(conn)
        if isinstance(clients, str):
            return {"failure_text": clients}
        gmail_service, _, _ = clients
        try:
            original_body = gmail.get_message_body(gmail_service, state["gmail_message_id"])
        except HttpError as e:
            return {"failure_text": f"Couldn't load the original email: {e}"}

    configurable = config["configurable"]
    try:
        subject, body = llm.draft_email(
            configurable["genai_client"],
            configurable["gemini_model"],
            topic=state["topic"],
            original_subject=state.get("original_subject"),
            original_body=original_body,
        )
    except Exception as e:  # noqa: BLE001 - Gemini error after retries
        return {"failure_text": f"Couldn't draft that email: {e}", "original_body": original_body}

    if state["mode"] == "reply":
        subject = f"Re: {state['original_subject']}"

    return {"original_body": original_body, "subject": subject, "body": body}


async def relay_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]
    sender = state["sender"]

    if state.get("failure_text"):
        try:
            await send_whatsapp_message(access_token, phone_number_id, sender, state["failure_text"])
        except httpx.HTTPStatusError:
            logger.exception("Failed to send failure notice to %s", sender)
        return {}

    message_text = (
        f"Draft email to {state['recipient_display']}:\n\n"
        f"Subject: {state['subject']}\n\n{state['body']}\n\n"
        "Reply approve, suggest changes, or say reject."
    )
    try:
        message_id = await send_whatsapp_message(access_token, phone_number_id, sender, message_text)
    except httpx.HTTPStatusError:
        logger.exception("Failed to send draft email to %s", sender)
        return {}

    pool = configurable["pool"]
    thread_id = configurable["thread_id"]
    display_name = f"Email to {state['recipient_display']}: {state['subject']}"
    with pool.connection() as conn:
        repo.create_pending_item(conn, message_id, thread_id, "email", display_name)

    return {}


def route_after_relay(state: EmailState) -> str:
    return "await_review_node" if not state.get("failure_text") else END


def await_review_node(state: EmailState) -> dict:
    reply = interrupt({"kind": "email_review", "subject": state["subject"]})
    return {"review_reply_text": reply}


def parse_review_node(state: EmailState, config: RunnableConfig) -> dict:
    """No try/except here — same reasoning as assignment_graph's
    parse_review_node (see that file's docstring): a Gemini failure after
    its own retries leaves this thread parked here for a later reply to
    naturally retry. Only revise/reject close the pending item immediately
    (a new one is created on the next relay, or the thread simply ends);
    approve deliberately leaves it open — send_node closes it only on
    success, mirroring assignment_graph's parse_submit_node/
    submission_prep_node pair, so a failed send can be retried by replying
    "yes" again without losing the pending item."""
    configurable = config["configurable"]
    decision, feedback = llm.parse_review_reply(
        configurable["genai_client"], configurable["gemini_model"], state["review_reply_text"]
    )

    if decision in ("revise", "reject"):
        pool = configurable["pool"]
        message_id = configurable["_pending_item_message_id"]
        with pool.connection() as conn:
            repo.close_pending_item(conn, message_id)

    return {"review_decision": decision, "review_feedback": feedback}


def route_after_review(state: EmailState) -> str:
    return {"approve": "send_node", "revise": "revise_node", "reject": END}[state["review_decision"]]


async def revise_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    try:
        subject, body = llm.draft_email(
            configurable["genai_client"],
            configurable["gemini_model"],
            topic=state["topic"],
            original_subject=state.get("original_subject"),
            original_body=state.get("original_body"),
            prior_subject=state["subject"],
            prior_body=state["body"],
            feedback=state["review_feedback"],
        )
    except Exception as e:  # noqa: BLE001 - Gemini error after retries
        return {"failure_text": f"Couldn't revise that email: {e}"}

    if state["mode"] == "reply":
        subject = f"Re: {state['original_subject']}"

    return {"subject": subject, "body": body}


async def send_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pool = configurable["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"failure_text": f"Couldn't send that email: {clients}"}
    gmail_service, _, _ = clients

    retry_hint = 'Reply "yes" again to retry — nothing was lost.'
    try:
        gmail.send_message(
            gmail_service,
            state["recipient_email"],
            state["subject"],
            state["body"],
            in_reply_to_header=state.get("gmail_message_id_header") or None,
            thread_id=state.get("gmail_thread_id"),
        )
    except HttpError as e:
        return {"failure_text": f"Couldn't send that email: {e}. {retry_hint}"}

    message_id = configurable["_pending_item_message_id"]
    with pool.connection() as conn:
        repo.close_pending_item(conn, message_id)

    return {"sent": True}


async def relay_send_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]
    sender = state["sender"]

    message = state.get("failure_text") or f"Sent to {state['recipient_display']}."
    try:
        await send_whatsapp_message(access_token, phone_number_id, sender, message)
    except httpx.HTTPStatusError:
        logger.exception("Failed to send outcome notice to %s", sender)

    return {}


def build_email_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(EmailState)
    g.add_node("draft_node", draft_node)
    g.add_node("relay_node", relay_node)
    g.add_node("await_review_node", await_review_node)
    g.add_node("parse_review_node", parse_review_node)
    g.add_node("revise_node", revise_node)
    g.add_node("send_node", send_node)
    g.add_node("relay_send_node", relay_send_node)

    g.set_entry_point("draft_node")
    g.add_edge("draft_node", "relay_node")
    g.add_conditional_edges(
        "relay_node", route_after_relay, {"await_review_node": "await_review_node", END: END}
    )
    g.add_edge("await_review_node", "parse_review_node")
    g.add_conditional_edges(
        "parse_review_node",
        route_after_review,
        {"send_node": "send_node", "revise_node": "revise_node", END: END},
    )
    g.add_edge("revise_node", "relay_node")
    g.add_edge("send_node", "relay_send_node")
    g.add_edge("relay_send_node", END)

    return g.compile(checkpointer=checkpointer)
