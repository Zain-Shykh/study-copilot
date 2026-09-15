# Architecture

Diagrams of the system as it's actually built today. See `CLAUDE.md` for
the prose version and `docs/product_definition.md` for behavior/permission
rules.

## 1. High-level system architecture

One long-running process with two independent halves — a webhook path that
answers on-demand messages, and a scheduler that proactively checks Gmail
and Classroom. Both share the same Postgres pool, Google API clients, and
WhatsApp-send helper.

```mermaid
flowchart TB
    subgraph EXT["External services"]
        WA["WhatsApp Business<br/>(Meta Cloud API)"]
        GAPI["Google APIs<br/>Gmail / Classroom / Drive"]
        GEM["Gemini API<br/>(intent classification, summarization)"]
        CC["Claude Code CLI<br/>(headless subprocess,<br/>user's own Pro/Max login)"]
    end

    NGROK["ngrok tunnel<br/>(public HTTPS endpoint)"]
    WA <-- "webhook POST (inbound) /<br/>Graph API send (outbound)" --> NGROK

    subgraph PROC["FastAPI process — agent/main.py"]
        direction TB
        HOOK["Webhook route<br/>agent/webhook/routes.py"]
        ROUTER["Router Graph<br/>(LangGraph)"]
        ASSIGN["Assignment Graph<br/>(LangGraph, one instance<br/>per in-flight assignment)"]
        SCHED["APScheduler<br/>agent/scheduler/jobs.py<br/>fires 08:00 / 20:00"]

        NGROK --> HOOK
        HOOK --> ROUTER
        ROUTER -. "asyncio.create_task<br/>(fire-and-forget)" .-> ASSIGN
    end

    PG[("PostgreSQL<br/>— LangGraph checkpoints (per thread_id)<br/>— pending_items (reply routing)<br/>— oauth_credentials<br/>— email_checkpoint<br/>— notified_milestones<br/>— claude_sessions")]

    ROUTER <--> PG
    ASSIGN <--> PG
    SCHED <--> PG

    ROUTER --> GAPI
    ROUTER --> GEM
    ASSIGN --> GAPI
    ASSIGN --> CC
    SCHED --> GAPI

    ROUTER -- "send reply" --> WA
    ASSIGN -- "send draft / result" --> WA
    SCHED -- "send digest / notification" --> WA
```

**Why it's split this way:** proactive checks are always read-only and
never need approval, so they deliberately bypass LangGraph entirely —
plain async functions, not graph nodes. Everything that can *write*
(send email, submit an assignment) only ever happens inside the LangGraph
graphs, gated behind explicit `interrupt()` approval pauses.

## 2. Router graph — as implemented today

Thread ID: `user:<whatsapp-number>`. One instance, long-lived, handles
every on-demand command.

```mermaid
flowchart TD
    START(["inbound WhatsApp message"]) --> RE["route_entry_node<br/>(reset per-turn state)"]

    RE -- "reply targets router's own<br/>pending question" --> KIND{"pending_question.kind?"}
    RE -- "reply-to targets an open<br/>pending_items row" --> HPI["handle_pending_item_reply"]
    RE -- "fresh command" --> CI["classify_intent_node<br/>(Gemini, forced function call)"]

    KIND -- "confirm_start_assignment" --> HCONF["handle_confirmation_node"]
    KIND -- "disambiguate_assignment" --> HDIS["handle_disambiguation_node"]
    KIND -- "disambiguate_pending_item" --> HPD["handle_pending_item_disambiguation_node"]

    CI --> INTENT{"intent?"}
    INTENT -- "answer_question" --> AQ["answer_question_node"]
    INTENT -- "work_on_assignment" --> RA["resolve_assignment_node<br/>(fuzzy-match vs live Classroom data)"]
    INTENT -- "respond_to_pending" --> RP["resolve_pending_item_node<br/>(fuzzy-match vs open pending_items)"]
    INTENT -- "unrecognized" --> FB["fallback_node"]

    subgraph LOOP["bounded Gemini tool-calling loop (max 4 remote calls)"]
        AQ --> DECIDE{"model reads question,<br/>decides what it needs"}
        DECIDE -- "call a tool" --> TOOLS["get_courses()<br/>get_all_assignments()<br/>get_missing_assignments()<br/>get_announcements(course_name?)<br/>get_recent_emails(filters...)"]
        TOOLS -- "raw data back" --> DECIDE
        DECIDE -- "enough to answer" --> ANSWER["model writes the final<br/>reply itself, in its own words"]
    end

    RA -- "confident match" --> HCONF
    RA -- "ambiguous" --> HDIS
    RA -- "no match" --> SEND
    HDIS --> HCONF
    HCONF -- "confirm" --> SPAWN1(["spawn Assignment Graph<br/>(background task)"])
    HCONF -- "decline" --> SEND

    RP -- "exactly one pending item" --> SPAWN2(["resume Assignment Graph<br/>(background task,<br/>Command(resume=...))"])
    RP -- "ambiguous" --> HPD
    RP -- "none / no match" --> SEND
    HPD --> SPAWN2
    HPI --> SPAWN2

    ANSWER --> SEND["send_reply_node<br/>(only node that calls<br/>the WhatsApp send API)"]
    FB --> SEND
    SPAWN1 --> SEND

    SEND --> END(["reply sent (or nothing,<br/>if a background resume<br/>was dispatched — Decision #3)"])
```

Note the two different "spawn" edges: confirming "work on X" starts a
**brand-new** assignment-graph thread; resolving a reply to something
already in flight **resumes** an existing paused one via
`Command(resume=...)`. Either way it's dispatched as a detached
`asyncio.create_task`, tracked in `app.state.background_tasks` so it
survives being referenced only locally — the router returns immediately
either way, keeping the webhook fast regardless of how long drafting takes.

`answer_question_node` replaces what used to be separate `classroom_node`/
`gmail_node` handlers wired to fixed intents (`list_courses`/`whats_due`/
`summarize_emails`/`search_emails`) with a single bounded Gemini
tool-calling loop (spec: `specs/tool-calling-read-answers.md`) — the model
calls `get_courses`/`get_all_assignments`/`get_missing_assignments`/
`get_announcements`/`get_recent_emails` itself, gets raw data back, and
writes its own reply instead of picking from a fixed set of canned
formatters. This fixed two real bugs hit during live testing: "what's
due" used to default to a hardcoded 48-hour window, silently dropping an
assignment due further out; "latest announcement" had no matching intent
at all and fell through to Gmail search. `work_on_assignment` and
`respond_to_pending` are unaffected — they stay deterministic,
classification-routed state-machine transitions (Decision #5 in the spec),
not free-form Q&A, since they trigger side effects (dispatching a
background graph run) rather than just producing an answer.

## 3. Assignment graph — as implemented today

Thread ID: `assignment:<course_id>:<coursework_id>`. One instance per
assignment; multiple can be paused at their own `interrupt()` simultaneously.

```mermaid
flowchart TD
    START(["spawned with course_id,<br/>coursework_id, title"]) --> ING["ingest_node<br/>(fail-fast — stops before<br/>drafting on any attachment failure)"]

    ING -- "failed" --> REL
    ING -- "success" --> DRAFT["draft_node<br/>(headless Claude Code,<br/>sandboxed to this assignment's folder)"]
    DRAFT --> SAVE["save_session_node<br/>(Postgres: claude_sessions)"]
    SAVE --> REL["relay_node<br/>(sends draft + creates<br/>pending_items row)"]

    REL -- "ingest/draft failed" --> END1(["END — failure message sent"])
    REL -- "success" --> AWAIT1["await_review_node<br/>interrupt() — PAUSED"]

    AWAIT1 -.->|"durable across restarts<br/>(Postgres checkpoint)"| RESUME1(["user replies"])
    RESUME1 --> PARSE1["parse_review_node<br/>(Gemini: approve / revise / reject)"]

    PARSE1 -- "revise" --> DRAFT
    PARSE1 -- "reject" --> END2(["END"])
    PARSE1 -- "approve" --> ASK["ask_submit_node<br/>(sends submit-confirm question,<br/>opens a new pending_items row)"]

    ASK --> AWAIT2["await_submit_node<br/>interrupt() — PAUSED"]
    AWAIT2 -.->|"durable across restarts"| RESUME2(["user replies"])
    RESUME2 --> PARSE2["parse_submit_node<br/>(Gemini: confirm / decline)"]

    PARSE2 -- "decline" --> END3(["END"])
    PARSE2 -- "confirm" --> PREP["submission_prep_node<br/>(package per manifest,<br/>upload to Drive)"]
    PREP --> RELS["relay_submit_node<br/>(sends Drive links + Turn In instructions)"]
    RELS --> END4(["END"])
```

The two `interrupt()` calls are the actual pause points — LangGraph
suspends real execution there, and the Postgres checkpoint is the only
thing keeping that state alive. That's what makes an approval survive a
process restart: a crash after "Starting on X…" but before the draft is
ready is not recoverable this way (still executing, no checkpoint to
resume from) — a crash *after* the draft was sent, while waiting on your
"approve"/"revise" reply, is fully recoverable.

## Thread-ID summary

| Graph | Thread ID pattern | Lifetime |
|---|---|---|
| Router | `user:<whatsapp-number>` | One per user, effectively permanent |
| Assignment | `assignment:<course_id>:<coursework_id>` | One per assignment, from "work on X" to terminal (reject/decline/submit) |
| Email-draft | `email:<uuid>` | **Not implemented yet** — scaffold only |
