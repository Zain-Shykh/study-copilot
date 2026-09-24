"""Unit tests for every node function in agent/graph/email_graph.py, plus
integration tests running the compiled graph end-to-end through the
approve/revise/reject review interrupt with an in-memory checkpointer —
covering the draft -> review -> send lifecycle from specs/email-drafting.md,
which previously had zero implementation and zero test coverage."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from googleapiclient.errors import HttpError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.graph import email_graph as eg


def _config(**overrides) -> dict:
    configurable = {
        "pool": MagicMock(),
        "thread_id": "email:abc123",
        "whatsapp_access_token": "test-token",
        "whatsapp_phone_number_id": "phone123",
        "genai_client": MagicMock(),
        "gemini_model": "gemini-x",
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _conn_of(config: dict):
    return config["configurable"]["pool"].connection.return_value.__enter__.return_value


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.facebook.com/x")
    response = httpx.Response(400, request=request)
    return httpx.HTTPStatusError("Bad Request", request=request, response=response)


def _http_error(status: int = 500) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Error"
    return HttpError(resp, b'{"error": {"message": "boom"}}')


REPLY_STATE = {
    "mode": "reply",
    "sender": "923115224115",
    "recipient_email": "registrar@school.edu",
    "recipient_display": "Registrar <registrar@school.edu>",
    "original_subject": "Your transcript",
    "gmail_message_id": "m1",
    "gmail_thread_id": "t1",
    "gmail_message_id_header": "<orig@mail.gmail.com>",
    "topic": "ask when it'll be ready",
}

NEW_STATE = {
    "mode": "new",
    "sender": "923115224115",
    "recipient_email": "jane.doe@school.edu",
    "recipient_display": "jane.doe@school.edu",
    "original_subject": None,
    "gmail_message_id": None,
    "gmail_thread_id": None,
    "gmail_message_id_header": None,
    "topic": "reschedule our meeting to Thursday",
}


class TestDraftNode:
    def test_reply_mode_fetches_original_body_and_forces_re_subject(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(eg.gmail, "get_message_body", MagicMock(return_value="Original body text."))
        monkeypatch.setattr(
            eg.llm, "draft_email", lambda *a, **k: ("Ignored model subject", "Drafted reply body.")
        )

        result = asyncio.run(eg.draft_node(dict(REPLY_STATE), _config()))

        assert result == {
            "original_body": "Original body text.",
            "subject": "Re: Your transcript",
            "body": "Drafted reply body.",
        }

    def test_reply_mode_skips_refetch_when_original_body_already_in_state(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", MagicMock())
        get_body = MagicMock()
        monkeypatch.setattr(eg.gmail, "get_message_body", get_body)
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("S", "Revised body."))
        state = dict(REPLY_STATE, original_body="Already fetched.")

        result = asyncio.run(eg.draft_node(state, _config()))

        get_body.assert_not_called()
        eg.google_auth.load_google_clients.assert_not_called()
        assert result["original_body"] == "Already fetched."

    def test_new_mode_uses_model_subject_directly(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("Meeting reschedule", "Body text."))

        result = asyncio.run(eg.draft_node(dict(NEW_STATE), _config()))

        assert result == {"original_body": None, "subject": "Meeting reschedule", "body": "Body text."}

    def test_reply_mode_auth_failure_returns_failure_text(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: "Google access has expired")

        result = asyncio.run(eg.draft_node(dict(REPLY_STATE), _config()))

        assert result == {"failure_text": "Google access has expired"}

    def test_reply_mode_gmail_fetch_error_returns_failure_text(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(eg.gmail, "get_message_body", MagicMock(side_effect=_http_error()))

        result = asyncio.run(eg.draft_node(dict(REPLY_STATE), _config()))

        assert "Couldn't load the original email" in result["failure_text"]

    def test_gemini_failure_returns_failure_text(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "draft_email", MagicMock(side_effect=RuntimeError("down")))

        result = asyncio.run(eg.draft_node(dict(NEW_STATE), _config()))

        assert "Couldn't draft that email" in result["failure_text"]


class TestRelayNode:
    def test_failure_text_sends_failure_message_only(self, monkeypatch):
        send_mock = AsyncMock()
        monkeypatch.setattr(eg, "send_whatsapp_message", send_mock)
        state = dict(REPLY_STATE, failure_text="Couldn't draft that email: down")

        result = asyncio.run(eg.relay_node(state, _config()))

        assert result == {}
        send_mock.assert_awaited_once_with(
            "test-token", "phone123", "923115224115", "Couldn't draft that email: down"
        )

    def test_failure_text_send_error_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(eg, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        state = dict(REPLY_STATE, failure_text="boom")

        result = asyncio.run(eg.relay_node(state, _config()))

        assert result == {}

    def test_success_sends_draft_and_creates_pending_item(self, monkeypatch):
        monkeypatch.setattr(eg, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))
        monkeypatch.setattr(eg.repo, "create_pending_item", MagicMock())
        state = dict(REPLY_STATE, subject="Re: Your transcript", body="Could you tell me when it's ready?")
        config = _config()

        result = asyncio.run(eg.relay_node(state, config))

        assert result == {}
        body = eg.send_whatsapp_message.call_args[0][3]
        assert "Subject: Re: Your transcript" in body
        assert "Could you tell me when it's ready?" in body
        assert "Reply approve, suggest changes, or say reject." in body
        eg.repo.create_pending_item.assert_called_once_with(
            _conn_of(config), "wamid.OUT1", "email:abc123", "email",
            "Email to Registrar <registrar@school.edu>: Re: Your transcript",
        )

    def test_send_error_skips_pending_item_creation(self, monkeypatch):
        monkeypatch.setattr(eg, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        monkeypatch.setattr(eg.repo, "create_pending_item", MagicMock())
        state = dict(REPLY_STATE, subject="S", body="B")

        asyncio.run(eg.relay_node(state, _config()))

        eg.repo.create_pending_item.assert_not_called()


class TestRouteAfterRelay:
    def test_no_failure_goes_to_await_review(self):
        assert eg.route_after_relay(dict(REPLY_STATE)) == "await_review_node"

    def test_failure_ends(self):
        assert eg.route_after_relay(dict(REPLY_STATE, failure_text="x")) == eg.END


class TestParseReviewNode:
    def test_approve_leaves_pending_item_open(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "parse_review_reply", lambda client, model, text: ("approve", None))
        monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())
        state = dict(REPLY_STATE, review_reply_text="looks good")

        result = eg.parse_review_node(state, _config(_pending_item_message_id="wamid.OUT1"))

        assert result == {"review_decision": "approve", "review_feedback": None}
        eg.repo.close_pending_item.assert_not_called()

    def test_revise_closes_pending_item(self, monkeypatch):
        monkeypatch.setattr(
            eg.llm, "parse_review_reply", lambda client, model, text: ("revise", "make it shorter")
        )
        monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())
        state = dict(REPLY_STATE, review_reply_text="make it shorter")
        config = _config(_pending_item_message_id="wamid.OUT1")

        result = eg.parse_review_node(state, config)

        assert result == {"review_decision": "revise", "review_feedback": "make it shorter"}
        eg.repo.close_pending_item.assert_called_once_with(_conn_of(config), "wamid.OUT1")

    def test_reject_closes_pending_item(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "parse_review_reply", lambda client, model, text: ("reject", None))
        monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())
        state = dict(REPLY_STATE, review_reply_text="no")
        config = _config(_pending_item_message_id="wamid.OUT1")

        eg.parse_review_node(state, config)

        eg.repo.close_pending_item.assert_called_once_with(_conn_of(config), "wamid.OUT1")


class TestRouteAfterReview:
    @pytest.mark.parametrize(
        "decision,expected", [("approve", "send_node"), ("revise", "revise_node"), ("reject", eg.END)]
    )
    def test_routes_by_decision(self, decision, expected):
        assert eg.route_after_review(dict(REPLY_STATE, review_decision=decision)) == expected


class TestReviseNode:
    def test_regenerates_and_keeps_reply_subject_fixed(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("Ignored", "Shorter body."))
        state = dict(
            REPLY_STATE, original_body="Orig.", subject="Re: Your transcript", body="Long body.",
            review_feedback="make it shorter",
        )

        result = asyncio.run(eg.revise_node(state, _config()))

        assert result == {"subject": "Re: Your transcript", "body": "Shorter body."}

    def test_new_mode_uses_regenerated_subject(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("Better subject", "Better body."))
        state = dict(NEW_STATE, subject="S", body="B", review_feedback="add more detail")

        result = asyncio.run(eg.revise_node(state, _config()))

        assert result == {"subject": "Better subject", "body": "Better body."}

    def test_failure_returns_failure_text(self, monkeypatch):
        monkeypatch.setattr(eg.llm, "draft_email", MagicMock(side_effect=RuntimeError("down")))
        state = dict(NEW_STATE, subject="S", body="B", review_feedback="x")

        result = asyncio.run(eg.revise_node(state, _config()))

        assert "Couldn't revise that email" in result["failure_text"]


class TestSendNode:
    def test_auth_failure_returns_failure_text(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: "Google access has expired")
        state = dict(REPLY_STATE, subject="S", body="B")

        result = asyncio.run(eg.send_node(state, _config(_pending_item_message_id="wamid.OUT1")))

        assert "Couldn't send that email" in result["failure_text"]

    def test_http_error_returns_failure_text_with_retry_hint_and_leaves_pending_item(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(eg.gmail, "send_message", MagicMock(side_effect=_http_error()))
        monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())
        state = dict(REPLY_STATE, subject="S", body="B")

        result = asyncio.run(eg.send_node(state, _config(_pending_item_message_id="wamid.OUT1")))

        assert "Couldn't send that email" in result["failure_text"]
        assert "again to retry" in result["failure_text"]
        eg.repo.close_pending_item.assert_not_called()

    def test_success_sends_via_gmail_and_closes_pending_item(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(eg.gmail, "send_message", MagicMock(return_value="sent1"))
        monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())
        state = dict(REPLY_STATE, subject="Re: Your transcript", body="Body text.")
        config = _config(_pending_item_message_id="wamid.OUT1")

        result = asyncio.run(eg.send_node(state, config))

        assert result == {"sent": True}
        eg.gmail.send_message.assert_called_once_with(
            "gmail", "registrar@school.edu", "Re: Your transcript", "Body text.",
            in_reply_to_header="<orig@mail.gmail.com>", thread_id="t1",
        )
        eg.repo.close_pending_item.assert_called_once_with(_conn_of(config), "wamid.OUT1")

    def test_new_mode_sends_without_reply_headers(self, monkeypatch):
        monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(eg.gmail, "send_message", MagicMock(return_value="sent1"))
        monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())
        state = dict(NEW_STATE, subject="Meeting", body="Body.")

        asyncio.run(eg.send_node(state, _config(_pending_item_message_id="wamid.OUT1")))

        eg.gmail.send_message.assert_called_once_with(
            "gmail", "jane.doe@school.edu", "Meeting", "Body.",
            in_reply_to_header=None, thread_id=None,
        )


class TestRelaySendNode:
    def test_failure_text_sends_failure_message(self, monkeypatch):
        send_mock = AsyncMock()
        monkeypatch.setattr(eg, "send_whatsapp_message", send_mock)
        state = dict(REPLY_STATE, failure_text="Couldn't send that email: boom")

        result = asyncio.run(eg.relay_send_node(state, _config()))

        assert result == {}
        send_mock.assert_awaited_once_with(
            "test-token", "phone123", "923115224115", "Couldn't send that email: boom"
        )

    def test_success_sends_confirmation(self, monkeypatch):
        send_mock = AsyncMock()
        monkeypatch.setattr(eg, "send_whatsapp_message", send_mock)
        state = dict(REPLY_STATE, sent=True)

        asyncio.run(eg.relay_send_node(state, _config()))

        send_mock.assert_awaited_once_with(
            "test-token", "phone123", "923115224115", "Sent to Registrar <registrar@school.edu>."
        )

    def test_send_error_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(eg, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        state = dict(REPLY_STATE, sent=True)

        result = asyncio.run(eg.relay_send_node(state, _config()))

        assert result == {}


# --- Integration tests: compiled graph through the review interrupt ---

THREAD_ID = "email:abc123"


def _graph_config(**overrides) -> dict:
    configurable = {
        "thread_id": THREAD_ID,
        "pool": MagicMock(),
        "whatsapp_access_token": "test-token",
        "whatsapp_phone_number_id": "phone123",
        "genai_client": MagicMock(),
        "gemini_model": "gemini-x",
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _stub_common_dependencies(monkeypatch):
    monkeypatch.setattr(eg.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
    monkeypatch.setattr(eg.gmail, "get_message_body", MagicMock(return_value="Original message body."))
    monkeypatch.setattr(eg.gmail, "send_message", MagicMock(return_value="sent1"))
    monkeypatch.setattr(eg, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))
    monkeypatch.setattr(eg.repo, "create_pending_item", MagicMock())
    monkeypatch.setattr(eg.repo, "close_pending_item", MagicMock())


@pytest.fixture
def graph():
    return eg.build_email_graph(InMemorySaver())


class TestEmailGraphIntegration:
    def test_full_happy_path_approve_sends_via_gmail(self, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch)
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("Re: Your transcript", "Drafted body."))
        monkeypatch.setattr(eg.llm, "parse_review_reply", lambda client, model, text: ("approve", None))

        async def run():
            await graph.ainvoke(dict(REPLY_STATE), config=_graph_config())
            snapshot = await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})
            assert snapshot.next == ("await_review_node",)

            await graph.ainvoke(
                Command(resume="approve"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert final_snapshot.values["sent"] is True
        eg.gmail.send_message.assert_called_once()

    def test_reject_ends_without_sending(self, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch)
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("S", "B"))
        monkeypatch.setattr(eg.llm, "parse_review_reply", lambda client, model, text: ("reject", None))

        async def run():
            await graph.ainvoke(dict(NEW_STATE), config=_graph_config())
            await graph.ainvoke(
                Command(resume="no thanks"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert final_snapshot.values.get("sent") is not True
        eg.gmail.send_message.assert_not_called()

    def test_revise_loops_back_through_relay_then_approve_sends(self, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch)
        drafts = iter([("S", "Long draft."), ("S", "Short draft.")])
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: next(drafts))
        decisions = iter([("revise", "make it shorter"), ("approve", None)])
        monkeypatch.setattr(eg.llm, "parse_review_reply", lambda client, model, text: next(decisions))

        async def run():
            await graph.ainvoke(dict(NEW_STATE), config=_graph_config())
            await graph.ainvoke(
                Command(resume="make it shorter"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            snapshot = await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})
            assert snapshot.next == ("await_review_node",)
            assert snapshot.values["body"] == "Short draft."

            await graph.ainvoke(
                Command(resume="approve"), config=_graph_config(_pending_item_message_id="wamid.OUT2")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert final_snapshot.values["sent"] is True

    def test_send_failure_ends_with_failure_text_and_open_pending_item(self, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch)
        monkeypatch.setattr(eg.gmail, "send_message", MagicMock(side_effect=_http_error()))
        monkeypatch.setattr(eg.llm, "draft_email", lambda *a, **k: ("S", "B"))
        monkeypatch.setattr(eg.llm, "parse_review_reply", lambda client, model, text: ("approve", None))

        async def run():
            await graph.ainvoke(dict(NEW_STATE), config=_graph_config())
            await graph.ainvoke(
                Command(resume="approve"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert "Couldn't send that email" in final_snapshot.values["failure_text"]
        eg.repo.close_pending_item.assert_not_called()
