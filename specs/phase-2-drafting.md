# Phase 2 — Drafting — Spec

Source: `docs/implementation_plan.md` ("Phase 2"), `docs/product_definition.md`
(In-scope Assignment drafting, Assignment material ingestion, Claude Code
interaction contract, Local workspace decision, Permission matrix,
Architecture), `docs/database_schema.md`, `specs/phase-1-read-only-foundation.md`.

## Decisions made for this phase (not fully pinned down by the docs above —
confirmed with the user before writing this spec)

1. **Ingestion failures abort and report, with no retry/skip/abort loop.**
   `docs/implementation_plan.md`'s Phase 2 core requirements literally say a
   failed attachment should prompt retry/skip/abort, but its own out-of-scope
   list excludes `interrupt()`-based pausing — and a retry/skip/abort prompt
   is inherently a pause-and-wait-for-a-reply interaction, so the two lines
   contradicted each other. **Resolved**: on any attachment
   download/conversion failure, the agent stops before invoking Claude Code,
   sends one message naming exactly which file failed and why, and the
   assignment attempt ends there — no pending state, no interrupt. To retry,
   the user sends a fresh "work on `<assignment>`" message, which re-runs
   ingestion from scratch (including files that succeeded before). This
   keeps Phase 2 fully free of `interrupt()`/pause machinery, matching the
   out-of-scope list; `implementation_plan.md`'s Phase 2 core requirements
   bullet should be corrected to drop "retry/skip/abort" language to match.
   Materials Classroom exposes that aren't file attachments at all (a link,
   a YouTube video, a Form) are **not** a failure — they're simply
   unsupported by design (per `docs/product_definition.md`'s ingestion
   section) and are just noted in the summary sent alongside a successful
   draft, never triggering the abort path.
2. **Draft relay uses a real WhatsApp document attachment, not inline text.**
   `draft.md` can easily exceed WhatsApp's ~4096-character text message
   limit and Markdown doesn't render in WhatsApp anyway. **Resolved**: the
   agent uploads `draft.md` to Meta's Graph API as media and sends it as a
   document message, followed by a separate short text message with Claude
   Code's summary note (sources, assumptions, anything inaccessible). This
   requires adding a media-upload capability to
   `agent/graph/nodes/whatsapp_send.py`, which Phase 1 didn't need.
3. **Starting an assignment is always a two-stage, reply-threaded
   confirmation flow — not a single-shot "yes/no if ambiguous" check.**
   - Stage 1 — **resolve**: the router classifier gets a new intent,
     `work_on_assignment`, extracting the free-text name the user gave
     (e.g. "the bio essay"). The agent fetches a live list of the user's
     courses + assignments (reusing Phase 1's `list_courses`/
     `list_assignments`) and fuzzy-matches the given text against real
     titles.
     - One confident match → go straight to stage 2.
     - Several close matches → send a numbered disambiguation question
       listing the candidates (course + title + due date).
     - No plausible match → tell the user nothing matched, no pending state.
   - Stage 2 — **confirm**: regardless of how confidently stage 1 resolved,
     the agent always asks an explicit yes/no confirmation before starting
     anything (e.g. "Should I start the Intro to AI assignment (due
     `<date>`, `<course>`)?"). Only an affirmative reply actually kicks off
     ingestion.
   - **Both stage-1 disambiguation and stage-2 confirmation are answered
     exclusively via WhatsApp's native reply-to-message feature.** The
     agent records the WhatsApp message ID of whichever question it just
     asked. The very next inbound message is checked: if its payload's
     `context.id` matches that recorded message ID, it's parsed as the
     answer (free text, not fixed keywords — same "parse for intent" style
     already locked in elsewhere in the product). If it doesn't match (no
     `context.id` at all, or it's a reply to something else, or it's a
     fresh message), the pending question is **silently abandoned** — no
     "did you still want that?" nagging — and the message is instead run
     through the normal intent classifier as an independent command. This
     avoids a second free-text "is this even about my pending question"
     classification step and reuses a mechanism `docs/product_definition.md`
     already documents (reply-to-message routing) as the unambiguous,
     preferred way to resolve "which pending thing are you replying to."
   - This whole mechanism lives entirely in the **router thread's own
     persisted state** (a `pending_question` field on `RouterState`,
     carried across separate invocations of the same `thread_id` by the
     Postgres checkpointer) — no `interrupt()`, no new Postgres table. Only
     one assignment flow can have a pending question at a time; starting a
     second "work on `<X>`" while one is still pending overwrites nothing —
     see §6.3 for the exact behavior.
4. **Ingestion + Claude Code drafting run as a background task, not inline
   in the webhook request.** Not directly addressed by the product docs, but
   forced by two things already locked in: Meta's webhook expects a fast
   response (a Claude Code run doing research + drafting can take minutes,
   far past any reasonable webhook timeout — Phase 1's spec explicitly
   flagged this as something to revisit here), and the "Drafting
   concurrency" decision already requires a second request to *queue*
   rather than block the requester. **Resolved**: once the user confirms
   (stage 2 above), the router graph replies immediately ("Starting on
   `<assignment>` — I'll send the draft when it's ready") and schedules the
   actual ingest-then-draft work as an `asyncio.Task` that runs
   independently of the webhook request/response cycle, sending the draft
   (or a failure report) as its own later WhatsApp message when done. This
   is an implementation necessity, not a product-shape choice, so it's
   recorded here rather than asked about.
5. **Two implementation details need verifying against the actually
   installed versions at build time, not asserted here with false
   confidence**: (a) whether the Drive v3 export endpoint supports
   `text/markdown` directly as an export `mimeType` for native Google Docs
   (if not, export `text/html` or `text/plain` instead and convert via
   Pandoc, already a dependency — a mechanical substitution, not a behavior
   change); (b) the exact Claude Code CLI flag names for restricting tool
   access (`Read`/`Write`/`WebSearch`/`WebFetch` only, no `Bash`) and for
   emitting the session ID on a completed run (`--output-format json`
   assumed here). §5 and §7 below use best-current-knowledge flag names —
   confirm against `claude --help` on the installed CLI before implementing.

---

## 1. Objective (restated from the plan)

Add the research + Claude Code drafting node for assignments; surface drafts
in WhatsApp for review. No approval/revision loop and no submission yet — a
draft is generated once and sent.

## 2. New files / modules

```
agent/
  llm.py                            # extended — new intent, two small confirm/disambiguate helpers
  google_auth.py                    # extended — add Drive client
  workspace/
    paths.py                        # NEW — ~/agent-workspace/<course>/<assignment>/ helpers
  graph/
    state.py                        # extended — reply_to_message_id, pending_question fields
    router_graph.py                 # extended — entry routing, resolve_assignment_node, confirmation handling
    assignment_graph.py             # NEW — per-assignment graph: ingest -> draft -> save session -> relay
    nodes/
      classroom.py                  # extended — find_assignment_candidates() fuzzy matcher
      drive.py                      # NEW — download/export/convert one attachment by mimeType
      ingestion.py                  # NEW — assembles source-material/, fail-fast on real failures
      claude_code.py                # NEW — headless subprocess wrapper, process-wide asyncio.Lock
      whatsapp_send.py              # extended — send_whatsapp_document() (media upload + send)
  db/
    repo.py                         # extended — save_claude_session()
  main.py                           # extended — build assignment graph at startup, background-task registry
```

No new tables — `claude_sessions` already exists (Phase 0's schema, unused
until now). `agent/graph/email_graph.py`, `agent/graph/nodes/claude_code.py`'s
`--resume` path, `agent/scheduler/` stay untouched/unused this phase.

---

## 3. `agent/workspace/paths.py`

```python
def slugify(name: str) -> str:
    """Lowercases, replaces whitespace with '-', strips anything outside
    [a-z0-9-]. Used for both course and assignment names so folder names are
    always filesystem-safe regardless of what Classroom returns."""

def assignment_dir(course_name: str, assignment_name: str) -> Path:
    """Returns ~/agent-workspace/<slugify(course_name)>/<slugify(assignment_name)>/,
    creating it (and its source-material/ subdirectory) if missing.
    Idempotent — calling it again for the same names returns the same path
    without wiping existing contents, since folders are persistent per the
    locked "Local workspace" decision."""
```

---

## 4. `agent/google_auth.py` additions

```python
def build_drive_client(creds: Credentials):
    """googleapiclient.discovery.build("drive", "v3", credentials=creds)"""
```

`get_google_clients`/`load_google_clients` (from Phase 1) now return a
3-tuple `(gmail_service, classroom_service, drive_service)` instead of 2.
`gmail_node`/`classroom_node` (Phase 1) are updated to unpack 3 values and
ignore the one they don't use — trivial signature-consistency change, no
behavior change to either.

---

## 5. `agent/graph/nodes/drive.py` (new)

```python
NATIVE_EXPORT_MIMETYPES = {
    "application/vnd.google-apps.document": "text/markdown",   # verify availability — see §0.5; fall back to text/html + pandoc if unsupported
    "application/vnd.google-apps.presentation": "text/markdown",
    "application/vnd.google-apps.spreadsheet": "text/markdown",
}
PANDOC_CONVERTIBLE_MIMETYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.oasis.opendocument.text",                                   # .odt
    "application/rtf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
}
PASSTHROUGH_PREFIXES = ("application/pdf", "image/")

def resolve_attachment(drive_service, drive_file_id: str, dest_dir: Path) -> Path | None:
    """Fetches file metadata (name, mimeType) via drive.files().get(fileId=...).
    Branches:
      - mimeType in NATIVE_EXPORT_MIMETYPES: drive.files().export(fileId=...,
        mimeType=...).execute(num_retries=3), writes result bytes to
        dest_dir/<slugified name>.md.
      - mimeType in PANDOC_CONVERTIBLE_MIMETYPES: drive.files().get_media(
        fileId=...).execute(num_retries=3) to a temp file, then
        pypandoc.convert_file(tmp_path, "md", outputfile=dest_dir/<name>.md).
      - mimeType startswith PASSTHROUGH_PREFIXES: get_media(...) written as-is
        to dest_dir/<original filename>.
      - anything else: returns None (caller treats as "unsupported, note in
        summary, not a failure" per §0.1).
    Raises on any Drive HttpError or Pandoc conversion error — caller (ingestion.py)
    catches this to trigger the fail-fast abort path, since these ARE real
    failures of an attachment Classroom did mark as a real file."""
```

---

## 6. Router graph changes (`agent/graph/state.py`, `agent/llm.py`, `agent/graph/router_graph.py`)

### 6.1 State (`agent/graph/state.py`)

```python
class RouterState(TypedDict, total=False):
    inbound_text: str
    whatsapp_message_id: str
    sender: str
    reply_to_message_id: str | None   # NEW — Meta's context.id for this inbound message, if any
    intent: str
    intent_args: dict[str, Any]
    reply_text: str
    pending_question: dict | None     # NEW — see shape below; persists across invocations via checkpointer
```

`pending_question` shape (set by `resolve_assignment_node`/disambiguation
handling, filled in and persisted by `send_reply_node`):
```python
{
    "kind": "disambiguate_assignment" | "confirm_start_assignment",
    "message_id": str,          # the WhatsApp id of the question message — filled in by send_reply_node
    "candidates": [              # only for "disambiguate_assignment"
        {"course_id": str, "course_name": str, "coursework_id": str,
         "title": str, "due": str | None},
        ...
    ],
    "resolved": {                # only for "confirm_start_assignment"
        "course_id": str, "course_name": str,
        "coursework_id": str, "title": str,
    },
}
```

### 6.2 `agent/webhook/routes.py` change

`receive_webhook` additionally extracts
`message.get("context", {}).get("id")` and passes it into the graph input as
`"reply_to_message_id"`.

### 6.3 Entry routing (`agent/graph/router_graph.py`)

The graph's entry point changes from Phase 1's direct `classify_intent` to a
new `route_entry_node`:

```python
def route_entry_node(state: RouterState) -> dict:
    """If state.get("pending_question") is set AND
    state.get("reply_to_message_id") == pending_question["message_id"]:
    leaves pending_question untouched (the next node consumes it) and signals
    "answering_pending" via a routing decision (see conditional edge below).
    Otherwise: clears pending_question to None (silent abandonment — no
    acknowledgement message) and signals "fresh_command"."""

def route_after_entry(state: RouterState) -> str:
    pq = state.get("pending_question")
    if pq is None:
        return "classify_intent"
    return {
        "disambiguate_assignment": "handle_disambiguation",
        "confirm_start_assignment": "handle_confirmation",
    }[pq["kind"]]
```

If the user starts a *second* "work on `<Y>`" while a `pending_question` is
already set for `<X>` and this new message isn't a reply to that pending
question's message (the normal case — a fresh message has no `context.id`
matching it), `route_entry_node` abandons `<X>`'s pending question exactly
as it would for any unrelated message, and `<Y>` is classified and resolved
normally. There is deliberately no "still waiting on X" nudge — matches the
"no follow-up questioning" tone already established for rejection elsewhere.

### 6.4 `agent/llm.py` additions

Extend `CLASSIFY_SYSTEM_PROMPT` and `ROUTE_MESSAGE_DECLARATION`'s `intent`
enum with a 6th value:
```
- work_on_assignment: the user wants to start drafting/researching a specific
  assignment (e.g. "work on the bio essay", "start the AI assignment",
  "draft the technical writing paper"). Extract the free-text name/description
  they used into assignment_reference. This is the ONLY intent that leads to
  eventually writing/drafting anything — still just resolves which assignment
  is meant at this stage, does not draft anything itself.
```
New schema property: `assignment_reference: types.Schema(type="STRING", description="only for work_on_assignment; the free-text name/description the user gave")`.

Two new small helpers, same forced-function-call + `_with_retry` pattern as
`classify_intent`:

```python
CONFIRM_DECLARATION = types.FunctionDeclaration(
    name="record_confirmation",
    description="Classify a reply to a yes/no confirmation question.",
    parameters=types.Schema(type="OBJECT", properties={
        "answer": types.Schema(type="STRING", enum=["confirm", "decline"]),
    }, required=["answer"]),
)

def parse_confirmation_reply(client, model, question: str, reply_text: str) -> str:
    """Single forced-function-call asking the model to read `question` (the
    question the agent asked) and `reply_text` (the user's reply) and decide
    confirm vs decline. An unclear/off-topic reply that still arrived via
    reply-to-message (so we know it's meant to answer this) is treated by
    the model as its best-guess decline — no separate "unclear" branch,
    keeping this consistent with the "reject ends things silently, no
    interrogation" tone. Returns "confirm" or "decline"."""

DISAMBIGUATE_DECLARATION = types.FunctionDeclaration(
    name="record_choice",
    description="Classify which numbered candidate a reply refers to, or none.",
    parameters=types.Schema(type="OBJECT", properties={
        "choice": types.Schema(type="INTEGER", description="1-based index into the candidate list, or 0 if unclear/none match"),
    }, required=["choice"]),
)

def resolve_disambiguation(client, model, candidates: list[dict], reply_text: str) -> int:
    """Single forced-function-call: given the numbered candidate list (course
    + title + due date per candidate, same text the user was shown) and the
    user's reply, returns the 1-based index they meant, or 0 if unclear.
    Both helpers use the same _with_retry wrapper as classify_intent/summarize_emails."""
```

### 6.5 New nodes

```python
def resolve_assignment_node(state: RouterState, config: RunnableConfig) -> dict:
    """Triggered for intent == "work_on_assignment". Loads Google clients,
    fetches list_courses() + list_assignments(scope="all") (Phase 1
    functions, reused as-is), calls
    classroom.find_assignment_candidates(courses, assignments,
    state["intent_args"]["assignment_reference"]) (see §6.6), then:
      - zero candidates: reply_text = "I couldn't find an assignment
        matching that — try a more specific name, or ask 'what's due' to
        see the list." No pending_question set.
      - one confident candidate: reply_text = a yes/no question naming the
        assignment/course/due-date; sets pending_question =
        {"kind": "confirm_start_assignment", "message_id": None,
         "resolved": {...}}.
      - multiple close candidates (up to 4 shown): reply_text = a numbered
        list + "which one did you mean?"; sets pending_question =
        {"kind": "disambiguate_assignment", "message_id": None,
         "candidates": [...]}.
    Same HttpError handling as classroom_node/gmail_node (§7.4/7.5 of
    Phase 1's spec) if the Classroom fetch itself fails."""

def handle_confirmation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "confirm_start_assignment". Calls
    llm.parse_confirmation_reply(genai_client, model, state["reply_text_of_the_question"], state["inbound_text"]).
    (The question text itself is reconstructed from pending_question["resolved"]
    rather than stored verbatim, to keep the state dict small.)
      - "confirm": schedules the background assignment run (§8), reply_text
        = "Starting on <title> — I'll send the draft when it's ready.",
        clears pending_question.
      - "decline": reply_text = "Okay, not starting that.", clears
        pending_question."""

def handle_disambiguation_node(state: RouterState, config: RunnableConfig) -> dict:
    """pending_question["kind"] == "disambiguate_assignment". Calls
    llm.resolve_disambiguation(genai_client, model, pending_question["candidates"], state["inbound_text"]).
      - valid 1-based choice: replaces pending_question with a fresh
        "confirm_start_assignment" one for that candidate, reply_text = the
        stage-2 confirmation question (does NOT start anything yet — still
        requires the separate confirm step, per §0.3).
      - 0/unclear: reply_text = "Sorry, I couldn't tell which one you meant
        — try naming it differently.", clears pending_question."""
```

`send_reply_node` (existing, Phase 1) gets one addition: after a successful
send, if `state.get("pending_question")` is not `None` and its
`"message_id"` is still `None`, it's set to the just-sent message's id
(returned by `send_whatsapp_message`) before the state is returned — this is
what makes the pending question's `message_id` available on the *next*
invocation of this thread.

### 6.6 `agent/graph/nodes/classroom.py` addition

```python
def find_assignment_candidates(courses: list[dict], assignments_by_course: dict[str, list[dict]], reference_text: str) -> list[dict]:
    """Scores every assignment against reference_text using
    difflib.SequenceMatcher(None, reference_text.lower(),
    f"{course_name} {assignment_title}".lower()).ratio() (stdlib only — no
    new fuzzy-matching dependency, consistent with the project's minimal
    Tech Stack). Returns candidates sorted by score descending, each as
    {"course_id", "course_name", "coursework_id", "title", "due", "score"}.
    Caller applies the confident-single-match vs ambiguous-multi-match vs
    no-match thresholds (starting defaults: single confident match if
    best_score >= 0.6 and beats the second-best by >= 0.15; otherwise show
    up to the top 4 candidates scoring >= 0.35 for disambiguation; otherwise
    no match. These are tunable starting points, not asserted as final —
    adjust after real-world testing)."""
```

### 6.7 Graph assembly (`agent/graph/router_graph.py`)

```python
def build_router_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(RouterState)
    g.add_node("route_entry", route_entry_node)
    g.add_node("classify_intent", classify_intent_node)               # Phase 1, unchanged
    g.add_node("resolve_assignment", resolve_assignment_node)          # NEW
    g.add_node("handle_confirmation", handle_confirmation_node)        # NEW
    g.add_node("handle_disambiguation", handle_disambiguation_node)    # NEW
    g.add_node("classroom_node", classroom_node)                       # Phase 1, unchanged
    g.add_node("gmail_node", gmail_node)                                # Phase 1, unchanged
    g.add_node("fallback_node", fallback_node)                          # Phase 1, unchanged
    g.add_node("send_reply", send_reply_node)                           # Phase 1, extended per §6.5
    g.set_entry_point("route_entry")
    g.add_conditional_edges("route_entry", route_after_entry, {
        "classify_intent": "classify_intent",
        "handle_confirmation": "handle_confirmation",
        "handle_disambiguation": "handle_disambiguation",
    })
    g.add_conditional_edges("classify_intent", route_after_classify, {
        "classroom_node": "classroom_node",
        "gmail_node": "gmail_node",
        "fallback_node": "fallback_node",
        "resolve_assignment": "resolve_assignment",   # NEW branch for work_on_assignment
    })
    for n in ("classroom_node", "gmail_node", "fallback_node", "resolve_assignment",
              "handle_confirmation", "handle_disambiguation"):
        g.add_edge(n, "send_reply")
    g.add_edge("send_reply", END)
    return g.compile(checkpointer=checkpointer)
```

---

## 7. `agent/graph/nodes/ingestion.py` (new)

```python
def build_task_brief(courseWork: dict) -> str:
    """Formats courseWork["title"] + courseWork.get("description", "") into
    Markdown, always written as source-material/task-brief.md — the one
    guaranteed source, whether or not there are attachments."""

async def ingest_assignment(classroom_service, drive_service, course_id: str, coursework_id: str, dest_dir: Path) -> dict:
    """1. courseWork = classroom_service.courses().courseWork().get(
       courseId=course_id, id=coursework_id).execute(num_retries=3).
    2. Writes task-brief.md (build_task_brief).
    3. For each entry in courseWork.get("materials", []):
         - if it has a "driveFile" key: calls drive.resolve_attachment(...)
           for that file. On success with a path, note it as ingested. On
           success returning None (unsupported mimeType), note it as
           "unsupported, skipped" (informational, not a failure). On any
           raised exception: STOP processing further materials immediately
           (fail-fast, per §0.1) and return
           {"success": False, "failed_file": <name>, "error": str(e)}.
         - if it has "link"/"youtubeVideo"/"form" instead: note as
           "unsupported, skipped" (never attempted — not a failure).
    4. If no failure occurred: return {"success": True,
       "ingested_files": [...], "unsupported_files": [...]}.
    Errors from the initial courseWork().get() call itself (not a
    per-attachment failure) are also treated as a fail-fast abort with the
    same {"success": False, ...} shape, `failed_file` set to None."""
```

---

## 8. `agent/graph/nodes/claude_code.py` (new)

```python
_invocation_lock = asyncio.Lock()   # process-wide, per the locked "Drafting concurrency" decision

DRAFT_PROMPT = """\
Read every file under source-material/ in this directory. task-brief.md
always describes the assignment; any other files are additional source
material or examples. Write your complete response to draft.md in this
directory. When done, also write a short plain-text summary (sources used,
key assumptions made, anything you couldn't find or access) to summary.txt
in this directory.
"""

async def run_claude_code(workspace_dir: Path) -> dict:
    """Acquires _invocation_lock (so a second concurrent assignment waits
    here — this is the actual queuing point for the "one headless session
    at a time" rule). While held, runs (exact flags to verify against the
    installed CLI per §0.5):
        claude -p DRAFT_PROMPT --output-format json \\
               --allowedTools "Read,Write,WebSearch,WebFetch"
    as an asyncio subprocess with cwd=workspace_dir (no Bash, no broader
    filesystem, no Google credentials in its environment — structurally
    incapable of sending/submitting anything, per the Claude Code
    interaction contract). On successful exit (0) with draft.md present:
    parses the JSON stdout for "session_id", reads draft.md + summary.txt,
    returns {"success": True, "session_id": ..., "draft_path": ...,
    "summary_text": ...}. On nonzero exit, missing draft.md, or a timeout:
    returns {"success": False, "error": <stderr or a timeout message>} —
    reported as a failed drafting attempt per the general "Drafting
    failures" error-handling rule, never a partial/broken draft forwarded."""
```

---

## 9. `agent/graph/assignment_graph.py` (new)

```python
class AssignmentState(TypedDict, total=False):
    course_id: str
    course_name: str
    coursework_id: str
    title: str
    sender: str                 # who to relay results to
    ingested: bool
    failure_text: str | None
    draft_path: str | None
    summary_text: str | None
    session_id: str | None

def ingest_node(state, config) -> dict:
    """Loads Google clients from config["configurable"]["pool"], computes
    workspace.paths.assignment_dir(course_name, title), calls
    ingestion.ingest_assignment(...). On success: ingested=True (plus notes
    unsupported_files into summary context for later). On failure:
    ingested=False, failure_text = a plain message naming the failed file
    and reason (§0.1) — no retry/skip/abort."""

def route_after_ingest(state) -> str:
    return "draft_node" if state["ingested"] else "relay_node"

def draft_node(state, config) -> dict:
    """Calls claude_code.run_claude_code(workspace_dir). On success: sets
    draft_path, summary_text, session_id. On failure: failure_text = a
    plain "drafting failed" message (§ Drafting failures rule)."""

def save_session_node(state, config) -> dict:
    """Only reached on a successful draft. Opens a pool connection, calls
    repo.save_claude_session(conn, thread_id, state["session_id"]) — thread_id
    is config["configurable"]["thread_id"] (the assignment:<course>:<coursework>
    string). Stored now so Phase 3's --resume revision loop has it available;
    nothing reads it back this phase."""

def relay_node(state, config) -> dict:
    """If failure_text is set: sends it as a plain WhatsApp text message to
    state["sender"] via whatsapp_send.send_whatsapp_message. Otherwise:
    sends draft_path as a document (whatsapp_send.send_whatsapp_document,
    see §10) followed by a separate text message with summary_text
    (prefixed with a one-line note about any unsupported/skipped materials,
    if any were noted during ingestion). Exceptions from either send are
    caught and logged (same "can't notify over the channel that's broken"
    reasoning as Phase 1's send_reply_node)."""

def build_assignment_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(AssignmentState)
    g.add_node("ingest_node", ingest_node)
    g.add_node("draft_node", draft_node)
    g.add_node("save_session_node", save_session_node)
    g.add_node("relay_node", relay_node)
    g.set_entry_point("ingest_node")
    g.add_conditional_edges("ingest_node", route_after_ingest, {
        "draft_node": "draft_node", "relay_node": "relay_node",
    })
    g.add_edge("draft_node", "save_session_node")
    g.add_edge("save_session_node", "relay_node")
    g.add_edge("relay_node", END)
    return g.compile(checkpointer=checkpointer)
```

Compiled once at startup in `agent/main.py` (same checkpointer instance as
the router graph — both share one Postgres connection for LangGraph state),
stored as `app.state.assignment_graph`. This graph never pauses this phase
(no `interrupt()` node yet), but is a real compiled `StateGraph` with its
own `thread_id = f"assignment:{course_id}:{coursework_id}"` — matching the
architecture doc's thread model — so Phase 3 can add the approval
`interrupt()` node to this same graph without restructuring it, the same
way Phase 1 wired the checkpointer into the router graph before anything
paused there either.

---

## 10. `agent/graph/nodes/whatsapp_send.py` addition

```python
async def send_whatsapp_document(access_token: str, phone_number_id: str, to: str, file_path: Path, caption: str | None = None) -> str:
    """Two-step Graph API call:
    1. POST https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/media
       (multipart form: file=<bytes>, type=<mimetype guessed from extension>,
       messaging_product=whatsapp) -> media_id.
    2. POST .../messages with
       {"messaging_product": "whatsapp", "to": to, "type": "document",
        "document": {"id": media_id, "filename": file_path.name, "caption": caption}}.
    Same timeout/raise_for_status/return-message-id shape as
    send_whatsapp_message (Phase 1). Raises httpx.HTTPStatusError on either
    step's failure — caller (relay_node) catches and logs, same as Phase 1's
    send failure handling."""
```

---

## 11. Background execution (`agent/main.py`, `agent/graph/router_graph.py`)

Per §0.4: `handle_confirmation_node`, on a "confirm" outcome, does:

```python
task = asyncio.create_task(
    run_assignment_flow(config["configurable"]["assignment_graph"], resolved, sender)
)
app_state.background_tasks.add(task)
task.add_done_callback(lambda t: (app_state.background_tasks.discard(t), _log_if_failed(t)))
```

`app.state.background_tasks: set[asyncio.Task]` is initialized in
`agent/main.py`'s `lifespan` alongside the pool/graph — holding a reference
is required so the task isn't garbage-collected mid-run (a documented
`asyncio` footgun); `_log_if_failed` calls `task.exception()` and logs via
`logger.exception` if the task raised, as a last-resort safety net (mirrors
`routes.py`'s blanket catch — an actual bug here still shouldn't crash the
process or go unnoticed).

```python
async def run_assignment_flow(assignment_graph, resolved: dict, sender: str) -> None:
    """await assignment_graph.ainvoke(
        {"course_id":..., "course_name":..., "coursework_id":...,
         "title":..., "sender": sender},
        config={"configurable": {"thread_id": f"assignment:{course_id}:{coursework_id}",
                                  "pool": ..., "whatsapp_access_token": ..., "whatsapp_phone_number_id": ...}},
    )
    relay_node (inside the assignment graph) already sends the result to
    WhatsApp — this wrapper exists only to be the asyncio.Task entry point
    and to catch/log anything that escapes the graph entirely."""
```

---

## 12. Out of scope (restated from the plan, unchanged)

The approve/revise/reject loop, `interrupt()`-based pausing, `pending_items`
tracking, submission prep, `final.docx` generation, Drive upload,
`claude --resume` (session ID is stored now but not yet read back),
`agent/graph/email_graph.py`, `agent/scheduler/`.

---

## 13. Acceptance criteria mapping

| Acceptance criterion (implementation_plan.md) | Satisfied by |
|---|---|
| "Work on `<assignment>`" for a mix of PDF/DOCX/native-Doc/plain-text materials produces `draft.md` in the correct workspace folder and relays draft + summary to WhatsApp | §7 `ingest_assignment` + §5 `resolve_attachment` (per-mimeType handling) + §8 `run_claude_code` + §9 `relay_node` + §10 `send_whatsapp_document` |
| An assignment with one corrupted/unsupported attachment triggers fail-fast (per §0.1's resolved shape: report and stop, not the originally-planned retry/skip/abort prompt) and Claude Code is never invoked with incomplete materials | §7 `ingest_assignment`'s stop-on-first-failure behavior; §9 `route_after_ingest` skips `draft_node` entirely when `ingested=False` |
| Two back-to-back assignment requests cause the second to wait — only one Claude Code subprocess runs at a time | §8 `_invocation_lock` around the actual `claude` subprocess call |
| After a successful first draft, `claude_sessions` has a row with a valid session ID | §9 `save_session_node` + `repo.save_claude_session` |

Two additional Phase-2-specific behaviors this spec introduces beyond the
plan's original acceptance criteria (needed given §0's resolved design):
- Asking to work on an assignment always gets an explicit yes/no
  confirmation before anything starts, resolved via reply-to-message
  threading, never by a bare next-message guess.
- An unrelated message sent while a confirmation/disambiguation is pending
  is answered as its own independent command, and the pending question is
  dropped without comment.

---

## 14. Files changed/created — summary

| File | Change |
|---|---|
| `agent/workspace/paths.py` | New — `slugify`, `assignment_dir` |
| `agent/google_auth.py` | Add `build_drive_client`; `get_google_clients`/`load_google_clients` now return a 3-tuple |
| `agent/graph/nodes/drive.py` | New — `resolve_attachment` |
| `agent/graph/nodes/ingestion.py` | New — `build_task_brief`, `ingest_assignment` |
| `agent/graph/nodes/claude_code.py` | New — `run_claude_code`, process-wide `asyncio.Lock` |
| `agent/graph/nodes/whatsapp_send.py` | Add `send_whatsapp_document` |
| `agent/graph/nodes/classroom.py` | Add `find_assignment_candidates` |
| `agent/graph/assignment_graph.py` | New — `AssignmentState`, `build_assignment_graph` and its 4 nodes |
| `agent/graph/state.py` | Add `reply_to_message_id`, `pending_question` to `RouterState` |
| `agent/graph/router_graph.py` | Add `route_entry_node`, `resolve_assignment_node`, `handle_confirmation_node`, `handle_disambiguation_node`; extend `send_reply_node` to fill in `pending_question["message_id"]` |
| `agent/llm.py` | Add `work_on_assignment` intent + `assignment_reference` arg; add `parse_confirmation_reply`, `resolve_disambiguation` |
| `agent/webhook/routes.py` | Extract `context.id` into `reply_to_message_id` |
| `agent/db/repo.py` | Add `save_claude_session` |
| `agent/main.py` | Build + store `assignment_graph`; add `app.state.background_tasks` registry |
| `docs/implementation_plan.md` | **Recommend updating** Phase 2's core requirements to match §0.1's resolved abort-and-report behavior instead of retry/skip/abort, once this spec is approved |

No files under `agent/graph/email_graph.py`, `agent/scheduler/` touched. No
new Postgres tables (`claude_sessions` already existed, unused until now).
