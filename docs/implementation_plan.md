# Implementation Plan

Phased build plan for the project defined in `docs/product_definition.md`. Phases
1–4 match the "Build phases" already locked in that spec; Phase 0 is added here
as the infrastructure/accounts prerequisite that has to exist before Phase 1 can
start. See `docs/database_schema.md` and the spec's "Architecture" section for
the concrete tables/threads referenced below.

---

## Phase 0 — Environment & accounts setup

*Not a build phase from the spec — infrastructure that has to exist before any
feature code runs.*

### Objective
Every external account, credential, and local service the app depends on is
created and reachable, so Phase 1 can be built without infrastructure blockers.

### Core requirements
- Google Cloud project created; OAuth consent screen configured (Testing
  status, your account added as a test user); Gmail, Classroom, and Drive APIs
  enabled; OAuth client credentials downloaded.
- Meta developer account + app created; WhatsApp product added; free test
  phone number obtained; your own number added as a verified test recipient.
- ngrok account created, static domain claimed, authtoken generated.
- Project-specific Postgres database + role created (not the default
  `postgres` superuser); `agent/db/schema.sql` applied.
- `.env` populated with real values for every placeholder in `.env.example`.
- Python venv + dependencies installed (already done); Pandoc + PostgreSQL
  system packages confirmed present (already done).

### Out of scope
Any application logic — this phase is purely accounts, credentials, and local
services. No LangGraph, no webhook handling, no Google API calls beyond a
one-off OAuth verification.

### Acceptance criteria
- `psql $DATABASE_URL -c '\dt'` connects and lists the four app tables.
- A one-off script completes the Google OAuth flow and prints your Gmail
  profile address.
- A message sent via the Meta Graph API using the test number and test
  recipient arrives on your phone.
- The ngrok tunnel is reachable at the static domain and returns 200 on a
  placeholder FastAPI route.
- `.venv/bin/python -c "import agent"` succeeds with no import errors.

---

## Phase 1 — Read-only foundation

### Objective
WhatsApp bot + Gmail read/summarize + Classroom list/check, entirely via
on-demand chat commands. No writing, no drafting, no proactive messages.

### Core requirements
- FastAPI webhook route: verifies Meta's signature, parses inbound text
  messages, invokes the LangGraph router thread.
- Router thread (`thread_id = "user:<whatsapp-number>"`) with basic intent
  classification for on-demand read commands — no interrupts yet, since
  nothing in this phase needs approval.
- Gmail node: read/search inbox, summarize a batch, applying the documented
  defaults (10 most recent unread unless a sender/label/date range is given)
  and stating the assumption in the reply.
- Classroom node: list courses; list assignments (all/due-soon/overdue/missing),
  applying the documented default (all courses, 48h due-soon window).
- WhatsApp-send helper (`whatsapp_send.py`): composes and sends replies via
  the Graph API.
- LangGraph Postgres checkpointer wired up, even though no thread pauses yet
  in this phase — later phases depend on it already being in place.

### Out of scope
Drafting, sending, submitting, revision loops, approval interrupts, Claude
Code integration, attachment ingestion/Pandoc conversion, proactive/scheduled
jobs, `pending_items`/`notified_milestones`/`email_checkpoint`/`claude_sessions`
tables (unused until later phases, though already created in Phase 0).

### Acceptance criteria
- "List my courses" returns your actual enrolled Classroom courses.
- "What's due" returns assignments due within 48 hours by default, matching
  the documented default.
- "Summarize my unread emails" returns a summary of the 10 most recent unread
  emails and states that default in the reply.
- No message the agent sends ever requests approval or offers to draft, send,
  or submit anything — every response in this phase is read-only.
- An unrecognized/ambiguous message gets a plain "didn't understand" reply
  rather than crashing the process.

---

## Phase 2 — Drafting

### Objective
Add the research + Claude Code drafting node for assignments; surface drafts
in WhatsApp for review. No approval/revision loop and no submission yet — a
draft is generated once and sent.

### Core requirements
- Assignment thread (`thread_id = "assignment:<course_id>:<coursework_id>"`)
  created once you say "work on this" **and then explicitly confirm**: the
  router fuzzy-matches your free-text reference against your live Classroom
  courses/assignments (disambiguating first, via a reply-threaded numbered
  question, if more than one plausible match), then always asks a final
  yes/no confirmation before starting anything — resolved via WhatsApp's
  native reply-to-message, never by guessing at the next message. See
  `specs/phase-2-drafting.md` §0.3/§6 for the full mechanism.
- Ingestion node (`ingestion.py`): assembles `source-material/task-brief.md`
  from the CourseWork title+description; resolves each attachment by Drive
  `mimeType` (native Docs exported to Markdown, PDFs left as-is, DOCX/ODT/
  RTF/PPTX converted via Pandoc, images passed through, anything else
  flagged unsupported).
- Fail-fast ingestion behavior: any attachment failing to download/convert
  stops the flow before Claude Code runs, reports exactly which file failed
  and why, and ends that attempt — never drafts from incomplete materials.
  No retry/skip/abort prompt (that would require pause/resume machinery
  this phase deliberately doesn't have yet — see
  `specs/phase-2-drafting.md` §0.1); to retry, re-issue "work on `<X>`" and
  ingestion runs again from scratch.
- Claude Code subprocess wrapper (`claude_code.py`): working directory scoped
  to the assignment's own folder, Read/Write limited to `source-material/` in
  and `draft.md` out, plus WebSearch/WebFetch — no Bash, no Google API
  credentials.
- `claude_sessions` table: records the session ID after the first draft.
- In-process `asyncio.Lock` around the Claude Code invocation step, so a
  second drafting request queues rather than running concurrently.
- Ingestion + drafting run as a background task, not inline in the webhook
  request/response cycle — a headless Claude Code run can take minutes,
  well past what Meta's webhook expects back quickly. The confirmation
  reply is sent immediately ("Starting on `<assignment>`..."); the draft
  (or a failure report, including for any unmodeled/unexpected error, not
  just the ingestion/drafting failures above) is sent as its own separate
  message once the background task finishes — see
  `specs/phase-2-drafting.md` §11.
- Draft relay: `draft.md` sent as a WhatsApp document attachment, followed
  by a separate short text message with the summary note (sources,
  assumptions, anything inaccessible) — not crammed into one text message,
  since a real draft can exceed WhatsApp's ~4096-character text limit and
  WhatsApp doesn't render Markdown.

### Out of scope
The approve/revise/reject loop, `interrupt()`-based pausing, `pending_items`
tracking, submission prep, `final.docx` generation, Drive upload.

### Acceptance criteria
- "Work on <assignment>" for an assignment with a mix of PDF, DOCX, native
  Google Doc, and plain-text-only materials produces a `draft.md` in the
  correct workspace folder and relays the draft + summary to WhatsApp.
- An assignment with one corrupted/unsupported attachment triggers the
  fail-fast abort-and-report behavior (names the failed file and why) and
  Claude Code is never invoked with incomplete materials.
- Requesting two assignments back-to-back, before the first finishes, causes
  the second to wait — verified that only one Claude Code subprocess is ever
  running at a time.
- After a successful first draft, `claude_sessions` has a row for that
  assignment's thread with a valid session ID.

---

## Phase 3 — Approval + submission

### Objective
Wire up `interrupt()`-based approval (free-text approve/revise/reject) on top
of Phase 2's drafting, and implement the submission path as a manual-link
handoff — per the spec, drafting itself is auto (no approval to *produce* a
draft) but reviewing it and submitting are two separate approval-gated steps.

### Core requirements
- First `interrupt()` added after drafting in the assignment graph: pauses the
  thread, writes a `pending_items` row linking the sent draft message to it.
- Reply-intent parsing, three-way fork: **approve** → moves to a distinct
  "ready to submit?" state (does *not* touch Drive yet); **revise** →
  `claude --resume <session-id>` with your feedback, new `draft.md`, same
  interrupt again, uncapped rounds; **reject** → thread ends silently, no
  follow-up questioning, workspace folder retained.
- Second `interrupt()` for submission approval, separate from draft approval
  (matches the Permission Matrix having "Draft: Auto" and "Submit: Approval
  required" as distinct rows) — only on explicit approval here does the
  submission-prep node run.
- Submission-prep node: Pandoc `draft.md` → `final.docx`, upload to Drive
  (`drive.file` scope), set sharing permissions for the teacher, send a
  "ready to submit" link — you do the final "Turn in" click in Classroom.
- Same `interrupt()` + three-way fork pattern added to the email-draft graph,
  single interrupt (send approval) since there's no separate prep stage —
  approving sends immediately via Gmail.
- Reply routing wired for both graphs: exact match via `pending_items` on
  reply-to-message, fuzzy `display_name` match in the router thread otherwise,
  falling back to asking you to disambiguate only when genuinely unclear.
- Startup crash-recovery scan: on process start, find any thread whose last
  checkpoint is neither terminal nor a known paused-interrupt (i.e. it was
  mid-execution when the process died) and send a heads-up asking whether to
  retry, instead of leaving it stuck silently.

### Out of scope
Scheduled/proactive polling (Phase 4), calling the Classroom `turnIn` API
directly (deferred to v2 per the spec — v1 is manual-link handoff only).

### Acceptance criteria
- Approving a draft does not touch Drive until a second, separate "yes,
  submit" confirmation is given.
- Replying "make this shorter" on a pending assignment draft triggers a
  `--resume` call and a revised `draft.md` is sent, preserving prior context.
- Explicitly rejecting a draft (assignment or email) ends the thread with no
  follow-up message, and its workspace folder remains on disk afterward.
- Two assignment drafts pending at once can each be approved/rejected
  independently by replying to the correct WhatsApp message.
- Restarting the process while a thread is paused at either interrupt loses
  nothing — the next reply resumes it correctly.
- Restarting the process while Claude Code is actively running produces a
  heads-up message on the next startup naming the interrupted assignment.
- Approving an email draft sends it via Gmail; rejecting discards it;
  revising regenerates it — same three-way loop as assignments.

---

## Phase 4 — Proactive mode

### Objective
A scheduled polling job, running a few times a day (not continuously), that
messages you unprompted about new/due assignments, new announcements, or the
email digest — strictly informational, layered on top of everything already
built in Phases 1–3.

### Core requirements
- APScheduler configured in-process, alongside the webhook server, firing a
  few times a day (e.g. morning/evening).
- `poll_gmail_job`: reads `email_checkpoint.last_history_id`, fetches only
  messages newer than it (inbox-wide, unfiltered, independent of read/unread
  status), sends one batched digest, advances the checkpoint.
- `poll_classroom_job`: for each enrolled course, checks assignments/
  announcements against `notified_milestones` and sends any milestone not yet
  notified (posted / due-soon at 48h / overdue for assignments; posted only
  for announcements), recording each one sent.
- Proactive poll-cycle failure handling: retry with backoff, then a single
  deduplicated "check failed, will retry" notification per ongoing outage,
  with a follow-up once checks succeed again.
- These jobs are plain async functions — not LangGraph nodes or threads —
  sharing the same Google API clients, Postgres pool, and WhatsApp-send
  helper as the graph nodes.

### Out of scope
Any new write/approval-gated action — this phase only adds notifications on
top of the drafting/submission flow already built.

### Acceptance criteria
- A new assignment posted in Classroom produces exactly one "posted"
  notification at the next poll, never repeating on later polls.
- An assignment due within 48 hours produces exactly one "due soon"
  notification, once, regardless of how many subsequent polls occur before
  the due date.
- New emails since the last check arrive as a single batched digest;
  previously-seen unread backlog is never re-reported.
- Simulating an outage across two consecutive Gmail/Classroom poll cycles
  produces exactly one failure notification, not two, followed by a recovery
  notice once checks succeed again.
- No proactively-sent message ever requests approval or triggers a write
  action on its own.

---

## Beyond v1 (tracked, not phased)

Explicitly deferred items already flagged in the spec, not part of any phase
above:
- Calling the Classroom `studentSubmissions.modifyAttachments`/`.turnIn` API
  directly, once tested against the real school account (replacing the
  manual-link handoff from Phase 3).
- Interactive WhatsApp buttons for approval, if free-text intent parsing
  proves ambiguous in practice.
- A standalone "just search the web for X" chat command (web search is
  currently only available inside the drafting step).
- OS keychain/secret-manager storage for credentials, in place of the local
  `.env` file.
