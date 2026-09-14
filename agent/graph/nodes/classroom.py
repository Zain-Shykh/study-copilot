"""Google Classroom API calls: courses, coursework, announcements, submissions."""

import difflib
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig

from agent import google_auth
from agent.graph.state import RouterState

_MISSING_STATES = {"TURNED_IN", "RETURNED"}


def list_courses(classroom_service) -> list[dict]:
    courses: list[dict] = []
    page_token = None
    while True:
        response = (
            classroom_service.courses()
            .list(courseStates=["ACTIVE"], pageToken=page_token)
            .execute(num_retries=3)
        )
        courses.extend(response.get("courses", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return courses


def _due_datetime(coursework: dict) -> datetime | None:
    due_date = coursework.get("dueDate")
    if not due_date:
        return None
    due_time = coursework.get("dueTime", {})
    return datetime(
        year=due_date["year"],
        month=due_date["month"],
        day=due_date["day"],
        hour=due_time.get("hours", 0),
        minute=due_time.get("minutes", 0),
        second=due_time.get("seconds", 0),
        tzinfo=timezone.utc,
    )


def _is_missing(classroom_service, course_id: str, coursework_id: str) -> bool:
    response = (
        classroom_service.courses()
        .courseWork()
        .studentSubmissions()
        .list(courseId=course_id, courseWorkId=coursework_id, userId="me")
        .execute(num_retries=3)
    )
    submissions = response.get("studentSubmissions", [])
    if not submissions:
        return True
    return submissions[0].get("state") not in _MISSING_STATES


def list_assignments(
    classroom_service, courses: list[dict], scope: str, window_hours: int
) -> list[dict]:
    now = datetime.now(timezone.utc)
    assignments: list[dict] = []

    for course in courses:
        page_token = None
        while True:
            response = (
                classroom_service.courses()
                .courseWork()
                .list(courseId=course["id"], courseStates=["PUBLISHED"], pageToken=page_token)
                .execute(num_retries=3)
            )
            for coursework in response.get("courseWork", []):
                due = _due_datetime(coursework)
                item = {"course": course, "courseWork": coursework, "due": due}

                if scope == "all":
                    assignments.append(item)
                elif scope == "due_soon":
                    if due is not None and now <= due <= now + timedelta(hours=window_hours):
                        assignments.append(item)
                elif scope == "overdue":
                    if due is not None and due < now:
                        assignments.append(item)
                elif scope == "missing":
                    if due is not None and due < now:
                        if _is_missing(classroom_service, course["id"], coursework["id"]):
                            assignments.append(item)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    return assignments


def list_announcements(classroom_service, courses: list[dict]) -> list[dict]:
    announcements: list[dict] = []
    for course in courses:
        page_token = None
        while True:
            response = (
                classroom_service.courses()
                .announcements()
                .list(
                    courseId=course["id"],
                    announcementStates=["PUBLISHED"],
                    pageToken=page_token,
                )
                .execute(num_retries=3)
            )
            for a in response.get("announcements", []):
                announcements.append({"course": course, "announcement": a})
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return announcements


def format_courses_reply(courses: list[dict]) -> str:
    if not courses:
        return "You're not enrolled in any active courses."
    lines = [f"- {c['name']}" for c in courses]
    return "Your courses:\n" + "\n".join(lines)


def format_assignments_reply(assignments: list[dict], scope: str, window_hours: int) -> str:
    scope_headers = {
        "due_soon": f"Here's what's due in the next {window_hours} hours:",
        "all": "Here's everything assigned:",
        "overdue": "Here's what's overdue:",
        "missing": "Here's what you're missing:",
    }
    header = scope_headers[scope]

    if not assignments:
        return f"{header}\nNothing found."

    lines = []
    for item in assignments:
        course_name = item["course"]["name"]
        title = item["courseWork"]["title"]
        due = item["due"]
        due_text = due.strftime("%Y-%m-%d %H:%M UTC") if due else "no due date"
        lines.append(f"- [{course_name}] {title} — due {due_text}")

    return header + "\n" + "\n".join(lines)


def classroom_node(state: RouterState, config: RunnableConfig) -> dict:
    pool = config["configurable"]["pool"]
    intent = state["intent"]
    intent_args = state.get("intent_args", {})

    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"reply_text": clients}
    _, classroom_service, _ = clients

    try:
        courses = list_courses(classroom_service)
        if intent == "list_courses":
            reply_text = format_courses_reply(courses)
        else:  # whats_due
            scope = intent_args.get("due_scope", "due_soon")
            window_hours = intent_args.get("due_window_hours", 48)
            assignments = list_assignments(classroom_service, courses, scope, window_hours)
            reply_text = format_assignments_reply(assignments, scope, window_hours)
    except HttpError as e:
        reply_text = f"Couldn't reach Classroom right now: {e}"

    return {"reply_text": reply_text}


def find_assignment_candidates(
    courses: list[dict], assignments_by_course: dict[str, list[dict]], reference_text: str
) -> list[dict]:
    """Scores every assignment against reference_text using
    difflib.SequenceMatcher on "<course name> <assignment title>" (stdlib
    only — no new fuzzy-matching dependency). Returns candidates sorted by
    score descending. Caller applies match-confidence thresholds."""
    reference = reference_text.lower()
    candidates: list[dict] = []

    for course in courses:
        course_id = course["id"]
        for coursework in assignments_by_course.get(course_id, []):
            title = coursework["title"]
            haystack = f"{course['name']} {title}".lower()
            score = difflib.SequenceMatcher(None, reference, haystack).ratio()
            due = _due_datetime(coursework)
            candidates.append(
                {
                    "course_id": course_id,
                    "course_name": course["name"],
                    "coursework_id": coursework["id"],
                    "title": title,
                    "due": due.strftime("%Y-%m-%d %H:%M UTC") if due else None,
                    "score": score,
                }
            )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates
