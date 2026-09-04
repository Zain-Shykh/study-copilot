# Phase 0 — Environment & Accounts Setup — Spec

Source: `docs/implementation_plan.md` ("Phase 0"), `docs/product_definition.md`
(Decisions #7, #10; Tech stack), `docs/database_schema.md`.

Phase 0 has no LangGraph/webhook/graph logic. Its only deliverables are: (a)
manual account/infra setup performed by the user following the runbook below,
and (b) three throwaway Python scripts under `scripts/` that prove each
external dependency is actually reachable with real credentials. Nothing in
`scripts/` is imported by `agent/` now or in any later phase — each script is
self-contained (its own imports, its own env loading, no shared `scripts/`
helper module) so it can be deleted without affecting the real app.

No code under `agent/` is created or modified in this phase.

---

## 1. Manual setup runbook

Performed once, by the user, outside of any script. Listed here so the spec
is a complete record of what "Phase 0 done" means, even though Claude cannot
execute account-creation/browser-consent steps itself.

### 1.1 Google Cloud
Two distinct roles here: whoever *owns* the Cloud project/OAuth client, and
whoever the app is actually *authorized to act as* (the account whose Gmail
and Classroom get read). Confirmed with the user: both roles are the same
account — the **university Google account** — since that's where the actual
Gmail/Classroom data lives and there's no separate personal account involved.

1. Log into the **university Google account** at console.cloud.google.com,
   create a Google Cloud project there (any name, e.g. `personal-agent`).
2. APIs & Services → Enable APIs: **Gmail API**, **Google Classroom API**,
   **Google Drive API**.
3. APIs & Services → OAuth consent screen: External, **Testing** publish
   status, add the **university email** as a test user. Scopes don't need to
   be pre-declared here for Testing mode with a Desktop-app client.
4. Credentials → Create Credentials → OAuth client ID → Application type
   **Desktop app**. Copy the generated Client ID / Client Secret into
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`.

**Known risk, not yet confirmed either way**: some university Google
Workspace domains have an admin-level policy blocking third-party/unverified
OAuth apps from connecting at all, independent of which scopes are
requested (the same restriction `docs/product_definition.md` already flags
as a risk for the Classroom write API in v2 — it can also apply here, to any
external app, at the domain admin's discretion). This won't be known until
`verify_google_oauth.py` (§3.1) is actually run against the university
account. If the consent screen shows an error naming a blocked/disallowed
app rather than the normal permission-grant screen (e.g.
`admin_policy_enforced` or "this app is blocked"), that confirms the
domain restricts third-party OAuth apps — stop and go back to the user
rather than trying workarounds, since there's no client-side fix for an
admin-enforced policy.

### 1.2 Meta / WhatsApp
1. Create a Meta developer account at developers.facebook.com, create an app
   (type: Business).
2. Add the **WhatsApp** product to the app. Meta provisions a free test phone
   number automatically.
3. In WhatsApp → API Setup, add the university-account owner's own WhatsApp
   number as a verified test recipient (Meta sends a one-time code to that
   number).
3a. **Required before `verify_whatsapp_send.py` will work**: WhatsApp only
   allows sending a free-form text message to a recipient within an open
   24-hour messaging session (opened by the recipient messaging first, or by
   the business sending an approved template). There's no session yet with a
   freshly-added test recipient, so from the API Setup dashboard's "Send a
   message from your test number" panel, send the pre-built template message
   (e.g. "Order Confirmation") to the verified number, then **reply to it
   from the phone** (even just "ok"). Only after that reply does the session
   open both directions — `verify_whatsapp_send.py`'s plain-text send (§3.2)
   will otherwise fail with a recipient-outside-window error that looks like
   a bug in the script but isn't one.
4. Copy from the API Setup page into `.env`: `META_APP_ID`, `META_APP_SECRET`
   (App settings → Basic), the temporary **access token** shown on the API
   Setup page → `META_WHATSAPP_ACCESS_TOKEN` (valid 24h — fine for this
   phase's one-off send test; Phase 1 will need a longer-lived token, out of
   scope here), and the **Phone number ID** → `META_WHATSAPP_PHONE_NUMBER_ID`.
5. Set `META_WEBHOOK_VERIFY_TOKEN` in `.env` to any string of your choosing
   (e.g. a random 32-char value) — not used by any Phase 0 script, but
   required now so `.env` has no blank placeholders; it's the shared secret
   Meta's webhook handshake will use in Phase 1.
6. Set `MY_WHATSAPP_NUMBER` in `.env` to your verified test recipient number
   from step 3, in the digits-only E.164 form Meta's Graph API expects for
   the `to` field (e.g. `923001234567`, no leading `+`).

### 1.3 ngrok
1. Create an ngrok account, claim one free static domain, copy the authtoken.
2. Install the `ngrok` binary if not already present (`which ngrok`) — e.g.
   via the apt repo ngrok publishes, or the tarball from ngrok.com/download.
3. Run `ngrok config add-authtoken <token>` once — this writes the token into
   ngrok's own config file (`~/.config/ngrok/ngrok.yml`), so it does not need
   to be read from `.env` by any script.
4. Set `NGROK_AUTHTOKEN` (same token, kept in `.env` for reference/record —
   not read by `verify_ngrok_tunnel.py`, which relies on step 3 already being
   done) and `NGROK_STATIC_DOMAIN` in `.env` to the bare hostname only, no
   scheme (e.g. `my-name.ngrok-free.app`).

### 1.4 PostgreSQL
Postgres 16 is already installed and running (confirmed: `pg_lsclusters`
shows `main` cluster online on port 5432). Only a project-specific role/DB
need creating — the app must never connect as the `postgres` superuser role.

```bash
sudo -u postgres psql -c "CREATE ROLE local_agent_app LOGIN PASSWORD '<choose-a-password>';"
sudo -u postgres psql -c "CREATE DATABASE local_agent OWNER local_agent_app;"
psql "postgresql://local_agent_app:<password>@localhost:5432/local_agent" -f agent/db/schema.sql
```

Set `DATABASE_URL` in `.env` to
`postgresql://local_agent_app:<password>@localhost:5432/local_agent`.

This creates only the four app tables (`pending_items`, `email_checkpoint`,
`notified_milestones`, `claude_sessions`) via the existing
`agent/db/schema.sql` (already correct, no changes needed to it in this
phase). LangGraph's own checkpoint tables are **not** created here —
`langgraph-checkpoint-postgres`'s `PostgresSaver.setup()` creates those, and
it isn't invoked until Phase 1 wires up the checkpointer. This matches the
Phase 0 acceptance criterion, which lists only the four app tables.

### 1.5 Pandoc / venv
Both already confirmed present — no action:
- `pandoc` is on `PATH` (`/usr/bin/pandoc`).
- `.venv` has every dependency from `pyproject.toml` installed
  (`fastapi`, `uvicorn`, `httpx`, `google-api-python-client`, `google-auth*`,
  `pypandoc`, `apscheduler`, `python-dotenv`, `langgraph*`, `psycopg*`).
- `.venv/bin/python -c "import agent"` already succeeds today (every
  `agent/**/__init__.py` is empty, so importing the package triggers no
  submodule imports) — this stays true through this phase since no `agent/`
  file is touched.

---

## 2. `.env.example` changes

Two additions needed (both consumed by the scripts in §3), plus updating the
`DATABASE_URL` template to show the expected role-based shape instead of the
current superuser-implying default:

```diff
 META_WHATSAPP_ACCESS_TOKEN=
 META_WHATSAPP_PHONE_NUMBER_ID=
 META_WEBHOOK_VERIFY_TOKEN=
+MY_WHATSAPP_NUMBER=

 NGROK_AUTHTOKEN=
 NGROK_STATIC_DOMAIN=

-DATABASE_URL=postgresql://localhost/local_agent
+DATABASE_URL=postgresql://local_agent_app:CHANGE_ME@localhost:5432/local_agent
```

`.env` (the real, gitignored file) must have every one of these populated
with real values per §1 before the acceptance criteria can pass.

---

## 3. New files: `scripts/`

All three scripts:
- Load `.env` via `dotenv.load_dotenv()` at the top of `main()`.
- Read every value they need from `os.environ`; if a required variable is
  missing/empty, print `f"Missing required env var: {name}"` to stderr and
  `sys.exit(1)` before attempting any network call — fail fast on
  misconfiguration rather than letting a library raise an opaque error later.
- Have no `try/except` around errors they can't meaningfully handle (e.g. a
  network call to Google/Meta failing) beyond catching the specific
  library exception to print its actual status/message before exiting 1 —
  per the codebase's "never fabricate a result" principle: a failure prints
  plainly and exits non-zero, it never prints a fake success.
- Are runnable as `.venv/bin/python scripts/<name>.py` with no arguments.

### 3.1 `scripts/verify_google_oauth.py`

Proves the Google Cloud project, OAuth consent screen, and all three enabled
APIs are correctly configured and reachable from this codebase's dependency
set (`google-auth-oauthlib`, `google-api-python-client`).

```python
SCOPES = [
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
TOKEN_CACHE_PATH = pathlib.Path(__file__).parent / ".google_token.json"

def build_client_config(client_id: str, client_secret: str) -> dict: ...
def load_or_run_flow(client_id: str, client_secret: str) -> google.oauth2.credentials.Credentials: ...
def main() -> None: ...
```

All scopes from product_definition.md's "Scopes needed" are requested in one
consent grant — this proves the consent screen accepts the full set Phase 1+
will eventually need (catching a Testing-mode scope restriction now, rather
than piecemeal later), even though only Gmail is exercised by an actual API
call below. Requesting Classroom/Drive scopes without calling those APIs is
sufficient here: the acceptance criterion only requires printing the Gmail
profile, and a real Classroom/Drive smoke test needs live course/file data
that doesn't exist yet — that verification happens naturally in Phase 1/2.

Flow:
1. `main()` loads `.env`, reads `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET`, exits 1 if either is empty.
2. `load_or_run_flow`: if `TOKEN_CACHE_PATH` exists, load it via
   `Credentials.from_authorized_user_file`; if expired and has a refresh
   token, call `creds.refresh(Request())`. Otherwise (no cache, or refresh
   fails), build `client_config` via `build_client_config` (standard
   "installed" app shape: `auth_uri`
   `https://accounts.google.com/o/oauth2/auth`, `token_uri`
   `https://oauth2.googleapis.com/token`, `redirect_uris: ["http://localhost"]`),
   run `InstalledAppFlow.from_client_config(client_config, SCOPES).run_local_server(port=0)`
   — this opens the system browser for consent. On success, write
   `creds.to_json()` to `TOKEN_CACHE_PATH` so re-running the script doesn't
   require re-consenting.
3. `main()` builds the Gmail service (`googleapiclient.discovery.build("gmail", "v1", credentials=creds)`)
   and calls `service.users().getProfile(userId="me").execute()`.
4. Prints `f"Gmail profile: {profile['emailAddress']}"` and exits 0.

Errors:
- Missing client ID/secret → exit 1 before opening any browser.
- Consent denied / flow interrupted → `run_local_server` raises; caught,
  print the exception message, exit 1.
- `getProfile` failing (e.g. Gmail API not actually enabled despite consent
  succeeding) → catch `googleapiclient.errors.HttpError`, print
  `f"Gmail API call failed: {e.status_code} {e.reason}"`, exit 1.

`TOKEN_CACHE_PATH` (`scripts/.google_token.json`) must be added to
`.gitignore` (§5) — it holds a real refresh token.

### 3.2 `scripts/verify_whatsapp_send.py`

Proves the Meta app, WhatsApp product, test number, and verified recipient
are all correctly wired, using `httpx` directly against the Graph API (no
wrapper library — matches the tech-stack decision, since this is the same
call shape Phase 1's `whatsapp_send.py` will make).

```python
GRAPH_API_VERSION = "v21.0"

def send_test_message(access_token: str, phone_number_id: str, to: str) -> str:
    """POSTs a text message via the Graph API; returns the sent message's id."""
def main() -> None: ...
```

Flow:
1. `main()` loads `.env`, reads `META_WHATSAPP_ACCESS_TOKEN`,
   `META_WHATSAPP_PHONE_NUMBER_ID`, `MY_WHATSAPP_NUMBER`; exits 1 if any is
   empty.
2. `send_test_message` does:
   ```python
   response = httpx.post(
       f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages",
       headers={"Authorization": f"Bearer {access_token}"},
       json={
           "messaging_product": "whatsapp",
           "to": to,
           "type": "text",
           "text": {"body": "Phase 0 verification: WhatsApp send is working."},
       },
       timeout=10,
   )
   response.raise_for_status()
   return response.json()["messages"][0]["id"]
   ```
3. `main()` prints `f"Sent, message id: {message_id}"` and exits 0.

Errors:
- Missing env vars → exit 1 before the request.
- Non-2xx response (`raise_for_status` raises `httpx.HTTPStatusError`) →
  catch it, print `f"WhatsApp send failed: {e.response.status_code} {e.response.text}"`
  (Meta's error body includes a human-readable `error.message`), exit 1. No
  retry — this is a one-shot manual check, not the production send path
  (which gets retry/backoff in Phase 1's real error handling).
- **Expected failure if run before runbook step 1.2.3a**: a recipient with no
  open 24-hour messaging session rejects free-form text with a 4xx naming the
  recipient as outside the allowed window. This is not a bug in the script —
  it means step 1.2.3a (send the dashboard template, reply from the phone)
  wasn't done yet. The script does not special-case or retry this; it prints
  the plain error like any other failed request, per the same
  never-fabricate-success principle as everything else here.

### 3.3 `scripts/verify_ngrok_tunnel.py`

Proves the ngrok static domain actually routes to a process running on this
machine, end to end, in one script run (no separate manual `curl` step).

```python
def create_app() -> fastapi.FastAPI:
    """One route: GET /health -> 200 {"status": "ok"}."""

def start_placeholder_server(port: int) -> threading.Thread:
    """Runs uvicorn.Server(...).run() in a daemon thread; returns the thread."""

def start_ngrok_tunnel(domain: str, port: int) -> subprocess.Popen:
    """Launches `ngrok http --domain <domain> <port>` as a subprocess."""

def wait_for_local_server(port: int, timeout_s: float = 5.0) -> None:
    """Polls http://127.0.0.1:{port}/health until it responds or timeout_s elapses."""

def main() -> None: ...
```

Flow:
1. `main()` loads `.env`, reads `NGROK_STATIC_DOMAIN`; exits 1 if empty.
2. Starts the placeholder FastAPI app (`create_app()`, single `GET /health`
   returning `{"status": "ok"}`) on `127.0.0.1:8000` via
   `start_placeholder_server`, using a daemon thread so process exit cleans
   it up automatically.
3. `wait_for_local_server(8000)` polls locally first, so a tunnel failure and
   a local-server failure are never confused with each other in the output.
4. `start_ngrok_tunnel(domain, 8000)` launches the `ngrok` binary as a
   subprocess. Sleeps 3s (ngrok's tunnel establishment is near-instant once
   the authtoken is configured per §1.3 step 3; no polling loop needed for a
   one-shot manual verification script).
5. `httpx.get(f"https://{domain}/health", timeout=10)`; asserts
   `status_code == 200` and `response.json() == {"status": "ok"}`.
6. Prints `"ngrok tunnel OK: <domain>/health -> 200"` and exits 0.
7. `finally` block always terminates the ngrok subprocess
   (`process.terminate(); process.wait(timeout=5)`) so a failed run never
   leaves an orphaned tunnel holding the static domain.

Errors:
- `NGROK_STATIC_DOMAIN` empty → exit 1 before starting anything.
- `ngrok` binary not on `PATH` → `subprocess.Popen` raises `FileNotFoundError`;
  caught, print `"ngrok binary not found — install it and run 'ngrok config add-authtoken' first (see specs/phase-0-environment-setup.md §1.3)"`,
  exit 1.
- Local `/health` never responds within `wait_for_local_server`'s timeout →
  print `"placeholder FastAPI server did not start locally"`, exit 1 (skip
  the ngrok step entirely — no point tunneling to a server that isn't up).
- Public request non-200 or wrong body → print
  `f"ngrok tunnel check failed: got {response.status_code} {response.text}"`,
  exit 1.
- Public request raising (DNS/connection error — domain not claimed/authtoken
  not configured) → catch `httpx.HTTPError`, print the exception, exit 1.

---

## 4. Files changed/created — summary

| File | Change |
|---|---|
| `scripts/verify_google_oauth.py` | New |
| `scripts/verify_whatsapp_send.py` | New |
| `scripts/verify_ngrok_tunnel.py` | New |
| `.env.example` | Add `MY_WHATSAPP_NUMBER`; update `DATABASE_URL` template (§2) |
| `.env` | User fills in every real value per §1 (not committed) |
| `.gitignore` | Add `scripts/.google_token.json` (§5) |
| `agent/db/schema.sql` | No change — applied as-is via `psql` (§1.4) |
| `agent/**` | No change |

No new pip dependency — `httpx`, `fastapi`, `uvicorn`, `google-auth-oauthlib`,
`google-api-python-client`, `python-dotenv` are all already in
`pyproject.toml`/installed.

---

## 5. `.gitignore` addition

```diff
 .env
 __pycache__/
 *.pyc
 .venv/
 *.egg-info/
+scripts/.google_token.json
```

---

## 6. Acceptance criteria mapping

| Acceptance criterion (implementation_plan.md) | Satisfied by |
|---|---|
| `psql $DATABASE_URL -c '\dt'` lists the four app tables | §1.4 runbook (`CREATE ROLE`/`CREATE DATABASE` + `psql -f agent/db/schema.sql`) |
| `scripts/verify_google_oauth.py` completes OAuth and prints Gmail profile | §3.1 |
| `scripts/verify_whatsapp_send.py` sends a message that arrives on your phone | §3.2 |
| `scripts/verify_ngrok_tunnel.py` reaches the ngrok static domain, 200 | §3.3 |
| `.venv/bin/python -c "import agent"` succeeds | Already true today (confirmed); unaffected since no `agent/` file changes |

---

## 7. Out of scope (unchanged from implementation_plan.md)

No LangGraph graphs/state, no real webhook route in `agent/webhook/routes.py`
beyond its current one-line docstring, no Google API calls from `agent/`
code, no long-lived token storage design (Phase 1 designs how the real app
persists/refreshes credentials — `scripts/.google_token.json` is a
throwaway cache for this script's own convenience, not a pattern later
phases inherit).
