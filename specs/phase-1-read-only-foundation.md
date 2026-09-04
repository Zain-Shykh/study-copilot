# Phase 1 — Read-Only Foundation — Spec

Source: `docs/implementation_plan.md` ("Phase 1"), `docs/product_definition.md`
(Interface, In-scope Gmail/Classroom, Permission matrix, Ambiguity handling,
Tech stack, Architecture), `docs/database_schema.md`, `specs/phase-0-environment-setup.md`.

## Decisions made for this phase (not fully pinned down by the docs above —
confirmed with the user before writing this spec)

1. **Intent classification and email summarization both use a lightweight
   Anthropic API call**, not keyword/regex matching and not the Claude Code
   CLI. Rationale discussed and confirmed with the user:
   - "Summarize my unread emails" needs to read as an actual condensed
     summary (per `docs/product_definition.md`'s "Summarize one email or a
     batch" capability), which a snippet listing wouldn't satisfy.
   - Once an LLM call exists for summarization, using it for intent
     classification too is more robust to phrasing variety than keyword
     matching, and Phase 2/3 will need real free-text parsing anyway
     (revision feedback, approval intent) — this dependency isn't wasted.
   - Reusing the Claude Code CLI subprocess (instead of a plain API call) was
     ruled out: it's structurally scoped in `docs/product_definition.md`'s
     "Claude Code interaction contract" to assignment drafting only (no
     Google API access, workspace-folder-scoped), and the app's single
     `asyncio.Lock` around Claude Code invocations exists specifically to
     serialize *drafting* runs — routing every chat message through that
     same lock would queue ordinary reads behind an in-progress draft,
     which contradicts reads being always-available/no-approval per the
     Permission Matrix.
   - **This introduces a new dependency not in `docs/product_definition.md`'s
     current "Tech stack" section: a separate `ANTHROPIC_API_KEY` and the
     `anthropic` Python SDK, billed independently from the Claude Code
     Pro/Max usage.** `docs/product_definition.md`'s "Decisions (locked) #2"
     only commits to no auto-send path, and its tech-stack rationale for
     avoiding a separate Anthropic API key is scoped to *drafting*
     specifically — but the doc doesn't currently mention this new
     dependency at all. **Recommend updating `docs/product_definition.md`'s
     Tech stack section once this spec is approved**, so it stays the
     accurate source of truth; not done as part of this spec since spec
     files describe implementation, not edit the locked product doc.
2. **Google OAuth credentials for the real app are persisted in a new
   Postgres table**, not a file (unlike Phase 0's throwaway
   `scripts/.google_token.json`), per `specs/phase-0-environment-setup.md`
   §7's note that Phase 1 designs this properly. New table
   `oauth_credentials` (see §3 below) — **this is an addition to
   `docs/database_schema.md`'s four documented tables; that doc should be
   updated alongside this spec's approval.**
3. **WhatsApp access token**: the user will generate a permanent Meta
   System User access token (via Meta Business Settings → System Users) as a
   one-time manual step, set once in `.env`'s `META_WHATSAPP_ACCESS_TOKEN`,
   replacing Phase 0's 24h temporary token. No in-app token refresh logic is
   built in this phase — that's an out-of-band manual setup step, listed in
   §6 below.
4. **Model choice for the Anthropic API calls**: `claude-haiku-4-5-20251001`
   — fast/cheap, appropriate for short classification and summarization
   calls (not full agentic work). Configurable via `ANTHROPIC_MODEL` in
   `.env` (defaults to this value in code if unset), so it can be changed
   without a code edit if the user wants a different model later.

---

## 1. Objective (restated from the plan)

WhatsApp bot + Gmail read/summarize + Classroom list/check, entirely via
on-demand chat commands. No writing, no drafting, no proactive messages, no
approval interrupts.

## 2. New files / modules

```
agent/
  config.py                        # loads/validates .env into a Settings object
  main.py                          # FastAPI app, startup: Postgres pool + checkpointer + graph build
  llm.py                           # NEW — Anthropic client wrapper: classify_intent(), summarize_emails()
  google_auth.py                   # NEW — credential load/refresh/persist, Google API client builders
  setup_google_auth.py             # NEW — one-time interactive OAuth bootstrap (run manually before first start)
  webhook/routes.py                # GET verify handshake, POST inbound message handler
  graph/
    state.py                       # RouterState TypedDict
    router_graph.py                # builds the router StateGraph
    nodes/
      gmail.py                     # list/search/summarize
      classroom.py                 # list_courses, list_assignments
      whatsapp_send.py             # send_whatsapp_message() — plain async function, also a graph node
  db/
    schema.sql                     # add oauth_credentials table
    repo.py                        # get/save_google_credentials()
```

Everything else in the module layout (`agent/scheduler/`,
`agent/graph/assignment_graph.py`, `agent/graph/email_graph.py`,
`agent/graph/nodes/claude_code.py`, `agent/graph/nodes/drive.py`,
`agent/graph/nodes/ingestion.py`, `agent/workspace/`) stays as an empty
docstring-only stub — untouched in this phase, per the plan's out-of-scope
list.

---

## 3. Database changes

### 3.1 New table: `oauth_credentials`

Added to `agent/db/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS oauth_credentials (
    provider          TEXT PRIMARY KEY,       -- always 'google' in v1 (single provider)
    credentials_json  TEXT NOT NULL,           -- google.oauth2.credentials.Credentials.to_json()
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

One row (`provider = 'google'`), holding the full serialized `Credentials`
object (access token, refresh token, scopes, expiry) as JSON text — mirrors
exactly what Phase 0's script cached to a file, just relocated to Postgres.

### 3.2 `agent/db/repo.py` additions

```python
def get_google_credentials(conn: psycopg.Connection) -> Credentials | None:
    """SELECT credentials_json FROM oauth_credentials WHERE provider = 'google'.
    Returns None if no row exists (not yet bootstrapped).
    Deserializes via Credentials.from_authorized_user_info(json.loads(row)).
    """

def save_google_credentials(conn: psycopg.Connection, creds: Credentials) -> None:
    """INSERT INTO oauth_credentials (provider, credentials_json, updated_at)
    VALUES ('google', %s, now())
    ON CONFLICT (provider) DO UPDATE SET credentials_json = EXCLUDED.credentials_json,
                                          updated_at = now()."""
```

### 3.3 LangGraph checkpointer

`agent/main.py` startup calls `PostgresSaver.setup()` (from
`langgraph-checkpoint-postgres`, sync — or `AsyncPostgresSaver.setup()` if
using the async variant) once against `DATABASE_URL`, creating LangGraph's
own checkpoint tables if they don't exist yet. This is the first phase that
touches them (Phase 0 explicitly left them uncreated). The checkpointer
instance is then passed to `StateGraph.compile(checkpointer=...)` when
building the router graph — wired up now even though no node in this phase's
graph pauses at an `interrupt()`, per the plan's explicit requirement ("later
phases depend on it already being in place").

No other Phase 0 tables (`pending_items`, `email_checkpoint`,
`notified_milestones`, `claude_sessions`) are read or written in this phase.

---

## 4. `agent/config.py`

```python
@dataclass
class Settings:
    database_url: str
    google_oauth_client_id: str
    google_oauth_client_secret: str
    meta_whatsapp_access_token: str
    meta_whatsapp_phone_number_id: str
    meta_webhook_verify_token: str
    meta_app_secret: str
    my_whatsapp_number: str
    anthropic_api_key: str
    anthropic_model: str  # defaults to "claude-haiku-4-5-20251001" if env var unset

def load_settings() -> Settings:
    """Calls dotenv.load_dotenv(); reads each field from os.environ.
    Raises RuntimeError(f"Missing required env var: {name}") listing every
    missing var at once (not just the first) if any required field is empty —
    fail fast at process startup, not on first use."""
```

`.env.example` additions needed for this phase:
```diff
 GOOGLE_OAUTH_CLIENT_ID=
 GOOGLE_OAUTH_CLIENT_SECRET=
+
+ANTHROPIC_API_KEY=
+ANTHROPIC_MODEL=claude-haiku-4-5-20251001
```
(`MY_WHATSAPP_NUMBER`, `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN` already
exist from Phase 0.)

`load_settings()` is called once in `agent/main.py` at startup; the resulting
`Settings` instance is stored on `app.state.settings` and threaded through to
graph nodes via LangGraph's `config["configurable"]` at invocation time (or a
module-level singleton — either is fine; spec leaves this as an
implementation detail since it doesn't affect behavior or the acceptance
criteria).

---

## 5. Google OAuth (`agent/google_auth.py`, `agent/setup_google_auth.py`)

### 5.1 One-time bootstrap: `agent/setup_google_auth.py`

Run manually once, before the server is started for the first time:
`.venv/bin/python -m agent.setup_google_auth`.

```python
SCOPES = [  # same 9 scopes as scripts/verify_google_oauth.py — see that file
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

def main() -> None:
    """Loads settings. Runs InstalledAppFlow.from_client_config(...).run_local_server(port=0)
    (opens system browser for consent — same shape as Phase 0's script, minus
    the file-cache step). On success, opens a psycopg connection to
    DATABASE_URL and calls repo.save_google_credentials(conn, creds).
    Prints "Google credentials stored." and exits 0.
    If DATABASE_URL is unreachable, prints the connection error and exits 1
    without having lost the just-obtained credentials from memory (i.e. don't
    swallow the consent result if the DB write fails — print the credentials'
    refresh token status so the user isn't forced to re-consent, then exit 1)."""
```

This file is a **permanent part of the app** (unlike Phase 0's
`scripts/verify_google_oauth.py`, which stays a disposable, unrelated
throwaway check per that phase's spec — the two are not merged, since one
writes to a local cache file and the other to Postgres, and Phase 0's script
must keep working standalone with no dependency on Phase 1 code).

### 5.2 Runtime credential access: `agent/google_auth.py`

```python
def get_credentials(conn: psycopg.Connection) -> Credentials:
    """Calls repo.get_google_credentials(conn). Raises RuntimeError(
    "No Google credentials found — run `.venv/bin/python -m agent.setup_google_auth` first."
    ) if None (not yet bootstrapped).
    If creds.expired and creds.refresh_token: calls creds.refresh(Request()),
    then repo.save_google_credentials(conn, creds) to persist the refreshed
    access token/expiry — write-through on every refresh so the stored copy
    never goes stale.
    Returns the valid Credentials."""

def build_gmail_client(creds: Credentials):
    """googleapiclient.discovery.build("gmail", "v1", credentials=creds)"""

def build_classroom_client(creds: Credentials):
    """googleapiclient.discovery.build("classroom", "v1", credentials=creds)"""
```

Called once per inbound message at the start of graph execution (a
`load_google_clients` step, see §7) — credentials are cheap to check/refresh
per-request at this message volume (single user, on-demand only); no
in-memory caching across requests is needed for this phase's scale, so
there's no staleness risk from caching an expired client.

### 5.3 Errors

- **Refresh fails** (refresh token itself revoked/expired —
  `google.auth.exceptions.RefreshError`): this is the auth-error case from
  `docs/product_definition.md`'s error handling — never auto-retried,
  surfaced immediately. `load_google_clients` catches this, and the graph's
  reply becomes: `"Google access has expired — please re-run
  `.venv/bin/python -m agent.setup_google_auth` to re-authenticate."` The
  webhook still replies 200 to Meta (message received, handled), sends this
  text back over WhatsApp.
- **No credentials row at all**: same message text as above (bootstrap never
  run) — both cases converge on the same user-facing instruction, no need to
  distinguish "never set up" from "revoked" in the reply.

---

## 6. WhatsApp

### 6.1 Manual setup (documented here, not scripted — mirrors
`specs/phase-0-environment-setup.md`'s runbook style)

Meta Business Settings → Users → System Users → create a system user →
assign it the WhatsApp app with `whatsapp_business_messaging` permission →
generate a token with no expiration → replace `META_WHATSAPP_ACCESS_TOKEN` in
`.env` with this permanent token. This replaces Phase 0's 24h temporary
token; `scripts/verify_whatsapp_send.py` continues to work unchanged against
whichever token is currently in `.env` (it doesn't care which kind).

### 6.2 `agent/webhook/routes.py`

```python
@router.get("/webhook")
def verify_webhook(request: Request) -> PlainTextResponse:
    """Reads hub.mode, hub.verify_token, hub.challenge query params.
    If mode == "subscribe" and verify_token == settings.meta_webhook_verify_token:
        return PlainTextResponse(challenge, status_code=200)
    Else: return PlainTextResponse("", status_code=403)."""

@router.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """1. Reads raw body bytes.
    2. Verifies X-Hub-Signature-256 header: HMAC-SHA256(app_secret, raw_body),
       hex digest, compared to the header (stripped of its "sha256=" prefix)
       via hmac.compare_digest. Mismatch -> 403, no further processing.
    3. Parses JSON body. Meta's shape:
       entry[0].changes[0].value.messages[0] — if absent (e.g. a status-update
       callback for message delivery/read receipts, not an inbound message),
       return 200 immediately with no action — these callbacks are expected
       and not an error.
    4. Extracts: sender = messages[0]["from"], msg_id = messages[0]["id"],
       text = messages[0]["text"]["body"] (if messages[0]["type"] != "text",
       e.g. an image/voice note: reply "I can only read text messages right
       now." and return 200 — no graph invocation).
    5. If sender != settings.my_whatsapp_number: return 200 with no action
       (single-user product — silently ignore, not an error worth surfacing
       to anyone, since Meta's test number isn't public).
    6. Invokes the router graph:
       result = await graph.ainvoke(
           {"inbound_text": text, "whatsapp_message_id": msg_id, "sender": sender},
           config={"configurable": {"thread_id": f"user:{sender}"}},
       )
    7. Calls send_whatsapp_message(sender, result["reply_text"]) — actually,
       per §7, the send happens as the graph's own last node, so routes.py
       does not call it separately; result is only used for logging.
    8. Returns Response(status_code=200) always, once processing (success or
       a caught failure reply) completes — Meta expects a fast 2xx regardless
       of whether the *content* of the reply was a success or failure message
       to the user.
    Uncaught exceptions inside step 6 (a bug, not a modeled failure case):
       caught by a blanket try/except around the graph invocation, logged
       with traceback to stderr, and replies "Something went wrong handling
       that — please try again." over WhatsApp, still returning 200 to Meta
       (a 5xx would make Meta retry-storm the same webhook event)."""
```

No background task queue — `graph.ainvoke` is awaited inline before
responding to Meta. At this phase's scale (single user, no long-running
nodes — no Claude Code, no Drive downloads), the two Anthropic calls plus one
Google API call per message complete well within Meta's webhook response
tolerance, so there's no need for a fire-and-forget/background-task pattern
here. (Revisit if Phase 2's Claude Code drafting step, which is genuinely
slow, needs a different handling shape when that phase is spec'd — not a
concern for Phase 1's read-only nodes.)

---

## 7. Router graph (`agent/graph/state.py`, `agent/graph/router_graph.py`)

### 7.1 State

```python
class RouterState(TypedDict):
    inbound_text: str
    whatsapp_message_id: str
    sender: str
    intent: str            # one of: "list_courses" | "whats_due" | "summarize_emails"
                            #   | "search_emails" | "unrecognized"
    intent_args: dict       # classifier-extracted args, shape depends on intent (see 7.2)
    reply_text: str         # final composed text, set by the gmail/classroom/fallback node
```

### 7.2 Node: `classify_intent` (`agent/llm.py`)

```python
def classify_intent(client: anthropic.Anthropic, model: str, text: str) -> tuple[str, dict]:
    """Single Anthropic messages.create() call using forced tool use (tool_choice
    = {"type": "tool", "name": "route_message"}) with a tool schema:

    {
      "name": "route_message",
      "input_schema": {
        "type": "object",
        "properties": {
          "intent": {
            "type": "string",
            "enum": ["list_courses", "whats_due", "summarize_emails", "search_emails", "unrecognized"]
          },
          "due_window_hours": {"type": "integer", "description": "only for whats_due; defaults to 48 if not mentioned"},
          "due_scope": {"type": "string", "enum": ["due_soon", "all", "overdue", "missing"], "description": "only for whats_due"},
          "email_count": {"type": "integer", "description": "only for summarize_emails/search_emails; defaults to 10"},
          "email_sender": {"type": "string", "description": "only for search_emails, if a sender/from was named"},
          "email_subject": {"type": "string", "description": "only for search_emails, if subject keywords were named"},
          "email_label": {"type": "string", "description": "only for search_emails, if a label/folder was named"}
        },
        "required": ["intent"]
      }
    }

    System prompt gives the model the exact defaults from
    docs/product_definition.md's "Ambiguity handling" section (10 most recent
    unread; 48h due-soon window) so the model fills them in rather than
    omitting them, and instructs it to classify as "unrecognized" for
    anything that isn't clearly one of the four supported read commands
    (drafting/sending/submitting requests included — those are out of scope
    this phase and must fall to the "not yet supported" reply, not be
    silently misrouted to a read node).

    Returns (intent, args_dict) parsed from the tool_use block's `input`.
    """
```

Errors: the Anthropic API call itself can fail (network/5xx/429) — treated
as a transient error per the general error-handling rule: retried up to 3
times with backoff (reuse `docs/product_definition.md`'s general policy,
implemented as a small local retry helper — no new dependency; Anthropic's
SDK has built-in retry with backoff for 429/5xx already enabled by default
in the `anthropic` Python SDK, so this is satisfied by using the SDK's
default `max_retries` rather than hand-rolling it). If retries are exhausted,
`classify_intent` raises; the graph's top-level node wraps this and sets
`reply_text = "Couldn't process that message right now — please try again."`
rather than propagating an unhandled exception to `routes.py`'s blanket
catch (keeps the "something went wrong" generic message reserved for actual
bugs, not a modeled/expected API failure).

### 7.3 Conditional routing

```python
def route_after_classify(state: RouterState) -> str:
    return {
        "list_courses": "classroom_node",
        "whats_due": "classroom_node",
        "summarize_emails": "gmail_node",
        "search_emails": "gmail_node",
        "unrecognized": "fallback_node",
    }[state["intent"]]
```

### 7.4 Node: `classroom_node` (`agent/graph/nodes/classroom.py`)

```python
def list_courses(classroom_service) -> list[dict]:
    """classroom_service.courses().list(courseStates=["ACTIVE"]).execute()["courses"],
    paginating via nextPageToken until exhausted."""

def list_assignments(classroom_service, courses: list[dict], scope: str, window_hours: int) -> list[dict]:
    """For each course: courseWork().list(courseId=course["id"], courseStates=["PUBLISHED"]).execute(),
    paginated. For each courseWork item with a dueDate/dueTime, compute a
    timezone-aware due datetime (Classroom's dueDate/dueTime are in the
    course's own timezone info via courseWork; if absent, treat as UTC —
    Classroom typically returns these in UTC already per its API docs) and
    filter per `scope`:
      - "due_soon": now <= due <= now + window_hours (courseWork with no due date excluded)
      - "all": every PUBLISHED courseWork item regardless of due date
      - "overdue": due < now
      - "missing": due < now AND (per-student submission state, see below, is
        not TURNED_IN/RETURNED)
    "missing" additionally calls
    courseWork().studentSubmissions().list(courseId=..., courseWorkId=...,
    userId="me").execute() per candidate assignment to check submission
    state — only called for assignments already past due (keeps the extra
    API calls bounded to the overdue subset, not every assignment)."""

def format_courses_reply(courses: list[dict]) -> str: ...
def format_assignments_reply(assignments: list[dict], scope: str, window_hours: int) -> str:
    """Plain formatted list per docs/product_definition.md's tone (structured,
    bullet points): course name, assignment title, due date/time (or "no due
    date"). States the applied scope/window in the reply when it's a default
    rather than explicitly requested, per Ambiguity handling
    (e.g. "Here's what's due in the next 48 hours:")."""
```

Node function: given `state["intent"]` and `state["intent_args"]`, calls
`list_courses` (for `list_courses` intent) or `list_courses` +
`list_assignments` (for `whats_due`, always fetching courses first since
Classroom's API has no cross-course assignment listing endpoint), then the
matching `format_*_reply`, setting `state["reply_text"]`.

Errors: Classroom API call failure — transient (429/5xx) retried 3x via the
`google-api-python-client`'s own retry-on-error support
(`googleapiclient.http.build_http()` / the `num_retries` param on
`.execute(num_retries=3)`) — no hand-rolled backoff loop needed, the client
library already supports this natively. Exhausted retries or a 401/403 (auth
error, distinct from the credential-refresh case in §5.3 — this is Classroom
API access itself being denied, e.g. API not enabled) set `reply_text` to
`f"Couldn't reach Classroom right now: {error}"`, per the on-demand
command-failure handling rule (plain report, no silent retry beyond the 3
attempts, no guess at an answer).

### 7.5 Node: `gmail_node` (`agent/graph/nodes/gmail.py`)

```python
def list_messages(gmail_service, query: str, max_results: int) -> list[dict]:
    """gmail_service.users().messages().list(userId="me", q=query, maxResults=max_results).execute(),
    then messages().get(userId="me", id=m["id"], format="metadata",
    metadataHeaders=["From", "Subject", "Date"]).execute() per result for
    headers, plus the top-level "snippet" field (included by default even in
    format=metadata) as the body preview passed to summarization."""

def build_query(intent_args: dict, unread_only: bool) -> str:
    """Builds a Gmail search query string:
    - summarize_emails: "is:unread" unless intent_args overrides (matches the
      documented default — 10 most recent unread, no sender/label/date given).
    - search_emails: from:{email_sender} / subject:{email_subject} /
      label:{email_label}, combined with AND (space-joined, Gmail's default),
      omitting any that weren't extracted by the classifier."""

def summarize_emails(client: anthropic.Anthropic, model: str, emails: list[dict]) -> str:
    """Anthropic messages.create() call (plain text response, no tool use):
    system prompt sets the docs/product_definition.md tone (formal,
    structured); user message lists each email's From/Subject/Date/snippet;
    asks for a concise digest, one entry per email, matching the "brief
    one-liner per new email" style already established for the Phase 4 email
    digest (reused here for consistency, even though this is the on-demand
    path, not the proactive one)."""
```

Node function: given `state["intent"]` and `state["intent_args"]`:
- `summarize_emails`: `build_query` → `list_messages` (default `max_results =
  intent_args.get("email_count", 10)`) → if the result list is empty, set
  `reply_text = "No unread emails found."` (no LLM call needed for an empty
  batch — never fabricate a summary of nothing) → else `summarize_emails` →
  set `reply_text`, prefixed with the stated default per Ambiguity handling
  (e.g. `"Here are your 10 most recent unread emails:\n\n" + summary`) only
  when `intent_args` didn't override the count/filter; when the user did
  specify args, state those instead (e.g. `"Emails from professor@uni.edu:"`).
- `search_emails`: same `list_messages` call, but the reply is a **plain
  formatted list** (sender/subject/date/snippet per email, no LLM
  summarization call) — "search" in `docs/product_definition.md`'s
  capability list ("Read and search inbox") is listing/finding matching
  emails, not summarizing them; only the explicit "Summarize" capability
  triggers the Anthropic summarization call. If the result list is empty:
  `reply_text = "No emails found matching that."`.

Errors: same shape as §7.4 — Gmail API failures via
`googleapiclient`'s `num_retries=3`, auth errors reported per §5.3, plain
failure message on exhaustion. The Anthropic summarization call failing
follows §7.2's error handling (SDK default retries, then a plain "couldn't
summarize right now" reply) — distinguished from the Gmail-fetch failure
message so the user knows which part failed (fetch vs. summarize).

### 7.6 Node: `fallback_node`

```python
def fallback_node(state: RouterState) -> RouterState:
    state["reply_text"] = (
        "I didn't understand that. I can: list your courses, tell you "
        "what's due, summarize your unread emails, or search your inbox."
    )
    return state
```

No retry/error path — this node can't fail (no external calls).

### 7.7 Node: `send_reply` (`agent/graph/nodes/whatsapp_send.py`)

```python
async def send_whatsapp_message(access_token: str, phone_number_id: str, to: str, body: str) -> str:
    """Same POST shape as scripts/verify_whatsapp_send.py's send_test_message,
    but async (httpx.AsyncClient) since it's called from an async graph node
    inside the FastAPI event loop, not a one-shot script. Returns the sent
    message's id.
    Raises httpx.HTTPStatusError on non-2xx — caller (the graph node) catches
    it, logs the error (can't notify the user over WhatsApp if WhatsApp
    itself is the thing failing), and lets routes.py's POST handler still
    return 200 to Meta (the inbound webhook was received and handled; the
    *outbound* send failing is a separate, already-logged problem — nothing
    productive comes from Meta retrying the inbound delivery)."""
```

Graph's final node: calls `send_whatsapp_message(..., to=state["sender"],
body=state["reply_text"])`. This node is also directly importable (not just
reachable via the graph) so Phase 4's scheduler jobs can call it later
without going through LangGraph, per `docs/product_definition.md`'s
architecture note that proactive jobs "share the same ... WhatsApp-send
helper as the graph nodes."

### 7.8 Graph assembly (`agent/graph/router_graph.py`)

```python
def build_router_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(RouterState)
    g.add_node("classify_intent", classify_intent_node)
    g.add_node("classroom_node", classroom_node)
    g.add_node("gmail_node", gmail_node)
    g.add_node("fallback_node", fallback_node)
    g.add_node("send_reply", send_reply_node)
    g.set_entry_point("classify_intent")
    g.add_conditional_edges("classify_intent", route_after_classify,
                             {"classroom_node": "classroom_node",
                              "gmail_node": "gmail_node",
                              "fallback_node": "fallback_node"})
    g.add_edge("classroom_node", "send_reply")
    g.add_edge("gmail_node", "send_reply")
    g.add_edge("fallback_node", "send_reply")
    g.add_edge("send_reply", END)
    return g.compile(checkpointer=checkpointer)
```

Google/Anthropic clients are built once per invocation inside
`classify_intent_node`/`classroom_node`/`gmail_node` (via a small
`get_google_clients(conn) -> (gmail_service, classroom_service)` helper in
`agent/google_auth.py`, and a module-level `anthropic.Anthropic(api_key=...)`
client built once at process startup and passed through `config` — the
Anthropic client itself is stateless/thread-safe and doesn't need per-request
construction, unlike the Google credentials which need a freshness check).

---

## 8. `agent/main.py`

```python
def create_app() -> FastAPI:
    """Loads Settings. Opens a psycopg connection pool against DATABASE_URL.
    Runs PostgresSaver.setup() once. Builds the Anthropic client. Builds the
    compiled router graph, storing it (plus the pool, settings, anthropic
    client) on app.state for webhook/routes.py to use.
    Mounts the webhook router.
    Does NOT start APScheduler in this phase (Phase 4's job)."""
```

Run via `.venv/bin/uvicorn agent.main:create_app --factory --reload` for
local dev (documented in a short "Running it" note, not a new script) —
paired with a separately-running `ngrok http --domain <domain> 8000` per
`specs/phase-0-environment-setup.md` §1.3, same as that phase's verification
setup but now pointing at the real app instead of the placeholder route.

---

## 9. Out of scope (restated from the plan, unchanged)

Drafting, sending, submitting, revision loops, approval interrupts, Claude
Code integration, attachment ingestion/Pandoc conversion, proactive/scheduled
jobs, `pending_items`/`notified_milestones`/`email_checkpoint`/
`claude_sessions` tables (unused until later phases, though already created
in Phase 0).

---

## 10. Acceptance criteria mapping

| Acceptance criterion (implementation_plan.md) | Satisfied by |
|---|---|
| "List my courses" returns actual enrolled Classroom courses | §7.4 `list_courses` + `format_courses_reply` |
| "What's due" returns assignments due within 48h by default | §7.4 `list_assignments` with `scope="due_soon", window_hours=48` (classifier default per §7.2's system prompt) |
| "Summarize my unread emails" returns a summary of the 10 most recent unread and states that default | §7.5 `summarize_emails` path, default `email_count=10`, `is:unread` query, reply prefixed with the stated default |
| No message ever requests approval or offers to draft/send/submit | No `interrupt()` anywhere in this graph (§7.8); classifier system prompt explicitly routes any such request to `unrecognized` (§7.2) |
| Unrecognized/ambiguous message gets a plain "didn't understand" reply, no crash | §7.6 `fallback_node`; §6.2 step 8's blanket try/except around graph invocation as a last-resort backstop |

---

## 11. Files changed/created — summary

| File | Change |
|---|---|
| `agent/config.py` | Implemented (`Settings`, `load_settings`) |
| `agent/main.py` | Implemented (`create_app`) |
| `agent/llm.py` | New — `classify_intent`, `summarize_emails` |
| `agent/google_auth.py` | New — `get_credentials`, `build_gmail_client`, `build_classroom_client` |
| `agent/setup_google_auth.py` | New — one-time interactive bootstrap CLI |
| `agent/webhook/routes.py` | Implemented (`GET`/`POST /webhook`) |
| `agent/graph/state.py` | Implemented (`RouterState`) |
| `agent/graph/router_graph.py` | Implemented (`build_router_graph`) |
| `agent/graph/nodes/gmail.py` | Implemented |
| `agent/graph/nodes/classroom.py` | Implemented |
| `agent/graph/nodes/whatsapp_send.py` | Implemented (`send_whatsapp_message`) |
| `agent/db/schema.sql` | Add `oauth_credentials` table |
| `agent/db/repo.py` | Implemented (`get_google_credentials`, `save_google_credentials`) |
| `pyproject.toml` | Add `anthropic` dependency |
| `.env.example` | Add `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` |
| `docs/database_schema.md` | **Recommend updating** to document `oauth_credentials` (not part of this spec's file changes, but should be kept in sync — flagging per §0) |
| `docs/product_definition.md` | **Recommend updating** Tech stack section to record the new Anthropic API dependency (see §0) |

No files under `agent/graph/assignment_graph.py`, `agent/graph/email_graph.py`,
`agent/graph/nodes/claude_code.py`, `agent/graph/nodes/drive.py`,
`agent/graph/nodes/ingestion.py`, `agent/scheduler/`, `agent/workspace/` are
touched.
