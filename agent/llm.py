"""Gemini client wrapper: intent classification and email summarization."""

import time

from google import genai
from google.genai import types

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

CLASSIFY_SYSTEM_PROMPT = """\
You classify one inbound WhatsApp message into exactly one supported read \
command by calling route_message. Supported commands:

- list_courses: the user wants to see their enrolled Classroom courses.
- whats_due: the user is asking about assignments (due soon, all, overdue,
  or missing). If they don't specify a window, default due_window_hours to
  48 and due_scope to "due_soon". If they say "everything" or "all
  assignments", use due_scope "all". If they ask what's overdue, use
  "overdue". If they ask what they're missing/haven't turned in, use
  "missing".
- summarize_emails: the user wants a summary/digest of their emails. If they
  don't specify a count or filter, default email_count to 10 and assume
  unread-only.
- search_emails: the user wants to find/list specific emails (e.g. from a
  sender, with a subject, in a label/folder) without asking for a summary.
  Extract whichever of email_sender/email_subject/email_label were
  mentioned; default email_count to 10 if not specified.
- work_on_assignment: the user wants to start drafting/researching a specific
  assignment (e.g. "work on the bio essay", "start the AI assignment",
  "draft the technical writing paper"). Extract the free-text name/description
  they used into assignment_reference. This is the ONLY intent that leads to
  eventually writing/drafting anything — still just resolves which assignment
  is meant at this stage, does not draft anything itself.
- respond_to_pending: the user is making a decision about a draft or
  pending item they were previously shown — approving it, asking for
  changes, rejecting it, or answering a submit yes/no question — whether
  or not they name which one. Includes replies like "yes", "looks good",
  "make it shorter", "no", "reject it", "approve the bio essay". If they
  name a specific course/assignment, extract it into
  pending_item_reference; leave it unset if they didn't name one.
- unrecognized: anything that isn't clearly one of the above commands.
  This includes any request to send, submit, reply to, or turn in
  something that isn't a reply to a pending item — those are not
  supported yet and must be classified as unrecognized, never routed to a
  read command.

Always call route_message exactly once with your best classification.
"""

ROUTE_MESSAGE_DECLARATION = types.FunctionDeclaration(
    name="route_message",
    description="Classify an inbound WhatsApp message into one supported read command.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "intent": types.Schema(
                type="STRING",
                enum=[
                    "list_courses",
                    "whats_due",
                    "summarize_emails",
                    "search_emails",
                    "work_on_assignment",
                    "respond_to_pending",
                    "unrecognized",
                ],
            ),
            "due_window_hours": types.Schema(
                type="INTEGER",
                description="only for whats_due; defaults to 48 if not mentioned",
            ),
            "due_scope": types.Schema(
                type="STRING",
                enum=["due_soon", "all", "overdue", "missing"],
                description="only for whats_due",
            ),
            "email_count": types.Schema(
                type="INTEGER",
                description="only for summarize_emails/search_emails; defaults to 10",
            ),
            "email_sender": types.Schema(
                type="STRING",
                description="only for search_emails, if a sender/from was named",
            ),
            "email_subject": types.Schema(
                type="STRING",
                description="only for search_emails, if subject keywords were named",
            ),
            "email_label": types.Schema(
                type="STRING",
                description="only for search_emails, if a label/folder was named",
            ),
            "assignment_reference": types.Schema(
                type="STRING",
                description="only for work_on_assignment; the free-text name/description the user gave",
            ),
            "pending_item_reference": types.Schema(
                type="STRING",
                description="only for respond_to_pending, if a specific item was named",
            ),
        },
        required=["intent"],
    ),
)

SUMMARIZE_SYSTEM_PROMPT = """\
You write concise, formal email digests for a personal assistant app. Given \
a list of emails (From/Subject/Date/snippet), produce one short line per \
email summarizing what it's about. Be factual — do not invent details not \
present in the snippet. Structured, no preamble, no closing remarks.
"""


def _with_retry(fn):
    last_error = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - transient network/5xx/429 from the API
            last_error = e
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (2**attempt))
    raise last_error


def classify_intent(client: genai.Client, model: str, text: str) -> tuple[str, dict]:
    """Classifies an inbound message into (intent, args) via a forced
    route_message function call. Raises after 3 failed attempts."""

    def call():
        return client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=CLASSIFY_SYSTEM_PROMPT,
                tools=[types.Tool(function_declarations=[ROUTE_MESSAGE_DECLARATION])],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY",
                        allowed_function_names=["route_message"],
                    )
                ),
            ),
        )

    response = _with_retry(call)
    call_ = response.function_calls[0]
    args = dict(call_.args or {})
    return args.pop("intent"), args


def summarize_emails(client: genai.Client, model: str, emails: list[dict]) -> str:
    """Returns a concise digest of the given emails as plain text. Raises
    after 3 failed attempts."""
    lines = []
    for email in emails:
        lines.append(
            f"From: {email.get('from', '')}\n"
            f"Subject: {email.get('subject', '')}\n"
            f"Date: {email.get('date', '')}\n"
            f"Snippet: {email.get('snippet', '')}"
        )
    content = "\n\n".join(lines)

    def call():
        return client.models.generate_content(
            model=model,
            contents=content,
            config=types.GenerateContentConfig(
                system_instruction=SUMMARIZE_SYSTEM_PROMPT,
            ),
        )

    response = _with_retry(call)
    return response.text


CONFIRM_DECLARATION = types.FunctionDeclaration(
    name="record_confirmation",
    description="Classify a reply to a yes/no confirmation question.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "answer": types.Schema(type="STRING", enum=["confirm", "decline"]),
        },
        required=["answer"],
    ),
)

CONFIRM_SYSTEM_PROMPT = """\
You are told a yes/no question that was just asked, and the user's reply to \
it. Decide whether the reply confirms ("yes") or declines ("no"). If the \
reply is unclear, off-topic, or ambiguous, treat it as a decline — do not \
guess "confirm" without a clear affirmative signal.
"""


def parse_confirmation_reply(client: genai.Client, model: str, question: str, reply_text: str) -> str:
    """Classifies a reply to a yes/no confirmation question as "confirm" or
    "decline" via a forced record_confirmation function call. Raises after
    3 failed attempts."""
    content = f"Question asked: {question}\nUser's reply: {reply_text}"

    def call():
        return client.models.generate_content(
            model=model,
            contents=content,
            config=types.GenerateContentConfig(
                system_instruction=CONFIRM_SYSTEM_PROMPT,
                tools=[types.Tool(function_declarations=[CONFIRM_DECLARATION])],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY",
                        allowed_function_names=["record_confirmation"],
                    )
                ),
            ),
        )

    response = _with_retry(call)
    return response.function_calls[0].args["answer"]


DISAMBIGUATE_DECLARATION = types.FunctionDeclaration(
    name="record_choice",
    description="Classify which numbered candidate a reply refers to, or none.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "choice": types.Schema(
                type="INTEGER",
                description="1-based index into the candidate list, or 0 if unclear/none match",
            ),
        },
        required=["choice"],
    ),
)

DISAMBIGUATE_SYSTEM_PROMPT = """\
You are given a numbered list of candidates that were just shown to the \
user, and the user's reply. Decide which 1-based index they meant. If the \
reply doesn't clearly pick one of the listed candidates, return 0.
"""


def resolve_disambiguation(client: genai.Client, model: str, candidate_lines: list[str], reply_text: str) -> int:
    """Classifies which numbered candidate the reply refers to via a forced
    record_choice function call. candidate_lines are pre-formatted "N. ..."
    display lines, in the same order as the caller's candidate list — kept
    generic (rather than a fixed dict shape) so it works for both
    assignment-shaped and pending-item-shaped candidates. Returns a
    1-based index, or 0 if unclear. Raises after 3 failed attempts."""
    content = "Candidates:\n" + "\n".join(candidate_lines) + f"\n\nUser's reply: {reply_text}"

    def call():
        return client.models.generate_content(
            model=model,
            contents=content,
            config=types.GenerateContentConfig(
                system_instruction=DISAMBIGUATE_SYSTEM_PROMPT,
                tools=[types.Tool(function_declarations=[DISAMBIGUATE_DECLARATION])],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY",
                        allowed_function_names=["record_choice"],
                    )
                ),
            ),
        )

    response = _with_retry(call)
    return int(response.function_calls[0].args["choice"])


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
