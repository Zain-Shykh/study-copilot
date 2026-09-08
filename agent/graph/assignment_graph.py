"""Per-assignment graph: ingest materials -> draft via Claude Code -> interrupt -> revise/submit."""

import logging
from pathlib import Path
from typing import TypedDict

import httpx
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent import google_auth
from agent.db import repo
from agent.graph.nodes import claude_code, ingestion
from agent.graph.nodes.whatsapp_send import send_whatsapp_document, send_whatsapp_message
from agent.workspace import paths

logger = logging.getLogger(__name__)


class AssignmentState(TypedDict, total=False):
    course_id: str
    course_name: str
    coursework_id: str
    title: str
    sender: str  # who to relay results to
    ingested: bool
    unsupported_files: list[str]
    failure_text: str | None
    draft_path: str | None
    summary_text: str | None
    session_id: str | None


async def ingest_node(state: AssignmentState, config: RunnableConfig) -> dict:
    pool = config["configurable"]["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"ingested": False, "failure_text": clients}
    _, classroom_service, drive_service = clients

    dest_dir = paths.assignment_dir(state["course_name"], state["title"])
    result = await ingestion.ingest_assignment(
        classroom_service, drive_service, state["course_id"], state["coursework_id"], dest_dir
    )

    if not result["success"]:
        failed_file = result["failed_file"]
        retry_hint = f'Send "work on {state["title"]}" again to retry.'
        if failed_file:
            failure_text = f'Couldn\'t ingest "{failed_file}": {result["error"]}. {retry_hint}'
        else:
            failure_text = f'Couldn\'t load the assignment details: {result["error"]}. {retry_hint}'
        return {"ingested": False, "failure_text": failure_text}

    return {"ingested": True, "unsupported_files": result["unsupported_files"]}


def route_after_ingest(state: AssignmentState) -> str:
    return "draft_node" if state["ingested"] else "relay_node"


async def draft_node(state: AssignmentState, config: RunnableConfig) -> dict:
    dest_dir = paths.assignment_dir(state["course_name"], state["title"])
    result = await claude_code.run_claude_code(dest_dir)

    if not result["success"]:
        return {"failure_text": f'Drafting "{state["title"]}" failed: {result["error"]}'}

    return {
        "draft_path": str(result["draft_path"]),
        "summary_text": result["summary_text"],
        "session_id": result["session_id"],
    }


def save_session_node(state: AssignmentState, config: RunnableConfig) -> dict:
    session_id = state.get("session_id")
    if not session_id:
        # Draft failed before a session id was produced — nothing to save.
        return {}

    pool = config["configurable"]["pool"]
    thread_id = config["configurable"]["thread_id"]
    with pool.connection() as conn:
        repo.save_claude_session(conn, thread_id, session_id)
    return {}


async def relay_node(state: AssignmentState, config: RunnableConfig) -> dict:
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

    try:
        await send_whatsapp_document(
            access_token, phone_number_id, sender, Path(state["draft_path"])
        )
    except httpx.HTTPStatusError:
        logger.exception("Failed to send draft document to %s", sender)
        return {}

    summary_text = state.get("summary_text") or ""
    unsupported_files = state.get("unsupported_files") or []
    if unsupported_files:
        summary_text = (
            f"Skipped unsupported material(s): {', '.join(unsupported_files)}\n\n{summary_text}"
        )

    try:
        await send_whatsapp_message(
            access_token, phone_number_id, sender, summary_text.strip() or "Draft ready."
        )
    except httpx.HTTPStatusError:
        logger.exception("Failed to send draft summary to %s", sender)

    return {}


def build_assignment_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(AssignmentState)
    g.add_node("ingest_node", ingest_node)
    g.add_node("draft_node", draft_node)
    g.add_node("save_session_node", save_session_node)
    g.add_node("relay_node", relay_node)

    g.set_entry_point("ingest_node")
    g.add_conditional_edges(
        "ingest_node",
        route_after_ingest,
        {"draft_node": "draft_node", "relay_node": "relay_node"},
    )
    g.add_edge("draft_node", "save_session_node")
    g.add_edge("save_session_node", "relay_node")
    g.add_edge("relay_node", END)

    return g.compile(checkpointer=checkpointer)
