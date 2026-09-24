"""Main/router thread graph — handles on-demand commands not tied to one in-flight item."""

import asyncio
import difflib
import email.utils
import logging
import uuid

import httpx
from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from agent import google_auth, llm
from agent.db import repo
from agent.graph.nodes import classroom, gmail
from agent.graph.nodes.answer_question import answer_question_node
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.graph.state import RouterState
from agent.llm import classify_intent

logger = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "I didn't understand that. I can: list your courses, tell you "
    "what's due, summarize your unread emails, search your inbox, or "
    "work on an assignment."
)

# Tunable starting thresholds for resolving "work on <assignment>" (and,
# per Phase 3, "which pending item did you mean") — adjust after
# real-world testing, not asserted as final. See spec §0.3/§6.6.
_CONFIDENT_MATCH_SCORE = 0.6
_CONFIDENT_MATCH_MARGIN = 0.15
_DISAMBIGUATION_MIN_SCORE = 0.35
_DISAMBIGUATION_MAX_CANDIDATES = 4

NO_MATCH_REPLY = (
    "I couldn't find an assignment matching that — try a more specific "
    "name, or ask \"what's due\" to see the list."
)


def _find_targeted_pending_item(state: RouterState, config: RunnableConfig) -> dict | None:
    """Reply-to-message lookup only — unambiguous when it hits. A fresh
    message or a reply to something not currently tracked falls through to
    the by-name respond_to_pending intent instead (§6.3), since that needs
    classify_intent's output first."""
    reply_to = state.get("reply_to_message_id")
    if not reply_to:
        return None
    pool = config["configurable"]["pool"]
    with pool.connection() as conn:
        return repo.get_pending_item(conn, reply_to)


def route_entry_node(state: RouterState, config: RunnableConfig) -> dict:
    """Resets this turn's transient fields, then checks (in priority
    order): a reply to the router's own pending confirm/disambiguate
    question (Phase 2 mechanism, scoped to this thread — leaves
    pending_question in place for the next node to consume), then whether
    this message targets a currently-pending assignment item via
    WhatsApp's reply-to-message feature. Otherwise this turn starts as an
    independent command."""
    pending_question = state.get("pending_question")
    if pending_question is not None and state.get("reply_to_message_id") == pending_question.get(
        "message_id"
    ):
        return {"reply_text": None}

    item = _find_targeted_pending_item(state, config)
    return {"pending_question": None, "reply_text": None, "matched_pending_item": item}


def route_after_entry(state: RouterState) -> str:
    pending_question = state.get("pending_question")
    if pending_question is not None:
        return {
            "disambiguate_assignment": "handle_disambiguation",
            "confirm_start_assignment": "handle_confirmation",
            "confirm_draft_email": "handle_email_confirmation",
            "awaiting_email_details": "handle_email_details",
            "disambiguate_pending_item": "handle_pending_item_disambiguation",
        }[pending_question["kind"]]
    if state.get("matched_pending_item") is not None:
        return "handle_pending_item_reply"
    return "classify_intent"


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
        "answer_question": "answer_question_node",
        "work_on_assignment": "resolve_assignment",
        "draft_email": "resolve_email",
        "respond_to_pending": "resolve_pending_item",
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
        assignments, _failed_courses = classroom.list_assignments(
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


def _extract_email_address(from_header: str) -> str:
    return email.utils.parseaddr(from_header)[1]


def _resolved_from_match(match: dict, topic: str) -> dict:
    return {
        "mode": "reply",
        "recipient_email": _extract_email_address(match["from"]),
        "recipient_display": match["from"],
        "original_subject": match["subject"],
        "gmail_message_id": match["id"],
        "gmail_thread_id": match["thread_id"],
        "gmail_message_id_header": match["message_id_header"],
        "topic": topic,
    }


def _confirm_email_question_text(resolved: dict) -> str:
    if resolved["mode"] == "reply":
        suffix = f" — {resolved['topic']}" if resolved["topic"] else ""
        return (
            f'Should I draft a reply to "{resolved["original_subject"]}" from '
            f'{resolved["recipient_display"]}{suffix}?'
        )
    return f'Should I draft a new email to {resolved["recipient_email"]} — {resolved["topic"]}?'


def _confirm_email_reply(resolved: dict) -> dict:
    return {
        "reply_text": _confirm_email_question_text(resolved),
        "pending_question": {"kind": "confirm_draft_email", "message_id": None, "resolved": resolved},
    }


def _load_gmail_service(config: RunnableConfig) -> tuple[object | None, str | None]:
    """Returns (gmail_service, None) on success, or (None, error_reply_text)
    if credentials are missing/expired. Returned as a pair rather than
    relying on isinstance(result, str) to detect an error, since the
    gmail_service itself could coincidentally be a string (e.g. a test
    stub) — this codebase's usual google_auth.load_google_clients callers
    check isinstance on the raw tuple-or-string return before unpacking,
    never on an already-unpacked single value. No actual async I/O
    despite being usable from an async node — plain sync helper, callable
    from either."""
    pool = config["configurable"]["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return None, clients
    gmail_service, _, _ = clients
    return gmail_service, None


def _is_valid_email(text: str) -> bool:
    return "@" in text and "." in text.split("@")[-1]


def _ask_for_email_details(mode: str, address: str | None, topic: str, asking_for: str) -> dict:
    if asking_for == "address":
        question = (
            "Who would you like to reply to — give me their email address?"
            if mode == "reply"
            else "Who would you like to email? Give me their email address."
        )
    else:
        question = f"What should the email to {address} say?"
    return {
        "reply_text": question,
        "pending_question": {
            "kind": "awaiting_email_details",
            "message_id": None,
            "mode": mode,
            "address": address,
            "topic": topic,
            "asking_for": asking_for,
        },
    }


def _resolve_email_target(mode: str, address_text: str, topic: str, gmail_service) -> dict:
    """Core resolution once mode/address/topic are known (or partially
    known) — asks for whatever's still missing as a tracked
    pending_question (so the next reply continues it rather than being
    misclassified from scratch, see route_after_entry), otherwise
    resolves straight to a confirmation.

    A reply requires a real address just like a new email — no more
    fuzzy free-text search across the whole mailbox by name/subject,
    since that could silently match an unrelated email (Gmail's generic
    relevance ranking, not a "did this actually mean what the user
    meant" check). Once we have an address, from: is exact and
    deterministic, and Gmail returns results newest-first by default, so
    "the email from X" unambiguously means the most recent one from X.
    See spec §6.1."""
    address = address_text if address_text and _is_valid_email(address_text) else None

    if not address:
        return _ask_for_email_details(mode, None, topic, "address")
    if not topic:
        return _ask_for_email_details(mode, address, topic, "topic")

    if mode == "new":
        resolved = {
            "mode": "new",
            "recipient_email": address,
            "recipient_display": address,
            "original_subject": None,
            "gmail_message_id": None,
            "gmail_thread_id": None,
            "gmail_message_id_header": None,
            "topic": topic,
        }
        return _confirm_email_reply(resolved)

    try:
        matches = gmail.list_messages(gmail_service, f"from:{address}", 1)
    except HttpError as e:
        return {"reply_text": f"Couldn't search Gmail right now: {e}", "pending_question": None}

    if not matches:
        return {
            "reply_text": f"I couldn't find any emails from {address} to reply to.",
            "pending_question": None,
        }

    return _confirm_email_reply(_resolved_from_match(matches[0], topic))


async def resolve_email_node(state: RouterState, config: RunnableConfig) -> dict:
    """Resolves the draft_email intent into a mode/address/topic, asking
    for whatever's missing, then confirming before drafting anything —
    see spec §6.1."""
    gmail_service, error = _load_gmail_service(config)
    if error:
        return {"reply_text": error}

    intent_args = state.get("intent_args", {})
    mode = intent_args.get("email_mode") or "new"
    address_text = (intent_args.get("email_reference") or "").strip()
    topic = intent_args.get("email_topic", "")

    return _resolve_email_target(mode, address_text, topic, gmail_service)


def handle_email_details_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "awaiting_email_details" — the user is
    answering a follow-up asking for the still-missing address or
    topic."""
    pending_question = state["pending_question"]
    mode = pending_question["mode"]
    address = pending_question["address"]
    topic = pending_question["topic"]
    answer_text = state["inbound_text"].strip()

    if pending_question["asking_for"] == "address":
        address = answer_text
    else:
        topic = answer_text

    gmail_service, error = _load_gmail_service(config)
    if error:
        return {"reply_text": error, "pending_question": None}

    return _resolve_email_target(mode, address, topic, gmail_service)


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
    student_info: str = "",
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
                    "student_info": student_info,
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


async def resume_assignment_thread(
    assignment_graph,
    thread_id: str,
    pending_item_message_id: str,
    reply_text: str,
    sender: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    pool,
    genai_client,
    gemini_model: str,
) -> None:
    """Resumes an assignment thread paused at interrupt() with the user's
    reply text. Per Decision #3, this never sends an ack itself — the
    graph's own parse_review_node/parse_submit_node are the sole source of
    any outbound message for a modeled outcome (approve/revise/reject/
    confirm/decline), including sending nothing at all for reject/decline.
    Only an unmodeled crash (the graph raising before reaching one of
    those nodes) gets a generic failure message here, mirroring
    run_assignment_flow's own top-level try/except."""
    try:
        await assignment_graph.ainvoke(
            Command(resume=reply_text),
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "pool": pool,
                    "whatsapp_access_token": whatsapp_access_token,
                    "whatsapp_phone_number_id": whatsapp_phone_number_id,
                    "genai_client": genai_client,
                    "gemini_model": gemini_model,
                    "_pending_item_message_id": pending_item_message_id,
                }
            },
        )
    except Exception:
        logger.exception("Unhandled error resuming assignment thread %s", thread_id)
        try:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                sender,
                "Something went wrong processing that reply — please try again.",
            )
        except Exception:
            logger.exception("Failed to send failure notice for thread %s", thread_id)


async def run_email_flow(
    email_graph,
    resolved: dict,
    sender: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    pool,
    genai_client,
    gemini_model: str,
) -> None:
    """Runs draft -> relay as a single sequential graph invocation, same
    shape as run_assignment_flow. Unlike assignments, email drafting needs
    Gemini from the very first invocation (there's no separate "ingest"
    step), so genai_client/gemini_model are passed here too, not only on
    resume. See spec §6.3."""
    thread_id = f"email:{uuid.uuid4()}"
    try:
        await email_graph.ainvoke(
            {
                "mode": resolved["mode"],
                "recipient_email": resolved["recipient_email"],
                "recipient_display": resolved["recipient_display"],
                "original_subject": resolved["original_subject"],
                "gmail_message_id": resolved["gmail_message_id"],
                "gmail_thread_id": resolved["gmail_thread_id"],
                "gmail_message_id_header": resolved["gmail_message_id_header"],
                "topic": resolved["topic"],
                "sender": sender,
            },
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "pool": pool,
                    "whatsapp_access_token": whatsapp_access_token,
                    "whatsapp_phone_number_id": whatsapp_phone_number_id,
                    "genai_client": genai_client,
                    "gemini_model": gemini_model,
                }
            },
        )
    except Exception:
        logger.exception("Unhandled error running email flow for %s", resolved["recipient_display"])
        try:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                sender,
                "Something went wrong while drafting that email — please try again.",
            )
        except Exception:
            logger.exception("Failed to send failure notice for email to %s", resolved["recipient_display"])


async def resume_email_thread(
    email_graph,
    thread_id: str,
    pending_item_message_id: str,
    reply_text: str,
    sender: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    pool,
    genai_client,
    gemini_model: str,
) -> None:
    """Same shape and same Decision #3 rationale as resume_assignment_thread
    — no ack sent here; the graph's own parse_review_node is the sole
    source of any outbound message for a modeled outcome."""
    try:
        await email_graph.ainvoke(
            Command(resume=reply_text),
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "pool": pool,
                    "whatsapp_access_token": whatsapp_access_token,
                    "whatsapp_phone_number_id": whatsapp_phone_number_id,
                    "genai_client": genai_client,
                    "gemini_model": gemini_model,
                    "_pending_item_message_id": pending_item_message_id,
                }
            },
        )
    except Exception:
        logger.exception("Unhandled error resuming email thread %s", thread_id)
        try:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                sender,
                "Something went wrong processing that reply — please try again.",
            )
        except Exception:
            logger.exception("Failed to send failure notice for thread %s", thread_id)


def _dispatch_pending_resume(item: dict, reply_text: str, state: RouterState, config: RunnableConfig) -> None:
    configurable = config["configurable"]
    if item["item_type"] == "assignment":
        coro = resume_assignment_thread(
            configurable["assignment_graph"],
            item["thread_id"],
            item["message_id"],
            reply_text,
            state["sender"],
            configurable["whatsapp_access_token"],
            configurable["whatsapp_phone_number_id"],
            configurable["pool"],
            configurable["genai_client"],
            configurable["gemini_model"],
        )
    else:  # "email"
        coro = resume_email_thread(
            configurable["email_graph"],
            item["thread_id"],
            item["message_id"],
            reply_text,
            state["sender"],
            configurable["whatsapp_access_token"],
            configurable["whatsapp_phone_number_id"],
            configurable["pool"],
            configurable["genai_client"],
            configurable["gemini_model"],
        )
    task = asyncio.create_task(coro)
    background_tasks = configurable["background_tasks"]
    background_tasks.add(task)
    task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))


async def handle_pending_item_reply(state: RouterState, config: RunnableConfig) -> dict:
    """The reply-to-message case: state["matched_pending_item"] was
    resolved unambiguously by route_entry_node. Dispatches the resume as a
    background task and returns no reply_text — see Decision #3.

    Must be async (not sync def): a sync node is offloaded by LangGraph to
    a worker thread with no running event loop, and asyncio.create_task
    (inside _dispatch_pending_resume) requires one — confirmed against the
    installed langgraph version while implementing this phase."""
    item = state["matched_pending_item"]
    _dispatch_pending_resume(item, state["inbound_text"], state, config)
    return {}


async def resolve_pending_item_node(state: RouterState, config: RunnableConfig) -> dict:
    """The by-name (non-reply-to) case, reached via the respond_to_pending
    intent. Fuzzy-matches intent_args["pending_item_reference"] against
    every currently-pending assignment item's display_name."""
    pool = config["configurable"]["pool"]
    reference_text = state.get("intent_args", {}).get("pending_item_reference", "")

    with pool.connection() as conn:
        items = repo.list_pending_items(conn)

    if not items:
        return {"reply_text": "There's nothing pending right now."}

    if len(items) == 1:
        # Only one thing it could be, regardless of match confidence.
        _dispatch_pending_resume(items[0], state["inbound_text"], state, config)
        return {}

    if not reference_text.strip():
        # No name given at all (e.g. a bare "yes") with more than one item
        # pending — this isn't "zero matches found", it's "nothing to score
        # against", so ask among all of them rather than reporting no match.
        lines = [f'{i + 1}. {c["display_name"]}' for i, c in enumerate(items)]
        return {
            "reply_text": "Which one did you mean?\n" + "\n".join(lines),
            "pending_question": {
                "kind": "disambiguate_pending_item",
                "message_id": None,
                "candidates": items,
                "original_text": state["inbound_text"],
            },
        }

    candidates = []
    for item in items:
        score = difflib.SequenceMatcher(
            None, reference_text.lower(), item["display_name"].lower()
        ).ratio()
        candidates.append({**item, "score": score})
    candidates.sort(key=lambda c: c["score"], reverse=True)

    best = candidates[0]
    second_score = candidates[1]["score"]
    if best["score"] >= _CONFIDENT_MATCH_SCORE and (best["score"] - second_score) >= _CONFIDENT_MATCH_MARGIN:
        _dispatch_pending_resume(best, state["inbound_text"], state, config)
        return {}

    shortlisted = [c for c in candidates if c["score"] >= _DISAMBIGUATION_MIN_SCORE][
        :_DISAMBIGUATION_MAX_CANDIDATES
    ]
    if not shortlisted:
        return {"reply_text": "I couldn't find a pending item matching that — try naming it differently."}
    if len(shortlisted) == 1:
        _dispatch_pending_resume(shortlisted[0], state["inbound_text"], state, config)
        return {}

    lines = [f'{i + 1}. {c["display_name"]}' for i, c in enumerate(shortlisted)]
    return {
        "reply_text": "Which one did you mean?\n" + "\n".join(lines),
        "pending_question": {
            "kind": "disambiguate_pending_item",
            "message_id": None,
            "candidates": shortlisted,
            "original_text": state["inbound_text"],
        },
    }


async def handle_confirmation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "confirm_start_assignment".

    Async (not sync def) for the same reason as handle_pending_item_reply
    — this node's asyncio.create_task(run_assignment_flow(...)) call needs
    a running event loop, which a sync node doesn't get (LangGraph offloads
    sync nodes to a worker thread). Pre-existing Phase 2 code had this as
    a sync def; fixed while touching this file for Phase 3, since the same
    bug would otherwise silently break "work on <assignment>" confirmations
    in production."""
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
                configurable.get("student_info", ""),
            )
        )
        background_tasks = configurable["background_tasks"]
        background_tasks.add(task)
        task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))
        reply_text = f"Starting on {resolved['title']} — I'll send the draft when it's ready."
    else:
        reply_text = "Okay, not starting that."

    return {"reply_text": reply_text, "pending_question": None}


async def handle_email_confirmation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "confirm_draft_email"."""
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    resolved = pending_question["resolved"]
    question = _confirm_email_question_text(resolved)

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
            run_email_flow(
                configurable["email_graph"],
                resolved,
                state["sender"],
                configurable["whatsapp_access_token"],
                configurable["whatsapp_phone_number_id"],
                configurable["pool"],
                configurable["genai_client"],
                configurable["gemini_model"],
            )
        )
        background_tasks = configurable["background_tasks"]
        background_tasks.add(task)
        task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))
        reply_text = "Drafting that email — I'll send it over when it's ready."
    else:
        reply_text = "Okay, not drafting that."

    return {"reply_text": reply_text, "pending_question": None}


def handle_disambiguation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "disambiguate_assignment"."""
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    candidates = pending_question["candidates"]
    lines = [
        f'{i + 1}. [{c["course_name"]}] {c["title"]} — due {c["due"] or "no due date"}'
        for i, c in enumerate(candidates)
    ]

    try:
        choice = llm.resolve_disambiguation(
            configurable["genai_client"], configurable["gemini_model"], lines, state["inbound_text"]
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


async def handle_pending_item_disambiguation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "disambiguate_pending_item"."""
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    candidates = pending_question["candidates"]
    original_text = pending_question["original_text"]
    lines = [f'{i + 1}. {c["display_name"]}' for i, c in enumerate(candidates)]

    try:
        choice = llm.resolve_disambiguation(
            configurable["genai_client"], configurable["gemini_model"], lines, state["inbound_text"]
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

    # Resuming uses the user's *original* respond_to_pending reply
    # (their actual approve/revise/reject/confirm content), not this
    # disambiguation answer — and, per Decision #3, sends no ack.
    _dispatch_pending_resume(candidates[choice - 1], original_text, state, config)
    return {"pending_question": None}


async def send_reply_node(state: RouterState, config: RunnableConfig) -> dict:
    reply_text = state.get("reply_text")
    if not reply_text:
        # A pending-item resume was dispatched with no ack (Decision #3),
        # or a disambiguation resolved straight into one — legitimately
        # nothing to send this turn.
        return {}

    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]

    try:
        message_id = await send_whatsapp_message(
            access_token, phone_number_id, state["sender"], reply_text
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
    g.add_node("resolve_email", resolve_email_node)
    g.add_node("resolve_pending_item", resolve_pending_item_node)
    g.add_node("handle_confirmation", handle_confirmation_node)
    g.add_node("handle_disambiguation", handle_disambiguation_node)
    g.add_node("handle_email_confirmation", handle_email_confirmation_node)
    g.add_node("handle_email_details", handle_email_details_node)
    g.add_node("handle_pending_item_reply", handle_pending_item_reply)
    g.add_node("handle_pending_item_disambiguation", handle_pending_item_disambiguation_node)
    g.add_node("answer_question_node", answer_question_node)
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
            "handle_email_confirmation": "handle_email_confirmation",
            "handle_email_details": "handle_email_details",
            "handle_pending_item_disambiguation": "handle_pending_item_disambiguation",
            "handle_pending_item_reply": "handle_pending_item_reply",
        },
    )
    g.add_conditional_edges(
        "classify_intent",
        route_after_classify,
        {
            "answer_question_node": "answer_question_node",
            "fallback_node": "fallback_node",
            "resolve_assignment": "resolve_assignment",
            "resolve_email": "resolve_email",
            "resolve_pending_item": "resolve_pending_item",
            "send_reply": "send_reply",
        },
    )
    for node_name in (
        "answer_question_node",
        "fallback_node",
        "resolve_assignment",
        "resolve_email",
        "resolve_pending_item",
        "handle_confirmation",
        "handle_disambiguation",
        "handle_email_confirmation",
        "handle_email_details",
        "handle_pending_item_reply",
        "handle_pending_item_disambiguation",
    ):
        g.add_edge(node_name, "send_reply")
    g.add_edge("send_reply", END)

    return g.compile(checkpointer=checkpointer)
