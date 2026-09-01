# Personal Agent — Scope & Spec (v0.1)

## One-line purpose
A WhatsApp-controlled personal assistant that reads your Gmail and Google Classroom,
answers questions about them on demand, and — with your explicit approval at every
write step — can research, draft, and submit homework assignments.

## Interface
- **Primary**: WhatsApp bot (Meta Cloud API), two entry points into the same
  LangGraph app:
  1. **User-initiated**: you send a message ("summarize my emails", "what's due?")
  2. **Agent-initiated**: a scheduled job polls Gmail/Classroom and pushes a message
     to you when something new shows up (new assignment, due-soon reminder, draft ready)
- **Why WhatsApp over Telegram/Discord**: Telegram and Discord are both
  blocked/throttled by major Pakistani ISPs, requiring a VPN to stay connected —
  a real reliability risk for something meant to notify you about due dates.
  WhatsApp is unrestricted in Pakistan and, since the agent only replies inside
  conversations you initiate, falls under Meta's free "service conversation"
  tier — no messaging cost for this use case.
- **Setup cost**: more involved than Telegram's BotFather flow — needs a Meta
  developer account, a WhatsApp Business phone number, and webhook verification.
  One-time setup, not an ongoing cost.
- **Mechanics**: Meta calls your webhook (not long-polling like Telegram would
  need) — since this runs on your own machine (per the hosting decision below),
  you'll need a tunnel (e.g. Cloudflare Tunnel or ngrok) exposing that webhook
  endpoint, since Meta requires a public HTTPS URL to call.
- No other channels for v1 (no separate web dashboard, no voice).

---

## In scope — capabilities

### Gmail
- Read and search inbox (recent, unread, by sender/subject/label) — on-demand
- Summarize one email or a batch ("summarize my unread emails") — on-demand
- Notify proactively with a batched digest of new mail since the last check,
  across the whole inbox (see Agent behavior below for the checkpoint logic)
- Draft replies/new emails — on-demand only
- Send email — **approval required every time** (see Permission Matrix)

### Google Classroom
- List enrolled courses
- List assignments: all, due-soon, overdue, or missing
- Fetch assignment details + download attachments (via Drive) — on-demand only
- Notify proactively when a new assignment posts, a new announcement posts,
  or a due date approaches (see Agent behavior below for dedup rules)

### Web search
- General-purpose research tool available to the drafting step
- Not exposed as a standalone "just search the web for X" chat command in v1
  (can add later if useful)

### Assignment drafting
- Given an assignment + its attachments, use Claude (via Claude Code headless mode,
  your Pro/Max subscription) plus web search to produce a draft response
- Draft is always shown to you before anything is finalized
- See "Claude Code interaction contract" under Agent behavior for exactly
  what the headless session can access and how revisions/output are handled

### Submission
- Prepare the finished work in your Drive
- **Approval required every time** — after approval:
  - **v1**: agent uploads the final file to Drive, sets sharing permissions so
    your teacher can open it, and sends you a "ready to submit" link — you do
    the final "Turn in" click yourself in Classroom. Chosen over the API path
    because a failed/partial API turn-in can silently leave a teacher with a
    submission marked "turned in" that they can't actually open (see risks below).
  - **Later (v2+)**: revisit calling the Classroom API directly
    (`studentSubmissions.modifyAttachments` + `.turnIn`) once tested against
    your real school account. Known risks to test for first: (1) some school
    Google Workspace domains block third-party OAuth apps from calling the
    Classroom API entirely, independent of scopes granted; (2) files attached
    via the API don't get Classroom's automatic teacher-sharing, so the app
    would need to set Drive permissions on the file itself; (3) `turnIn` only
    applies to standard file-based coursework, not quiz/short-answer types
    (not a concern for v1's writing-only scope).

---

## Out of scope (v1)
- Only Gmail + Classroom — no other inboxes, LMS platforms, or calendars
- No fully autonomous sending or submitting — every write action needs a thumbs-up
- Single user (you) — no multi-user/family accounts
- No group/collaborative assignments
- No plagiarism/citation-formatting checking
- No non-text assignment types (quizzes, code submissions, video) — writing-style
  assignments only, to start

---

## Permission matrix — what needs your approval

| Action | Auto or approval-required |
|---|---|
| Read/search email | Auto |
| Summarize email | Auto |
| Draft an email reply | Auto (drafted, not sent) |
| **Send an email** | **Approval required** |
| List/check Classroom assignments | Auto |
| Download assignment attachments | Auto |
| Draft an assignment response | Auto (drafted, not submitted) |
| **Submit an assignment** | **Approval required** |

---

## Build phases
1. **Phase 1 — Read-only foundation**: WhatsApp bot + Gmail read/summarize + Classroom
   list/check, all via on-demand chat commands. No writing, no drafting yet.
2. **Phase 2 — Drafting**: add the research + Claude drafting node for assignments;
   surface drafts in WhatsApp for review (no submission yet).
3. **Phase 3 — Approval + submission**: wire up `interrupt()`-based approval (you
   approve by replying with any affirmative free text — "yes", "send it",
   "approved" — parsed for intent, not a fixed keyword), implement the submit
   path as manual-link handoff (see Submission section above for why).
4. **Phase 4 — Proactive mode**: scheduled polling job, run a few times a day
   (e.g. morning/evening) rather than continuously, that messages you unprompted
   about new/due assignments or ready drafts.

---

## Agent behavior

### Tone
Formal/professional — complete sentences, no slang, structured with bullet
points/headers where that aids clarity even in chat replies.

### Ambiguity handling
On-demand read/summarize commands don't have a fixed scope (e.g. "summarize my
emails" doesn't say how many or since when). The agent picks a sensible default
and states it plainly in the reply rather than asking first — e.g. "Here are
your 10 most recent unread emails:" — so you can correct it in one follow-up
("go back further") instead of an extra round-trip up front. Defaults:
- Emails: 10 most recent unread, unless a sender/label/date range is given.
- Assignments: all courses, due-soon window = next 48 hours, unless you ask
  for "all", "overdue", or "missing" explicitly.

### Draft review loop (emails and assignments)
Every draft sent to you is a three-way fork, decided by parsing your reply's
intent — not a fixed keyword:
1. **Approve** (any affirmative free text — "yes", "send it", "approved") →
   proceeds to the gated action (send / prep for submission).
2. **Revise** (anything read as feedback/instructions — "make this shorter",
   "add a source about X") → agent regenerates the draft incorporating the
   feedback and re-sends it for review. No cap on revision rounds; you keep
   iterating until you approve or reject.
3. **Reject** (explicit "no"/"reject"/"discard") → draft is dropped silently.
   No follow-up questioning why — the workspace folder stays on disk (per the
   persistent-folders decision) so nothing is lost, and you re-request a draft
   manually whenever you're ready to try again.

### Referring to a specific pending item
When multiple drafts/approvals are pending at once (e.g. two assignment
drafts awaiting review), you can refer to the one you mean either way:
- **Reply-to-message**: WhatsApp's native reply/swipe-to-quote on the specific
  draft message — agent reads which message you replied to. Preferred when
  available since it's unambiguous.
- **By name**: a fresh message naming the course/assignment/email (e.g.
  "approve the Bio essay") — agent fuzzy-matches your phrasing against the
  currently pending items. Falls back to asking you to disambiguate only if
  the match is genuinely unclear (e.g. two pending items with very similar names).

### Proactive vs on-demand actions
Every action falls into exactly one of two trigger categories:

- **Proactive (agent-initiated, fixed-interval polling — Phase 4)**: strictly
  informational, never a write action. Covers: new Classroom assignments,
  new Classroom announcements, due-soon/overdue reminders, and the email
  digest (see below).
- **On-demand only (never triggered automatically, only when you explicitly
  ask)**: fetching/sharing full assignment details + attachments, starting to
  draft an assignment ("work on this"), submitting an assignment, drafting or
  sending an email. The agent never starts drafting or submitting work on its
  own initiative just because something is due soon — a due-soon notification
  is only ever a heads-up, not a trigger for action.

This is a separate axis from the Permission Matrix's Auto/Approval-required
distinction: that axis governs whether a *requested* action needs your
approval; this one governs whether the agent acts *without being asked* at all.

### Proactive notification dedup
Each assignment/announcement gets notified at most once per milestone, tracked
in local state (not re-derived from Classroom each poll, so a milestone
already sent never repeats even if the underlying due date/status is
re-fetched):
- Assignments: once when first seen (posted), once at a fixed lead time
  before the due date (48h out), once if it becomes overdue.
- Announcements: once when first seen (no due date, so no further milestones).

### Email digest checkpoint
The proactive email digest cannot rely on Gmail's unread flag as "what's new"
— you read some emails directly in Gmail without the agent knowing, and old
backlog mail can sit unread for weeks without being new. Instead the agent
tracks its own checkpoint (last-seen timestamp or Gmail `historyId`) in local
state, independent of read/unread status:
- Each poll, it fetches only messages that arrived **after the last
  checkpoint** — regardless of whether you've since read them or left them
  unread — then advances the checkpoint.
- Covers your whole inbox, unfiltered — no domain filtering, no category
  filtering (promotions/social/spam included). Every new email since the last
  checkpoint appears in the digest.
- Old unread backlog is never re-reported, since it predates the checkpoint
  from before the agent started tracking it.
- Delivered as one batched digest per poll (brief one-liner per new email),
  not a separate message per email.

### Assignment material ingestion
Before Claude Code is invoked to draft, the main agent assembles everything
Classroom provides for that assignment into `source-material/`:
1. **Task brief** — the CourseWork object's `title` + `description`
   (Classroom's own free-text instructions) is always saved as
   `source-material/task-brief.md`, whether or not there are file
   attachments. This is the one guaranteed source of truth, covering the
   "text instructions only, no file" case.
2. **Attachments** (0 to N, resolved by Drive `mimeType`, never by filename):
   - Native Google Doc/Slides/Sheets → exported directly to Markdown via
     Drive's export API.
   - PDF → downloaded as-is; left for Claude Code's own (multimodal) Read
     tool to read directly, including scanned/image-only pages.
   - DOCX/ODT/RTF/PPTX → downloaded, then converted to Markdown by the main
     agent using **Pandoc** (one-time system install — see setup note
     below), since Claude Code can't parse these formats itself.
   - Images → pass through as-is, same as PDF.
   - Anything else (spreadsheets, forms, links, video) → flagged as
     unsupported/"needs your manual review" rather than attempted with a
     lossy parse.
3. No pre-classification of "which file is the task vs. which is material" —
   Classroom doesn't distinguish this either. Claude Code reads everything in
   `source-material/` and determines from content which defines the task.

**Ingestion failures block drafting — they do not proceed partially.** This
is a deliberate exception to the general "Partial batch failures" rule below:
losing 1 of 10 independent emails in a summary doesn't invalidate the other
9, but assignment materials aren't independent — if a task-defining doc or a
piece of required reading fails to parse, Claude Code would draft from either
an incomplete task description or a response missing required source
material. That's a wasted headless run: tokens spent, no usable draft
produced, and worse, a draft that reads as complete when it silently wasn't.
So: if any attachment fails to download/convert, the agent stops **before**
invoking Claude Code, tells you exactly which file failed and why, and asks
how to proceed:
- **Retry** — if it looks transient (e.g. a Drive fetch timeout).
- **Skip that file and proceed anyway** — your explicit call that it wasn't
  essential (e.g. optional supplementary reading).
- **Abort** — you'll sort out that file separately (paste its content as
  text, fix it in Classroom, etc.) before trying again.

#### Setup note — Pandoc
Pandoc needs to be installed once on your machine for the DOCX/ODT/RTF/PPTX
conversion step. On Ubuntu:
```
sudo apt update && sudo apt install -y pandoc
```

### Claude Code interaction contract
When you say "work on this"/"draft this assignment", the LangGraph app hands
off to Claude Code running in headless mode (your Pro/Max subscription, no
API token billing) as the actual drafting engine, scoped tightly per assignment:

- **Working directory & tool scope**: invoked with its working directory set
  to that assignment's own folder
  (`~/agent-workspace/<course>/<assignment>/`), permitted to read/write only
  within it (`source-material/` in, `draft.md` out) plus WebSearch/WebFetch
  for research. No Bash, no broader filesystem access, and critically no
  access to Gmail/Classroom/Drive credentials — this session is structurally
  incapable of sending or submitting anything, even in principle. That stays
  the exclusive job of the main agent's gated actions.
- **Session continuity across revisions**: the first draft starts a new
  Claude Code session; every subsequent revision round (per the draft review
  loop above) resumes that same session (`claude --resume <session-id>`)
  rather than starting fresh — it already has full context of the sources it
  read and what it tried before, so revisions stay coherent and cheaper than
  re-deriving context each round. The session ID is stored alongside that
  assignment's workspace folder for the life of the assignment.
- **Output**: two artifacts per run — `draft.md` (the actual draft, unchanged
  in purpose from the workspace layout decision) plus a short summary note
  (sources used, key assumptions made, anything it couldn't find/access).
  Both are relayed to you together on WhatsApp; the main agent still composes
  the surrounding message (tone, approval-loop instructions) — Claude Code
  never messages you directly.
- **Failures**: covered by the general "Drafting failures" case below — a
  headless run that errors, times out, or produces no usable draft is
  reported as a failed attempt, never forwarded as a partial/broken draft.
- **Shared usage quota**: the agent's headless sessions use the same Pro/Max
  login as any Claude Code you run interactively (e.g. in VS Code) for
  unrelated work — they draw from the same account-level usage limits.
  Accepted trade-off: no isolation between the two (no separate subscription
  for the agent). If a headless run gets rate-limited because of unrelated
  interactive usage, that's just a normal drafting failure per the error
  handling below — reported plainly, no silent retry.

### Error handling — API failures
Governing principle: **never fabricate a result**. If a Gmail, Classroom,
Drive, or Claude call fails, the agent reports the failure plainly — it never
returns a plausible-looking summary/draft/status based on partial or absent
data. Beyond that, behavior depends on the failure type:

- **Transient errors** (timeouts, rate limits/429, 5xx): auto-retried with
  exponential backoff, up to 3 attempts, invisible to you if a retry
  succeeds. Only surfaced as a failure once retries are exhausted.
- **Auth errors** (401/403, expired or revoked OAuth token): never
  auto-retried — retrying a bad token just burns your rate limit for no
  reason. Surfaced immediately, naming which account/service needs
  re-authentication (e.g. "Classroom access has expired — please
  re-authenticate.").
- **On-demand command failure** (you asked for something right now and it
  failed after retries): agent tells you plainly what failed and why, in one
  message, without silently retrying forever or guessing. You decide whether
  to ask it to try again.
- **Partial batch failures** (e.g. summarizing 10 emails and 1 fails to
  load): agent proceeds with what succeeded and notes the count/items that
  failed, rather than aborting the whole batch or silently dropping them.
- **Drafting failures** (Claude/web-search step errors while drafting an
  email or assignment response): reported as a failed draft, same as any
  on-demand failure — no partial/broken draft is ever sent for your review.
- **Proactive poll-cycle failure** (a scheduled check itself fails, e.g.
  Gmail/Classroom unreachable): after retries are exhausted, sends one
  heads-up that the check failed and will retry next cycle — not a guess
  that "nothing new" happened. Deduplicated like the assignment milestones:
  only one failure notification per ongoing outage, not one every poll cycle,
  with a follow-up once checks succeed again.

---

## Decisions (locked)
1. **Hosting**: your own computer, left running. The scheduler and WhatsApp bot
   process run locally — no cloud deployment for v1. (Trade-off: if the machine is
   off/asleep, the bot goes quiet and scheduled checks are skipped until it's back up.)
2. **Email autonomy**: every send is approval-gated, no exceptions. There is no
   auto-send path anywhere in the design — the "Auto" rows in the permission matrix
   above stop at reading/drafting; sending is always a human click.
3. **Approval mechanism**: you approve/reject via free-text reply (e.g. "yes",
   "send it", "approved") rather than a fixed keyword or interactive buttons —
   the agent parses your reply for approval intent against the specific pending
   action. Simpler to build; revisit interactive buttons later if free-text
   parsing proves ambiguous in practice.
4. **Classroom scope**: all enrolled classes are monitored automatically — no
   per-class opt-in step needed in the Classroom watcher node.
5. **Local workspace**: the agent runs as a normal local process with regular
   filesystem access, not just Drive. Layout:
   ```
   ~/agent-workspace/
     <course-name>/
       <assignment-name>/
         source-material/   ← downloaded attachments from Classroom
         draft.md           ← Claude's working draft, sent to WhatsApp for review
         final.docx          ← approved version, only this gets uploaded to Drive
   ```
   Folders are **persistent** (kept after submission, not cleaned up) so past work
   stays browsable in Finder/Explorer. Drive is only touched at the last step —
   uploading the approved final file, since that's what the Classroom submission
   API requires.
6. **Final file conversion**: `final.docx` is produced from the approved
   `draft.md` using **Pandoc** (`pandoc draft.md -o final.docx`) — the same
   dependency already used in reverse for ingesting DOCX/ODT/RTF/PPTX
   attachments, so no new tool is introduced just for this direction.
7. **Local state store**: a single local **PostgreSQL** database holds all
   persistent state — LangGraph's own approval/interrupt checkpoints (via
   `langgraph-checkpoint-postgres`, so a pending approval survives a machine
   restart instead of being silently lost), plus app-specific tracking:
   notification dedup milestones, the Gmail digest checkpoint (`historyId`),
   and Claude Code session IDs per assignment. **Already installed and running**
   on this machine (PostgreSQL 16, `main` cluster on port 5432) — no `apt
   install` needed; only a project-specific database/role still needs creating.
8. **Scheduler**: the Phase 4 polling job runs **in-process**, inside the
   same long-running Python program that hosts the WhatsApp webhook server
   (e.g. via APScheduler) — one process to run and monitor, rather than a
   separate cron-triggered script. Trade-off accepted: a crash/hang in the
   webhook handling path also takes down the scheduled Gmail/Classroom
   checks until the process is restarted.
9. **Drafting concurrency**: only one Claude Code headless session runs at a
   time. If you ask to work on a second assignment before the first draft is
   done, the request queues and starts once the first finishes — avoids two
   headless sessions competing for your Pro/Max login/usage limits at once.
10. **Secrets storage**: OAuth tokens and API keys (Google, Meta) live in a
    local `.env`/config file on disk, readable only by your user account —
    matches the single-user, own-computer trust model already locked in above;
    no OS keychain/secret-manager layer for v1.

---

## Tech stack

### Language & runtime
- **Python 3.11+** — chosen over Node/TS because LangGraph's Python SDK is
  the more mature/first-class one, and Google's official client libraries for
  Gmail/Classroom/Drive/OAuth are Python-native with the best support.

### Orchestration
- `langgraph` — the core app graph, nodes, and `interrupt()`-based approval flow
- `langgraph-checkpoint-postgres` (+ `psycopg` v3 as its driver) — durable
  checkpointing against the local Postgres instance, so pending approvals
  survive a restart

### Webhook server (WhatsApp)
- `fastapi` + `uvicorn` — receives Meta's webhook calls; async, pairs
  naturally with LangGraph's async execution
- `httpx` — calls the Meta Graph API directly (send messages, mark read,
  etc.) — raw HTTP rather than a third-party WhatsApp wrapper library, to
  avoid depending on an unofficial package that can lag behind Meta's API
- **ngrok** (with a free static domain) — exposes the local webhook endpoint
  over a stable public HTTPS URL across restarts, no owned domain required

### Google APIs (Gmail, Classroom, Drive)
- `google-api-python-client` — REST calls to all three APIs
- `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2` — OAuth2 flow
  and credential refresh
- Scopes needed: `gmail.readonly`, `gmail.send`, `gmail.compose` (drafts);
  `classroom.courses.readonly`, `classroom.coursework.me`,
  `classroom.announcements.readonly`,
  `classroom.student-submissions.me.readonly` (write-scope
  `classroom.student-submissions.students` deferred to the v2 API turn-in
  path); `drive.readonly` (download attachments) + `drive.file` (upload your
  own final submission file, not broad `drive` access)

### Assignment drafting
- **Claude Code CLI**, invoked headless (`claude -p ... --resume <id>`) as a
  subprocess — no new install needed, uses your existing Pro/Max login; no
  separate Anthropic API key/billing involved

### Document conversion
- **Pandoc** (system binary, `sudo apt install -y pandoc`) — DOCX/ODT/RTF/PPTX
  → Markdown on the way in, Markdown → `final.docx` on the way out
- `pypandoc` — thin Python wrapper around the Pandoc binary, instead of
  hand-rolling subprocess calls

### Scheduler
- `APScheduler` — in-process, fires the Gmail/Classroom polling job a few
  times a day inside the same long-running program as the webhook server

### Database
- **PostgreSQL** (`sudo apt install -y postgresql`) — single local instance,
  holds LangGraph's checkpoints plus app-specific tables (dedup milestones,
  Gmail digest checkpoint, Claude Code session IDs per assignment)
- Plain `psycopg` for the small number of app-specific tables — no ORM
  (SQLAlchemy, etc.) given the scope is a handful of simple tables, not worth
  the extra abstraction

### Config & secrets
- `python-dotenv` — loads the local `.env` file (Google OAuth client
  secrets, Meta access token, ngrok authtoken)

### Drafting concurrency
- No external task queue (Celery/Redis, etc.) — the one-at-a-time Claude
  Code queue is implemented in-process with a simple `asyncio.Lock`/queue,
  matched to the scale of a single-user tool

### Not needed
- No vector store/RAG — per-assignment source material is a handful of
  files read directly by Claude Code, not a large corpus needing semantic
  search
- No separate web-search API (SerpAPI, Brave Search, etc.) — Claude Code's
  built-in WebSearch tool covers the drafting step's research needs
- No OS keychain/secret manager, no Celery/Redis, no SQLAlchemy — all
  deliberately deferred past v1 per the decisions above

---

## Architecture

### System overview
```
Meta WhatsApp Cloud API
   │  inbound: webhook POST          outbound: Graph API send call
   ▼
ngrok (static domain) ──► FastAPI webhook route ──► LangGraph app
                                                        │
                       ┌────────────────────────────────┼───────────────────────────┐
                       ▼                                ▼                           ▼
                Google APIs                     Claude Code CLI              Pandoc (subprocess)
             (Gmail/Classroom/Drive)         (subprocess, headless,        DOCX/ODT/RTF/PPTX ⇄ MD
                                                --resume per session)

                                              PostgreSQL
                                    (LangGraph checkpoints + app-state tables)

APScheduler (same process, separate from LangGraph)
   └─► poll_gmail_job / poll_classroom_job — plain async functions sharing
       the same Google API clients, Postgres pool, and WhatsApp-send helper
```
The scheduler is deliberately **not** routed through LangGraph. Proactive
checks (per the Proactive vs on-demand decision above) are always read-only,
single-shot, and never need approval or multi-turn state — forcing them
through the graph/interrupt machinery would add nothing. They're plain
functions that read/write the same Postgres tables and call the same
Google-API/WhatsApp helpers the graph nodes use, to avoid duplicating that
logic in two places.

### LangGraph thread model
LangGraph persists state per `thread_id` via the Postgres checkpointer — the
question is what maps to a thread:

- **Main/router thread** (`thread_id = "user:<your-whatsapp-number>"`):
  handles every inbound message that isn't clearly about one specific
  in-flight item — read/summarize commands, "what's due", "list courses",
  and recognizing "work on this assignment" as a request to start a new
  assignment thread. Mostly stateless per turn; no long-lived interrupts of
  its own.
- **Assignment thread** (`thread_id = "assignment:<course_id>:<coursework_id>"`):
  created the first time you say "work on this." Its own graph: ingest
  materials → invoke Claude Code (new session) → **interrupt** (wait for your
  approve/revise/reject) → revise loops back through Claude Code with
  `--resume`, approve moves to submission prep → a second **interrupt** for
  the submission approval → done. This is what lets a draft sit paused for
  hours or days waiting on you, durably, without holding up anything else.
- **Email-draft thread** (`thread_id = "email:<uuid>"`): same pattern, one
  interrupt for send approval, scoped separately so multiple email drafts in
  flight at once stay distinguishable (per "Referring to a specific pending
  item" above).

Each is an independent thread specifically so multiple assignments/emails
can each be sitting at their own paused interrupt simultaneously — resuming
one has no effect on the others.

### Crash recovery
A thread paused **at an interrupt** (waiting on your WhatsApp reply) is
already safe — Postgres has its checkpoint, nothing is lost if the process
restarts, and your next reply resumes it normally. No special handling
needed there.

The gap is a thread that was **actively mid-node** when the process crashed
(e.g. Claude Code's subprocess still running, or mid-download of an
attachment) — LangGraph only checkpoints between nodes, so that in-progress
work is lost, and nothing resumes it automatically; it would otherwise sit
stuck at its last checkpoint silently, indefinitely. To close this: on
process startup, scan for any thread whose last checkpoint is neither a
terminal state nor a known paused-interrupt — i.e. it was mid-execution when
the crash happened — and send a heads-up ("I was working on the Bio essay
when I restarted — want me to retry?") rather than leaving it stuck without
telling you, consistent with the "never fabricate, never silently drop" error
handling principle above.

### Routing a reply to the right pending thread
A small Postgres table, `pending_items` (`whatsapp_message_id`, `thread_id`,
`item_type`, `display_name`, `status`), is written every time the agent sends
a draft/notification message. On an inbound reply:
1. **Reply-to-message** — WhatsApp's payload includes the message ID you
   replied to; look it up in `pending_items` and resume that `thread_id`
   directly via LangGraph's `Command(resume=...)`. Unambiguous.
2. **Fresh message, no reply-to** — routed through the main thread, which
   fuzzy-matches your text against `display_name` of currently pending
   items (e.g. "approve the Bio essay"); only falls back to asking you to
   disambiguate if the match is genuinely unclear, per the behavior already
   defined.

### Drafting concurrency in practice
The one-at-a-time Claude Code rule is enforced with a plain in-process
`asyncio.Lock` around the "invoke Claude Code" step — not a Postgres
advisory lock — because the scheduler decision already accepted that a
process restart requires re-triggering in-flight work manually; adding
cross-restart lock durability here would be inconsistent with that.

### Database (app-specific tables, alongside LangGraph's own checkpoint tables)
- `pending_items` — see above
- `email_checkpoint` (single row: `last_history_id`, `updated_at`) — the
  Gmail digest checkpoint
- `notified_milestones` (`course_id`, `coursework_id`, `milestone_type`,
  `notified_at`) — assignment/announcement notification dedup
- `claude_sessions` (`assignment_thread_id`, `claude_session_id`,
  `updated_at`) — session ID to `--resume`, per assignment

### Module layout
```
agent/
  main.py                  # FastAPI app, mounts webhook route, starts APScheduler
  config.py                # loads .env
  webhook/routes.py        # verifies Meta signature, parses payload, invokes LangGraph
  graph/
    state.py               # LangGraph state schemas
    router_graph.py         # main/router thread graph
    assignment_graph.py     # per-assignment graph (ingest → draft → interrupt → revise/submit)
    email_graph.py           # per-email-draft graph
    nodes/
      classroom.py, gmail.py, drive.py    # Google API calls
      ingestion.py                          # attachment resolution + Pandoc + fail-fast
      claude_code.py                        # subprocess wrapper (new/--resume)
      whatsapp_send.py                      # composes + sends via Graph API
  scheduler/jobs.py         # poll_gmail_job, poll_classroom_job — plain functions
  db/
    schema.sql               # pending_items, email_checkpoint, notified_milestones, claude_sessions
    repo.py                   # thin data-access functions (psycopg)
  workspace/paths.py         # ~/agent-workspace/<course>/<assignment>/ path helpers
```