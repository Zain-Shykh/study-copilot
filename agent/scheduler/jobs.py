"""Proactive polling jobs. Plain async functions, not LangGraph nodes —
scheduled by APScheduler in agent/main.py, sharing the same Google API
clients / Postgres pool / WhatsApp-send helper the graph nodes use. Never
triggers a write/approval-gated action — see product_definition.md's
Proactive vs on-demand section."""

import logging

from agent import google_auth
from agent.db import repo
from agent.graph.nodes.classroom import list_announcements, list_assignments, list_courses
from agent.graph.nodes.gmail import get_current_history_id, get_messages_by_id, get_new_message_ids
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.llm import summarize_emails

logger = logging.getLogger(__name__)

_DUE_SOON_WINDOW_HOURS = 48

# In-process outage-dedup flags — resets on restart, consistent with the
# already-accepted "a crash/hang takes down checks until restarted"
# trade-off from the Scheduler decision.
_gmail_outage = False
_classroom_outage = False


async def poll_gmail_job(
    pool,
    genai_client,
    gemini_model,
    whatsapp_access_token,
    whatsapp_phone_number_id,
    my_whatsapp_number,
) -> None:
    global _gmail_outage
    try:
        with pool.connection() as conn:
            clients = google_auth.load_google_clients(conn)
            if isinstance(clients, str):
                raise RuntimeError(clients)
            gmail_service, _, _ = clients

            checkpoint = repo.get_email_checkpoint(conn)
            if checkpoint is None:
                repo.save_email_checkpoint(conn, get_current_history_id(gmail_service))
                _gmail_outage = False
                return  # baseline only, nothing to report yet

            message_ids = get_new_message_ids(gmail_service, checkpoint)
            if message_ids is None:
                repo.save_email_checkpoint(conn, get_current_history_id(gmail_service))
                _gmail_outage = False
                return  # expired checkpoint, re-baseline

            new_history_id = get_current_history_id(gmail_service)
            if message_ids:
                messages = get_messages_by_id(gmail_service, message_ids)
                digest = summarize_emails(genai_client, gemini_model, messages)
                await send_whatsapp_message(
                    whatsapp_access_token,
                    whatsapp_phone_number_id,
                    my_whatsapp_number,
                    f"{len(messages)} new email(s) since your last check:\n\n{digest}",
                )
            repo.save_email_checkpoint(conn, new_history_id)

        if _gmail_outage:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                my_whatsapp_number,
                "Gmail checks are working again.",
            )
        _gmail_outage = False

    except Exception:
        logger.exception("poll_gmail_job failed")
        if not _gmail_outage:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                my_whatsapp_number,
                "Couldn't check Gmail just now — will retry at the next scheduled check.",
            )
        _gmail_outage = True


def _format_milestones(entries: list[tuple[str, dict]]) -> str:
    labels = {
        "posted": "New assignment",
        "due_soon": "Due soon",
        "overdue": "Overdue",
        "announcement_posted": "New announcement",
    }
    lines = []
    for milestone_type, item in entries:
        course_name = item["course"]["name"]
        if milestone_type == "announcement_posted":
            text = item["announcement"].get("text", "")[:120]
            lines.append(f"- [{labels[milestone_type]}] {course_name}: {text}")
        else:
            title = item["courseWork"]["title"]
            due = item["due"]
            due_text = due.strftime("%Y-%m-%d %H:%M UTC") if due else "no due date"
            lines.append(f"- [{labels[milestone_type]}] {course_name}: {title} — due {due_text}")
    return "\n".join(lines)


async def poll_classroom_job(
    pool, whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number
) -> None:
    global _classroom_outage
    try:
        new_entries: list[tuple[str, dict]] = []

        with pool.connection() as conn:
            clients = google_auth.load_google_clients(conn)
            if isinstance(clients, str):
                raise RuntimeError(clients)
            _, classroom_service, _ = clients

            courses = list_courses(classroom_service)

            for milestone_type, scope, window in (
                ("posted", "all", 0),
                ("due_soon", "due_soon", _DUE_SOON_WINDOW_HOURS),
                ("overdue", "missing", 0),
            ):
                assignments, _failed_courses = list_assignments(classroom_service, courses, scope, window)
                for item in assignments:
                    key = f"assignment:{item['courseWork']['id']}"
                    if not repo.is_milestone_notified(conn, key, milestone_type):
                        repo.record_milestone_notified(conn, item["course"]["id"], key, milestone_type)
                        new_entries.append((milestone_type, item))

            for entry in list_announcements(classroom_service, courses):
                key = f"announcement:{entry['announcement']['id']}"
                if not repo.is_milestone_notified(conn, key, "announcement_posted"):
                    repo.record_milestone_notified(conn, entry["course"]["id"], key, "announcement_posted")
                    new_entries.append(("announcement_posted", entry))

        if new_entries:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                my_whatsapp_number,
                "Classroom updates:\n\n" + _format_milestones(new_entries),
            )

        if _classroom_outage:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                my_whatsapp_number,
                "Classroom checks are working again.",
            )
        _classroom_outage = False

    except Exception:
        logger.exception("poll_classroom_job failed")
        if not _classroom_outage:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                my_whatsapp_number,
                "Couldn't check Classroom just now — will retry at the next scheduled check.",
            )
        _classroom_outage = True
