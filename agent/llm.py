"""Gemini client wrapper: intent classification, tool-calling read answers,
and email summarization."""

import time
from datetime import datetime, timezone
from typing import Callable

from google import genai
from google.genai import types

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

CLASSIFY_SYSTEM_PROMPT = """\
You classify one inbound WhatsApp message into exactly one category by
calling route_message. Categories:

- answer_question: the user is asking about their Classroom courses,
  assignments, announcements, or email in any form — listing, counting,
  a specific item's details, a summary, a search. Anything read-only.
- work_on_assignment: the user wants to start drafting/researching a
  specific assignment (e.g. "work on the bio essay", "start the AI
  assignment"). Extract the free-text name/description they used into
  assignment_reference. This is the ONLY intent that leads to eventually
  writing/drafting anything — still just resolves which assignment is
  meant at this stage, does not draft anything itself.
- draft_email: the user wants to reply to an existing email or compose a
  new one (e.g. "reply to the registrar's email about my transcript",
  "email jane.doe@school.edu about rescheduling"). Extract who/which email
  into email_reference and what it should say into email_topic (leave
  email_topic unset if genuinely not given — e.g. "reply to that email
  from my advisor" with no stated content is still valid). This is the
  ONLY intent that leads to drafting an email — still just identifies the
  target/topic at this stage, does not draft or send anything itself.
- respond_to_pending: the user is making a decision about a draft or
  pending item they were previously shown — approving it, asking for
  changes, rejecting it, or answering a submit yes/no question — whether
  or not they name which one. Includes replies like "yes", "looks good",
  "make it shorter", "no", "reject it", "approve the bio essay". If they
  name a specific course/assignment/email, extract it into
  pending_item_reference; leave it unset if they didn't name one.
- unrecognized: anything that isn't clearly one of the above. A request to
  submit/turn in an assignment other than through its own approval loop
  (i.e. not a reply to a pending item) is still unrecognized — assignment
  submission only ever happens via that loop, never as a standalone
  command.

Always call route_message exactly once with your best classification.
"""

ROUTE_MESSAGE_DECLARATION = types.FunctionDeclaration(
    name="route_message",
    description="Classify an inbound WhatsApp message.",
    parameters=types.Schema(
        type="OBJECT",
        properties={
            "intent": types.Schema(
                type="STRING",
                enum=[
                    "answer_question",
                    "work_on_assignment",
                    "draft_email",
                    "respond_to_pending",
                    "unrecognized",
                ],
            ),
            "assignment_reference": types.Schema(
                type="STRING",
                description="only for work_on_assignment; the free-text name/description the user gave",
            ),
            "email_reference": types.Schema(
                type="STRING",
                description=(
                    "only for draft_email; either a literal email address to send a new "
                    "email to, or free text identifying an existing email to reply to "
                    "(e.g. a sender name, subject, or topic)"
                ),
            ),
            "email_topic": types.Schema(
                type="STRING",
                description="only for draft_email; what the email should say, as given",
            ),
            "pending_item_reference": types.Schema(
                type="STRING",
                description="only for respond_to_pending, if a specific item was named",
            ),
        },
        required=["intent"],
    ),
)

ANSWER_SYSTEM_PROMPT = """\
You answer one inbound WhatsApp message about the user's Gmail and Google
Classroom by calling the available tools to fetch real data, then writing
a short, direct reply. Rules:
- Always call at least one tool before answering — never guess or use
  outside knowledge about the user's courses, assignments, or email.
- Call as many tools as you need to fully answer, including calling the
  same tool again with different arguments.
- If a tool result notes it couldn't check something (e.g. a course
  returned an error), mention that limitation briefly in your answer
  rather than silently ignoring it or treating the data as complete.
- If nothing in the tool results answers the question, say so plainly —
  do not fabricate an answer.
- For any relative-time question ("due soon", "next 2 weeks", "overdue",
  "today"), compute the window from the actual date given to you above —
  never guess or assume what today's date is.
- Keep the reply concise and in plain text formatted for WhatsApp — short
  lines, no markdown headers, no asterisk bullets (use "-").
"""


def answer_question(
    client: genai.Client,
    model: str,
    text: str,
    tools: list[Callable],
    max_remote_calls: int = 4,
) -> str:
    """Runs Gemini's automatic function-calling loop (SDK-managed — passing
    plain Python callables as `tools` makes generate_content execute them
    itself and loop until a final text answer, bounded by
    automatic_function_calling.maximum_remote_calls) to answer a free-text
    question. Raises after 3 failed attempts (network/5xx/429, via
    _with_retry) or RuntimeError if the model exhausts max_remote_calls
    without producing a final text answer."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_instruction = f"Today's date is {today} (UTC).\n\n{ANSWER_SYSTEM_PROMPT}"

    def call():
        return client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=max_remote_calls,
                ),
            ),
        )

    response = _with_retry(call)
    if not response.text:
        raise RuntimeError("answer_question: model produced no final text")
    return response.text

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
