# Phase 4 — Proactive Mode — Spec

Source: `docs/implementation_plan.md` ("Phase 4"), `docs/product_definition.md`
(Proactive vs on-demand actions, Proactive notification dedup, Email digest
checkpoint, Error handling — Proactive poll-cycle failure, Decisions #4/#7/#8,
Architecture/module layout), `agent/db/schema.sql`,
`specs/phase-3-approval-submission.md`.

## Decisions made for this phase (not fully pinned down by the docs above —
flagged here for review, not yet confirmed with the user)

1. **Gmail checkpoint bootstrap.** `email_checkpoint` has no row until this
   phase's first poll ever runs. Nothing in the docs says what happens on
   that first run. Dumping the user's entire unread/older history as a
   "digest" would be useless noise. **Proposed**: the first poll (no
   checkpoint row) calls `users.getProfile` to read the current
   `historyId`, saves it as the baseline, and sends nothing that cycle —
   only messages that arrive *after* the baseline are ever reported, same
   principle as the already-locked "old unread backlog is never
   re-reported" rule. The same reset (re-baseline, skip that cycle) also
   handles Gmail returning `404` from `history.list`, which happens when a
   `historyId` has aged out (Gmail only guarantees ~1 week, in practice
   often longer) — treated as "checkpoint expired," not a poll failure.
2. **Milestone identity for `notified_milestones`.** The table's
   `coursework_id` column (PK'd with `milestone_type`) was designed in
   Phase 0 with only assignments in mind. Announcements have their own
   `id`, not a `courseWork` id, and no `item_type` column exists to keep
   the two id spaces apart. **Proposed** (no schema change): store a
   prefixed key in that column — `f"assignment:{coursework_id}"` /
   `f"announcement:{announcement_id}"` — for every row this phase writes,
   so the two id spaces can never collide even though they share a column
   and a `milestone_type` value (`"posted"`). Cheaper than a migration for
   what's really just "item id, scoped by type."
3. **"Overdue" only fires for work that's still actually unsubmitted.** The
   plan just says "overdue for assignments," and a plain `due < now` check
   (matching `classroom.py`'s existing `"overdue"` scope) would re-nag
   about something you already turned in. **Proposed**: reuse
   `list_assignments(..., scope="missing")` instead — it already exists
   (Phase 1), already does `due < now` *and* the extra
   `studentSubmissions.list` check for "not turned in" — so the overdue
   milestone is really "confirmed missing," at the cost of one extra
   Classroom API call per overdue item per poll.
4. **Poll-cycle failure dedup needs new state, and it's in-memory, not
   Postgres.** No existing table tracks "is there an ongoing outage" for
   Gmail/Classroom polling. A durable table would survive a restart (and
   Phase 4's own acceptance criteria only test dedup *within* a run of
   consecutive polls) — but the Scheduler decision already locked in "a
   crash/hang takes down checks until restarted" as an accepted trade-off,
   so re-arming outage-dedup on restart is consistent with that, not a new
   gap. **Proposed**: two plain in-process booleans (`_gmail_outage`,
   `_classroom_outage`) in `agent/scheduler/jobs.py`, set on failure,
   cleared (with a one-time recovery message) on the next success.
5. **Batching**: each poll cycle sends **one** combined Classroom message
   covering every new milestone found that cycle (assignments +
   announcements together), not one message per milestone — mirrors the
   already-locked "one batched digest" rule for email, and keeps a poll
   that finds five things at once from firing five separate WhatsApp
   messages. The plan's "exactly one notification per milestone, never
   repeating" acceptance criterion is about *never re-sending the same
   milestone*, not about one message per milestone, so batching doesn't
   conflict with it.
6. **Fixed schedule, no new setting.** "morning/evening" (from the product
   doc) is picked as a concrete **08:00 and 20:00, local system time**,
   hardcoded as a module constant rather than a new `.env` var — matches
   the scale of the rest of this app's config (no setting has been added
   for a value this rarely-changed elsewhere). Flagging in case the user
   wants it configurable instead.
7. **`list_courses`/scope reuse.** Per the already-locked "all enrolled
   classes, no per-class opt-in" decision, both poll jobs just call the
   existing `list_courses(classroom_service)` unchanged — no new
   course-selection logic anywhere in this phase.

---

## 1. Objective (restated from the plan)

Add an in-process APScheduler job that fires twice a day and pushes you
unprompted, read-only notifications about new/due Classroom work,
Classroom announcements, and new Gmail since the last check — strictly
informational, never a trigger for any write/approval-gated action, layered
on top of everything built in Phases 1–3 with no changes to either
LangGraph graph.

---

## 2. `agent/db/repo.py` additions

```python
def get_email_checkpoint(conn: psycopg.Connection) -> str | None:
    """Returns the last-processed Gmail historyId, or None if no checkpoint
    has been established yet (this phase's first-ever poll)."""
    with conn.cursor() as cur:
        cur.execute("SELECT last_history_id FROM email_checkpoint WHERE id = 'singleton'")
        row = cur.fetchone()
    return row[0] if row else None


def save_email_checkpoint(conn: psycopg.Connection, history_id: str) -> None:
    """Inserts or advances the singleton email-checkpoint row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO email_checkpoint (id, last_history_id, updated_at)
            VALUES ('singleton', %s, now())
            ON CONFLICT (id) DO UPDATE
                SET last_history_id = EXCLUDED.last_history_id,
                    updated_at = now()
            """,
            (history_id,),
        )
    conn.commit()


def is_milestone_notified(conn: psycopg.Connection, item_key: str, milestone_type: str) -> bool:
    """item_key is the prefixed id from Decision #2 above
    (f"assignment:{coursework_id}" / f"announcement:{announcement_id}")."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM notified_milestones WHERE coursework_id = %s AND milestone_type = %s",
            (item_key, milestone_type),
        )
        return cur.fetchone() is not None


def record_milestone_notified(conn: psycopg.Connection, course_id: str, item_key: str, milestone_type: str) -> None:
    """Idempotent — ON CONFLICT DO NOTHING, since a batched poll cycle may
    check the same item's dedup state more than once in edge cases (e.g.
    a retried job)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notified_milestones (course_id, coursework_id, milestone_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (coursework_id, milestone_type) DO NOTHING
            """,
            (course_id, item_key, milestone_type),
        )
    conn.commit()
```

No `schema.sql` changes — `email_checkpoint`/`notified_milestones` already
have every column needed (Decisions #1/#2 work within the existing shape).

---

## 3. `agent/graph/nodes/gmail.py` additions

Refactors `list_messages`' per-id fetch into a shared helper so the digest
path (fetch by known ids, no search query) can reuse it:

```python
def _get_message_metadata(gmail_service, message_id: str) -> dict:
    msg = (
        gmail_service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata",
             metadataHeaders=["From", "Subject", "Date"])
        .execute(num_retries=3)
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg["id"], "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""), "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
    }


def list_messages(gmail_service, query: str, max_results: int) -> list[dict]:
    response = (
        gmail_service.users().messages().list(userId="me", q=query, maxResults=max_results)
        .execute(num_retries=3)
    )
    return [_get_message_metadata(gmail_service, ref["id"])
            for ref in response.get("messages", [])]


def get_current_history_id(gmail_service) -> str:
    """Current historyId — used both to establish/reset the checkpoint
    baseline (Decision #1) and to advance it after a successful poll."""
    return gmail_service.users().getProfile(userId="me").execute(num_retries=3)["historyId"]


def get_new_message_ids(gmail_service, start_history_id: str) -> list[str] | None:
    """Message ids added since start_history_id (deduped — the same
    message can appear in multiple history records), or None if Gmail
    reports 404 (checkpoint too old/expired — see Decision #1; caller
    re-baselines instead of treating this as a failure)."""
    message_ids: set[str] = set()
    page_token = None
    try:
        while True:
            response = (
                gmail_service.users().history()
                .list(userId="me", startHistoryId=start_history_id,
                      historyTypes=["messageAdded"], pageToken=page_token)
                .execute(num_retries=3)
            )
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    message_ids.add(added["message"]["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        if e.resp.status == 404:
            return None
        raise
    return list(message_ids)


def get_messages_by_id(gmail_service, message_ids: list[str]) -> list[dict]:
    return [_get_message_metadata(gmail_service, mid) for mid in message_ids]
```

`gmail_node` (Phase 1's on-demand path) is otherwise unchanged — this is
additive.

---

## 4. `agent/graph/nodes/classroom.py` additions

```python
def list_announcements(classroom_service, courses: list[dict]) -> list[dict]:
    announcements: list[dict] = []
    for course in courses:
        page_token = None
        while True:
            response = (
                classroom_service.courses().announcements()
                .list(courseId=course["id"], announcementStates=["PUBLISHED"], pageToken=page_token)
                .execute(num_retries=3)
            )
            for a in response.get("announcements", []):
                announcements.append({"course": course, "announcement": a})
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return announcements
```

`_due_datetime`, `_is_missing`, and `list_assignments` are reused as-is
(Decision #3/#7) — `list_assignments`'s `"all"`/`"due_soon"`/`"missing"`
scopes cover posted/due-soon/overdue respectively; no new scope needed.

---

## 5. New: `agent/scheduler/jobs.py`

```python
"""Proactive polling jobs. Plain async functions, not LangGraph nodes —
scheduled by APScheduler in agent/main.py, sharing the same Google API
clients / Postgres pool / WhatsApp-send helper the graph nodes use. Never
triggers a write/approval-gated action — see product_definition.md's
Proactive vs on-demand section."""

import logging

from googleapiclient.errors import HttpError

from agent import google_auth
from agent.db import repo
from agent.graph.nodes.classroom import list_announcements, list_assignments, list_courses
from agent.graph.nodes.gmail import get_current_history_id, get_messages_by_id, get_new_message_ids
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.llm import summarize_emails

logger = logging.getLogger(__name__)

_DUE_SOON_WINDOW_HOURS = 48

# In-process outage-dedup flags — see Decision #4 (restart-resets is an
# accepted trade-off, same as the Scheduler decision already locked).
_gmail_outage = False
_classroom_outage = False


async def poll_gmail_job(pool, genai_client, gemini_model, whatsapp_access_token,
                          whatsapp_phone_number_id, my_whatsapp_number) -> None:
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
                return  # Decision #1: baseline only, nothing to report yet

            message_ids = get_new_message_ids(gmail_service, checkpoint)
            if message_ids is None:
                repo.save_email_checkpoint(conn, get_current_history_id(gmail_service))
                _gmail_outage = False
                return  # Decision #1: expired checkpoint, re-baseline

            new_history_id = get_current_history_id(gmail_service)
            if message_ids:
                messages = get_messages_by_id(gmail_service, message_ids)
                digest = summarize_emails(genai_client, gemini_model, messages)
                await send_whatsapp_message(
                    whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number,
                    f"{len(messages)} new email(s) since your last check:\n\n{digest}",
                )
            repo.save_email_checkpoint(conn, new_history_id)

        if _gmail_outage:
            await send_whatsapp_message(
                whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number,
                "Gmail checks are working again.",
            )
        _gmail_outage = False

    except Exception:
        logger.exception("poll_gmail_job failed")
        if not _gmail_outage:
            await send_whatsapp_message(
                whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number,
                "Couldn't check Gmail just now — will retry at the next scheduled check.",
            )
        _gmail_outage = True


def _format_milestones(entries: list[tuple[str, dict]]) -> str:
    labels = {"posted": "New assignment", "due_soon": "Due soon", "overdue": "Overdue",
              "announcement_posted": "New announcement"}
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


async def poll_classroom_job(pool, whatsapp_access_token, whatsapp_phone_number_id,
                              my_whatsapp_number) -> None:
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
                for item in list_assignments(classroom_service, courses, scope, window):
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
                whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number,
                "Classroom updates:\n\n" + _format_milestones(new_entries),
            )

        if _classroom_outage:
            await send_whatsapp_message(
                whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number,
                "Classroom checks are working again.",
            )
        _classroom_outage = False

    except Exception:
        logger.exception("poll_classroom_job failed")
        if not _classroom_outage:
            await send_whatsapp_message(
                whatsapp_access_token, whatsapp_phone_number_id, my_whatsapp_number,
                "Couldn't check Classroom just now — will retry at the next scheduled check.",
            )
        _classroom_outage = True
```

Notes:
- Both jobs call `record_milestone_notified`/`save_email_checkpoint`
  *inside* the `with pool.connection()` block, **before** the WhatsApp
  send. If the send itself throws, the milestone is already marked
  notified and won't retry next cycle. **This is a deliberate trade-off,
  not an oversight**: the
  alternative (send first, record after) risks the opposite failure —
  Gmail/Classroom succeeds, the message goes out, then a Postgres blip
  loses the record and the same item gets re-sent next cycle. Given
  WhatsApp send failures should be rare and Postgres blips likewise, this
  spec picks "never duplicate" over "never silently lose one" — flagging
  for the user's review since the docs don't state a preference.
- Existing transient-error retry (`.execute(num_retries=3)`) is already
  used by every Google API call these jobs make (reused from Phase 1's
  `gmail.py`/`classroom.py`, nothing new here) — satisfies the product
  doc's "auto-retried with exponential backoff, up to 3 attempts" for
  transient errors without new retry code. Auth errors surface immediately
  via `load_google_clients`' existing string-return path (turned into a
  `RuntimeError` here so both jobs' outer `except Exception` catches it
  the same way as any other poll-cycle failure — the message worded the
  same either way, "couldn't check X," since a poll failure notification
  isn't the place to walk the user through re-authenticating; that already
  happens on the next on-demand command per Phase 1's existing behavior).

---

## 6. `agent/main.py` changes

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.scheduler.jobs import poll_classroom_job, poll_gmail_job

_POLL_HOURS = "8,20"  # Decision #6 — 08:00/20:00 local time, hardcoded
```

Inside `lifespan`, after `app.state.background_tasks` is set up and the
crash-recovery scan runs, before `yield`:

```python
scheduler = AsyncIOScheduler()
scheduler.add_job(
    poll_gmail_job, CronTrigger(hour=_POLL_HOURS),
    args=[pool, app.state.genai_client, settings.gemini_model,
          settings.meta_whatsapp_access_token, settings.meta_whatsapp_phone_number_id,
          settings.my_whatsapp_number],
)
scheduler.add_job(
    poll_classroom_job, CronTrigger(hour=_POLL_HOURS),
    args=[pool, settings.meta_whatsapp_access_token, settings.meta_whatsapp_phone_number_id,
          settings.my_whatsapp_number],
)
scheduler.start()
app.state.scheduler = scheduler
```

After `yield` (teardown, alongside the existing `pool.close()`):

```python
scheduler.shutdown(wait=False)
```

No `agent/config.py` changes (Decision #6 — no new setting).

---

## 7. New: `agent/scheduler/__init__.py`

Empty — just makes `agent/scheduler/` a package, matching the module
layout already documented in `docs/product_definition.md`.

---

## 8. Out of scope (restated from the plan)

Any new write/approval-gated action — this phase only adds notifications on
top of the drafting/submission flow already built in Phases 2–3. No
LangGraph changes (`router_graph.py`, `assignment_graph.py`,
`email_graph.py` untouched). No new Google OAuth scopes — Gmail history and
Classroom announcements are both already covered by the scopes granted in
Phase 0 (`gmail.readonly`, `classroom.announcements.readonly`).

---

## 9. Acceptance criteria mapping

| Acceptance criterion (`implementation_plan.md`) | Satisfied by |
|---|---|
| A new assignment posted produces exactly one "posted" notification, never repeating | §5 `poll_classroom_job`'s `"posted"`/`"all"` branch + `repo.is_milestone_notified`/`record_milestone_notified` dedup (§2) |
| An assignment due within 48h produces exactly one "due soon" notification, regardless of later polls before the due date | Same dedup mechanism, `"due_soon"` branch reusing `list_assignments(scope="due_soon", window_hours=48)` |
| New emails since the last check arrive as a single batched digest; previously-seen unread backlog is never re-reported | §5 `poll_gmail_job` — one `send_whatsapp_message` call per cycle when `message_ids` is non-empty; Decision #1's checkpoint-only-forward semantics |
| Simulating an outage across two consecutive poll cycles produces exactly one failure notification, followed by a recovery notice once checks succeed | §5 `_gmail_outage`/`_classroom_outage` flags (Decision #4) — only sent on the *first* failure after a success, and only a recovery notice on the *first* success after a failure |
| No proactively-sent message ever requests approval or triggers a write action on its own | §5/§6 — both jobs only ever call `send_whatsapp_message`; neither touches `pending_items`, an `interrupt()`, or any graph |

---

## 10. Files changed/created — summary

| File | Change |
|---|---|
| `agent/db/repo.py` | Add `get_email_checkpoint`, `save_email_checkpoint`, `is_milestone_notified`, `record_milestone_notified` |
| `agent/graph/nodes/gmail.py` | Refactor `list_messages` to share `_get_message_metadata`; add `get_current_history_id`, `get_new_message_ids`, `get_messages_by_id` |
| `agent/graph/nodes/classroom.py` | Add `list_announcements`; `list_assignments`/`_is_missing`/`list_courses` reused unchanged |
| `agent/scheduler/__init__.py` | New — empty package init |
| `agent/scheduler/jobs.py` | New — `poll_gmail_job`, `poll_classroom_job`, outage-dedup flags |
| `agent/main.py` | Start/stop an `AsyncIOScheduler` in `lifespan`, register both jobs on a twice-daily `CronTrigger` |

No changes to `agent/db/schema.sql`, `agent/config.py`, `agent/llm.py`
(`summarize_emails` reused as-is), `agent/graph/router_graph.py`,
`agent/graph/assignment_graph.py`, or `agent/graph/email_graph.py`.

`apscheduler` is already declared in `pyproject.toml` (added in Phase 0,
unused until now) — no dependency changes needed.
