# Personal Agent

A WhatsApp-controlled personal assistant (single user) that reads Gmail and
Google Classroom, answers questions about them on demand, and — with explicit
approval at every write step — researches, drafts, and submits homework
assignments.

Full behavior/permission spec: [`docs/product_definition.md`](docs/product_definition.md).
Phase-by-phase build plan: [`docs/implementation_plan.md`](docs/implementation_plan.md).
Per-phase implementation specs: [`specs/`](specs/).

## What it does

- **On demand (via WhatsApp message):**
  - Read/search/summarize Gmail
  - List Classroom courses and assignments (all / due-soon / overdue / missing)
  - Fetch an assignment's details and attachments
  - Draft a homework response using Claude Code (headless, your Pro/Max login)
    plus web search — always shown to you before anything is finalized
  - Revise a draft, or approve it to move to submission
  - Draft and send email — **send always requires your explicit approval**
- **Proactively (no message from you needed):**
  - Twice a day (08:00 / 20:00), polls Gmail for new mail since the last
    check and pushes a batched digest
  - Polls Classroom for new assignments, new announcements, and due-soon/overdue
    items, and notifies once per item (deduplicated)
  - Never performs a write or requires approval — proactive checks are
    strictly read-only

## Architecture

One long-running process with two independent halves:

1. **Answering the user** — a FastAPI webhook (`agent/webhook/routes.py`)
   receives Meta WhatsApp callbacks (exposed via an ngrok tunnel) and routes
   them into a **LangGraph** app. State is checkpointed per-thread in
   Postgres, which is what lets an approval step survive a process restart.
2. **Watching things proactively** — an in-process **APScheduler** job
   (`agent/scheduler/jobs.py`) runs the read-only Gmail/Classroom polls
   directly, bypassing LangGraph entirely (no approval needed).

Assignment drafting is delegated to the `claude` CLI running headless
(`agent/graph/nodes/claude_code.py`), sandboxed to one assignment's own
working folder with no Bash and no Google credentials — it cannot send or
submit anything itself.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture writeup (thread model,
database tables, error-handling rules, Claude Code sandboxing contract).

## Prerequisites

- Python 3.11+
- PostgreSQL (system package, e.g. `apt install postgresql`)
- Pandoc (system package, e.g. `apt install pandoc`) — used for DOCX/ODT/RTF/PPTX
  attachment conversion
- The [`claude`](https://claude.com/product/claude-code) CLI, logged in with
  your own Pro/Max account (used headless for drafting — no separate Anthropic
  API key needed)
- An [ngrok](https://ngrok.com) account with a static domain (or an
  equivalent tunnel) — Meta requires a public HTTPS URL for webhooks
- Accounts/credentials described in
  [`docs/implementation_plan.md`](docs/implementation_plan.md#phase-0--environment--accounts-setup):
  a Google Cloud project (Gmail, Classroom, Drive APIs enabled; OAuth client
  credentials), a Meta developer app with a WhatsApp test number, and a
  Gemini API key (used for lightweight summarization, separate from Claude
  Code drafting)

## Setup

### 1. Install dependencies

Dependencies are isolated in a venv, never installed to system Python:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 2. Create the database

```bash
sudo -u postgres createuser local_agent_app --pwprompt
sudo -u postgres createdb local_agent --owner local_agent_app
psql "postgresql://local_agent_app:<password>@localhost:5432/local_agent" -f agent/db/schema.sql
```

This creates the app's six tables (`pending_items`, `email_checkpoint`,
`notified_milestones`, `claude_sessions`, `oauth_credentials`,
`processed_messages`). LangGraph's own checkpoint tables are created
automatically on first app startup.

### 3. Configure secrets

```bash
cp .env.example .env
```

Fill in every value in `.env`:

| Variable | Purpose |
|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | Google Cloud OAuth client (Gmail/Classroom/Drive) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Gemini API, used for summarization |
| `META_APP_ID` / `META_APP_SECRET` | Meta developer app |
| `META_WHATSAPP_ACCESS_TOKEN` / `META_WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Cloud API send credentials |
| `META_WEBHOOK_VERIFY_TOKEN` | Shared secret Meta uses to verify webhook subscription |
| `MY_WHATSAPP_NUMBER` | Your own WhatsApp number — the only number the bot will act on |
| `NGROK_AUTHTOKEN` / `NGROK_STATIC_DOMAIN` | Tunnel exposing the webhook publicly |
| `DATABASE_URL` | e.g. `postgresql://local_agent_app:<password>@localhost:5432/local_agent` |

### 4. Authenticate with Google (one-time)

```bash
.venv/bin/python -m agent.setup_google_auth
```

Runs the OAuth consent flow in your browser and stores the resulting token in
the `oauth_credentials` table. Re-run this if Google access ever expires and
can't silently refresh (you'll get a WhatsApp message telling you to).

## Running it

You need three things running at once:

**Terminal 1 — tunnel:**

```bash
ngrok http --url=<your NGROK_STATIC_DOMAIN> 8000
```

**Terminal 2 — app:**

```bash
.venv/bin/uvicorn agent.main:create_app --factory --host 0.0.0.0 --port 8000
```

Starts the webhook server and the twice-daily proactive-poll scheduler in
one process.

**One-time — point Meta at the tunnel:** in the Meta developer app dashboard
→ WhatsApp → Configuration, set the webhook URL to
`https://<your-ngrok-domain>/webhook`, the verify token to
`META_WEBHOOK_VERIFY_TOKEN`, and subscribe to the `messages` field.

Then message your WhatsApp test number from `MY_WHATSAPP_NUMBER` (e.g.
"what's due this week?") to confirm it responds.

## Observability (optional)

Set `LANGSMITH_TRACING_V2=true`, `LANGSMITH_API_KEY`, and `LANGSMITH_PROJECT`
in `.env` (get an API key at smith.langchain.com) to send traces to
LangSmith. Graph-level tracing (node execution order, full state at each
step) is automatic — no code changes needed. Every Gemini call in
`agent/llm.py` is also wrapped with `@traceable`, so each shows up as its
own span with the exact prompt and response. Traces go to LangSmith's
cloud, so only enable this with data you're okay leaving your machine.

## Verifying Phase 0 infrastructure

Throwaway scripts in `scripts/` (not imported by `agent/`, disposable) check
that each external dependency actually works:

```bash
.venv/bin/python scripts/verify_google_oauth.py     # completes OAuth, prints your Gmail address
.venv/bin/python scripts/verify_whatsapp_send.py     # sends a test WhatsApp message
.venv/bin/python scripts/verify_ngrok_tunnel.py      # confirms the tunnel is reachable
```

## Development workflow

This repo is **spec-driven**: no implementation code is written for a phase
until that phase's spec exists in `specs/` and has been explicitly approved.
See [`CLAUDE.md`](CLAUDE.md) for the full rule and the per-phase process.

Tests run via `pytest` (`.venv/bin/python -m pytest -q`, 232 passing). No
linter or formatter is configured yet.

Regenerate the pinned lockfile after adding/upgrading a dependency:

```bash
.venv/bin/pip freeze > requirements-lock.txt
```

`pyproject.toml` is the hand-edited source of truth for dependencies (loose
versions); `requirements-lock.txt` is the reproducible pin, not edited by
hand.
