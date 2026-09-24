"""Per-assignment graph: ingest materials -> draft via Claude Code -> interrupt -> revise/submit."""

import logging
import zipfile
from pathlib import Path
from typing import TypedDict

import httpx
import pypandoc
from googleapiclient.http import MediaFileUpload
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from agent import google_auth, llm
from agent.db import repo
from agent.graph.nodes import claude_code, ingestion
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
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
    submission_files: list[str] | None  # paths under submission/, replaces Phase 2's draft_path
    manifest: dict | None               # parsed submission_manifest.json — see claude_code.py
    summary_text: str | None
    session_id: str | None
    review_reply_text: str | None       # raw text from the draft-review interrupt
    review_decision: str | None         # "approve" | "revise" | "reject"
    review_feedback: str | None         # only set when review_decision == "revise"
    submit_reply_text: str | None       # raw text from the submit-confirm interrupt
    submit_decision: str | None         # "confirm" | "decline"
    final_files: list[str] | None       # packaged output(s) actually uploaded
    drive_links: list[str] | None


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
    student_info = config["configurable"].get("student_info", "")
    result = await claude_code.run_claude_code(dest_dir, student_info=student_info)

    if not result["success"]:
        return {"failure_text": f'Drafting "{state["title"]}" failed: {result["error"]}'}

    return {
        "submission_files": [str(p) for p in result["submission_files"]],
        "manifest": result["manifest"],
        "summary_text": result["summary_text"],
        "session_id": result["session_id"],
    }


async def revise_node(state: AssignmentState, config: RunnableConfig) -> dict:
    pool = config["configurable"]["pool"]
    thread_id = config["configurable"]["thread_id"]
    with pool.connection() as conn:
        session_id = repo.get_claude_session(conn, thread_id)

    dest_dir = paths.assignment_dir(state["course_name"], state["title"])
    result = await claude_code.run_claude_code(
        dest_dir, resume_session_id=session_id, feedback=state["review_feedback"]
    )

    if not result["success"]:
        return {"failure_text": f'Revising "{state["title"]}" failed: {result["error"]}'}

    return {
        "submission_files": [str(p) for p in result["submission_files"]],
        "manifest": result["manifest"],
        "summary_text": result["summary_text"],
        "session_id": result["session_id"],
    }


def save_session_node(state: AssignmentState, config: RunnableConfig) -> dict:
    session_id = state.get("session_id")
    if not session_id:
        # Draft/revision failed before a session id was produced — nothing to save.
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

    manifest = state["manifest"]
    submission_files = state["submission_files"]
    pool = configurable["pool"]

    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        logger.error("Couldn't upload draft to Drive for review (%s): %s", sender, clients)
        return {}
    _, _, drive_service = clients

    try:
        drive_links = [_upload_to_drive(drive_service, Path(f)) for f in submission_files]
    except Exception:  # noqa: BLE001 - Drive HttpError or transport error
        logger.exception("Failed to upload draft to Drive for %s", sender)
        return {}

    summary_text = state.get("summary_text") or ""
    unsupported_files = state.get("unsupported_files") or []
    if unsupported_files:
        summary_text = (
            f"Skipped unsupported material(s): {', '.join(unsupported_files)}\n\n{summary_text}"
        )
    summary_text = summary_text.strip() or "Draft ready."
    links_text = "\n".join(
        f"{rel_path}: {link}" for rel_path, link in zip(manifest["files"], drive_links)
    )
    message_text = f"{summary_text}\n\n{links_text}\n\nReply approve, suggest changes, or say reject."

    try:
        message_id = await send_whatsapp_message(access_token, phone_number_id, sender, message_text)
    except httpx.HTTPStatusError:
        logger.exception("Failed to send draft summary to %s", sender)
        return {}

    thread_id = configurable["thread_id"]
    display_name = f'{state["course_name"]} — {state["title"]}'
    with pool.connection() as conn:
        repo.create_pending_item(conn, message_id, thread_id, "assignment", display_name)

    return {}


def route_after_relay(state: AssignmentState) -> str:
    return "await_review_node" if not state.get("failure_text") else END


def await_review_node(state: AssignmentState) -> dict:
    reply = interrupt({"kind": "draft_review", "title": state["title"]})
    return {"review_reply_text": reply}


def parse_review_node(state: AssignmentState, config: RunnableConfig) -> dict:
    """No try/except around parse_review_reply here — deliberately. If
    Gemini fails after its own 3 internal retries, letting the exception
    propagate leaves this thread parked at this node (LangGraph re-runs a
    failed node from scratch on the next resume, using the same
    review_reply_text already in state) and the pending_items row stays
    open, so a later reply to the same message naturally retries the
    parse. The generic "something went wrong" message is sent by the
    caller's outer try/except (router_graph.resume_assignment_thread), not
    here — matches Decision #3's wrapper pattern and avoids double-sending."""
    configurable = config["configurable"]
    decision, feedback = llm.parse_review_reply(
        configurable["genai_client"], configurable["gemini_model"], state["review_reply_text"]
    )

    pool = configurable["pool"]
    message_id = configurable["_pending_item_message_id"]
    with pool.connection() as conn:
        repo.close_pending_item(conn, message_id)

    return {"review_decision": decision, "review_feedback": feedback}


def route_after_review(state: AssignmentState) -> str:
    return {"approve": "ask_submit_node", "revise": "revise_node", "reject": END}[state["review_decision"]]


def _submit_question_text(title: str) -> str:
    return (
        f'"{title}" approved. Ready to submit? Replying yes packages and '
        f"uploads it to your Drive; no leaves it as-is for now."
    )


async def ask_submit_node(state: AssignmentState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]
    sender = state["sender"]
    title = state["title"]

    try:
        message_id = await send_whatsapp_message(
            access_token, phone_number_id, sender, _submit_question_text(title)
        )
    except httpx.HTTPStatusError:
        logger.exception("Failed to send submit-confirm question to %s", sender)
        return {}

    pool = configurable["pool"]
    thread_id = configurable["thread_id"]
    display_name = f'{state["course_name"]} — {title}'
    with pool.connection() as conn:
        repo.create_pending_item(conn, message_id, thread_id, "assignment", display_name)

    return {}


def await_submit_node(state: AssignmentState) -> dict:
    reply = interrupt({"kind": "submit_confirm", "title": state["title"]})
    return {"submit_reply_text": reply}


def parse_submit_node(state: AssignmentState, config: RunnableConfig) -> dict:
    """Same no-try/except reasoning as parse_review_node."""
    configurable = config["configurable"]
    answer = llm.parse_confirmation_reply(
        configurable["genai_client"],
        configurable["gemini_model"],
        _submit_question_text(state["title"]),
        state["submit_reply_text"],
    )

    if answer == "decline":
        pool = configurable["pool"]
        message_id = configurable["_pending_item_message_id"]
        with pool.connection() as conn:
            repo.close_pending_item(conn, message_id)
        return {"submit_decision": "decline"}

    # confirm: pending item stays open until submission_prep_node actually
    # succeeds (Decision #4) — a Drive/Pandoc failure can be retried by
    # simply replying "yes" again.
    return {"submit_decision": "confirm"}


def route_after_submit(state: AssignmentState) -> str:
    return "submission_prep_node" if state["submit_decision"] == "confirm" else END


def _package_submission(manifest: dict, submission_dir: Path, dest_dir: Path, title: str) -> list[Path]:
    """Mechanically executes manifest exactly as declared by Claude Code —
    no interpretation of the assignment's own guidelines happens here.
    manifest's optional "output_name" (e.g. a roll number the assignment
    asked for) overrides the assignment-title-derived default name for a
    packaged "zip"/"pdf"/"docx" output; it's ignored for "as-is", where
    each file already keeps the name Claude Code gave it."""
    format_ = manifest["format"]
    files = manifest["files"]
    output_name = manifest.get("output_name")
    base_name = paths.slugify(output_name) if output_name else paths.slugify(title)

    if format_ == "as-is":
        return [submission_dir / rel_path for rel_path in files]

    if format_ == "zip":
        zip_path = dest_dir / f"{base_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path in files:
                zf.write(submission_dir / rel_path, arcname=rel_path)
        return [zip_path]

    # "pdf" / "docx" — flat files only, enforced by claude_code.py's manifest validation
    extra_args = ["--pdf-engine=wkhtmltopdf"] if format_ == "pdf" else []
    # output_name only unambiguously applies when there's a single output file
    single_named = bool(output_name) and len(files) == 1
    outputs = []
    for rel_path in files:
        src = submission_dir / rel_path
        stem = base_name if single_named else Path(rel_path).stem
        out_path = dest_dir / f"{stem}.{format_}"
        pypandoc.convert_file(str(src), format_, outputfile=str(out_path), extra_args=extra_args)
        outputs.append(out_path)
    return outputs


def _upload_to_drive(drive_service, file_path: Path) -> str:
    media = MediaFileUpload(str(file_path))
    created = (
        drive_service.files()
        .create(body={"name": file_path.name}, media_body=media, fields="id")
        .execute(num_retries=3)
    )
    result = (
        drive_service.files().get(fileId=created["id"], fields="webViewLink").execute(num_retries=3)
    )
    return result["webViewLink"]


async def submission_prep_node(state: AssignmentState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pool = configurable["pool"]
    title = state["title"]
    manifest = state["manifest"]
    dest_dir = paths.assignment_dir(state["course_name"], title)
    submission_dir = dest_dir / "submission"

    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"failure_text": f'Couldn\'t prepare "{title}" for submission: {clients}'}
    _, _, drive_service = clients

    retry_hint = 'Reply "yes" again to retry — nothing was lost.'
    try:
        final_files = _package_submission(manifest, submission_dir, dest_dir, title)
    except Exception as e:  # noqa: BLE001 - zipfile/Pandoc error, reported plainly
        return {"failure_text": f'Couldn\'t prepare "{title}" for submission: {e}. {retry_hint}'}

    try:
        drive_links = [_upload_to_drive(drive_service, f) for f in final_files]
    except Exception as e:  # noqa: BLE001 - Drive HttpError or transport error
        return {"failure_text": f'Couldn\'t prepare "{title}" for submission: {e}. {retry_hint}'}

    message_id = configurable["_pending_item_message_id"]
    with pool.connection() as conn:
        repo.close_pending_item(conn, message_id)

    return {"final_files": [str(f) for f in final_files], "drive_links": drive_links}


async def relay_submit_node(state: AssignmentState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]
    sender = state["sender"]

    if state.get("failure_text"):
        try:
            await send_whatsapp_message(access_token, phone_number_id, sender, state["failure_text"])
        except httpx.HTTPStatusError:
            logger.exception("Failed to send submission failure notice to %s", sender)
        return {}

    lines = [
        f"- {Path(f).name}: {link}"
        for f, link in zip(state["final_files"], state["drive_links"])
    ]
    message = (
        "Uploaded to your Drive:\n" + "\n".join(lines) + "\n\n"
        "Open the assignment in Classroom (Add or create → Drive), attach "
        "the file(s) above, and click Turn In to submit."
    )
    try:
        await send_whatsapp_message(access_token, phone_number_id, sender, message)
    except httpx.HTTPStatusError:
        logger.exception("Failed to send submission summary to %s", sender)

    return {}


def build_assignment_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(AssignmentState)
    g.add_node("ingest_node", ingest_node)
    g.add_node("draft_node", draft_node)
    g.add_node("revise_node", revise_node)
    g.add_node("save_session_node", save_session_node)
    g.add_node("relay_node", relay_node)
    g.add_node("await_review_node", await_review_node)
    g.add_node("parse_review_node", parse_review_node)
    g.add_node("ask_submit_node", ask_submit_node)
    g.add_node("await_submit_node", await_submit_node)
    g.add_node("parse_submit_node", parse_submit_node)
    g.add_node("submission_prep_node", submission_prep_node)
    g.add_node("relay_submit_node", relay_submit_node)

    g.set_entry_point("ingest_node")
    g.add_conditional_edges(
        "ingest_node",
        route_after_ingest,
        {"draft_node": "draft_node", "relay_node": "relay_node"},
    )
    g.add_edge("draft_node", "save_session_node")
    g.add_edge("revise_node", "save_session_node")
    g.add_edge("save_session_node", "relay_node")
    g.add_conditional_edges(
        "relay_node",
        route_after_relay,
        {"await_review_node": "await_review_node", END: END},
    )
    g.add_edge("await_review_node", "parse_review_node")
    g.add_conditional_edges(
        "parse_review_node",
        route_after_review,
        {"ask_submit_node": "ask_submit_node", "revise_node": "revise_node", END: END},
    )
    g.add_edge("ask_submit_node", "await_submit_node")
    g.add_edge("await_submit_node", "parse_submit_node")
    g.add_conditional_edges(
        "parse_submit_node",
        route_after_submit,
        {"submission_prep_node": "submission_prep_node", END: END},
    )
    g.add_edge("submission_prep_node", "relay_submit_node")
    g.add_edge("relay_submit_node", END)

    return g.compile(checkpointer=checkpointer)
