"""Main/router thread graph — handles on-demand commands not tied to one in-flight item."""

import logging

import httpx
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.graph.nodes.classroom import classroom_node
from agent.graph.nodes.gmail import gmail_node
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.graph.state import RouterState
from agent.llm import classify_intent

logger = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "I didn't understand that. I can: list your courses, tell you "
    "what's due, summarize your unread emails, or search your inbox."
)


def classify_intent_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    genai_client = configurable["genai_client"]
    gemini_model = configurable["gemini_model"]

    try:
        intent, args = classify_intent(genai_client, gemini_model, state["inbound_text"])
    except Exception:
        logger.exception("classify_intent failed after retries")
        return {"reply_text": "Couldn't process that message right now — please try again."}

    return {"intent": intent, "intent_args": args}


def route_after_classify(state: RouterState) -> str:
    if state.get("reply_text"):
        # classify_intent already failed and set a reply — skip straight to sending it.
        return "send_reply"
    return {
        "list_courses": "classroom_node",
        "whats_due": "classroom_node",
        "summarize_emails": "gmail_node",
        "search_emails": "gmail_node",
        "unrecognized": "fallback_node",
    }[state["intent"]]


def fallback_node(state: RouterState) -> dict:
    return {"reply_text": FALLBACK_REPLY}


async def send_reply_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]

    try:
        await send_whatsapp_message(
            access_token, phone_number_id, state["sender"], state["reply_text"]
        )
    except httpx.HTTPStatusError:
        logger.exception("Failed to send WhatsApp reply to %s", state["sender"])

    return {}


def build_router_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(RouterState)
    g.add_node("classify_intent", classify_intent_node)
    g.add_node("classroom_node", classroom_node)
    g.add_node("gmail_node", gmail_node)
    g.add_node("fallback_node", fallback_node)
    g.add_node("send_reply", send_reply_node)

    g.set_entry_point("classify_intent")
    g.add_conditional_edges(
        "classify_intent",
        route_after_classify,
        {
            "classroom_node": "classroom_node",
            "gmail_node": "gmail_node",
            "fallback_node": "fallback_node",
            "send_reply": "send_reply",
        },
    )
    g.add_edge("classroom_node", "send_reply")
    g.add_edge("gmail_node", "send_reply")
    g.add_edge("fallback_node", "send_reply")
    g.add_edge("send_reply", END)

    return g.compile(checkpointer=checkpointer)
