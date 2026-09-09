"""Main/router thread graph — handles on-demand commands not tied to one in-flight item."""

import asyncio
import logging

import httpx
from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent import google_auth, llm
from agent.graph.nodes import classroom
from agent.graph.nodes.classroom import classroom_node
from agent.graph.nodes.gmail import gmail_node
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.graph.state import RouterState
from agent.llm import classify_intent

logger = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "I didn't understand that. I can: list your courses, tell you "
    "what's due, summarize your unread emails, search your inbox, or "
    "work on an assignment."
)

# Tunable starting thresholds for resolving "work on <assignment>" — adjust
# after real-world testing, not asserted as final. See spec §0.3/§6.6.
_CONFIDENT_MATCH_SCORE = 0.6
_CONFIDENT_MATCH_MARGIN = 0.15
_DISAMBIGUATION_MIN_SCORE = 0.35
_DISAMBIGUATION_MAX_CANDIDATES = 4

NO_MATCH_REPLY = (
    "I couldn't find an assignment matching that — try a more specific "
    "name, or ask \"what's due\" to see the list."
)


def route_entry_node(state: RouterState) -> dict:
    """If this inbound message is a reply to the currently pending
    question, leaves pending_question untouched so the next node can
    consume it. Otherwise silently abandons any pending question — no
    "still waiting" nudge — so the message is classified as an independent
    command instead."""
    pending_question = state.get("pending_question")
    if pending_question is not None and state.get("reply_to_message_id") == pending_question.get(
        "message_id"
    ):
        return {}
    return {"pending_question": None}


def route_after_entry(state: RouterState) -> str:
    pending_question = state.get("pending_question")
    if pending_question is None:
        return "classify_intent"
    return {
        "disambiguate_assignment": "handle_disambiguation",
        "confirm_start_assignment": "handle_confirmation",
    }[pending_question["kind"]]


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
        "work_on_assignment": "resolve_assignment",
        "unrecognized": "fallback_node",
    }[state["intent"]]


def fallback_node(state: RouterState) -> dict:
    return {"reply_text": FALLBACK_REPLY}


def _confirm_question_text(resolved: dict) -> str:
    due_text = resolved.get("due") or "no due date"
    return f'Should I start "{resolved["title"]}" (due {due_text}, {resolved["course_name"]})?'


def _confirm_reply(candidate: dict) -> dict:
    resolved = {
        "course_id": candidate["course_id"],
        "course_name": candidate["course_name"],
        "coursework_id": candidate["coursework_id"],
        "title": candidate["title"],
        "due": candidate["due"],
    }
    return {
        "reply_text": _confirm_question_text(resolved),
        "pending_question": {
            "kind": "confirm_start_assignment",
            "message_id": None,
            "resolved": resolved,
        },
    }


def resolve_assignment_node(state: RouterState, config: RunnableConfig) -> dict:
    """Fuzzy-matches the free-text assignment reference against the user's
    live Classroom courses/assignments, then either asks for disambiguation
    or goes straight to the stage-2 confirmation question. See spec §0.3."""
    pool = config["configurable"]["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"reply_text": clients}
    _, classroom_service, _ = clients

    reference_text = state.get("intent_args", {}).get("assignment_reference", "")

    try:
        courses = classroom.list_courses(classroom_service)
        assignments = classroom.list_assignments(
            classroom_service, courses, scope="all", window_hours=0
        )
    except HttpError as e:
        return {"reply_text": f"Couldn't reach Classroom right now: {e}"}

    assignments_by_course: dict[str, list[dict]] = {}
    for item in assignments:
        assignments_by_course.setdefault(item["course"]["id"], []).append(item["courseWork"])

    candidates = classroom.find_assignment_candidates(courses, assignments_by_course, reference_text)
    if not candidates:
        return {"reply_text": NO_MATCH_REPLY, "pending_question": None}

    best = candidates[0]
    second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    if best["score"] >= _CONFIDENT_MATCH_SCORE and (best["score"] - second_score) >= _CONFIDENT_MATCH_MARGIN:
        return _confirm_reply(best)

    shortlisted = [c for c in candidates if c["score"] >= _DISAMBIGUATION_MIN_SCORE][
        :_DISAMBIGUATION_MAX_CANDIDATES
    ]
    if not shortlisted:
        return {"reply_text": NO_MATCH_REPLY, "pending_question": None}
    if len(shortlisted) == 1:
        return _confirm_reply(shortlisted[0])

    lines = [
        f'{i + 1}. [{c["course_name"]}] {c["title"]} — due {c["due"] or "no due date"}'
        for i, c in enumerate(shortlisted)
    ]
    return {
        "reply_text": "Which one did you mean?\n" + "\n".join(lines),
        "pending_question": {
            "kind": "disambiguate_assignment",
            "message_id": None,
            "candidates": shortlisted,
        },
    }


def _log_if_failed(task: "asyncio.Task") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background assignment task raised an uncaught exception", exc_info=exc)


async def run_assignment_flow(
    assignment_graph,
    resolved: dict,
    sender: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    pool,
) -> None:
    """Runs the ingest -> draft -> save-session -> relay pipeline for one
    assignment as a single sequential graph invocation. `relay_node` (the
    pipeline's last node) is what actually sends the result to WhatsApp —
    there is no separate "detect completion" step. See spec §11."""
    course_id = resolved["course_id"]
    coursework_id = resolved["coursework_id"]

    try:
        await assignment_graph.ainvoke(
            {
                "course_id": course_id,
                "course_name": resolved["course_name"],
                "coursework_id": coursework_id,
                "title": resolved["title"],
                "sender": sender,
            },
            config={
                "configurable": {
                    "thread_id": f"assignment:{course_id}:{coursework_id}",
                    "pool": pool,
                    "whatsapp_access_token": whatsapp_access_token,
                    "whatsapp_phone_number_id": whatsapp_phone_number_id,
                }
            },
        )
        # relay_node already sent the result (success or a modeled failure)
        # to WhatsApp — nothing further to do here.
    except Exception:
        # An actual bug, not a modeled failure — relay_node never got to
        # run, so the user has had no message since "Starting on X...".
        # Log it AND send a generic failure message directly, so silence is
        # never the outcome of a real crash.
        logger.exception("Unhandled error running assignment flow for %s", resolved["title"])
        try:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                sender,
                f"Something went wrong while working on {resolved['title']} — please try again.",
            )
        except Exception:
            logger.exception("Failed to send failure notice for %s", resolved["title"])


def handle_confirmation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "confirm_start_assignment"."""
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    resolved = pending_question["resolved"]
    question = _confirm_question_text(resolved)

    try:
        answer = llm.parse_confirmation_reply(
            configurable["genai_client"], configurable["gemini_model"], question, state["inbound_text"]
        )
    except Exception:
        logger.exception("parse_confirmation_reply failed after retries")
        return {
            "reply_text": "Couldn't process that reply right now — please try again.",
            "pending_question": None,
        }

    if answer == "confirm":
        task = asyncio.create_task(
            run_assignment_flow(
                configurable["assignment_graph"],
                resolved,
                state["sender"],
                configurable["whatsapp_access_token"],
                configurable["whatsapp_phone_number_id"],
                configurable["pool"],
            )
        )
        background_tasks = configurable["background_tasks"]
        background_tasks.add(task)
        task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))
        reply_text = f"Starting on {resolved['title']} — I'll send the draft when it's ready."
    else:
        reply_text = "Okay, not starting that."

    return {"reply_text": reply_text, "pending_question": None}


def handle_disambiguation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "disambiguate_assignment"."""
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    candidates = pending_question["candidates"]

    try:
        choice = llm.resolve_disambiguation(
            configurable["genai_client"], configurable["gemini_model"], candidates, state["inbound_text"]
        )
    except Exception:
        logger.exception("resolve_disambiguation failed after retries")
        return {
            "reply_text": "Couldn't process that reply right now — please try again.",
            "pending_question": None,
        }

    if choice < 1 or choice > len(candidates):
        return {
            "reply_text": "Sorry, I couldn't tell which one you meant — try naming it differently.",
            "pending_question": None,
        }

    return _confirm_reply(candidates[choice - 1])


async def send_reply_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]

    try:
        message_id = await send_whatsapp_message(
            access_token, phone_number_id, state["sender"], state["reply_text"]
        )
    except httpx.HTTPStatusError:
        logger.exception("Failed to send WhatsApp reply to %s", state["sender"])
        return {}

    pending_question = state.get("pending_question")
    if pending_question is not None and pending_question.get("message_id") is None:
        pending_question = {**pending_question, "message_id": message_id}
        return {"pending_question": pending_question}

    return {}


def build_router_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(RouterState)
    g.add_node("route_entry", route_entry_node)
    g.add_node("classify_intent", classify_intent_node)
    g.add_node("resolve_assignment", resolve_assignment_node)
    g.add_node("handle_confirmation", handle_confirmation_node)
    g.add_node("handle_disambiguation", handle_disambiguation_node)
    g.add_node("classroom_node", classroom_node)
    g.add_node("gmail_node", gmail_node)
    g.add_node("fallback_node", fallback_node)
    g.add_node("send_reply", send_reply_node)

    g.set_entry_point("route_entry")
    g.add_conditional_edges(
        "route_entry",
        route_after_entry,
        {
            "classify_intent": "classify_intent",
            "handle_confirmation": "handle_confirmation",
            "handle_disambiguation": "handle_disambiguation",
        },
    )
    g.add_conditional_edges(
        "classify_intent",
        route_after_classify,
        {
            "classroom_node": "classroom_node",
            "gmail_node": "gmail_node",
            "fallback_node": "fallback_node",
            "resolve_assignment": "resolve_assignment",
            "send_reply": "send_reply",
        },
    )
    for node_name in (
        "classroom_node",
        "gmail_node",
        "fallback_node",
        "resolve_assignment",
        "handle_confirmation",
        "handle_disambiguation",
    ):
        g.add_edge(node_name, "send_reply")
    g.add_edge("send_reply", END)

    return g.compile(checkpointer=checkpointer)
