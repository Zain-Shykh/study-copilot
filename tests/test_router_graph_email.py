"""Unit + integration tests for the email-drafting additions to
agent/graph/router_graph.py from specs/email-drafting.md: resolve_email_node
(target resolution via Gmail search), the confirm/disambiguate handlers, and
the generalization of _dispatch_pending_resume/resolve_pending_item_node to
cover email pending items (previously hardcoded to assignments only)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from googleapiclient.errors import HttpError
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import router_graph as rg
from agent.graph.router_graph import build_router_graph

SENDER = "923115224115"


def _http_error(status: int = 403) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Error"
    return HttpError(resp, b'{"error": {"message": "denied"}}')


def _config(**overrides) -> dict:
    configurable = {
        "pool": MagicMock(),
        "thread_id": f"user:{SENDER}",
        "genai_client": MagicMock(),
        "gemini_model": "gemini-x",
    }
    configurable.update(overrides)
    return {"configurable": configurable}


MATCH = {
    "id": "m1",
    "thread_id": "t1",
    "from": "Registrar <registrar@school.edu>",
    "subject": "Your transcript",
    "date": "Mon",
    "snippet": "...",
    "message_id_header": "<orig@mail.gmail.com>",
}


class TestResolveEmailNode:
    def test_empty_target_asks_who(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        state = {"intent_args": {"email_reference": "", "email_topic": ""}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert result == {
            "reply_text": "Who would you like to email, or which email are you replying to?",
            "pending_question": None,
        }

    def test_literal_address_without_topic_asks_what_it_should_say(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        state = {"intent_args": {"email_reference": "jane.doe@school.edu", "email_topic": ""}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert result == {
            "reply_text": "What should the email to jane.doe@school.edu say?",
            "pending_question": None,
        }

    def test_literal_address_with_topic_confirms_new_mode(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        state = {"intent_args": {"email_reference": "jane.doe@school.edu", "email_topic": "reschedule to Thursday"}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert result["pending_question"]["kind"] == "confirm_draft_email"
        resolved = result["pending_question"]["resolved"]
        assert resolved["mode"] == "new"
        assert resolved["recipient_email"] == "jane.doe@school.edu"
        assert "jane.doe@school.edu" in result["reply_text"]

    def test_single_gmail_match_confirms_reply_mode(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(rg.gmail, "list_messages", MagicMock(return_value=[MATCH]))
        state = {"intent_args": {"email_reference": "registrar", "email_topic": "ask when it'll be ready"}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        resolved = result["pending_question"]["resolved"]
        assert resolved["mode"] == "reply"
        assert resolved["recipient_email"] == "registrar@school.edu"
        assert resolved["gmail_thread_id"] == "t1"
        assert resolved["gmail_message_id_header"] == "<orig@mail.gmail.com>"
        assert '"Your transcript"' in result["reply_text"]

    def test_no_gmail_match_suggests_giving_an_address(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(rg.gmail, "list_messages", MagicMock(return_value=[]))
        state = {"intent_args": {"email_reference": "the dean", "email_topic": "x"}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert "couldn't find an email matching" in result["reply_text"]
        assert result["pending_question"] is None

    def test_multiple_gmail_matches_ask_to_disambiguate(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        second = dict(MATCH, id="m2", subject="Fee deadline")
        monkeypatch.setattr(rg.gmail, "list_messages", MagicMock(return_value=[MATCH, second]))
        state = {"intent_args": {"email_reference": "registrar", "email_topic": "x"}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert result["pending_question"]["kind"] == "disambiguate_email"
        assert result["pending_question"]["candidates"] == [MATCH, second]
        assert result["pending_question"]["topic"] == "x"

    def test_gmail_search_error_reports_plainly(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(rg.gmail, "list_messages", MagicMock(side_effect=_http_error()))
        state = {"intent_args": {"email_reference": "registrar", "email_topic": "x"}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert "Couldn't search Gmail right now" in result["reply_text"]

    def test_auth_failure_returns_reply_text(self, monkeypatch):
        monkeypatch.setattr(rg.google_auth, "load_google_clients", lambda conn: "Google access has expired")
        state = {"intent_args": {"email_reference": "registrar", "email_topic": "x"}}

        result = asyncio.run(rg.resolve_email_node(state, _config()))

        assert result == {"reply_text": "Google access has expired"}


class TestHandleEmailDisambiguationNode:
    def test_valid_choice_confirms_that_match(self, monkeypatch):
        second = dict(MATCH, id="m2", subject="Fee deadline", **{"from": "Bursar <bursar@school.edu>"})
        monkeypatch.setattr(rg.llm, "resolve_disambiguation", lambda client, model, lines, text: 2)
        state = {
            "pending_question": {"kind": "disambiguate_email", "candidates": [MATCH, second], "topic": "pay the fee"},
            "inbound_text": "the second one",
        }

        result = rg.handle_email_disambiguation_node(state, _config())

        resolved = result["pending_question"]["resolved"]
        assert resolved["recipient_email"] == "bursar@school.edu"
        assert resolved["topic"] == "pay the fee"

    def test_unclear_choice_asks_to_rename(self, monkeypatch):
        monkeypatch.setattr(rg.llm, "resolve_disambiguation", lambda client, model, lines, text: 0)
        state = {
            "pending_question": {"kind": "disambiguate_email", "candidates": [MATCH], "topic": "x"},
            "inbound_text": "neither",
        }

        result = rg.handle_email_disambiguation_node(state, _config())

        assert "couldn't tell which one" in result["reply_text"]
        assert result["pending_question"] is None


class TestHandleEmailConfirmationNode:
    def test_confirm_dispatches_run_email_flow_and_acks(self, monkeypatch):
        monkeypatch.setattr(rg.llm, "parse_confirmation_reply", lambda *a, **k: "confirm")
        run_mock = AsyncMock()
        monkeypatch.setattr(rg, "run_email_flow", run_mock)
        resolved = {
            "mode": "new", "recipient_email": "jane.doe@school.edu", "recipient_display": "jane.doe@school.edu",
            "original_subject": None, "gmail_message_id": None, "gmail_thread_id": None,
            "gmail_message_id_header": None, "topic": "reschedule",
        }
        state = {
            "pending_question": {"kind": "confirm_draft_email", "resolved": resolved},
            "inbound_text": "yes",
            "sender": SENDER,
        }
        config = _config(
            whatsapp_access_token="tok", whatsapp_phone_number_id="phone123",
            background_tasks=set(), email_graph=MagicMock(),
        )

        async def run():
            return await rg.handle_email_confirmation_node(state, config)

        result = asyncio.run(run())

        assert result == {"reply_text": "Drafting that email — I'll send it over when it's ready.", "pending_question": None}
        run_mock.assert_awaited_once()

    def test_decline_does_not_dispatch(self, monkeypatch):
        monkeypatch.setattr(rg.llm, "parse_confirmation_reply", lambda *a, **k: "decline")
        run_mock = AsyncMock()
        monkeypatch.setattr(rg, "run_email_flow", run_mock)
        resolved = {
            "mode": "new", "recipient_email": "x@y.com", "recipient_display": "x@y.com",
            "original_subject": None, "gmail_message_id": None, "gmail_thread_id": None,
            "gmail_message_id_header": None, "topic": "x",
        }
        state = {
            "pending_question": {"kind": "confirm_draft_email", "resolved": resolved},
            "inbound_text": "no",
            "sender": SENDER,
        }
        config = _config(background_tasks=set())

        result = asyncio.run(rg.handle_email_confirmation_node(state, config))

        assert result == {"reply_text": "Okay, not drafting that.", "pending_question": None}
        run_mock.assert_not_called()


# --- Integration: the generalized pending-item dispatch picks the right graph ---

def _base_config(**overrides) -> dict:
    configurable = {
        "thread_id": f"user:{SENDER}",
        "pool": MagicMock(),
        "genai_client": MagicMock(),
        "gemini_model": "gemini-x",
        "whatsapp_access_token": "test-token",
        "whatsapp_phone_number_id": "phone123",
        "assignment_graph": MagicMock(),
        "email_graph": MagicMock(),
        "background_tasks": set(),
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _inbound(text: str) -> dict:
    return {"inbound_text": text, "whatsapp_message_id": "wamid.IN1", "sender": SENDER, "reply_to_message_id": None}


@pytest.fixture
def graph():
    return build_router_graph(InMemorySaver())


def test_respond_to_pending_by_name_dispatches_to_resume_email_thread_for_an_email_item(monkeypatch, graph):
    """Regression check for the bug this spec fixes: resolve_pending_item_node
    used to only ever consider item_type='assignment', and
    _dispatch_pending_resume always called resume_assignment_thread —
    an email draft's "approve" reply would previously have been silently
    misrouted into the assignment graph."""
    monkeypatch.setattr(rg, "classify_intent", MagicMock(return_value=("respond_to_pending", {})))
    monkeypatch.setattr(
        rg.repo, "list_pending_items",
        lambda conn, item_type=None: [
            {"message_id": "wamid.OUT1", "thread_id": "email:abc", "item_type": "email", "display_name": "Email to registrar"}
        ],
    )
    resume_email_mock = AsyncMock()
    resume_assignment_mock = AsyncMock()
    monkeypatch.setattr(rg, "resume_email_thread", resume_email_mock)
    monkeypatch.setattr(rg, "resume_assignment_thread", resume_assignment_mock)
    config = _base_config()
    background_tasks = config["configurable"]["background_tasks"]

    async def run():
        await graph.ainvoke(_inbound("approve"), config=config)
        if background_tasks:
            await asyncio.gather(*background_tasks)

    asyncio.run(run())

    resume_email_mock.assert_awaited_once()
    resume_assignment_mock.assert_not_called()
    email_graph_arg, thread_id_arg, message_id_arg = resume_email_mock.call_args[0][:3]
    assert email_graph_arg == config["configurable"]["email_graph"]
    assert thread_id_arg == "email:abc"
    assert message_id_arg == "wamid.OUT1"
