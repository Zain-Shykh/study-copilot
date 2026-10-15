# Logging & observability

Status: **approved and implemented.**

## 1. Problem

- Logging is never configured (no `basicConfig`/`dictConfig` anywhere). Under
  uvicorn, `logger.exception(...)` falls through to Python's last-resort
  handler: stderr, message only — no timestamp, level, module, function.
- 32 log calls exist in 7 modules: 28 `.exception`, 3 `.error`, 1 `.warning`,
  **0 `.info`**. Failures are recorded, normal activity is not.
- Many `except` blocks convert an error into a user-facing message **without
  logging it** (Classroom/Drive ingestion, email draft/revise/send, submission
  prep, every `answer_question` tool, per-course Classroom failures,
  Claude Code failures, Google auth failure). The cause only survives in a
  WhatsApp message.
- One WhatsApp message fans out into webhook → router graph → background task →
  assignment/email graph → Claude Code. With several in flight, log lines cannot
  be attributed to a message.
- Meta's error response body (the thing that explained the earlier WhatsApp
  400) is discarded by `raise_for_status()`.
- Output is console-only; nothing persists.

## 2. Goals / non-goals

Goals: any failure can be traced to the exact function (`module.function:line`)
and to the single inbound message that caused it; normal activity is visible at
`INFO`; logs persist on disk with bounded size; no secrets or personal content
in logs.

Non-goals (explicitly out of scope): log aggregation service (ELK/Datadog),
OpenTelemetry tracing/metrics, shipping logs off the machine, changing
`agent/setup_google_auth.py`'s `print()` calls (interactive CLI, not the
server), logging uvicorn's own access log into the file, any change to user-
facing WhatsApp messages or graph behavior.

## 3. Decisions (confirmed by the user)

| # | Decision |
|---|---|
| 1 | Scope: configure logging, file output, correlation IDs, boundary INFO/WARNING logs, safety rules |
| 2 | File: `logs/app.log`, `RotatingFileHandler`, 5 MB × 5 backups (max ≈ 30 MB), plus console |
| 3 | Format: human-readable text by default; JSON lines when `LOG_FORMAT=json` |
| 4 | Default level `INFO`, overridable via `LOG_LEVEL` |
| 5 | WhatsApp message text, email subjects/bodies, Drive file titles, assignment content are **never** logged — only IDs, lengths, counts, intent names |

## 4. Configuration (`agent/config.py`, `.env.example`)

New optional env vars, added to `Settings` **after** `student_info` with
defaults:

| Env var | Settings field | Default | Valid |
|---|---|---|---|
| `LOG_LEVEL` | `log_level: str = "INFO"` | `INFO` | `DEBUG, INFO, WARNING, ERROR` (case-insensitive, stored upper-case) |
| `LOG_FORMAT` | `log_format: str = "text"` | `text` | `text, json` (stored lower-case) |
| `LOG_DIR` | `log_dir: str = "logs"` | `logs` | any path; relative to the process's cwd (the project root, where uvicorn is started) |

`load_settings()` validates `LOG_LEVEL` and `LOG_FORMAT`; an invalid value
raises `RuntimeError("Invalid LOG_LEVEL 'x' — expected one of DEBUG, INFO, WARNING, ERROR")`
(same fail-fast-at-startup behavior as missing required vars). Empty/unset →
default. `.gitignore` gains `logs/`. `.env.example` gains the three vars
(commented defaults).

## 5. New module `agent/logging_setup.py`

```python
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    def filter(self, record) -> bool:      # sets record.request_id = request_id_var.get(); returns True

class JsonFormatter(logging.Formatter):
    def format(self, record) -> str:
        # one JSON object per line, keys:
        # ts (ISO-8601, local time with offset), level, request_id, logger,
        # function, line, message, exc (formatted traceback, only if exc_info)

def setup_logging(level: str, log_format: str, log_dir: Path) -> None: ...
def log_node(fn):  # see §7
```

### `setup_logging`
- Idempotent: removes handlers it previously installed on the root logger
  (marked with an attribute) before adding new ones, so calling it twice
  (tests, reload) never duplicates lines.
- Root logger level = `level`. Two handlers, both with `RequestIdFilter`:
  - `StreamHandler(sys.stderr)`
  - `RotatingFileHandler(log_dir / "app.log", maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")`
- Text format: `%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s.%(funcName)s:%(lineno)d | %(message)s`,
  `datefmt="%Y-%m-%dT%H:%M:%S%z"`. JSON format uses `JsonFormatter` on both.
- `log_dir` is created (`mkdir(parents=True, exist_ok=True)`). **If the
  directory/file cannot be created or opened (`OSError`), the app does not
  fail to start**: console handler only, plus one `WARNING` "file logging
  disabled: <error>".
- Third-party loggers forced to `WARNING`: `httpx`, `httpcore`, `urllib3`,
  `googleapiclient`, `google.auth`, `google_genai`, `langsmith`, `asyncio`.
  Rationale: httpx at INFO logs every request URL (incl. the Meta phone-number
  ID); the others are chatty with no diagnostic value.
- uvicorn's own loggers are left untouched (they keep their own console
  handlers; their access lines are not mirrored into the file).

### Correlation ID
`request_id_var` holds an 8-char id (`uuid.uuid4().hex[:8]`) generated per
inbound webhook message. `asyncio.create_task` copies the current context, so
the background graph run, the flows it spawns, and any later resume tasks
inherit it. Lines outside a request (startup) show `-`.

## 6. Wiring

### `agent/main.py`
- After `load_settings()` in `create_app()`:
  `setup_logging(settings.log_level, settings.log_format, Path(settings.log_dir))`.
- Lifespan: `INFO` "app starting (log_level=%s, log_format=%s)"; after
  crash-recovery scan: `INFO` "crash recovery: %d interrupted thread(s)";
  Google startup check: `INFO` "startup google auth check: ok" or
  `WARNING` "startup google auth check: %s" (problem text) — in addition to
  the existing WhatsApp message; `INFO` "app shutting down" before `pool.close()`.

### `agent/webhook/routes.py` (`receive_webhook`, `_process_text_message`)
| Point | Level | Message (fields) |
|---|---|---|
| bad/missing signature | WARNING | `invalid webhook signature` (no request id) |
| non-owner sender | WARNING | `ignored message from non-owner sender ***<last4>` |
| after `msg_id` known | — | `request_id_var.set(uuid4().hex[:8])` (set *before* the dedup check so duplicate lines carry an id) |
| every inbound message | INFO | `inbound message wamid=<id> type=<type> reply_to=<id or None> chars=<len>` — this line links the short request id to Meta's wamid |
| duplicate delivery | INFO | `duplicate delivery ignored wamid=<id>` |
| `_process_text_message` start/end | INFO | `turn started thread_id=user:***<last4>` / `turn finished in <s>s` |
| `status` command | INFO | `status check: connected` / `status check: <problem text>` |

`_process_text_message`'s existing `logger.exception` stays. The message text
is never logged.

### `agent/graph/router_graph.py`
- Existing `_log_if_failed`/exception logging unchanged.
- `classify_intent_node`: INFO `intent=<intent> arg_keys=<sorted keys>`.
- `resolve_assignment_node`: INFO outcome `assignment resolve: confident|ambiguous|none candidates=<n> best_score=<x.xx>`.
- `resolve_pending_item_node`: INFO same shape (`pending resolve: …`, plus
  the single-item / no-name shortcuts).
- `_dispatch_pending_resume`, `handle_confirmation_node`,
  `handle_email_confirmation_node`: INFO `dispatching <assignment|email> thread_id=<id>`.
- `run_assignment_flow`, `resume_assignment_thread`, `run_email_flow`,
  `resume_email_thread`: INFO `flow started thread_id=<id> kind=<run|resume>`
  and `flow finished thread_id=<id> in <s>s`.
- `send_reply_node`: INFO `reply sent chars=<len>`.

### Node-level logging via `log_node` (see §7) — applied in
`build_router_graph`, `build_assignment_graph`, `build_email_graph` at every
`g.add_node(name, fn)` as `g.add_node(name, log_node(fn))`. No node body changes.
Routing functions (`route_after_*`) are not wrapped.

### Previously silent failure paths — add logging (message text to the user unchanged)
| Location | Add |
|---|---|
| `nodes/ingestion.py:36` (Classroom fetch) | `logger.exception("ingestion: Classroom fetch failed course_id=%s coursework_id=%s", …)` |
| `nodes/ingestion.py:55` (attachment) | `logger.exception("ingestion: attachment failed drive_file_id=%s", file_id)` (file **id**, not title) |
| `email_graph.py` draft_node (two `except`), revise_node, send_node | `logger.exception` with the failing step name |
| `assignment_graph.py` submission_prep_node (package, Drive upload) | `logger.exception("submission prep failed (%s)", step)` |
| `nodes/answer_question.py` six `except HttpError` | `logger.exception("answer_question tool %s failed", tool_name)` |
| `nodes/classroom.py:105` per-course failure | `logger.warning("list_assignments: course_id=%s failed", id, exc_info=True)` |
| `google_auth.load_google_clients` `except` | `logger.warning("Google credentials unusable: %s", e)` (add module logger) |
| `google_auth.get_credentials` after refresh | `logger.info("Google access token refreshed")` |

### External-call boundaries
- **`nodes/whatsapp_send.py`**: INFO `whatsapp text sent to=***<last4> chars=<len> id=<id>` / `whatsapp document sent … filename=<name> id=<id>`
  (document filename is the submission file name the user chose — accepted).
  Before each `raise_for_status()`: if `response.is_error`,
  `logger.error("whatsapp send failed status=%s body=%s", code, response.text[:500])`
  (Meta's error JSON; contains no secrets). Applies to text, media upload, and
  document send. Exceptions still raised exactly as today.
- **`llm._retry_model`**: on each failed attempt,
  `logger.warning("gemini call failed model=%s attempt=%d/%d: %s", model, n, _MAX_ATTEMPTS, e)`.
  The existing fallback warning stays. No prompts or responses logged.
- **`nodes/claude_code.py` `run_claude_code`**: INFO `claude code started dir=<workspace basename> mode=<fresh|revise|resume>`;
  INFO `claude code finished in <s>s success=<bool> returncode=<rc>`;
  WARNING on timeout (`claude code timed out after <n>s; checking disk for valid manifest`) and whether the manifest was recovered;
  ERROR on non-zero exit with `stderr[:1000]`; ERROR on `OSError` start failure;
  WARNING with the validation message when the manifest is invalid.

## 7. `log_node`

```python
def log_node(fn):
    """Wraps a LangGraph node: logs duration on success, logs the traceback and
    re-raises on failure, and treats LangGraph's interrupt/bubble-up control
    flow as a pause, not an error."""
```
- Returns an `async def` wrapper if `inspect.iscoroutinefunction(fn)` else a
  plain `def` wrapper (LangGraph decides threading by this — a sync node must
  stay sync, an async node async, preserving the existing "async nodes can
  `create_task`, sync nodes run in a worker thread" behavior).
- Uses `functools.wraps(fn)` so `inspect.signature` still exposes the original
  `state` / `config` parameters (LangGraph injects `config` by signature).
- Success: `INFO node=<fn.__name__> ok in <s>s`.
- `except GraphBubbleUp` (`langgraph.errors`; covers `GraphInterrupt` raised by
  `interrupt()`): `INFO node=<name> paused` and **re-raise** — never logged as an
  error.
- Any other `Exception`: `logger.exception("node=%s failed", name)` then
  **re-raise unchanged** (graph behavior identical; the existing outer handlers
  still run). Note this can log the same failure twice (node + outer handler);
  the node line carries the traceback, the outer line the user-facing context.
  Accepted.
- Node log lines are emitted from the wrapper, so `%(funcName)s` would show
  `wrapper`; the node name is therefore included explicitly in the message.

## 8. Safety rules (apply to every added log line)
- Never log: WhatsApp message text, email subjects/bodies/addresses, Drive file
  titles, assignment titles/descriptions, tokens, secrets, `Authorization`
  headers, full phone numbers (log `***` + last 4 digits).
- Log IDs, counts, lengths, durations, intent names, `intent_args` **keys**.
- Lazy formatting (`logger.x("…%s", v)`), never f-strings.
- `logger.exception` only inside `except`.
- No sensitive-data scrubber/regex filter is added (YAGNI; the rule is enforced
  by what is passed to the logger, and checked by a test, §10).

## 9. Docs to update
`README.md` (config table: three new vars; new "Logs" section: location,
rotation, how to follow one message with `grep <request_id> logs/app.log`,
`LOG_LEVEL=DEBUG`), `CLAUDE.md` (new "Logging" subsection under Architecture),
`docs/ARCHITECTURE.md` (short "Logging" section + one line in §1),
`docs/product_definition.md` (one paragraph in the error-handling section:
logs are local, rotated, contain no message content), `.env.example`,
`.gitignore`.

## 10. Tests (new `tests/test_logging_setup.py` plus additions)
1. `setup_logging` creates `app.log`; called twice → still exactly 2 handlers;
   invalid level/format → `load_settings` raises `RuntimeError`; unwritable
   `LOG_DIR` → console-only, app does not raise; third-party loggers at WARNING.
2. Text and JSON output: a record logged with `request_id_var` set contains the
   id; JSON line parses and has keys `ts, level, request_id, logger, function, line, message`; `exc` present only with `exc_info`.
3. `log_node`: sync and async success (INFO line, return value unchanged);
   exception → ERROR line with traceback and the same exception re-raised;
   `GraphBubbleUp` → INFO "paused", no ERROR, re-raised; `inspect.signature`
   and `iscoroutinefunction` preserved; a tiny real `StateGraph` using wrapped
   sync + async nodes **with a `config` parameter and an `interrupt()`**
   compiles and runs under `ainvoke` (proves LangGraph accepts the wrapper).
4. Context propagation: `request_id_var` set before `asyncio.create_task` is
   visible inside the task, **and inside a sync node run by LangGraph in a worker
   thread**. If the latter fails, stop and revise this spec (do not silently work around).
5. Webhook: the inbound line has the wamid and `chars=`, the request id is set
   for the dispatched turn, and the message text appears in **no** captured log
   record (`caplog`) for a representative message.
6. `whatsapp_send`: on a mocked 400 response, the error body is logged and
   `httpx.HTTPStatusError` is still raised.
7. Full existing suite still passes.

## 11. Acceptance criteria
- Starting the app creates `logs/app.log`; each line has timestamp, level,
  request id, `module.function:line`, message.
- `grep <request_id> logs/app.log` after one WhatsApp message shows the webhook,
  intent, node durations, flow start/finish, and WhatsApp sends for that
  message — including work done in background tasks.
- Forcing a failure in any listed formerly-silent path produces a log line with
  a traceback naming the function, while the user still receives the same
  WhatsApp message as before.
- A failed Meta send logs Meta's response body.
- An interrupt (draft awaiting approval) is logged as "paused", never as an error.
- With `LOG_LEVEL=WARNING`, normal traffic produces no INFO lines.
- No test or log sample contains message text, tokens, or full phone numbers.
- File never exceeds 5 MB per file / 6 files total.
- All tests pass.

## 12. Files changed
New: `agent/logging_setup.py`, `tests/test_logging_setup.py`.
Changed: `agent/config.py`, `agent/main.py`, `agent/webhook/routes.py`,
`agent/google_auth.py`, `agent/llm.py`, `agent/graph/router_graph.py`,
`agent/graph/assignment_graph.py`, `agent/graph/email_graph.py`,
`agent/graph/nodes/{answer_question,classroom,claude_code,ingestion,whatsapp_send}.py`,
tests (`test_webhook_idempotency.py`, `test_google_auth.py`, others only if a
signature changes), `.env.example`, `.gitignore`, the docs in §9.
No database changes. No new dependencies.
