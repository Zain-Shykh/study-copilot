# Phase 3 — Approval + Submission — Spec

Source: `docs/implementation_plan.md` ("Phase 3"), `docs/product_definition.md`
(Submission, Permission matrix, Draft review loop, Referring to a specific
pending item, Crash recovery, Routing a reply to the right pending thread,
Claude Code interaction contract, Architecture/LangGraph thread model),
`docs/database_schema.md`, `specs/phase-2-drafting.md`.

## Decisions made for this phase (not fully pinned down by the docs above —
confirmed with the user before writing this spec)

1. **Email drafting is out of scope for this phase.** Checked the actual
   codebase before writing this spec: `agent/graph/email_graph.py` is still
   a one-line docstring stub, there is no `draft_email` intent, and no node
   anywhere composes email reply text. `docs/implementation_plan.md`'s
   Phase 3 section assumes email drafting already exists (it says to wire
   the same approval loop "on top of Phase 2's drafting" for both graphs) —
   that assumption doesn't hold; only assignment drafting was built in
   Phase 2. **Resolved**: this phase covers only the assignment
   approve/revise/reject/submit loop. Email drafting (what engine composes
   it, what triggers it, then this same approval loop on top) is deferred
   to its own future phase. `docs/implementation_plan.md`'s Phase 3 section
   should be corrected to drop the email-specific acceptance criterion
   ("Approving an email draft sends it via Gmail...") until that phase
   exists.
2. **No Drive sharing-permissions step, and no new Google OAuth scope.**
   `docs/product_definition.md`'s Submission section says v1 "uploads the
   final file to Drive, sets sharing permissions so your teacher can open
   it, and sends you a 'ready to submit' link." Setting a permission for a
   specific teacher requires knowing their email, which needs the
   `classroom.profile.emails` scope (not currently requested) plus a
   one-time OAuth re-consent. Raised this with the user, who pointed out
   the sharing step is unnecessary: the actual submission action is
   Classroom's own manual "Add or create → Drive" attach-and-turn-in flow,
   which already grants the teacher access to whatever file the student
   attaches — that's Classroom's job, not something the agent needs to
   pre-arrange via a Drive permission. **Resolved**: submission-prep
   uploads the packaged submission file(s) — see Decision #8 for what
   "packaged" means, since it's no longer always a single `final.docx` —
   to the user's own Drive (already covered by the existing `drive.file`
   scope) and sends the link(s), full stop — no `permissions.create` call,
   no new scope, no re-auth. Recommend
   correcting `docs/product_definition.md`'s Submission section to drop the
   "sets sharing permissions" line.
3. **All approve/revise/reject/submit-confirm reply parsing happens inside
   `assignment_graph.py`'s own nodes, not in `router_graph.py`.** This is a
   deliberate departure from Phase 2's `handle_confirmation_node`/
   `handle_disambiguation_node` pattern, which parses the reply in the
   router itself. The reason: `implementation_plan.md`'s own acceptance
   criterion says an explicit reject "ends the thread with no follow-up
   message" — genuinely no message, not even an ack. The router can't know
   in advance whether a reply will resolve to approve/revise/reject, so it
   can't safely send any acknowledgment before dispatching. **Resolved**:
   the router's only job for a reply targeting a pending assignment item is
   to work out *which thread* it targets (via reply-to-message lookup or
   fuzzy name match) and resume that thread with the raw reply text — no
   ack, no pre-emptive message. Whichever node the graph resumes into
   (`parse_review_node` or `parse_submit_node`) does the actual three-way/
   binary parsing and is the sole source of any outbound message for that
   turn, including sending nothing at all for reject/decline. One accepted
   trade-off from this: unlike the initial "Starting on `<assignment>`..."
   ack Phase 2 sends before a multi-minute Claude Code run, a "make this
   shorter" revision reply gets no interim ack before its (also
   multi-minute) Claude Code re-run — silence until the revised draft
   arrives is the only way to keep the reject path genuinely silent without
   duplicating the parse step in both the router and the graph.
4. **`pending_items` lifecycle**: a row is inserted every time the agent
   sends a message that's awaiting a decision (the draft-ready message, and
   later the "ready to submit?" message), and closed (`status = 'resolved'`)
   the moment that decision is parsed — except submission-prep, which
   leaves its row open until the upload actually succeeds, so a Drive/
   Pandoc failure can be retried by simply replying "yes" again instead of
   losing the pending state. Only one row is ever `status = 'pending'` per
   thread at a time (a new row implicitly supersedes the last, since the
   previous one was already closed when its own decision was parsed) — see
   §5.
5. **Resolving a fresh (non-reply-to) message that names a pending item**
   needs a new Gemini classifier intent, `respond_to_pending`, alongside
   the existing six. Mirrors how `work_on_assignment` already extracts a
   free-text reference for fuzzy-matching against live Classroom data —
   this one extracts an optional reference for fuzzy-matching against
   *currently pending* item display names (`docs/product_definition.md`'s
   "Referring to a specific pending item" section, already locked, just not
   yet implemented). See §6.
6. **Crash recovery reuses the existing "work on `<assignment>`" re-entry
   path instead of a new confirm/retry subsystem.** A thread interrupted
   mid-node (ingest, first draft, or a revision) can always be safely
   restarted by re-sending "work on `<title>`" — ingestion is already
   idempotent (Phase 2 re-downloads from scratch on retry) and a plain
   `ainvoke` with fresh input on an existing `thread_id` starts that thread
   over rather than trying to resume a broken mid-node state. Building a
   second interrupt-and-reply flow just for the crash heads-up would
   duplicate that. **Resolved**: the startup scan's heads-up message
   directly tells the user to re-send "work on `<title>`" — no new pending
   state, no new table columns.
7. **LangGraph's `interrupt()`/`Command(resume=...)` primitives are this
   codebase's first real use of them** (Phases 1-2 deliberately avoided
   them). §5 and §6 below use the current `langgraph.types.interrupt` /
   `langgraph.types.Command` API as best-known — confirm the exact import
   path and `StateSnapshot` attribute names (`.next`, `.tasks[i].interrupts`
   used by the crash-recovery scan in §8) against the actually-installed
   `langgraph` version before implementing, same caveat Phase 2 flagged for
   the Claude Code CLI flags.
8. **The submission file is no longer always `final.docx` — the required
   format/structure is per-assignment and Claude Code is responsible for
   reading and following it, not the main agent.** Originally this spec
   hardcoded a single `pandoc draft.md -> final.docx` conversion, matching
   `docs/product_definition.md`'s Local Workspace layout (`final.docx` in
   the module tree). The user corrected this: `final.docx` was only ever
   meant as an example, and real assignments each state their own
   submission format in Classroom's own instructions (already captured
   verbatim in `source-material/task-brief.md` — no ingestion change
   needed) — code as raw `.py`/etc. files, a single PDF, everything bundled
   into one `.zip`, plain text, or indeed a `.docx`. Confirmed with the
   user this also means **code assignments are now in scope for v1**,
   overriding `docs/product_definition.md`'s "Out of scope (v1)" line
   ("No non-text assignment types (quizzes, code submissions, video) —
   writing-style assignments only") — that line needs correcting (see
   §10/§12). The user also clarified this isn't just about picking the
   right *file type* — Claude Code should produce the **whole assignment
   response** the guidelines describe, including recreating an actual
   folder layout when one is called for (e.g. a project with `src/`,
   `tests/`, a specific required directory structure), not just a flat
   pile of files with varied extensions.
   **Resolved mechanism**: Claude Code can only read task-brief.md's stated
   guidelines and *decide* what's needed — it has no `Bash` and its `Write`
   tool only produces text, so it's structurally unable to actually
   generate a `.zip` archive or a true binary PDF itself (unchanged from
   the existing "no Bash, no broader filesystem access" contract — this
   phase doesn't loosen that). So the division of labor is: Claude Code
   writes whatever file(s) the response actually needs (one or many, any
   text-based content — prose, code, data), **organized into subfolders
   under `submission/` matching whatever structure the assignment calls
   for**, plus a `submission_manifest.json` declaring the target packaging
   (`"as-is"`, `"zip"`, `"pdf"`, or `"docx"`) and which files it applies
   to. The main agent — which already has Pandoc for docx conversion and
   gains nothing new for zipping (Python's stdlib `zipfile` preserves
   subfolder paths on its own) — mechanically executes that declared plan
   at submission time, including recreating the exact folder layout inside
   the zip archive; it never decides *whether* folders are needed, only
   packages whatever Claude Code already laid out. One mechanical
   constraint this forces (not a judgment call, just what Drive/WhatsApp/
   Classroom attachments can physically represent): a real folder
   structure can only survive as a single `.zip` — "as-is"/"pdf"/"docx"
   packaging only supports flat files directly under `submission/`, so a
   manifest declaring one of those while `files` contains a nested path is
   invalid (see §5.4's validation rule) — same "reported as a failed
   attempt, never partially executed" handling as any other manifest
   error. This keeps "follow the assignment's guidelines" genuinely
   Claude's judgment call (it's the one reading and interpreting free-text
   instructions and deciding on a directory layout) while keeping the main
   agent's job purely mechanical, the same division already established
   for ingestion (Claude Code decides nothing about *how* a file is
   fetched/converted going in; the main agent decides nothing about *what*
   content or structure goes into a submission going out). See §4 and
   §5.4.
9. **PDF output needs a new one-time system dependency.** Pandoc alone
   converts Markdown to `.docx` natively, but producing a real `.pdf`
   needs a PDF engine (e.g. `wkhtmltopdf`, one `apt install` package,
   picked over a full LaTeX toolchain for size). Recommend adding a
   `sudo apt install -y wkhtmltopdf` line to `docs/product_definition.md`'s
   existing Pandoc "Setup note", alongside the doc corrections already
   flagged in Decisions #1/#2/#8. Not needed for `"zip"`/`"as-is"`
   submissions (e.g. code assignments) — only assignments whose stated
   format is PDF.

---

## 1. Objective (restated from the plan)

Wire up `interrupt()`-based approval on top of Phase 2's assignment
drafting: a three-way approve/revise/reject fork on the draft, and — only
on explicit approval — a second, separate interrupt asking whether to
submit, gating a submission-prep step that packages the approved work in
whatever format that specific assignment requires (per its own stated
guidelines — code files, a zip, a PDF, a doc, etc., decided by Claude Code,
see Decision #8) and uploads it to the user's Drive for manual "Turn In" in
Classroom. Also closes the crash-recovery gap: a heads-up on startup for
any assignment thread that was actively mid-node (not cleanly paused at an
interrupt, not terminal) when the process last stopped.

Email drafting + its approval loop, `agent/scheduler/`, and the Classroom
`turnIn` API are all out of scope — see §10.

---

## 2. `agent/db/repo.py` additions

```python
def get_claude_session(conn: psycopg.Connection, assignment_thread_id: str) -> str | None:
    """Returns the stored Claude Code session ID for an assignment thread,
    for a --resume revision call, or None if no draft has been produced yet."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claude_session_id FROM claude_sessions WHERE assignment_thread_id = %s",
            (assignment_thread_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def create_pending_item(
    conn: psycopg.Connection, message_id: str, thread_id: str, item_type: str, display_name: str
) -> None:
    """Records that whatsapp_message_id is awaiting a reply that should
    resume thread_id. item_type is 'assignment' or 'email' (schema CHECK)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pending_items (whatsapp_message_id, thread_id, item_type, display_name)
            VALUES (%s, %s, %s, %s)
            """,
            (message_id, thread_id, item_type, display_name),
        )
    conn.commit()


def get_pending_item(conn: psycopg.Connection, message_id: str) -> dict | None:
    """Looks up a pending item by the WhatsApp message it's attached to.
    Returns None if there's no row, or the row exists but is already
    resolved (a reply to a stale/superseded message)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT whatsapp_message_id, thread_id, item_type, display_name
            FROM pending_items WHERE whatsapp_message_id = %s AND status = 'pending'
            """,
            (message_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"message_id": row[0], "thread_id": row[1], "item_type": row[2], "display_name": row[3]}


def list_pending_items(conn: psycopg.Connection, item_type: str) -> list[dict]:
    """All currently-pending items of one type, for fuzzy by-name matching
    when a reply doesn't use WhatsApp's reply-to-message feature."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT whatsapp_message_id, thread_id, item_type, display_name
            FROM pending_items WHERE item_type = %s AND status = 'pending'
            """,
            (item_type,),
        )
        rows = cur.fetchall()
    return [
        {"message_id": r[0], "thread_id": r[1], "item_type": r[2], "display_name": r[3]}
        for r in rows
    ]


def close_pending_item(conn: psycopg.Connection, message_id: str) -> None:
    """Marks a pending item resolved, so a later reply to the same message
    (or a stale by-name match) never double-resumes the thread."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_items SET status = 'resolved' WHERE whatsapp_message_id = %s",
            (message_id,),
        )
    conn.commit()
```

No `schema.sql` changes — `pending_items` already has every column this
needs (added in Phase 0, unused until now).

---

## 3. `agent/llm.py` additions

### 3.1 `respond_to_pending` intent

Add to `CLASSIFY_SYSTEM_PROMPT`'s command list:

```
- respond_to_pending: the user is making a decision about a draft or
  pending item they were previously shown — approving it, asking for
  changes, rejecting it, or answering a submit yes/no question — whether
  or not they name which one. Includes replies like "yes", "looks good",
  "make it shorter", "no", "reject it", "approve the bio essay". If they
  name a specific course/assignment, extract it into
  pending_item_reference; leave it unset if they didn't name one.
```

Add `"respond_to_pending"` to `ROUTE_MESSAGE_DECLARATION`'s `intent` enum,
plus:

```python
"pending_item_reference": types.Schema(
    type="STRING",
    description="only for respond_to_pending, if a specific item was named",
),
```

Note: this is a fallback path only — see §6.1. WhatsApp's native reply-to
is checked first and is unambiguous; `respond_to_pending` only matters for
a fresh message with no `context.id`, or one that replies to something
that isn't a currently-tracked pending item.

### 3.2 Three-way draft review parsing

```python
REVIEW_DECLARATION = types.FunctionDeclaration(
    name="record_review",
    description="Classify a reply to a draft that was just shown for review.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "decision": types.Schema(type="STRING", enum=["approve", "revise", "reject"]),
            "feedback": types.Schema(
                type="STRING",
                description="only for revise: the requested changes, as given",
            ),
        },
        required=["decision"],
    ),
)

REVIEW_SYSTEM_PROMPT = """\
A draft was just sent to the user for review. Classify their reply:
- approve: a clear affirmative ("yes", "looks good", "approved", "send it").
- reject: an explicit rejection ("no", "reject", "discard", "scrap it").
- revise: anything read as feedback or requested changes ("make this
  shorter", "add a source about X", "fix the intro") — extract the
  requested changes into feedback.
If the reply is unclear, off-topic, or doesn't fit approve/reject cleanly,
classify it as revise with feedback set to the reply text verbatim — this
keeps the draft alive for another round instead of silently discarding it
(reject) or advancing it without a real approval (approve).
"""


def parse_review_reply(client: genai.Client, model: str, reply_text: str) -> tuple[str, str | None]:
    """Classifies a draft-review reply into ("approve"|"revise"|"reject",
    feedback). feedback is only non-None for "revise". Raises after 3
    failed attempts."""

    def call():
        return client.models.generate_content(
            model=model,
            contents=reply_text,
            config=types.GenerateContentConfig(
                system_instruction=REVIEW_SYSTEM_PROMPT,
                tools=[types.Tool(function_declarations=[REVIEW_DECLARATION])],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY", allowed_function_names=["record_review"],
                    )
                ),
            ),
        )

    response = _with_retry(call)
    args = response.function_calls[0].args
    return args["decision"], args.get("feedback")
```

Submission's yes/no question reuses the existing `parse_confirmation_reply`
— no new declaration needed there, since it's a plain binary with no
feedback to extract.

---

## 4. `agent/graph/nodes/claude_code.py` — revision support + submission contract

Per Decision #8, Claude Code's output contract changes from Phase 2's fixed
`draft.md` + `summary.txt` to a `submission/` folder (one or more files,
whatever the response actually needs) plus a `submission_manifest.json`
declaring how the main agent should package it, still alongside
`summary.txt`. This is a genuine revision to Phase 2's already-implemented
behavior, not purely additive — see §5.4 for the manifest's exact shape.

**Post-launch fix**: `DRAFT_PROMPT` was renamed to `DRAFT_PROMPT_TEMPLATE`
and gained two additions, found via a live failure (a real assignment whose
brief said "name the zip your roll number" — Claude Code had no way to know
the roll number, and `_package_submission`, see §5.4, always derived the
final packaged filename from the assignment title, ignoring any name Claude
Code might have wanted):
- A `__STUDENT_INFO__` placeholder (filled at call time by
  `_build_draft_prompt(student_info)`, a plain `.replace()` — not
  `.format()`, since the prompt's own JSON example already contains literal
  `{}` that would collide with format-string syntax) telling Claude Code
  what identifying info to use if the assignment asks for it, with an
  explicit fallback instructing it to flag the gap in `summary.txt` instead
  of guessing when none is configured (`STUDENT_INFO` env var, threaded
  through `Settings` → the router's `configurable["student_info"]` →
  `run_assignment_flow` → the assignment graph's own config →
  `draft_node` → `run_claude_code(..., student_info=...)`; `revise_node`
  does *not* re-supply it, since a `--resume`d session already has it from
  turn one).
- An optional `"output_name"` field in the `submission_manifest.json`
  schema (below), for when the assignment specifies an exact name for the
  packaged `zip`/`pdf`/`docx` output — ignored for `"as-is"`, where naming
  is already fully under Claude Code's control via the filename it chooses
  under `submission/`.

```python
DRAFT_PROMPT_TEMPLATE = """\
Read every file under source-material/ in this directory (including \
anything inside an extracted subfolder). task-brief.md always describes \
the assignment, including any stated submission requirements — format \
(e.g. "submit as a single PDF", "zip your .py files"), and structure (e.g. \
"include a src/ folder and a README", a required project layout). Follow \
them exactly, producing the complete, actual assignment response — not \
just a matching file type. If nothing is stated, use your judgment based \
on the nature of the response (prose -> a single Markdown file, a small \
script -> one file, a larger project -> whatever files/folders it \
actually needs).

If the assignment asks you to identify yourself in the submission or its \
filename (e.g. a roll number, student ID, or name), use exactly this: \
__STUDENT_INFO__

You have no way to run or execute anything (no Bash, no code execution) — \
only Read/Write/Edit/WebSearch/WebFetch. For a coding assignment, write \
the most careful, correct code you can by reasoning it through and \
tracing it by hand — you cannot compile, run, or test it yourself, so say \
so plainly in summary.txt rather than claiming it works.

Write your complete response under submission/ in this directory — one or \
more files, organized into subfolders if the required structure calls for \
it (e.g. submission/src/main.py, submission/tests/test_main.py), using \
whatever names/extensions fit the content (Markdown for prose, .py/.js/\
etc. for code, and so on). You cannot produce a .zip or a real .pdf/pptx/\
docx yourself, so never write one directly — instead, also write \
submission_manifest.json in this directory (not under submission/) \
describing how those files should be packaged:
  {"format": "as-is" | "zip" | "pdf" | "docx" | "pptx", "files": ["<paths relative to submission/, e.g. src/main.py>"], "output_name": "<optional, no extension>"}
- "as-is": upload each listed file unchanged (e.g. a single .py file, or \
  Classroom accepts multiple separate attachments). Only valid for flat \
  files directly under submission/ — no subfolders, since loose uploads \
  can't preserve a folder structure. Name the file itself under \
  submission/ exactly as the assignment requires — "output_name" is \
  ignored for this format.
- "zip": bundle every listed file into one .zip archive, preserving \
  whatever subfolder structure it's in under submission/. Required \
  whenever submission/ has more than a flat list of files, or the \
  assignment explicitly asks for a zip.
- "pdf" / "docx" / "pptx": convert the listed file(s) (must be Markdown/\
  text, flat, no subfolders) to that format, one output file per input \
  file listed.
Use "as-is" for a single flat file, "zip" whenever there's a real folder \
structure or the assignment explicitly asks for one, otherwise follow \
whatever specific format the assignment states. Set "output_name" only \
when the assignment specifies an exact filename for the packaged "zip"/\
"pdf"/"docx"/"pptx" output (e.g. "submit a zip named your roll number") — \
omit it otherwise.

When done, also write a short plain-text summary (sources used, key \
assumptions made, anything you couldn't find or access) to summary.txt in \
this directory.
"""

_NO_STUDENT_INFO = (
    "not configured — if the assignment needs one, say so in summary.txt instead of guessing"
)

def _build_draft_prompt(student_info: str) -> str:
    return DRAFT_PROMPT_TEMPLATE.replace("__STUDENT_INFO__", student_info or _NO_STUDENT_INFO)

REVISE_PROMPT_TEMPLATE = """\
The user reviewed your submission and asked for these changes:

{feedback}

Update the file(s)/folder(s) under submission/ in this directory to \
address the feedback (add/remove/rename/reorganize as needed — it doesn't \
have to match the previous structure). Re-write submission_manifest.json \
to match whatever's now in submission/, keeping the same format unless the \
feedback or the new structure implies the required format itself changed. \
When done, overwrite summary.txt with an updated short plain-text summary \
(sources used, key assumptions made, anything you couldn't find or access, \
and what changed in this revision).
"""


async def run_claude_code(
    workspace_dir: Path, *, resume_session_id: str | None = None, feedback: str | None = None,
    student_info: str = "",
) -> dict:
    """Runs a headless Claude Code session scoped to workspace_dir. A fresh
    submission when resume_session_id is None (student_info given to
    _build_draft_prompt for it); otherwise resumes that session with
    feedback as a revision request — student_info is not re-supplied here,
    since the resumed session already has it from turn one. Same return
    shape either way: {"success": True, "session_id":..., "submission_files":
    [Path,...], "manifest": {...}, "summary_text":...} or
    {"success": False, "error":...} — success now also requires submission/
    to be non-empty and submission_manifest.json to exist and parse with a
    valid "format" (one of as-is/zip/pdf/docx), non-empty "files" list
    (each of which must actually exist under submission/, paths may be
    nested e.g. "src/main.py"), and, if present, a non-empty string
    "output_name". Additionally, "as-is"/"pdf"/"docx" require every listed
    path to be flat (no "/") — those can't represent a folder structure;
    only "zip" may list nested paths. Any of that being violated is
    treated as a failed run, same as a missing draft.md was in Phase 2."""
    prompt = (
        _build_draft_prompt(student_info) if resume_session_id is None
        else REVISE_PROMPT_TEMPLATE.format(feedback=feedback)
    )

    args = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--tools", _TOOLS,
        "--allowedTools", _TOOLS,
    ]
    if resume_session_id:
        args += ["--resume", resume_session_id]

    # ... rest unchanged: subprocess_env stripping, _invocation_lock,
    # _TIMEOUT_SECONDS, create_subprocess_exec(*args, cwd=..., env=...).
    # Only the post-run success check changes, per the docstring above.
```

Everything else in this file (the lock, the env stripping, the timeout,
`Bash` never being granted) is unchanged from Phase 2 — see Decision #8 for
why `Bash` still isn't granted even though this phase adds zip/PDF output.
**Post-launch**: Bash was seriously investigated as a way to let Claude
Code compile/run/verify code, then explicitly rejected after live testing
found the CLI's `--restricted` sandbox mode does not reliably confine
Bash-executed code's file reads to the workspace — see
`specs/sandboxed-bash-execution.md` for the full investigation and why. No
Bash was ever shipped.

**Post-launch fix**: `--allowedTools` gained `Edit` (now
`Read,Write,Edit,WebSearch,WebFetch`). Found via a live failure whose actual
Claude Code session transcript (stored locally by the CLI itself, under
`~/.claude/projects/<hashed-workspace-path>/`) showed the run had, in fact,
completed all the coding work and then tried an `Edit` on a file it had
already `Write`n — which wasn't in the allowed list, so the CLI blocked it
pending interactive approval that a headless (`-p`) session can never
receive. The session's own final message: *"This is waiting on your
permission approval to edit `game.py`... so I can continue."* — then the
process exits (returncode 0) without ever reaching the
`submission_manifest.json`/`summary.txt` instructions, which is what
actually produced the "Claude Code finished without writing
submission_manifest.json" failure text. `Write`-then-`Edit` on the same
file is an ordinary, common coding pattern, so this wasn't an edge case.

**Post-launch fix (2)**: `_TOOLS = "Read,Write,Edit,WebSearch,WebFetch"` is
now passed to *both* `--tools` and `--allowedTools`, not just
`--allowedTools`. These are different flags — `--allowedTools` only
pre-approves specific actions, while `--tools` controls which tools are
*offered to the model as existing at all*. Without `--tools`, `Bash`
remained visible as an option (denied on each attempt, not absent) — and
both real failure transcripts (the one that motivated the `Edit` fix above,
and the Bash investigation in `specs/sandboxed-bash-execution.md`) show
Claude Code retrying `Bash` several times before giving up, wasting turns
each time. Live-tested: with `--tools` explicitly excluding Bash, a prompt
that directly asked it to run a shell command produced zero Bash attempts —
it said outright that no shell tool was available and moved on, instead of
trying and getting denied.

**Post-launch fix (3)**: `_VALID_FORMATS` gained `"pptx"` (now `{"as-is",
"zip", "pdf", "docx", "pptx"}`) — pandoc already supports Markdown→PPTX
conversion (confirmed via `pandoc --list-output-formats`), so this only
required widening the accepted-format set; `_package_submission`'s
existing generic `pypandoc.convert_file` branch (§5.2) needed no code
change. `DRAFT_PROMPT_TEMPLATE`'s manifest schema and format-bullet list
mention `"pptx"` alongside `"pdf"`/`"docx"` throughout.

**Post-launch fix (4)**: on `asyncio.TimeoutError`, `run_claude_code`
now checks `_validate_manifest(workspace_dir)` (a new `_build_success_result`
helper factors out the shared success-response construction) before
reporting failure. Found via a live run whose own CLI session transcript
(stored locally under `~/.claude/projects/<hashed-workspace-path>/`)
showed the conversation ending cleanly — `stop_reason: "end_turn"`, both
solution files written, a valid `submission_manifest.json`, a complete
`summary.txt` — all inside about 5 of the 15 minutes budgeted, yet the
app still reported "Claude Code timed out." The underlying `claude`
process apparently doesn't always exit/hand control back promptly after
finishing its conversational work; previously, that gap was enough for
our own timeout to fire and discard an already-complete, valid submission
as a false failure. Now, if the manifest is already valid on disk when
the kill happens, that result is used instead — `session_id` is `None` in
this recovered case (a killed process never gets to print its final
stdout JSON, which is the only place the session id is known), so a
subsequent revise starts a fresh Claude Code session rather than
`--resume`-ing — an accepted, documented degradation, far better than
discarding a completed draft and making the user wait through the full
process again for nothing.

**Post-launch fix (5)**: `ingest_node`, `draft_node`, and `revise_node` now
explicitly return `"failure_text": None` on every success path, instead of
omitting the key. `AssignmentState` is a plain `TypedDict` with no reducers,
so LangGraph merges each node's returned dict into the persisted checkpoint —
any key a node's return value doesn't include simply keeps its prior value.
Once `failure_text` was set by any genuine failure on a thread, it stayed set
forever afterward: a later retry could ingest, draft, and even resume the
same Claude Code session successfully, but `relay_node`'s very first check
(`if state.get("failure_text"): ...`) would still see the *old* value and
re-report the old failure — even though the run that just finished actually
succeeded, produced a valid manifest, and never got as far as creating a
`pending_items` row. Live-diagnosed on a real assignment thread: the CLI
subprocess for a resumed session completed cleanly in under 10 seconds
(confirmed via the session's own local transcript and a direct live
reproduction of the exact call), and `claude_sessions.updated_at` was freshly
written at the same moment — proving `draft_node` had returned a real
`session_id` (which the timeout path never sets), yet the user still received
the assignment's very first "Claude Code timed out" message, verbatim,
unchanged, from what was actually a stale field. Fixed at the source — every
success return in all three nodes now clears `failure_text` — rather than
in `relay_node`, since any future node that can set failure_text on failure
needs the same discipline on its own success path.

---

## 5. `agent/graph/assignment_graph.py` — rewrite

### 5.1 `AssignmentState` additions

```python
class AssignmentState(TypedDict, total=False):
    # ... ingestion-related Phase 2 fields unchanged (course_id, title, etc.) ...
    submission_files: list[str] | None  # paths under submission/, replaces Phase 2's draft_path
    manifest: dict | None               # parsed submission_manifest.json — see §5.4
    summary_text: str | None
    session_id: str | None
    review_reply_text: str | None       # raw text from the draft-review interrupt
    review_decision: str | None         # "approve" | "revise" | "reject"
    review_feedback: str | None         # only set when review_decision == "revise"
    submit_reply_text: str | None       # raw text from the submit-confirm interrupt
    final_files: list[str] | None       # packaged output(s) actually uploaded
    drive_links: list[str] | None
```

`draft_path` (singular) is dropped — a submission can be one file or many,
so every downstream node works off `submission_files`/`manifest` instead.

### 5.2 New/changed nodes

- **`ingest_node`, `save_session_node`** — unchanged from Phase 2.
- **`draft_node`** — same trigger/role as Phase 2 (first Claude Code run),
  but its output shape changes per §4: reads back `submission_files`,
  `manifest`, `summary_text`, `session_id` from `run_claude_code`'s result
  instead of a single `draft_path`.
- **`relay_node`** — **revised (post-launch fix):** no longer sends the
  submission files as WhatsApp documents. It instead uploads every file in
  `state["submission_files"]` to the user's Drive (raw, as Claude Code
  wrote them — no format conversion or zipping for review purposes, reusing
  `_upload_to_drive`, the same helper `submission_prep_node` uses for the
  final packaged upload) and sends one text message: the summary, then one
  `"<relative path under submission/>: <Drive webViewLink>"` line per file,
  then the review instructions ("Reply approve, suggest changes, or say
  reject."). This replaced an earlier design that sent every file as its
  own WhatsApp document, individually readable inline — switched because
  the user wanted review to happen via Drive links instead of inline
  document attachments. Loading the Drive client reuses
  `google_auth.load_google_clients` (same pattern as `ingest_node`/
  `submission_prep_node`); an auth failure or an upload error (any
  exception from `_upload_to_drive`) is logged and the node returns `{}`
  without sending anything further — same swallow-and-log shape Phase 2
  already used for a WhatsApp send failure, not a new failure mode. After a
  *successful* summary send it inserts a `pending_items` row
  (`item_type="assignment"`, `display_name=f"{course_name} — {title}"`,
  keyed on that summary message's id) via `repo.create_pending_item`. On
  failure (ingest or draft error) it behaves exactly as in Phase 2 — sends
  `failure_text`, nothing else.
- **`route_after_relay(state)`** — new: `"await_review_node"` if there's no
  `failure_text`, else `END`.
- **`await_review_node`** (new, sync):
  ```python
  from langgraph.types import interrupt

  def await_review_node(state: AssignmentState) -> dict:
      reply = interrupt({"kind": "draft_review", "title": state["title"]})
      return {"review_reply_text": reply}
  ```
- **`parse_review_node`** (new, sync): calls `llm.parse_review_reply` on
  `state["review_reply_text"]`, then `repo.close_pending_item` for the row
  this thread currently owns (its id is recoverable from
  `repo.list_pending_items` filtered to this `thread_id`, or simpler: the
  router already resolved which `pending_items` row triggered this resume
  and can pass its `message_id` through `config["configurable"]` — see
  §6.2). Returns `{"review_decision": decision, "review_feedback": feedback}`.
- **`route_after_review(state)`** →
  `{"approve": "ask_submit_node", "revise": "revise_node", "reject": END}[state["review_decision"]]`.
- **`revise_node`** (new, async): looks up the stored session id
  (`repo.get_claude_session`), calls
  `claude_code.run_claude_code(dest_dir, resume_session_id=session_id, feedback=state["review_feedback"])`,
  and returns the same `{"submission_files", "manifest", "summary_text", "session_id"}` /
  `{"failure_text"}` shape `draft_node` does — feeds into the same
  `save_session_node` → `relay_node` chain, so a revision round produces
  and relays a new submission exactly like the first one, then loops back
  to `await_review_node`.
- **`ask_submit_node`** (new, async): sends
  `f'"{state["title"]}" approved. Ready to submit? Replying yes packages and uploads it to your Drive; no leaves it as-is for now.'`,
  inserts a new `pending_items` row for this thread (same shape as
  `relay_node`'s).
- **`await_submit_node`** (new, sync): `interrupt({"kind": "submit_confirm", "title": ...})`
  → `{"submit_reply_text": reply}`.
- **`parse_submit_node`** (new, sync): calls the existing
  `llm.parse_confirmation_reply` with the same question text
  `ask_submit_node` sent. On **decline**: `repo.close_pending_item`, returns
  nothing further (routes to `END` — silent, matching reject). On
  **confirm**: does *not* close the pending item yet (see Decision #4) and
  routes to `submission_prep_node`.
- **`submission_prep_node`** (new, async): executes `state["manifest"]`
  exactly as declared (§5.4) — no interpretation of the assignment's own
  guidelines happens here, that already happened inside Claude Code:
  1. Branch on `manifest["format"]`:
     - `"as-is"`: each file in `manifest["files"]` is uploaded to Drive
       unchanged.
     - `"zip"`: `zipfile.ZipFile` bundles every file in `manifest["files"]`
       into one `<base_name>.zip` under the assignment folder, writing each
       with `arcname` set to its listed relative path — so a nested path
       like `src/main.py` ends up at the same `src/main.py` location
       inside the archive, exactly reproducing whatever folder structure
       Claude Code laid out under `submission/`. That single archive is
       then uploaded.
     - `"pdf"` / `"docx"`: `pypandoc.convert_file` converts each listed
       file individually to that format (source files must be Markdown/
       text — Claude Code only ever writes text, per §4), then each
       converted output is uploaded. When `manifest["output_name"]` is set
       and there's exactly one file, that file's output is named
       `<base_name>.{format}` instead of `<stem>.{format}` — with more than
       one file there's no single unambiguous name to apply, so each keeps
       its own stem.
     - `base_name` (post-launch fix) is `slugify(manifest["output_name"])`
       when that field is set, else `slugify(title)` as before — see §5.4.
       `"as-is"` ignores it entirely, since each file already keeps the
       name Claude Code gave it under `submission/`.
  2. Each upload is `drive_service.files().create(body={"name": ...}, media_body=MediaFileUpload(...)).execute()`
     followed by a `.get(fileId=..., fields="webViewLink")` call for its
     link. No `permissions().create` call — see Decision #2.
  3. On success: `repo.close_pending_item` (now that it actually
     succeeded), returns `{"final_files": [...], "drive_links": [...]}`.
  4. On failure (an unrecognized `manifest["format"]`, a missing source
     file, a Pandoc error, or a Drive error): returns
     `{"failure_text": f'Couldn\'t prepare "{title}" for submission: {e}. Reply "yes" again to retry — nothing was lost.'}`
     and leaves the `pending_items` row open, so that retry actually
     resumes the same interrupt.
- **`relay_submit_node`** (new, async): on success, sends one message
  listing every file in `state["final_files"]` alongside its
  `state["drive_links"]` entry, followed by
  `'Open the assignment in Classroom (Add or create → Drive), attach the file(s) above, and click Turn In to submit.'`.
  On failure, sends `state["failure_text"]`. Either way, routes to `END`.

### 5.3 Graph wiring

```python
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
g.add_conditional_edges("ingest_node", route_after_ingest, {...})       # unchanged
g.add_edge("draft_node", "save_session_node")
g.add_edge("revise_node", "save_session_node")                          # new: shared sink
g.add_edge("save_session_node", "relay_node")
g.add_conditional_edges("relay_node", route_after_relay,
                         {"await_review_node": "await_review_node", END: END})
g.add_edge("await_review_node", "parse_review_node")
g.add_conditional_edges("parse_review_node", route_after_review,
                         {"ask_submit_node": "ask_submit_node", "revise_node": "revise_node", END: END})
g.add_edge("ask_submit_node", "await_submit_node")
g.add_edge("await_submit_node", "parse_submit_node")
g.add_conditional_edges("parse_submit_node", route_after_submit,
                         {"submission_prep_node": "submission_prep_node", END: END})
g.add_edge("submission_prep_node", "relay_submit_node")
g.add_edge("relay_submit_node", END)
```

### 5.4 Submission manifest contract

`submission_manifest.json`, written by Claude Code (§4) alongside
`submission/`, is the one piece of format-decision-making that crosses
from Claude Code's judgment into the main agent's mechanical execution.
Shape:

```json
{
  "format": "as-is" | "zip" | "pdf" | "docx" | "pptx",
  "files": ["<path relative to submission/, e.g. main.py or src/main.py>", "..."],
  "output_name": "<optional, no extension>"
}
```

- `files` paths are always relative to `submission/`, never absolute, and
  may include subfolders (e.g. `src/main.py`) when Claude Code laid out a
  real folder structure there per the assignment's required layout —
  `run_claude_code`'s success check (§4) resolves and verifies each one
  exists before reporting success.
- `output_name` (post-launch fix) is optional; when present it must be a
  non-empty string (`run_claude_code`'s success check rejects a blank or
  non-string value the same way it rejects an invalid `format`). It names
  the final packaged `"zip"`/`"pdf"`/`"docx"` output — see §5.2's
  `submission_prep_node` bullet for exactly how — for assignments that
  specify an exact filename (most commonly a roll number/student ID).
  Ignored for `"as-is"`, and for `"pdf"`/`"docx"` with more than one file.
- **Only `"zip"` may list nested paths.** `"as-is"`/`"pdf"`/`"docx"`
  require every listed path to be flat (no subfolder) — a loose upload or
  a Pandoc conversion can't represent a folder structure, only a single
  archive can. A manifest violating this is treated as invalid (see
  below).
- `"as-is"` is the default Claude Code is told to prefer (§4's
  `DRAFT_PROMPT`) for a single flat file or a small set of files Classroom
  can accept as separate attachments; `"zip"` is required whenever
  `submission/` has a real folder structure, or the assignment explicitly
  asks for one.
- This same manifest is re-read on every revision round — a revise reply
  can change the required format/structure just by Claude Code rewriting
  `submission_manifest.json` and reorganizing `submission/`, no
  special-casing needed in the graph nodes; `submission_prep_node` always
  just executes whatever the *latest* manifest says at the time "ready to
  submit?" is answered yes.
- A manifest that's missing, fails to parse, names an unrecognized
  `format`, lists a file that doesn't exist under `submission/`, or lists
  a nested path under a non-`"zip"` format, is treated as a failed Claude
  Code run (§4) — never partially executed.

---

## 6. `agent/graph/router_graph.py` changes

### 6.1 Resolving which pending assignment a reply targets

New helper, checked in `route_entry_node` right after the existing
Phase 2 `pending_question` check (which stays first — it's unambiguous and
scoped to the router thread's own confirm/disambiguate exchange, a
different mechanism from cross-thread `pending_items`):

```python
def _find_targeted_pending_item(state: RouterState, config: RunnableConfig) -> dict | None:
    pool = config["configurable"]["pool"]
    reply_to = state.get("reply_to_message_id")
    with pool.connection() as conn:
        if reply_to:
            item = repo.get_pending_item(conn, reply_to)
            if item is not None:
                return item
        # Fresh message or a reply to something not currently tracked —
        # fall through to by-name fuzzy matching in classify_intent's
        # respond_to_pending branch instead (needs the classified intent
        # first, so it can't happen here).
        return None
```

`route_after_entry` gains a third branch: if `_find_targeted_pending_item`
finds a match, route straight to a new `handle_pending_item_reply` node
(bypassing `classify_intent` entirely, same as the existing
`pending_question` shortcut).

### 6.2 `handle_pending_item_reply` node

```python
def handle_pending_item_reply(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    item = configurable["_matched_pending_item"]  # stashed by route_entry_node/route_after_entry
    task = asyncio.create_task(
        resume_assignment_thread(
            configurable["assignment_graph"], item["thread_id"], item["message_id"],
            state["inbound_text"], configurable["whatsapp_access_token"],
            configurable["whatsapp_phone_number_id"], configurable["pool"],
        )
    )
    background_tasks = configurable["background_tasks"]
    background_tasks.add(task)
    task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))
    return {}  # no reply_text — send_reply_node sends nothing; see Decision #3
```

`resume_assignment_thread` mirrors `run_assignment_flow`'s shape: resumes
via `Command(resume=inbound_text)` against `thread_id`, passing the pending
item's `message_id` through `config["configurable"]["_pending_item_message_id"]`
so `parse_review_node`/`parse_submit_node` (§5.2) know which row to close,
wrapped in the same top-level try/except that sends a generic failure
message on an unmodeled crash (never total silence for an actual bug, only
for a real reject/decline).

Note `send_reply_node` must be adjusted not to send anything when
`reply_text` is absent from state (currently it always sends
`state["reply_text"]` — needs a guard, since this is the first node that
legitimately produces no reply at all).

### 6.3 By-name fallback: `respond_to_pending` intent

Added to `route_after_classify`'s dispatch: `"respond_to_pending": "resolve_pending_item"`.

`resolve_pending_item_node` — fuzzy-matches `intent_args.get("pending_item_reference", "")`
against `repo.list_pending_items(conn, "assignment")` display names (same
`difflib.SequenceMatcher` approach and threshold constants as
`find_assignment_candidates`, reused here). Three outcomes:
- No pending items at all → `{"reply_text": "There's nothing pending right now."}`.
- Exactly one pending item, or one clear best match above the confidence
  threshold → resumes it directly via the same `resume_assignment_thread`
  path as §6.2 (no ack, per Decision #3).
- Multiple plausible matches, none confident → asks the user to
  disambiguate via a new `pending_question` kind, `"disambiguate_pending_item"`,
  carrying `{"candidates": [...], "original_text": state["inbound_text"]}`
  (the *original* reply text is preserved so, once the user picks one,
  resuming uses their actual approve/revise/reject content — not the
  disambiguation answer itself).

`handle_pending_item_disambiguation_node` (new, parallels
`handle_disambiguation_node`): parses which candidate via
`llm.resolve_disambiguation` (reused as-is — it already just picks a
1-based index from a list), then dispatches `resume_assignment_thread`
using `pending_question["original_text"]`, same no-ack pattern.

`route_after_entry` dispatch dict gains `"disambiguate_pending_item": "handle_pending_item_disambiguation"`.

---

## 7. `agent/graph/state.py`

```python
class RouterState(TypedDict, total=False):
    # ... unchanged fields ...
    # pending_question["kind"] now one of: disambiguate_assignment |
    # confirm_start_assignment | disambiguate_pending_item
```

No new top-level fields — `pending_question`'s existing `dict | None` shape
already accommodates the third kind.

---

## 8. Crash recovery — new `agent/graph/recovery.py`

```python
def scan_for_interrupted_assignments(assignment_graph, pool, conn) -> list[str]:
    """Returns the titles of assignment threads that were actively mid-node
    (not cleanly paused at an interrupt, not terminal) when the process
    last stopped. Reads distinct assignment: thread_ids from the
    checkpointer's own table, then inspects each thread's current state."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE 'assignment:%'"
        )
        thread_ids = [r[0] for r in cur.fetchall()]

    interrupted_titles = []
    for thread_id in thread_ids:
        snapshot = assignment_graph.get_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.next:
            continue  # terminal — finished or already ended (reject/decline)
        paused_at_interrupt = any(getattr(t, "interrupts", None) for t in snapshot.tasks)
        if paused_at_interrupt:
            continue  # legitimately waiting on a reply — not a crash
        title = snapshot.values.get("title", thread_id)
        interrupted_titles.append(title)
    return interrupted_titles
```

Wired into `agent/main.py`'s `lifespan`, right after both graphs are built
and before `yield`: runs the scan once, and for each title found, sends
`f'I was working on "{title}" when I restarted — send "work on {title}" again if you\'d like me to retry.'`
to `settings.my_whatsapp_number` via the existing `send_whatsapp_message`
helper. Per Decision #6, no new pending state — the message is purely
informational and points back at the existing re-entry command.

`checkpoints` is `langgraph-checkpoint-postgres`'s own table name as of
the currently-pinned version — confirm this against what `checkpointer.setup()`
actually creates before implementing (see Decision #7's caveat).

---

## 9. `agent/main.py`

Adds the crash-recovery scan call described in §8, inside `lifespan`,
after `app.state.assignment_graph = build_assignment_graph(checkpointer)`
and before `yield`. Needs a raw `conn` for the `checkpoints` query — reuse
`pool.connection()` the same way every other node already does.

---

## 10. Out of scope (restated from the plan, revised per Decisions #1/#2/#8)

Email drafting and its approval loop (`agent/graph/email_graph.py` stays a
stub), `agent/scheduler/` (Phase 4), calling the Classroom `turnIn` API
directly (deferred to v2 per the locked product doc), any Drive
sharing-permissions step or new OAuth scope (Decision #2).

**No longer out of scope, per Decision #8**: code-assignment support
(`docs/product_definition.md`'s "writing-style assignments only" line is
superseded — recommend correcting it to drop "code submissions" from the
excluded list). Still genuinely out of scope: quiz/short-answer coursework
and video, which aren't file-based responses at all and need a different
ingestion/response shape this phase doesn't build.

---

## 11. Acceptance criteria mapping

| Acceptance criterion (`implementation_plan.md`, as this spec resolves it) | Satisfied by |
|---|---|
| Approving a draft does not touch Drive until a second, separate "yes, submit" confirmation | §5.2 `route_after_review` only reaches `ask_submit_node`/`await_submit_node` on approve; `submission_prep_node` only runs after `parse_submit_node` sees confirm |
| Replying "make this shorter" on a pending draft triggers `--resume` and a revised submission, preserving prior context | §4 `run_claude_code(resume_session_id=...)`; §5.2 `revise_node` reads the stored session id via `repo.get_claude_session` |
| Rejecting a draft (assignment) ends the thread with no follow-up message | §5.2 `route_after_review`'s `"reject": END` branch sends nothing; §6.2's `handle_pending_item_reply` never sends an ack itself (Decision #3) |
| Two assignment drafts pending at once can each be approved/rejected independently by replying to the correct message | §2 `pending_items` keyed per-message; §6.1 `_find_targeted_pending_item` resolves the *specific* message replied to |
| Restarting while paused at either interrupt loses nothing — the next reply resumes it correctly | LangGraph's Postgres checkpointer persists interrupt state by design (already relied on since the scheduler decision was locked); §8's scan explicitly skips threads paused at a real interrupt |
| Restarting while Claude Code is actively running produces a heads-up naming the interrupted assignment | §8 `scan_for_interrupted_assignments` |
| Approving an email draft sends it via Gmail; rejecting discards it; revising regenerates it | **Deferred — see Decision #1.** Not satisfied by this phase; revisit once email drafting itself is spec'd. |

One additional behavior this spec introduces beyond the plan's original
acceptance criteria (per Decision #8, not in the original plan text):
- Submitting the same assignment in different formats (code files as-is,
  a zip, a PDF) all go through the identical approve → submit-confirm →
  `submission_prep_node` path — the format itself never needs a
  code-level branch outside of §5.2's manifest execution, since the
  decision was already made and declared by Claude Code before this node
  ever runs.

---

## 12. Files changed/created — summary

| File | Change |
|---|---|
| `agent/db/repo.py` | Add `get_claude_session`, `create_pending_item`, `get_pending_item`, `list_pending_items`, `close_pending_item` |
| `agent/llm.py` | Add `respond_to_pending` intent + `pending_item_reference` arg; add `REVIEW_DECLARATION`/`parse_review_reply` |
| `agent/graph/nodes/claude_code.py` | `run_claude_code` gains `resume_session_id`/`feedback` params, `REVISE_PROMPT_TEMPLATE`; **`DRAFT_PROMPT` rewritten** and the success check changed to require `submission/` + a valid `submission_manifest.json` instead of `draft.md` (Decision #8 — revises Phase 2 behavior). **Post-launch fix**: `--allowedTools` gains `Edit`; `DRAFT_PROMPT` → `DRAFT_PROMPT_TEMPLATE` + `_build_draft_prompt`/`student_info` param; `"output_name"` added to the manifest contract and its validation. **Post-launch fix (2)**: `_TOOLS` constant passed to both `--tools` and `--allowedTools` (no Bash — see `specs/sandboxed-bash-execution.md` for the rejected Bash investigation) so Claude Code never even attempts Bash instead of retrying a denied one; `_VALID_FORMATS` gains `"pptx"` |
| `agent/graph/nodes/drive.py` | **Post-launch fix**: `ZIP_MIMETYPES` + a zip-extraction branch (with `_safe_extract` guarding against path traversal) in `resolve_attachment`, so `.zip` attachments are no longer silently marked unsupported |
| `agent/graph/assignment_graph.py` | Add `revise_node`, `await_review_node`, `parse_review_node`, `ask_submit_node`, `await_submit_node`, `parse_submit_node`, `submission_prep_node`, `relay_submit_node`; extend `AssignmentState` (`submission_files`/`manifest`/`final_files`/`drive_links` replace `draft_path`/`final_docx_path`/`drive_link`); `relay_node` now sends N documents instead of one; `submission_prep_node` branches on the manifest's format (zip via stdlib `zipfile`, pdf/docx via Pandoc, as-is direct upload) instead of a fixed Pandoc-to-docx call. **Post-launch fix**: `relay_node` no longer sends WhatsApp documents — it uploads each submission file to Drive (via the existing `_upload_to_drive` helper) and sends their links in the summary text instead; `draft_node` forwards `configurable["student_info"]` to `run_claude_code`; `_package_submission` honors an optional manifest `output_name`. **Post-launch fix (2)**: `ingest_node`/`draft_node`/`revise_node` now explicitly clear `"failure_text": None` on every success return, so a stale failure from an earlier attempt on the same thread can no longer leak into `relay_node`'s failure check on a later, successful run |
| `agent/graph/router_graph.py` | Add `_find_targeted_pending_item`, `handle_pending_item_reply`, `resolve_pending_item_node`, `handle_pending_item_disambiguation_node`; extend `route_entry_node`/`route_after_entry`/`route_after_classify`; guard `send_reply_node` against an absent `reply_text`. **Post-launch fix**: `run_assignment_flow` gains a `student_info` param, threaded into the assignment graph's own `configurable` dict; its one call site (in `handle_confirmation_node`) passes `configurable.get("student_info", "")` |
| `agent/graph/state.py` | Document the third `pending_question["kind"]` value (no new fields) |
| `agent/graph/recovery.py` | New — `scan_for_interrupted_assignments` |
| `agent/main.py` | Call the crash-recovery scan in `lifespan`, after both graphs are built |
| `agent/config.py` | **Post-launch fix**: `Settings` gains `student_info: str`, read from the optional `STUDENT_INFO` env var (defaults to `""`) |
| `agent/webhook/routes.py` | **Post-launch fix**: `_process_text_message`'s `configurable` dict gains `"student_info": settings.student_info` |
| `.env.example` | **Post-launch fix**: document `STUDENT_INFO` (optional) |
| `docs/implementation_plan.md` | **Recommend updating** Phase 3's acceptance criteria to drop the email-draft bullet until email drafting is its own spec'd phase |
| `docs/product_definition.md` | **Recommend updating**: drop "sets sharing permissions so your teacher can open it" from Submission (Decision #2); drop "code submissions" from the "Out of scope (v1)" exclusion list and correct the Local Workspace layout's `final.docx` line to reflect a variable `submission/`-derived output (Decision #8); add `wkhtmltopdf` to the Pandoc Setup note (Decision #9) |

No changes to `agent/db/schema.sql` (`pending_items`/`claude_sessions`
already had every column needed), `agent/graph/email_graph.py`, or
`agent/scheduler/`.
