"""Google Classroom API calls: courses, coursework, announcements, submissions."""

import difflib
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

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
) -> tuple[list[dict], list[str]]:
    """Returns (assignments, failed_course_names). A course whose coursework
    can't be read (e.g. a permission error scoped to just that course) is
    skipped rather than aborting the whole listing — partial-batch failures
    proceed with whatever succeeded, per this codebase's error-handling
    policy (CLAUDE.md)."""
    now = datetime.now(timezone.utc)
    assignments: list[dict] = []
    failed_courses: list[str] = []

    for course in courses:
        page_token = None
        try:
            while True:
                response = (
                    classroom_service.courses()
                    .courseWork()
                    .list(courseId=course["id"], courseWorkStates=["PUBLISHED"], pageToken=page_token)
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
        except HttpError:
            failed_courses.append(course["name"])
            continue

    return assignments, failed_courses


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
