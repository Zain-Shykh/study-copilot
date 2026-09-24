# Email Drafting — Spec

Implements the email side of `docs/product_definition.md`'s permission
matrix (`Draft an email reply` — Auto; `Send an email` — Approval required)
and the `email:<uuid>` thread described in `CLAUDE.md`'s thread model —
neither ever got built past a one-line stub (`agent/graph/email_graph.py`).
This was explicitly deferred out of Phase 3 (see
`docs/implementation_plan.md` lines 218–224 and
`specs/phase-3-approval-submission.md` Decision #1) because no drafting
engine existed for its approval loop to sit on top of. This spec is that
engine.

---

## Decisions made for this spec (confirmed with the user; not fully pinned
down by the docs above)

1. **Scope**: both replying to an existing email and composing a new one to
   an arbitrary recipient are in scope for v1 — matches the product
   definition's single "Draft replies/new emails" permission-matrix row.
2. **Recipient resolution for a new email**: the user must give the literal
   address (e.g. "email jane.doe@school.edu about..."). No fuzzy
   name-to-contact matching — avoids guessing who "my professor" is,
   consistent with the "never fabricate" principle. (A *reply* target is
   resolved differently — see §6.1 — because it's found via Gmail search
   against real message content, not guessed.)
3. **Drafting engine**: Gemini (the same `genai.Client` already used for
   `classify_intent`/`summarize_emails`/`answer_question`), via a direct
   `generate_content` call — not Claude Code. Claude Code is structurally
   scoped away from Gmail credentials and thread context by design (see
   CLAUDE.md's "Assignment drafting via Claude Code"), and its
   subprocess/WebSearch machinery is unneeded weight for a short email.
4. **Single-stage approval, not two**: unlike assignment drafting (draft
   review interrupt, *then* a separate submit-confirm interrupt), approving
   an email draft sends it directly — no second confirmation. This restores
   the *original* Phase 3 acceptance criterion that was dropped when email
   drafting was deferred: "Approving an email draft sends it via Gmail;
   rejecting discards it; revising regenerates it" (`docs/implementation_plan.md`
   line 221). The email permission matrix only has two rows (draft: auto,
   send: approval-required) — one review loop satisfies both, since
   "approve" *is* the approval.
5. **No new Postgres table**: everything an in-flight email draft needs
   (recipient, subject, body, original-message context, review state)
   fits in LangGraph's own checkpointed graph state, exactly like
   `AssignmentState`'s `review_decision`/`review_feedback` fields. Unlike
   assignment drafting, there's no Claude Code session to resume across
   revisions (Gemini calls are stateless — each revision just re-sends the
   full context), so no `claude_sessions`-equivalent table is needed either.
6. **No Gmail Draft objects**: sending happens via `messages.send` directly
   on approval. The `gmail.compose` OAuth scope (already requested in
   `agent/setup_google_auth.py`, unused until now) stays unused — our own
   Postgres/LangGraph state *is* the draft until it's approved, matching how
   an assignment draft never touches Drive until approved.
7. **No attachments, no CC/BCC** in v1 — not mentioned anywhere in the
   product definition's email section; single recipient, text body only.

---

## 1. Objective

Let the user say "reply to the email from the registrar about my
transcript — ask when it'll be ready" or "email jane.doe@school.edu about
rescheduling our meeting to Thursday", get a drafted reply/new email for
review on WhatsApp, revise it in place (no cap on rounds, same as
assignment drafts), and have "approve" send it via Gmail — all gated so
nothing sends without an explicit approval, per the permission matrix.

## 2. `agent/db/repo.py` changes

One existing function's scope needs widening — everything else in `repo.py`
is untouched.

```python
def list_pending_items(conn: psycopg.Connection, item_type: str | None = None) -> list[dict]:
    """All currently-pending items, optionally filtered to one item_type
    ('assignment' or 'email'). item_type=None returns both — needed for
    resolve_pending_item_node's by-name fallback, which must consider
    email drafts and assignment drafts together (§6.6)."""
    with conn.cursor() as cur:
        if item_type is None:
            cur.execute(
                "SELECT whatsapp_message_id, thread_id, item_type, display_name "
                "FROM pending_items WHERE status = 'pending'"
            )
        else:
            cur.execute(
                "SELECT whatsapp_message_id, thread_id, item_type, display_name "
                "FROM pending_items WHERE item_type = %s AND status = 'pending'",
                (item_type,),
            )
        rows = cur.fetchall()
    return [
        {"message_id": r[0], "thread_id": r[1], "item_type": r[2], "display_name": r[3]}
        for r in rows
    ]
```

Every other `repo.py` function (`create_pending_item`, `get_pending_item`,
`close_pending_item`, credential storage) already works for either
`item_type` unmodified — `pending_items.item_type` already has a CHECK
constraint allowing `'email'` (`agent/db/schema.sql` line 7), it was just
never written.

## 3. `agent/graph/nodes/gmail.py` additions

Three additions: capture two more headers on every message fetch (cheap,
backward compatible), and two new functions for reading a full message body
and sending.

### 3.1 `_get_message_metadata` — capture `threadId` and `Message-ID`

```python
def _get_message_metadata(gmail_service, message_id: str) -> dict:
    msg = (
        gmail_service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date", "Message-ID"],
        )
        .execute(num_retries=3)
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "message_id_header": headers.get("Message-ID", ""),
    }
```

`list_messages` and `get_messages_by_id` are unchanged — they already
return whatever this helper gives them, so both now carry the two new
keys for free. `thread_id` is Gmail's own thread id (for correct Gmail-UI
threading on reply); `message_id_header` is the RFC 5322 `Message-ID`
header value (distinct from Gmail's own `id`) needed for the
`In-Reply-To`/`References` headers on a reply, per RFC 5322 §3.6.4.

### 3.2 `get_message_body` — new

```python
import base64

def get_message_body(gmail_service, message_id: str) -> str:
    """Returns the plain-text body of one message, or "" if it has no
    text/plain part (e.g. an HTML-only email) — callers treat that as
    "body unavailable" rather than attempting a lossy HTML-to-text
    conversion, consistent with "never fabricate"."""
    msg = (
        gmail_service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute(num_retries=3)
    )

    def _find_text_plain(payload: dict) -> str | None:
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            found = _find_text_plain(part)
            if found is not None:
                return found
        return None

    return _find_text_plain(msg["payload"]) or ""
```

### 3.3 `send_message` — new

```python
import base64
from email.mime.text import MIMEText

def send_message(
    gmail_service,
    to: str,
    subject: str,
    body: str,
    *,
    in_reply_to_header: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Sends a new message, or a reply when in_reply_to_header/thread_id
    are given (sets In-Reply-To/References for correct threading in email
    clients, and Gmail's own threadId for correct threading in the Gmail
    UI). Returns the sent message's Gmail id. Raises HttpError on failure —
    the caller decides how to report/retry."""
    msg = MIMEText(body)
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to_header:
        msg["In-Reply-To"] = in_reply_to_header
        msg["References"] = in_reply_to_header

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body_payload: dict = {"raw": raw}
    if thread_id:
        body_payload["threadId"] = thread_id

    sent = gmail_service.users().messages().send(userId="me", body=body_payload).execute(num_retries=3)
    return sent["id"]
```

`base64`/`email.mime.text` are stdlib — no new dependency.

## 4. `agent/llm.py` additions

### 4.1 `draft_email` intent, added to `classify_intent`

`ROUTE_MESSAGE_DECLARATION`'s `intent` enum gains `"draft_email"`, plus two
new args:

```python
"email_mode": types.Schema(
    type="STRING",
    enum=["reply", "new"],
    description=(
        "only for draft_email; whether the user wants to reply to an "
        "existing email or send a brand new one, based on their wording "
        "— independent of whether an address is mentioned"
    ),
),
"email_reference": types.Schema(
    type="STRING",
    description=(
        "only for draft_email; the literal email address to send to/reply "
        "to, if one was given — leave unset if the user only described who "
        "without an address"
    ),
),
"email_topic": types.Schema(
    type="STRING",
    description="only for draft_email; what the email should say, as given",
),
```

**Updated (post-launch fix):** `email_reference` no longer accepts free
text identifying an email by sender name/subject/topic — see §6.1's
rewrite below. `resolve_email_node` originally used `"@" in
email_reference` to decide reply-vs-new and, when no address was given,
searched Gmail with the raw free-text reference. Both turned out to be
real bugs: (1) a phrase like "reply to X@y.com" still names an address,
so the "@"-based mode inference incorrectly treated an explicit reply
request as a new email; (2) free-text search for something like "the
last email" isn't understood by Gmail as "sort by date" — it's a literal
keyword search that returns Gmail's generic relevance-ranked (and often
irrelevant) matches, not the actual most recent email. `email_mode` now
carries the reply/new decision explicitly, and `email_reference` is only
ever a literal address (or unset).

`CLASSIFY_SYSTEM_PROMPT` gains a new bullet (inserted after
`work_on_assignment`, before `respond_to_pending`):

```
- draft_email: the user wants to reply to an existing email or compose a
  new one (e.g. "reply to the registrar's email about my transcript",
  "email jane.doe@school.edu about rescheduling"). Always set email_mode
  to "reply" or "new" based on the user's own wording ("reply to..." →
  reply; "email/send/write to..." → new) — never infer this from whether
  an address happens to be present; "reply to jane.doe@school.edu
  saying..." is still a reply, not a new email, even though it names an
  address. Extract email_reference ONLY if a literal email address was
  given — if the user only described who without an address (a name,
  role, or description), leave email_reference unset (the address will
  be asked for separately, never guessed or searched for by name).
  Extract what it should say into email_topic (leave email_topic unset
  if genuinely not given — e.g. "reply to that email from my advisor"
  with no stated content is still valid). This is the ONLY intent that
  leads to drafting an email — still just identifies the
  mode/target/topic at this stage, does not draft or send anything
  itself.
```

And the existing `unrecognized` bullet's email carve-out is corrected (it
currently claims *all* send/reply requests are unsupported, which stops
being true here):

```
- unrecognized: anything that isn't clearly one of the above. A request to
  submit/turn in an assignment other than through its own approval loop
  (i.e. not a reply to a pending item) is still unrecognized — assignment
  submission only ever happens via that loop, never as a standalone
  command.
```

### 4.2 `draft_email` — new function

```python
DRAFT_EMAIL_DECLARATION = types.FunctionDeclaration(
    name="record_draft",
    description="Record the drafted email's subject and body.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "subject": types.Schema(type="STRING", description="Suggested subject line."),
            "body": types.Schema(type="STRING", description="The email body text."),
        },
        required=["subject", "body"],
    ),
)

DRAFT_EMAIL_SYSTEM_PROMPT = """\
You draft one email on behalf of the user for their review and approval —
never invent facts, deadlines, names, or context you weren't given. Formal,
professional tone: complete sentences, no slang (matches the user's stated
preference for all agent replies). Call record_draft exactly once with a
suggested subject and the body text only (no "Subject:" line inside the
body, no placeholder brackets like "[Your Name]" — sign off naturally or
not at all).

If you're given the email being replied to, engage with its actual content
directly rather than writing a generic reply. If no specific requested
content is given for a reply, draft a brief, reasonable response based
solely on the original email's content.

If you're given a previous draft and requested changes, revise it
according to those changes while keeping the rest of the draft consistent
— don't regenerate from scratch.
"""


def draft_email(
    client: genai.Client,
    model: str,
    *,
    topic: str,
    original_subject: str | None = None,
    original_body: str | None = None,
    prior_subject: str | None = None,
    prior_body: str | None = None,
    feedback: str | None = None,
) -> tuple[str, str]:
    """Generates (subject, body) for an email draft or revision via a
    forced record_draft function call. original_subject/original_body are
    only set when drafting a reply. prior_subject/prior_body/feedback are
    only set on a revision round. Raises after 3 failed attempts."""
    parts = [f"Requested content: {topic}" if topic else "No specific requested content was given."]
    if original_subject or original_body:
        parts.append(f"Replying to this email:\nSubject: {original_subject}\n\n{original_body}")
    if prior_body:
        parts.append(f"Previous draft:\nSubject: {prior_subject}\n\n{prior_body}")
    if feedback:
        parts.append(f"Requested changes: {feedback}")
    content = "\n\n".join(parts)

    def call():
        return client.models.generate_content(
            model=model,
            contents=content,
            config=types.GenerateContentConfig(
                system_instruction=DRAFT_EMAIL_SYSTEM_PROMPT,
                tools=[types.Tool(function_declarations=[DRAFT_EMAIL_DECLARATION])],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY", allowed_function_names=["record_draft"],
                    )
                ),
            ),
        )

    response = _with_retry(call)
    args = response.function_calls[0].args
    return args["subject"], args["body"]
```

One function serves both the first draft and every revision round — the
caller (`email_graph.py`) decides which optional fields to fill in. For a
*reply*, the caller ignores the returned `subject` and uses a fixed
`f"Re: {original_subject}"` instead (deterministic — a reply's subject
line shouldn't be left to the model), so `draft_email` never needs to know
whether it's drafting a reply or a new email.

Nothing else in `llm.py` changes — `parse_review_reply` (approve/revise/
reject) and `parse_confirmation_reply` (confirm/decline) are already fully
generic and are reused as-is by the email graph and router changes below.

## 5. `agent/graph/state.py` additions

```python
class EmailState(TypedDict, total=False):
    mode: str  # "reply" | "new"
    sender: str  # WhatsApp number to relay results to
    recipient_email: str
    recipient_display: str  # for confirm/relay text — the From header for a reply, the raw address for new
    original_subject: str | None       # only for "reply"
    original_body: str | None          # only for "reply" — fetched once by draft_node, carried across revisions
    gmail_message_id: str | None       # the message being replied to
    gmail_thread_id: str | None
    gmail_message_id_header: str | None  # RFC 5322 Message-ID, for In-Reply-To/References
    topic: str                          # the user's original instruction — preserved across revisions
    subject: str | None                 # current draft subject
    body: str | None                    # current draft body
    review_reply_text: str | None       # raw text from the draft-review interrupt
    review_decision: str | None         # "approve" | "revise" | "reject"
    review_feedback: str | None         # only set when review_decision == "revise"
    failure_text: str | None
    sent: bool
```

`RouterState`'s `intent` comment gains `draft_email`; its `pending_question`
comment gains two new kinds documented in §6.

## 6. `agent/graph/router_graph.py` changes

### 6.1 Resolving an email target — `resolve_email_node` (revised)

**Post-launch fix, superseding the original design below.** The original
version used `"@" in email_reference` to decide reply-vs-new and, when no
address was given, searched Gmail with the raw free-text reference as a
query. Both were real bugs (see §4.1's note): an explicit "reply to
X@y.com" was misrouted to "new" because it named an address, and
free-text phrases like "the last email" don't mean anything to Gmail's
search engine — they're searched as literal keywords, returning
irrelevant relevance-ranked matches instead of the actual most recent
email.

The revised design: `email_mode` (from classify_intent, §4.1) drives
reply-vs-new directly, and a real email address is now *required* for
both modes — no more fuzzy free-text matching by name/subject. Given a
mode and a real address, "which email to reply to" is answered
deterministically: search Gmail with `from:<address>` and take the most
recent match, since Gmail's search results are already returned
newest-first with no extra sorting needed. Missing info (no valid
address, or no topic) is asked for one piece at a time and tracked as a
real `pending_question` (`kind: "awaiting_email_details"`) so the user's
next reply continues it rather than being reclassified from scratch —
closing a gap the original design also had (asking "who would you like
to email?" never tracked that a reply was expected).

```python
def _load_gmail_service(config: RunnableConfig) -> tuple[object | None, str | None]:
    """Returns (gmail_service, None) on success, or (None, error_reply_text)
    if credentials are missing/expired. Returned as a pair rather than
    relying on isinstance(result, str) to detect an error, since the
    gmail_service itself could coincidentally be a string (e.g. a test
    stub)."""
    pool = config["configurable"]["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return None, clients
    gmail_service, _, _ = clients
    return gmail_service, None


def _is_valid_email(text: str) -> bool:
    return "@" in text and "." in text.split("@")[-1]


def _ask_for_email_details(mode: str, address: str | None, topic: str, asking_for: str) -> dict:
    if asking_for == "address":
        question = (
            "Who would you like to reply to — give me their email address?"
            if mode == "reply"
            else "Who would you like to email? Give me their email address."
        )
    else:
        question = f"What should the email to {address} say?"
    return {
        "reply_text": question,
        "pending_question": {
            "kind": "awaiting_email_details",
            "message_id": None,
            "mode": mode,
            "address": address,
            "topic": topic,
            "asking_for": asking_for,
        },
    }


def _resolve_email_target(mode: str, address_text: str, topic: str, gmail_service) -> dict:
    address = address_text if address_text and _is_valid_email(address_text) else None

    if not address:
        return _ask_for_email_details(mode, None, topic, "address")
    if not topic:
        return _ask_for_email_details(mode, address, topic, "topic")

    if mode == "new":
        resolved = {
            "mode": "new",
            "recipient_email": address,
            "recipient_display": address,
            "original_subject": None,
            "gmail_message_id": None,
            "gmail_thread_id": None,
            "gmail_message_id_header": None,
            "topic": topic,
        }
        return _confirm_email_reply(resolved)

    try:
        matches = gmail.list_messages(gmail_service, f"from:{address}", 1)
    except HttpError as e:
        return {"reply_text": f"Couldn't search Gmail right now: {e}", "pending_question": None}

    if not matches:
        return {
            "reply_text": f"I couldn't find any emails from {address} to reply to.",
            "pending_question": None,
        }

    return _confirm_email_reply(_resolved_from_match(matches[0], topic))


async def resolve_email_node(state: RouterState, config: RunnableConfig) -> dict:
    gmail_service, error = _load_gmail_service(config)
    if error:
        return {"reply_text": error}

    intent_args = state.get("intent_args", {})
    mode = intent_args.get("email_mode") or "new"
    address_text = (intent_args.get("email_reference") or "").strip()
    topic = intent_args.get("email_topic", "")

    return _resolve_email_target(mode, address_text, topic, gmail_service)
```

`_extract_email_address`/`_resolved_from_match`/`_confirm_email_question_text`/
`_confirm_email_reply` (defined just above `resolve_email_node` in the
file) are unchanged from the original design below — `_resolved_from_match`
still needs a `from:` search result and a topic; only what feeds into it
changed.

<details>
<summary>Original design (superseded — kept for history)</summary>

Mirrors `resolve_assignment_node`'s shape (resolve → confirm before doing
anything), but the matching mechanism is different: Gmail's own search
does the fuzzy work (`gmail.list_messages` already accepts a free-text
query), so there's no local score-threshold logic to write — 0 results is
no-match, 1 is confident, 2+ is disambiguation.

```python
def _extract_email_address(from_header: str) -> str:
    return email.utils.parseaddr(from_header)[1]


def _resolved_from_match(match: dict, topic: str) -> dict:
    return {
        "mode": "reply",
        "recipient_email": _extract_email_address(match["from"]),
        "recipient_display": match["from"],
        "original_subject": match["subject"],
        "gmail_message_id": match["id"],
        "gmail_thread_id": match["thread_id"],
        "gmail_message_id_header": match["message_id_header"],
        "topic": topic,
    }


def _confirm_email_question_text(resolved: dict) -> str:
    if resolved["mode"] == "reply":
        suffix = f" — {resolved['topic']}" if resolved["topic"] else ""
        return f'Should I draft a reply to "{resolved["original_subject"]}" from {resolved["recipient_display"]}{suffix}?'
    return f'Should I draft a new email to {resolved["recipient_email"]} — {resolved["topic"]}?'


def _confirm_email_reply(resolved: dict) -> dict:
    return {
        "reply_text": _confirm_email_question_text(resolved),
        "pending_question": {"kind": "confirm_draft_email", "message_id": None, "resolved": resolved},
    }


async def resolve_email_node(state: RouterState, config: RunnableConfig) -> dict:
    pool = config["configurable"]["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"reply_text": clients}
    gmail_service, _, _ = clients

    target_text = state.get("intent_args", {}).get("email_reference", "").strip()
    topic = state.get("intent_args", {}).get("email_topic", "")

    if not target_text:
        return {
            "reply_text": "Who would you like to email, or which email are you replying to?",
            "pending_question": None,
        }

    if "@" in target_text:
        if not topic:
            return {
                "reply_text": f"What should the email to {target_text} say?",
                "pending_question": None,
            }
        resolved = {
            "mode": "new",
            "recipient_email": target_text,
            "recipient_display": target_text,
            "original_subject": None,
            "gmail_message_id": None,
            "gmail_thread_id": None,
            "gmail_message_id_header": None,
            "topic": topic,
        }
        return _confirm_email_reply(resolved)

    try:
        matches = gmail.list_messages(gmail_service, target_text, 5)
    except HttpError as e:
        return {"reply_text": f"Couldn't search Gmail right now: {e}"}

    if not matches:
        return {
            "reply_text": (
                f'I couldn\'t find an email matching "{target_text}" — if you want to '
                "email someone new, give me their email address directly."
            ),
            "pending_question": None,
        }
    if len(matches) == 1:
        return _confirm_email_reply(_resolved_from_match(matches[0], topic))

    lines = [f'{i + 1}. From {m["from"]} — "{m["subject"]}" ({m["date"]})' for i, m in enumerate(matches)]
    return {
        "reply_text": "Which email did you mean?\n" + "\n".join(lines),
        "pending_question": {
            "kind": "disambiguate_email",
            "message_id": None,
            "candidates": matches,
            "topic": topic,
        },
    }
```

A "new email, no topic given" reply asks for the content directly (§4.1's
`draft_email` prompt already handles "no topic" gracefully for a *reply*,
by falling back to the original email's content — but a genuinely new
email has nothing else to draft from, so it must be asked for).

</details>

### 6.2 `handle_email_details_node` (revised, replaces `handle_email_disambiguation_node`)

**Post-launch fix.** Since `_resolve_email_target` now always resolves a
reply to a single deterministic `from:`-search result (§6.1), there's no
longer a multi-candidate case to disambiguate — this node instead handles
the "awaiting_email_details" continuation: the user answering a follow-up
that asked for the still-missing address or topic.

```python
def handle_email_details_node(state: RouterState, config: RunnableConfig) -> dict:
    pending_question = state["pending_question"]
    mode = pending_question["mode"]
    address = pending_question["address"]
    topic = pending_question["topic"]
    answer_text = state["inbound_text"].strip()

    if pending_question["asking_for"] == "address":
        address = answer_text
    else:
        topic = answer_text

    gmail_service, error = _load_gmail_service(config)
    if error:
        return {"reply_text": error, "pending_question": None}

    return _resolve_email_target(mode, address, topic, gmail_service)
```

<details>
<summary>Original design (superseded — kept for history)</summary>

`handle_email_disambiguation_node`, mirrors `handle_disambiguation_node`:

```python
def handle_email_disambiguation_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    candidates = pending_question["candidates"]
    topic = pending_question["topic"]
    lines = [f'{i + 1}. From {c["from"]} — "{c["subject"]}" ({c["date"]})' for i, c in enumerate(candidates)]

    try:
        choice = llm.resolve_disambiguation(
            configurable["genai_client"], configurable["gemini_model"], lines, state["inbound_text"]
        )
    except Exception:
        logger.exception("resolve_disambiguation failed after retries")
        return {
            "reply_text": "Couldn't process that reply right now — please try again.",
            "pending_question": None,
        }

    if choice < 1 or choice > len(candidates):
        return {
            "reply_text": "Sorry, I couldn't tell which one you meant — try naming it differently.",
            "pending_question": None,
        }

    return _confirm_email_reply(_resolved_from_match(candidates[choice - 1], topic))
```

</details>

### 6.3 `run_email_flow` and `resume_email_thread` — new, mirror `run_assignment_flow`/`resume_assignment_thread`

```python
async def run_email_flow(
    email_graph,
    resolved: dict,
    sender: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    pool,
    genai_client,
    gemini_model: str,
) -> None:
    """Runs draft -> relay as a single sequential graph invocation, same
    shape as run_assignment_flow. Unlike assignments, email drafting needs
    Gemini from the very first invocation (there's no separate 'ingest'
    step), so genai_client/gemini_model are passed here too, not only on
    resume."""
    thread_id = f"email:{uuid.uuid4()}"
    try:
        await email_graph.ainvoke(
            {
                "mode": resolved["mode"],
                "recipient_email": resolved["recipient_email"],
                "recipient_display": resolved["recipient_display"],
                "original_subject": resolved["original_subject"],
                "gmail_message_id": resolved["gmail_message_id"],
                "gmail_thread_id": resolved["gmail_thread_id"],
                "gmail_message_id_header": resolved["gmail_message_id_header"],
                "topic": resolved["topic"],
                "sender": sender,
            },
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "pool": pool,
                    "whatsapp_access_token": whatsapp_access_token,
                    "whatsapp_phone_number_id": whatsapp_phone_number_id,
                    "genai_client": genai_client,
                    "gemini_model": gemini_model,
                }
            },
        )
    except Exception:
        logger.exception("Unhandled error running email flow for %s", resolved["recipient_display"])
        try:
            await send_whatsapp_message(
                whatsapp_access_token,
                whatsapp_phone_number_id,
                sender,
                "Something went wrong while drafting that email — please try again.",
            )
        except Exception:
            logger.exception("Failed to send failure notice for email to %s", resolved["recipient_display"])


async def resume_email_thread(
    email_graph,
    thread_id: str,
    pending_item_message_id: str,
    reply_text: str,
    sender: str,
    whatsapp_access_token: str,
    whatsapp_phone_number_id: str,
    pool,
    genai_client,
    gemini_model: str,
) -> None:
    """Same shape and same Decision #3 rationale as resume_assignment_thread
    — no ack sent here; the graph's own parse_review_node is the sole
    source of any outbound message for a modeled outcome."""
    try:
        await email_graph.ainvoke(
            Command(resume=reply_text),
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "pool": pool,
                    "whatsapp_access_token": whatsapp_access_token,
                    "whatsapp_phone_number_id": whatsapp_phone_number_id,
                    "genai_client": genai_client,
                    "gemini_model": gemini_model,
                    "_pending_item_message_id": pending_item_message_id,
                }
            },
        )
    except Exception:
        logger.exception("Unhandled error resuming email thread %s", thread_id)
        try:
            await send_whatsapp_message(
                whatsapp_access_token, whatsapp_phone_number_id, sender,
                "Something went wrong processing that reply — please try again.",
            )
        except Exception:
            logger.exception("Failed to send failure notice for thread %s", thread_id)
```

### 6.4 `handle_email_confirmation_node` — new, mirrors `handle_confirmation_node`

```python
async def handle_email_confirmation_node(state: RouterState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pending_question = state["pending_question"]
    resolved = pending_question["resolved"]
    question = _confirm_email_question_text(resolved)

    try:
        answer = llm.parse_confirmation_reply(
            configurable["genai_client"], configurable["gemini_model"], question, state["inbound_text"]
        )
    except Exception:
        logger.exception("parse_confirmation_reply failed after retries")
        return {
            "reply_text": "Couldn't process that reply right now — please try again.",
            "pending_question": None,
        }

    if answer == "confirm":
        task = asyncio.create_task(
            run_email_flow(
                configurable["email_graph"],
                resolved,
                state["sender"],
                configurable["whatsapp_access_token"],
                configurable["whatsapp_phone_number_id"],
                configurable["pool"],
                configurable["genai_client"],
                configurable["gemini_model"],
            )
        )
        background_tasks = configurable["background_tasks"]
        background_tasks.add(task)
        task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))
        reply_text = "Drafting that email — I'll send it over when it's ready."
    else:
        reply_text = "Okay, not drafting that."

    return {"reply_text": reply_text, "pending_question": None}
```

### 6.5 Generalizing pending-item dispatch — change `_dispatch_pending_resume`

This is the one genuinely shared chokepoint: `handle_pending_item_reply`,
`resolve_pending_item_node`, and `handle_pending_item_disambiguation_node`
all call this helper, and today it's hardcoded to always resume via
`assignment_graph`/`resume_assignment_thread`. Fixing it here fixes all
three call sites at once.

```python
def _dispatch_pending_resume(item: dict, reply_text: str, state: RouterState, config: RunnableConfig) -> None:
    configurable = config["configurable"]
    if item["item_type"] == "assignment":
        coro = resume_assignment_thread(
            configurable["assignment_graph"], item["thread_id"], item["message_id"], reply_text,
            state["sender"], configurable["whatsapp_access_token"], configurable["whatsapp_phone_number_id"],
            configurable["pool"], configurable["genai_client"], configurable["gemini_model"],
        )
    else:  # "email"
        coro = resume_email_thread(
            configurable["email_graph"], item["thread_id"], item["message_id"], reply_text,
            state["sender"], configurable["whatsapp_access_token"], configurable["whatsapp_phone_number_id"],
            configurable["pool"], configurable["genai_client"], configurable["gemini_model"],
        )
    task = asyncio.create_task(coro)
    background_tasks = configurable["background_tasks"]
    background_tasks.add(task)
    task.add_done_callback(lambda t: (background_tasks.discard(t), _log_if_failed(t)))
```

### 6.6 By-name fallback — `resolve_pending_item_node` change

One-line change: `items = repo.list_pending_items(conn, "assignment")` →
`items = repo.list_pending_items(conn)` (per §2's widened signature), so
"approve the email to the registrar" fuzzy-matches against email drafts
too, not just assignments. Everything else in that node (scoring,
disambiguation) is already generic over `display_name` and needs no change.

### 6.7 Graph wiring changes

`route_after_entry`'s `pending_question["kind"]` dispatch dict gains
(**revised** — `disambiguate_email` was replaced by
`awaiting_email_details` per §6.1/§6.2's post-launch fix):
```python
"confirm_draft_email": "handle_email_confirmation",
"awaiting_email_details": "handle_email_details",
```

`route_after_classify`'s intent dispatch dict gains:
```python
"draft_email": "resolve_email",
```

`build_router_graph` registers three new nodes (`resolve_email` →
`resolve_email_node`, `handle_email_confirmation` →
`handle_email_confirmation_node`, `handle_email_details` →
`handle_email_details_node`) and adds each to both the
`route_after_entry`/`route_after_classify` edge maps and the existing
"every terminal node routes to send_reply" loop.

## 7. `agent/graph/email_graph.py` — full implementation (replaces the stub)

```python
"""Per-email-draft graph: draft -> interrupt -> revise/send on approval."""

import logging

import httpx
from googleapiclient.errors import HttpError
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from agent import google_auth, llm
from agent.db import repo
from agent.graph.nodes import gmail
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.graph.state import EmailState

logger = logging.getLogger(__name__)


async def draft_node(state: EmailState, config: RunnableConfig) -> dict:
    original_body = state.get("original_body")
    if state["mode"] == "reply" and original_body is None:
        pool = config["configurable"]["pool"]
        with pool.connection() as conn:
            clients = google_auth.load_google_clients(conn)
        if isinstance(clients, str):
            return {"failure_text": clients}
        gmail_service, _, _ = clients
        try:
            original_body = gmail.get_message_body(gmail_service, state["gmail_message_id"])
        except HttpError as e:
            return {"failure_text": f"Couldn't load the original email: {e}"}

    configurable = config["configurable"]
    try:
        subject, body = llm.draft_email(
            configurable["genai_client"],
            configurable["gemini_model"],
            topic=state["topic"],
            original_subject=state.get("original_subject"),
            original_body=original_body,
        )
    except Exception as e:  # noqa: BLE001 - Gemini error after retries
        return {"failure_text": f"Couldn't draft that email: {e}", "original_body": original_body}

    if state["mode"] == "reply":
        subject = f"Re: {state['original_subject']}"

    return {"original_body": original_body, "subject": subject, "body": body}


async def relay_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]
    sender = state["sender"]

    if state.get("failure_text"):
        try:
            await send_whatsapp_message(access_token, phone_number_id, sender, state["failure_text"])
        except httpx.HTTPStatusError:
            logger.exception("Failed to send failure notice to %s", sender)
        return {}

    message_text = (
        f"Draft email to {state['recipient_display']}:\n\n"
        f"Subject: {state['subject']}\n\n{state['body']}\n\n"
        "Reply approve, suggest changes, or say reject."
    )
    try:
        message_id = await send_whatsapp_message(access_token, phone_number_id, sender, message_text)
    except httpx.HTTPStatusError:
        logger.exception("Failed to send draft email to %s", sender)
        return {}

    pool = configurable["pool"]
    thread_id = configurable["thread_id"]
    display_name = f"Email to {state['recipient_display']}: {state['subject']}"
    with pool.connection() as conn:
        repo.create_pending_item(conn, message_id, thread_id, "email", display_name)

    return {}


def route_after_relay(state: EmailState) -> str:
    return "await_review_node" if not state.get("failure_text") else END


def await_review_node(state: EmailState) -> dict:
    reply = interrupt({"kind": "email_review", "subject": state["subject"]})
    return {"review_reply_text": reply}


def parse_review_node(state: EmailState, config: RunnableConfig) -> dict:
    """No try/except here — same reasoning as assignment_graph's
    parse_review_node (see that file's docstring): a Gemini failure after
    its own retries leaves this thread parked here for a later reply to
    naturally retry. Only revise/reject close the pending item immediately
    (a new one is created on the next relay, or the thread simply ends);
    approve deliberately leaves it open — send_node closes it only on
    success, mirroring assignment_graph's parse_submit_node/
    submission_prep_node pair, so a failed send can be retried by replying
    "yes" again without losing the pending item."""
    configurable = config["configurable"]
    decision, feedback = llm.parse_review_reply(
        configurable["genai_client"], configurable["gemini_model"], state["review_reply_text"]
    )

    if decision in ("revise", "reject"):
        pool = configurable["pool"]
        message_id = configurable["_pending_item_message_id"]
        with pool.connection() as conn:
            repo.close_pending_item(conn, message_id)

    return {"review_decision": decision, "review_feedback": feedback}


def route_after_review(state: EmailState) -> str:
    return {"approve": "send_node", "revise": "revise_node", "reject": END}[state["review_decision"]]


async def revise_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    try:
        subject, body = llm.draft_email(
            configurable["genai_client"],
            configurable["gemini_model"],
            topic=state["topic"],
            original_subject=state.get("original_subject"),
            original_body=state.get("original_body"),
            prior_subject=state["subject"],
            prior_body=state["body"],
            feedback=state["review_feedback"],
        )
    except Exception as e:  # noqa: BLE001 - Gemini error after retries
        return {"failure_text": f"Couldn't revise that email: {e}"}

    if state["mode"] == "reply":
        subject = f"Re: {state['original_subject']}"

    return {"subject": subject, "body": body}


async def send_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    pool = configurable["pool"]
    with pool.connection() as conn:
        clients = google_auth.load_google_clients(conn)
    if isinstance(clients, str):
        return {"failure_text": f"Couldn't send that email: {clients}"}
    gmail_service, _, _ = clients

    retry_hint = 'Reply "yes" again to retry — nothing was lost.'
    try:
        gmail.send_message(
            gmail_service,
            state["recipient_email"],
            state["subject"],
            state["body"],
            in_reply_to_header=state.get("gmail_message_id_header") or None,
            thread_id=state.get("gmail_thread_id"),
        )
    except HttpError as e:
        return {"failure_text": f"Couldn't send that email: {e}. {retry_hint}"}

    message_id = configurable["_pending_item_message_id"]
    with pool.connection() as conn:
        repo.close_pending_item(conn, message_id)

    return {"sent": True}


async def relay_send_node(state: EmailState, config: RunnableConfig) -> dict:
    configurable = config["configurable"]
    access_token = configurable["whatsapp_access_token"]
    phone_number_id = configurable["whatsapp_phone_number_id"]
    sender = state["sender"]

    message = state.get("failure_text") or f"Sent to {state['recipient_display']}."
    try:
        await send_whatsapp_message(access_token, phone_number_id, sender, message)
    except httpx.HTTPStatusError:
        logger.exception("Failed to send outcome notice to %s", sender)

    return {}


def build_email_graph(checkpointer) -> CompiledStateGraph:
    g = StateGraph(EmailState)
    g.add_node("draft_node", draft_node)
    g.add_node("relay_node", relay_node)
    g.add_node("await_review_node", await_review_node)
    g.add_node("parse_review_node", parse_review_node)
    g.add_node("revise_node", revise_node)
    g.add_node("send_node", send_node)
    g.add_node("relay_send_node", relay_send_node)

    g.set_entry_point("draft_node")
    g.add_edge("draft_node", "relay_node")
    g.add_conditional_edges(
        "relay_node", route_after_relay, {"await_review_node": "await_review_node", END: END}
    )
    g.add_edge("await_review_node", "parse_review_node")
    g.add_conditional_edges(
        "parse_review_node",
        route_after_review,
        {"send_node": "send_node", "revise_node": "revise_node", END: END},
    )
    g.add_edge("revise_node", "relay_node")
    g.add_edge("send_node", "relay_send_node")
    g.add_edge("relay_send_node", END)

    return g.compile(checkpointer=checkpointer)
```

`revise_node` routing back into the same `relay_node` (rather than a
separate node) is deliberate — it's the identical "show the new draft,
wait for another review" step either way, same as how
`assignment_graph.py`'s `revise_node` feeds back through `save_session_node`
into its own `relay_node`.

## 8. Crash recovery — `agent/graph/recovery.py` generalization

Today's `scan_for_interrupted_assignments` only scans `assignment:%`
thread ids and takes one compiled graph. Generalized to cover both:

```python
"""Startup crash-recovery scan: finds assignment/email threads stuck
mid-node, not cleanly paused."""


async def scan_for_interrupted_threads(assignment_graph, email_graph, conn) -> list[str]:
    """Returns display names of assignment/email threads that were
    actively mid-node (not cleanly paused at an interrupt, not terminal)
    when the process last stopped. Reads distinct thread_ids per prefix
    from the checkpointer's own `checkpoints` table, then inspects each
    thread's current state with the graph that owns that prefix."""
    interrupted = []
    for prefix, graph, label_key in (
        ("assignment:", assignment_graph, "title"),
        ("email:", email_graph, "recipient_display"),
    ):
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE %s", (f"{prefix}%",))
            thread_ids = [r[0] for r in cur.fetchall()]

        for thread_id in thread_ids:
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            if not snapshot.next:
                continue  # terminal — finished, or already ended (reject/decline)
            paused_at_interrupt = any(getattr(t, "interrupts", None) for t in snapshot.tasks)
            if paused_at_interrupt:
                continue  # legitimately waiting on a reply — not a crash
            interrupted.append(snapshot.values.get(label_key, thread_id))
    return interrupted
```

This is a rename + signature change (`scan_for_interrupted_assignments` →
`scan_for_interrupted_threads`, now taking both graphs) — its one caller
(`main.py`, §9) and `tests/test_recovery.py` both need updating to match
when this is implemented.

## 9. `agent/main.py` changes

```python
from agent.graph.email_graph import build_email_graph
from agent.graph.recovery import scan_for_interrupted_threads
```

Inside `lifespan`, alongside the existing `app.state.assignment_graph`:

```python
app.state.email_graph = build_email_graph(checkpointer)
```

And the crash-recovery block changes to:

```python
with pool.connection() as conn:
    interrupted = await scan_for_interrupted_threads(
        app.state.assignment_graph, app.state.email_graph, conn
    )
for name in interrupted:
    try:
        await send_whatsapp_message(
            settings.meta_whatsapp_access_token,
            settings.meta_whatsapp_phone_number_id,
            settings.my_whatsapp_number,
            f'I was working on "{name}" when I restarted — send it again if '
            "you'd like me to retry.",
        )
    except Exception:
        logger.exception("Failed to send crash-recovery heads-up for %s", name)
```

(The heads-up message drops "work on" since that phrasing is specific to
assignments — a restarted email draft is retried by re-sending the
original request, whatever form it took.)

## 10. `agent/webhook/routes.py` changes

`_process_text_message`'s `configurable` dict gains one entry, alongside
the existing `"assignment_graph"`:

```python
"email_graph": request.app.state.email_graph,
```

## 11. Out of scope

- **Attachments on outbound emails** — not in the product definition's
  email section; text body only.
- **CC/BCC, multiple recipients** — single recipient only, matches how the
  product definition only ever describes "an email"/"a reply", never a
  distribution list.
- **Changing the resolved recipient after confirmation** — if the wrong
  email/address was resolved, the user rejects and re-requests, exactly
  like an assignment draft against the wrong assignment. No separate
  "actually, I meant..." correction flow.
- **Gmail Draft (compose) objects** — see Decision #6. `gmail.compose`
  scope stays provisioned but unused.
- **Fuzzy contact-name resolution for new emails** — see Decision #2;
  deferred indefinitely, not just to a later phase, since it's a "never
  fabricate" conflict, not a build-order one.

## 12. Acceptance criteria mapping

| Criterion (from `docs/implementation_plan.md`'s originally-dropped Phase 3 line + product_definition.md's draft-review-loop section) | Where satisfied |
|---|---|
| Approving an email draft sends it via Gmail | §7 `route_after_review` → `send_node` |
| Rejecting discards it, no follow-up message | §7 `route_after_review` → `END`; `relay_send_node` never runs on reject |
| Revising regenerates it, no cap on rounds | §7 `revise_node` → `relay_node` loop, same pattern as assignment drafts |
| Two email drafts pending at once can be approved/rejected independently | Each gets its own `email:<uuid>` thread (§6.3) and its own `pending_items` row |
| Reply-to-message and by-name routing both work for email drafts | §6.5/§6.6 generalize the existing dispatch/by-name-match machinery, which was previously hardcoded to assignments only |
| Restarting mid-send produces a heads-up, not silence | §8 generalizes crash recovery to scan `email:%` threads too |
| Never fabricate email content/facts | `DRAFT_EMAIL_SYSTEM_PROMPT` (§4.2) explicit instruction; recipient resolution never guesses (Decision #2) |

## 13. Files changed/created — summary

| File | Change |
|---|---|
| `agent/db/repo.py` | `list_pending_items` gains optional `item_type=None` (§2) |
| `agent/graph/nodes/gmail.py` | `_get_message_metadata` captures `thread_id`/`message_id_header`; new `get_message_body`, `send_message` (§3) |
| `agent/llm.py` | `draft_email` intent + args on `classify_intent`; new `draft_email` function (§4) |
| `agent/graph/state.py` | New `EmailState`; `RouterState` comments updated (§5) |
| `agent/graph/router_graph.py` | New `resolve_email_node`, `handle_email_details_node`, `handle_email_confirmation_node`, `run_email_flow`, `resume_email_thread`; `_dispatch_pending_resume` and `resolve_pending_item_node` generalized; graph wiring extended (§6) |
| `agent/graph/email_graph.py` | Full implementation, replaces the one-line stub (§7) |
| `agent/graph/recovery.py` | `scan_for_interrupted_assignments` → `scan_for_interrupted_threads`, now scans both prefixes (§8) |
| `agent/main.py` | Builds `email_graph`, wires it into crash recovery (§9) |
| `agent/webhook/routes.py` | Passes `email_graph` through `configurable` (§10) |
| `tests/test_recovery.py` | Needs updating for the renamed/reshaped `scan_for_interrupted_threads` (existing test, not new — noted here since §8 breaks it) |
