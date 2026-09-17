# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A WhatsApp-controlled personal assistant (single user) that reads Gmail and Google
Classroom, answers questions on demand, and — with explicit approval at every write
step — researches, drafts, and submits homework assignments. Full product/behavior
spec lives in `docs/product_definition.md`; **read it before implementing any
feature** — it's the source of truth for scope, permissions, and behavior, and this
file only summarizes the architecture that spans multiple files.

This repo currently contains only the scaffold: every file under `agent/` has a
one-line docstring naming its purpose but no implementation yet.

## Development workflow — spec-driven, phase by phase

**No implementation code is written for a phase until that phase's spec exists
in `specs/` and has been explicitly approved by the user.** This is the
governing rule for all work in this repo — read it before touching any file
under `agent/`.

Phases are built in the order laid out in `docs/implementation_plan.md`
(0 → 1 → 2 → 3 → 4). For each phase:

1. **Write the spec** at `specs/<phase-slug>.md` (e.g.
   `specs/phase-0-environment-setup.md`, `specs/phase-2-drafting.md`),
   synthesizing:
   - `docs/product_definition.md` — the behavior/permission rules this phase
     must satisfy
   - `docs/implementation_plan.md` — that phase's objective, core
     requirements, out-of-scope boundary, and acceptance criteria
   - `docs/database_schema.md` — any tables this phase reads or writes

   The spec must be a **complete implementation description**, not a
   restatement of the plan: exact files/modules to create or change, function
   signatures, data flow through them, the concrete Postgres queries/tables
   touched, every error case and how it's handled, and how the design
   satisfies each acceptance criterion already listed for that phase in the
   implementation plan. Write it so someone with no other context — including
   a future Claude session — could implement it correctly with no further
   design decisions left open.
2. **Get it approved.** Present the spec for review and stop. A question,
   discussion, or requested revision is not approval — wait for the user to
   explicitly approve before writing or editing any implementation code.
3. **Implement exactly what the approved spec describes.** If something
   forces a deviation mid-implementation, stop, update the spec, get it
   re-approved, then continue — don't silently drift from what was approved.
4. Move to the next phase and repeat from step 1.
5. **IMPORTANT** Do not make decisons yourself. Ask anything if unclear 
   before making decisions or writing specs. Clarify your concerns and do 
   not hallucinate or misinterpret anything.

## Environment setup

Dependencies are isolated in a venv (`.venv/`), never installed to system Python.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .          # installs from pyproject.toml
```

Regenerate the pinned lockfile after adding/upgrading a dependency:
```bash
.venv/bin/pip freeze > requirements-lock.txt
```
`pyproject.toml` is the hand-edited source of truth for dependencies (loose
versions); `requirements-lock.txt` is the reproducible pin of what's actually
installed, regenerated via `pip freeze`, not edited by hand.

Secrets live in `.env` (gitignored), copied from `.env.example` and filled in with
real values — Google OAuth client credentials, Meta/WhatsApp access token, ngrok
authtoken, and the Postgres connection URL.

System-level (non-pip) dependencies required, installed via `apt`, not pip:
- `postgresql` — local state store
- `pandoc` — document format conversion (used via the `pypandoc` wrapper)

No test runner, linter, or formatter is configured yet.

## Architecture

The process runs two structurally separate halves in one long-running program:

1. **Answering the user** — a FastAPI webhook (`agent/webhook/routes.py`) receives
   Meta's WhatsApp callbacks (exposed via an ngrok static domain) and hands
   messages to a **LangGraph** app.
2. **Watching things proactively** — an in-process **APScheduler** job
   (`agent/scheduler/jobs.py`) fires a few times a day to check Gmail/Classroom and
   push notifications. This deliberately bypasses LangGraph entirely: proactive
   checks are always read-only, single-shot, and never need approval, so they're
   plain async functions that share the same Google API clients, Postgres pool,
   and WhatsApp-send helper as the graph nodes — not routed through interrupts.

### LangGraph thread model

State is checkpointed per `thread_id` into Postgres (`langgraph-checkpoint-postgres`),
which is what lets a paused approval survive a process restart. Three kinds of
threads, not one:

- **Router thread** (`thread_id = "user:<whatsapp-number>"`) — handles on-demand
  commands not tied to one specific in-flight item (read/summarize, "what's due",
  routing "work on this" into a new assignment thread).
- **Assignment thread** (`thread_id = "assignment:<course_id>:<coursework_id>"`) —
  one per assignment: ingest materials → invoke Claude Code → `interrupt()` for
  draft approval → revise (loops back through Claude Code with `--resume`) or
  approve (moves to submission prep → a second `interrupt()` for submission
  approval).
- **Email-draft thread** (`thread_id = "email:<uuid>"`) — same interrupt pattern
  for send approval, scoped separately per draft.

Each is independent so multiple assignments/emails can each sit paused at their
own interrupt simultaneously. A `pending_items` Postgres table maps the WhatsApp
message ID of every sent draft/notification to its `thread_id`, so an inbound
reply resolves to the right thread — via WhatsApp's native reply-to-message when
available, falling back to fuzzy name matching against pending items otherwise.

### Assignment drafting via Claude Code

Drafting is delegated to the Claude Code CLI running headless
(`claude -p ... --resume <session-id>`), invoked as a subprocess from
`agent/graph/nodes/claude_code.py` — using the user's own Pro/Max login, not a
separate Anthropic API key. That headless session's working directory is scoped
to exactly one assignment's folder (`~/agent-workspace/<course>/<assignment>/`),
with read/write limited to `source-material/` in and `draft.md` out, plus
WebSearch/WebFetch — no Bash, no broader filesystem, no Google API credentials, so
it is structurally incapable of sending or submitting anything. Only one headless
session runs at a time, process-wide, enforced with an in-process `asyncio.Lock`
(not a durable cross-restart lock).

Before Claude Code runs, `agent/graph/nodes/ingestion.py` assembles everything
Classroom provides into that folder: the assignment's title+description always
becomes `source-material/task-brief.md`; attachments are resolved by Drive
`mimeType` (native Google Docs exported to Markdown, PDFs left as-is for Claude
Code's native multimodal PDF reading, DOCX/ODT/RTF/PPTX converted to Markdown via
Pandoc since Claude Code can't parse those formats). **If any attachment fails to
process, ingestion stops before invoking Claude Code** rather than drafting from
partial materials — this is a deliberate exception to how every other
batch/partial failure in this codebase is handled (which is to proceed with
what succeeded).

### Database

One local Postgres instance holds LangGraph's own checkpoint tables
(`langgraph-checkpoint-postgres`) plus six app-specific tables defined in
`agent/db/schema.sql`: `pending_items` (reply routing), `email_checkpoint`
(Gmail digest watermark — see below), `notified_milestones` (proactive
notification dedup), `claude_sessions` (assignment thread → Claude Code session
ID, for `--resume`), `oauth_credentials` (stored Google OAuth token), and
`processed_messages` (webhook delivery dedup — see "Error handling" below).
No ORM — plain `psycopg` via `agent/db/repo.py`, given the small number of
simple tables.

### Gmail digest checkpoint

The proactive email digest cannot use Gmail's unread flag as "what's new" (the
user reads mail outside the agent, and old backlog can sit unread indefinitely).
Instead it tracks its own watermark (`email_checkpoint.last_history_id`) and only
reports messages that arrived after it, regardless of read/unread state.

### Error handling

Governing rule across the codebase: **never fabricate a result** — a failed
Gmail/Classroom/Drive/Claude call is always reported plainly, never papered over
with a plausible-looking response built from partial data. Transient errors
(timeouts, 429, 5xx) auto-retry with backoff (3 attempts) before surfacing; auth
errors (401/403) never auto-retry. Most partial-batch failures proceed with
whatever succeeded and note what didn't — **except assignment ingestion**, which
is fail-fast (see above), because a partial draft wastes a Claude Code run and
produces output that looks complete but silently isn't.

A thread paused at an `interrupt()` is safe across a process restart (Postgres
already has its checkpoint). A thread killed mid-node (e.g. Claude Code
subprocess still running) is not — the intended fix is a startup scan for threads
stuck mid-execution that surfaces them to the user rather than leaving them stuck
silently (see "Crash recovery" in the spec).
