# Spec: Tool-calling answers for read questions

## Context

This isn't one of the phases in `docs/implementation_plan.md` — it's a
refactor of part of Phase 1's design, prompted by real bugs hit during live
testing of the already-implemented app:

- "What's the due date of [named assignment]?" and "How many assignments do
  I have?" were both answered from a hardcoded 48-hour "due soon" window,
  because that's `classify_intent`'s stated default whenever the user
  doesn't explicitly say "everything"/"all assignments" — so a real
  assignment due in 2 weeks was silently omitted from the answer.
- "What's the latest announcement in [course]?" was misclassified as
  `search_emails` and answered from Gmail search results — there is a
  working `classroom.list_announcements()` function, but no intent ever
  routes to it.

Root cause: `classify_intent` maps free text into a fixed enum of intents,
each wired to a fixed-parameter API call and a canned reply formatter
(`format_assignments_reply`, `_format_email_list`, etc). This is brittle by
construction — every new *phrasing* of a read question needs its own
intent/parameter combination, and gaps (like the announcements case) are
invisible until someone asks that exact question.

## Objective

Replace fixed-intent-classification + canned-formatter handling for
**read-only questions** (courses, assignments, announcements, email) with a
bounded Gemini tool-calling loop: the model calls real data-fetching
functions, gets raw results back, and writes the final WhatsApp reply
itself — tailored to whatever was actually asked, not a template.

**Writes stay exactly as they are.** `work_on_assignment` and
`respond_to_pending` are not free-form Q&A — they're state-machine
transitions (start a background assignment-graph run, resume a paused
thread) that need structured extraction and deterministic routing. Nothing
about their classification, their approval-gated flow, or the assignment
graph itself changes in this spec. Proactive polling (`agent/scheduler/jobs.py`)
also calls `classroom.list_assignments`/`gmail.list_messages` directly, not
through `classify_intent` — also unaffected.

## Design

### 1. `classify_intent` narrows to routing, not data-shaping

`agent/llm.py`'s `route_message` function declaration currently has 7
possible intents and Classroom/Gmail-specific args (`due_scope`,
`due_window_hours`, `email_count`, `email_sender`, `email_subject`,
`email_label`). It collapses to 4 intents with only the args the
*remaining* two stateful intents need:

```python
ROUTE_MESSAGE_DECLARATION = types.FunctionDeclaration(
    name="route_message",
    description="Classify an inbound WhatsApp message.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "intent": types.Schema(
                type="STRING",
                enum=[
                    "answer_question",
                    "work_on_assignment",
                    "respond_to_pending",
                    "unrecognized",
                ],
            ),
            "assignment_reference": types.Schema(
                type="STRING",
                description="only for work_on_assignment; the free-text name/description the user gave",
            ),
            "pending_item_reference": types.Schema(
                type="STRING",
                description="only for respond_to_pending, if a specific item was named",
            ),
        },
        required=["intent"],
    ),
)
```

`CLASSIFY_SYSTEM_PROMPT` is rewritten to match:

```python
CLASSIFY_SYSTEM_PROMPT = """\
You classify one inbound WhatsApp message into exactly one category by
calling route_message. Categories:

- answer_question: the user is asking about their Classroom courses,
  assignments, announcements, or email in any form — listing, counting,
  a specific item's details, a summary, a search. Anything read-only.
- work_on_assignment: the user wants to start drafting/researching a
  specific assignment (e.g. "work on the bio essay", "start the AI
  assignment"). Extract the free-text name/description they used into
  assignment_reference. This is the ONLY intent that leads to eventually
  writing/drafting anything — still just resolves which assignment is
  meant at this stage, does not draft anything itself.
- respond_to_pending: the user is making a decision about a draft or
  pending item they were previously shown — approving it, asking for
  changes, rejecting it, or answering a submit yes/no question — whether
  or not they name which one. Includes replies like "yes", "looks good",
  "make it shorter", "no", "reject it", "approve the bio essay". If they
  name a specific course/assignment, extract it into
  pending_item_reference; leave it unset if they didn't name one.
- unrecognized: anything that isn't clearly one of the above. This
  includes any request to send, submit, reply to, or turn in something
  that isn't a reply to a pending item — those are not supported yet and
  must be classified as unrecognized, never routed to answer_question.

Always call route_message exactly once with your best classification.
"""
```

### 2. New `agent/llm.py` function: `answer_question`

```python
ANSWER_SYSTEM_PROMPT = """\
You answer one inbound WhatsApp message about the user's Gmail and Google
Classroom by calling the available tools to fetch real data, then writing
a short, direct reply. Rules:
- Always call at least one tool before answering — never guess or use
  outside knowledge about the user's courses, assignments, or email.
- Call as many tools as you need to fully answer, including calling the
  same tool again with different arguments.
- If a tool result notes it couldn't check something (e.g. a course
  returned an error), mention that limitation briefly in your answer
  rather than silently ignoring it or treating the data as complete.
- If nothing in the tool results answers the question, say so plainly —
  do not fabricate an answer.
- Keep the reply concise and in plain text formatted for WhatsApp — short
  lines, no markdown headers, no asterisk bullets (use "-").
"""


def answer_question(
    client: genai.Client,
    model: str,
    text: str,
    tools: list[Callable],
    max_remote_calls: int = 4,
) -> str:
    """Runs Gemini's automatic function-calling loop (SDK-managed — passing
    plain Python callables as `tools` makes generate_content execute them
    itself and loop until a final text answer, bounded by
    automatic_function_calling.maximum_remote_calls) to answer a free-text
    question. Raises after 3 failed attempts (network/5xx/429, via
    _with_retry) or RuntimeError if the model exhausts max_remote_calls
    without producing a final text answer."""

    def call():
        return client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=ANSWER_SYSTEM_PROMPT,
                tools=tools,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=max_remote_calls,
                ),
            ),
        )

    response = _with_retry(call)
    if not response.text:
        raise RuntimeError("answer_question: model produced no final text")
    return response.text
```

`max_remote_calls=4` is a deliberate cap (Decision #1, below) — enough for
"list courses, then list that course's announcements" (2 calls) plus
headroom, without letting a confused loop run away on cost/latency.

**Tool functions must never raise.** The spec doesn't rely on the SDK's own
exception handling for automatically-called functions (unverified against
the installed version's exact behavior) — every tool function defined in
step 3 catches its own `HttpError` internally and returns an error
dict/note instead of raising, matching this codebase's existing convention
of catching Google API errors at the boundary rather than propagating them.

### 3. New file: `agent/graph/nodes/answer_question.py`

Houses the tool functions (closures over the request's own service clients)
and the node. Each tool is a plain function with type hints and a docstring
— the SDK derives the function-calling schema from both, so the docstrings
below are the actual prompt content the model sees, not just comments.

```python
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
        enrolled in or teaches, as a list of {"name": str}. Call this
        first if you need to know what courses exist or need a course
        name to pass to get_announcements."""
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
        max_results: int = 10,
        unread_only: bool = False,
        sender: str | None = None,
        subject_contains: str | None = None,
        after_date: str | None = None,
        before_date: str | None = None,
    ) -> list[dict]:
        """Returns Gmail messages (not Classroom). Use sender/subject_contains
        when the question names a specific sender or topic; use
        unread_only=True for "unread"/"new" email questions. Use
        after_date/before_date (each "YYYY-MM-DD", in the user's own
        timezone) to scope to a specific day or range instead of guessing
        from a message's own date field — after_date is inclusive,
        before_date is exclusive, so "yesterday" is
        after_date=<yesterday>, before_date=<today>, and "today" is
        after_date=<today> with before_date left unset. Pass a higher
        max_results (e.g. 25) whenever after_date/before_date is set, so a
        busy day isn't silently truncated. Leave all filters unset for a
        general "recent emails" question. Each item is {"from": str,
        "subject": str, "date": str, "snippet": str} — the snippet is a
        short excerpt, not the full body."""
        query_args = {
            "email_sender": sender,
            "email_subject": subject_contains,
            "after_date": after_date,
            "before_date": before_date,
        }
        query = gmail.build_query(query_args, unread_only=unread_only)
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
```

Note `get_recent_emails` reuses `gmail.build_query`, which expects
`intent_args`-shaped keys (`email_sender`/`email_subject`) — passing a
dict with those two keys directly is the minimal-diff way to reuse it
as-is rather than duplicating query-building logic.

`build_query` also accepts `after_date`/`before_date` (each `"YYYY-MM-DD"`)
and converts each to the Unix timestamp of that date's midnight in
`agent.config.USER_TIMEZONE` (`agent/graph/nodes/gmail.py`'s
`_date_to_epoch_seconds`) before appending Gmail's `after:`/`before:`
operators. Gmail's search accepts either a `YYYY/MM/DD` date or a Unix
timestamp for these operators — the timestamp form is used because the
day-string form's timezone handling is undocumented, while a computed
epoch boundary is exact and unambiguous. This — together with injecting
`datetime.now(USER_TIMEZONE)` instead of UTC as "today" in
`ANSWER_SYSTEM_PROMPT` — fixes a real bug where, for a user in Pakistan
(UTC+5), any question asked between midnight and 5am local time computed
"today" as the previous UTC day.

### 4. `agent/graph/router_graph.py` changes

- Remove `"classroom_node": "classroom_node", "gmail_node": "gmail_node"`
  entries from `route_after_classify`'s dict; replace with
  `"answer_question": "answer_question_node"`.
- Remove the `classroom_node`/`gmail_node` imports; add
  `from agent.graph.nodes.answer_question import answer_question_node`.
- In `build_router_graph`: replace `g.add_node("classroom_node", ...)` and
  `g.add_node("gmail_node", ...)` with
  `g.add_node("answer_question_node", answer_question_node)`; update the
  conditional-edge target dict and the `for node_name in (...)` tuple that
  wires every handler to `send_reply` accordingly.
- `RouterState.intent`'s docstring comment (line 11 of `agent/graph/state.py`)
  updates to: `one of: answer_question | work_on_assignment | respond_to_pending | unrecognized`.

### 5. Dead code removed from `agent/graph/nodes/classroom.py` and `gmail.py`

Once nothing routes to them, these become unused and are deleted:
`classroom_node`, `format_courses_reply`, `format_assignments_reply`,
`gmail_node`, `_describe_filter`, `_format_email_list`.

**Kept, unchanged** — still used by `resolve_assignment_node`,
`agent/scheduler/jobs.py`'s proactive polling, and the new tool functions
above: `list_courses`, `list_assignments`, `list_announcements`,
`find_assignment_candidates`, `_due_datetime`, `_is_missing`,
`list_messages`, `build_query`, `_get_message_metadata`,
`get_current_history_id`, `get_new_message_ids`, `get_messages_by_id`,
`summarize_emails` (the Gemini digest helper in `llm.py`, still used by
`poll_gmail_job`'s proactive digest — untouched by this spec).

## Decisions

1. **`max_remote_calls=4`.** Enough headroom for realistic multi-step
   questions (e.g. resolve a course name, then fetch its announcements)
   without an unbounded loop. If real usage shows 4 is too tight, this is
   a one-line change — not asserted as final, same spirit as the existing
   fuzzy-match thresholds in `router_graph.py`.
2. **Gmail tools keep light filter params (sender/subject/unread); Classroom
   tools fetch everything and let the model filter.** Asymmetric on
   purpose: an inbox can be large and unbounded, so letting the model
   choose Gmail's own search-style filters keeps each call cheap and
   Gmail's query syntax is something Gemini already models well from
   training. Classroom's course/assignment lists are small and bounded per
   user, so fetching everything and filtering in the model's final answer
   is cheap and — critically — is exactly what avoids re-creating the
   original bug (a rigid enum silently excluding data the user actually
   wanted).
3. **Tool functions catch their own `HttpError` and return an error
   payload instead of raising.** Doesn't rely on unverified SDK behavior
   for exceptions raised inside automatically-called functions; keeps
   error handling explicit and matches this codebase's existing
   "catch Google API errors at the boundary" convention.
4. **`get_missing_assignments` stays a separate tool from
   `get_all_assignments`**, not a filter over it. "Missing" requires one
   extra `studentSubmissions.list` call per overdue assignment
   (`_is_missing`) — meaningfully more expensive than a plain listing — so
   it should only run when the question actually needs it, not on every
   assignment question.
5. **`classify_intent` still exists and still does coarse routing.** This
   spec does not turn the whole router into one big tool-calling agent —
   `work_on_assignment`/`respond_to_pending` still need a fast, cheap,
   structured classification because they trigger side effects
   (dispatching a background graph run), not just an answer. Only the
   *read* path moves to tool-calling.

## Error handling summary

| Failure | Behavior |
|---|---|
| Gemini transient error (429/5xx/network), all 3 retries exhausted | `answer_question_node` catches, replies "Couldn't process that message right now — please try again." (identical to today's `classify_intent_node` fallback) |
| Model exhausts `max_remote_calls` without final text | `answer_question` raises `RuntimeError`, same fallback as above |
| A tool's underlying Google API call fails (`HttpError`) | Caught inside the tool itself, returned as `{"error": ...}` (or per-item `unavailable_courses` for the assignment tools) so the model can mention the limitation in its reply — matches the resilience already built into `classroom.list_assignments` this session |
| `google_auth.load_google_clients` returns a string (no/expired credentials) | Same as today — `answer_question_node` returns that string directly as `reply_text`, no tool loop attempted |
| `get_announcements(course_name=...)` doesn't match any course | Tool returns `{"error": "No course matching ..."}", model relays that rather than silently returning nothing |

No new Postgres tables/columns — this is entirely in-process LLM/API
orchestration, no new persisted state.

## Acceptance criteria

- "What's the due date of Design and Analysis of Algorithms assignment?"
  returns that assignment's actual due date regardless of how far out it
  is (not silently filtered by a 48h window).
- "How many assignments do I have?" reflects a count across all
  assignments, not just those due within 48 hours.
- "What's the latest announcement in [course]?" returns an actual
  Classroom announcement (via `get_announcements`), not Gmail search
  results.
- "Summarize my unread emails" / "emails from X" continue to work,
  answered in the model's own words from `get_recent_emails` data.
- A Classroom permission error scoped to one course (as hit this session)
  is mentioned as a limitation in the reply rather than blocking data from
  other courses, or crashing the turn.
- `work_on_assignment` and `respond_to_pending` behave identically to
  before this change — same classification prompt content for those two,
  same downstream flow, no regression.
- `.venv/bin/python -c "import agent.main"` succeeds with no import errors
  after `classroom_node`/`gmail_node` and their now-unused helpers are
  removed.

## Out of scope

- The email-draft graph (`email_graph.py`) — still an empty scaffold,
  untouched.
- `agent/scheduler/jobs.py` proactive polling — still calls
  `classroom.list_assignments`/`gmail.list_messages` directly, unaffected.
- `resolve_assignment_node`'s fuzzy-match flow for "work on X" — unaffected.
- Any change to the assignment graph's draft/review/submit flow.
- Widening `work_on_assignment`/`respond_to_pending` to also be
  tool-calling — deliberately kept deterministic (Decision #5).
