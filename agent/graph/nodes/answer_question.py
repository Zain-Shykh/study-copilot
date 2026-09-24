"""Tool-calling answers for read-only Classroom/Gmail questions — the
model calls these to fetch real data, then writes its own reply. See
specs/tool-calling-read-answers.md."""

import logging

from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig

from agent import google_auth, llm
from agent.graph.nodes import classroom, gmail
from agent.graph.state import RouterState

logger = logging.getLogger(__name__)


def answer_question_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pool = configurable["pool"]
    genai_client = configurable["genai_client"]
    gemini_model = configurable["gemini_model"]

    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"reply_text": clients}
    gmail_service, classroom_service, _ = clients

    def get_courses() -> list[dict]:
        """Returns every active Google Classroom course the user is
        enrolled in as a student (teacher/TA-role courses are excluded),
        as a list of {"name": str}. Call this first if you need to know
        what courses exist or need a course name to pass to
        get_announcements."""
        try:
            courses = classroom.list_courses(classroom_service)
        except HttpError as e:
            return [{"error": str(e)}]
        return [{"name": c["name"]} for c in courses]

    def get_all_assignments() -> dict:
        """Returns EVERY assignment across all courses regardless of due
        date — past, current, and future, with no time-window filtering.
        Use this for any question about assignments (a specific one by
        name, a count, what's due when, what's overdue) and pick out what
        the question actually asked for yourself; do not assume the user
        only wants what's due soon unless they say so. Each item is
        {"course": str, "title": str, "due": "<ISO datetime> or null"}.
        The response may also include "unavailable_courses": a list of
        course names that couldn't be checked (e.g. a permission error) —
        mention those briefly in your answer if present, don't ignore
        them."""
        try:
            courses = classroom.list_courses(classroom_service)
            assignments, failed = classroom.list_assignments(
                classroom_service, courses, scope="all", window_hours=0
            )
        except HttpError as e:
            return {"error": str(e)}
        result = {
            "assignments": [
                {
                    "course": item["course"]["name"],
                    "title": item["courseWork"]["title"],
                    "due": item["due"].isoformat() if item["due"] else None,
                }
                for item in assignments
            ]
        }
        if failed:
            result["unavailable_courses"] = failed
        return result

    def get_missing_assignments() -> dict:
        """Returns assignments that are past their due date AND not yet
        turned in (Classroom's own "missing" status) — a separate,
        heavier check than get_all_assignments (it queries each overdue
        assignment's submission status individually), so only call this
        when the question is specifically about what's missing/not turned
        in, not for general due-date questions. Same item shape as
        get_all_assignments."""
        try:
            courses = classroom.list_courses(classroom_service)
            assignments, failed = classroom.list_assignments(
                classroom_service, courses, scope="missing", window_hours=0
            )
        except HttpError as e:
            return {"error": str(e)}
        result = {
            "assignments": [
                {
                    "course": item["course"]["name"],
                    "title": item["courseWork"]["title"],
                    "due": item["due"].isoformat() if item["due"] else None,
                }
                for item in assignments
            ]
        }
        if failed:
            result["unavailable_courses"] = failed
        return result

    def get_announcements(course_name: str | None = None) -> list[dict]:
        """Returns Classroom announcements (not email). If course_name is
        given, only that course's announcements (matched case-insensitively
        against get_courses' names — if it doesn't match any course,
        returns an {"error": ...} explaining that instead of guessing).
        If course_name is omitted, returns announcements across every
        course. Each item is {"course": str, "text": str, "posted":
        "<ISO datetime or empty string>"}, newest first is not guaranteed
        — sort/filter yourself if the question asks for "latest"."""
        try:
            courses = classroom.list_courses(classroom_service)
        except HttpError as e:
            return [{"error": str(e)}]

        if course_name:
            matches = [c for c in courses if course_name.lower() in c["name"].lower()]
            if not matches:
                return [{"error": f'No course matching "{course_name}"'}]
            courses = matches

        try:
            entries = classroom.list_announcements(classroom_service, courses)
        except HttpError as e:
            return [{"error": str(e)}]
        return [
            {
                "course": e["course"]["name"],
                "text": e["announcement"].get("text", ""),
                "posted": e["announcement"].get("creationTime", ""),
            }
            for e in entries
        ]

    def get_recent_emails(
        gmail_query: str = "",
        after_date: str | None = None,
        before_date: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Returns Gmail messages (not Classroom) matching gmail_query,
        Gmail's own search syntax — the same operators you'd type into the
        Gmail search bar, e.g. "from:prof@uni.edu", "subject:midterm",
        "has:attachment", "is:unread", "category:updates",
        "label:important". Combine multiple operators in one
        space-separated string (space = AND). Use after_date/before_date
        (each "YYYY-MM-DD", in the user's own timezone) instead of writing
        after:/before: yourself inside gmail_query — these are computed
        exactly server-side, whereas hand-written after:/before: dates are
        timezone-ambiguous. after_date is inclusive, before_date is
        exclusive, so "yesterday" is after_date=<yesterday>,
        before_date=<today>, and "today" is after_date=<today> with
        before_date left unset. Pass a higher max_results (e.g. 25)
        whenever after_date/before_date is set or gmail_query is broad, so
        results aren't silently truncated. Leave gmail_query empty and
        after_date/before_date unset for a general "recent emails"
        question. Each item is {"from": str, "subject": str, "date": str,
        "snippet": str} — the snippet is a short excerpt, not the full
        body."""
        date_filter = gmail.build_date_filter(after_date, before_date)
        query = " ".join(part for part in [gmail_query, date_filter] if part)
        try:
            return gmail.list_messages(gmail_service, query, max_results)
        except HttpError as e:
            return [{"error": str(e)}]

    try:
        reply_text = llm.answer_question(
            genai_client,
            gemini_model,
            state["inbound_text"],
            tools=[
                get_courses,
                get_all_assignments,
                get_missing_assignments,
                get_announcements,
                get_recent_emails,
            ],
        )
    except Exception:
        logger.exception("answer_question failed after retries")
        return {"reply_text": "Couldn't process that message right now — please try again."}

    return {"reply_text": reply_text}
