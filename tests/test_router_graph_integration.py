"""Integration tests: run the compiled router graph end-to-end (route_entry
-> classify_intent -> handler -> send_reply) with an in-memory checkpointer,
mocking only the outer boundaries (Gemini classify_intent/answer_question,
Google API auth, WhatsApp send) — not individual node internals. Covers the
tool-calling-read-answers.md acceptance criteria that work_on_assignment
behaves identically to before, and that answer_question replies are sent
verbatim from the model's own text."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import router_graph as rg_module
from agent.graph.router_graph import build_router_graph

SENDER = "923115224115"


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
        "student_info": "",
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _inbound(text: str, message_id: str = "wamid.IN1") -> dict:
    return {
        "inbound_text": text,
        "whatsapp_message_id": message_id,
        "sender": SENDER,
        "reply_to_message_id": None,
    }


@pytest.fixture
def graph():
    return build_router_graph(InMemorySaver())


def _patch_send(monkeypatch) -> dict:
    sent: dict = {}

    async def fake_send(access_token, phone_number_id, to, body):
        sent["access_token"] = access_token
        sent["phone_number_id"] = phone_number_id
        sent["to"] = to
        sent["body"] = body
        return "wamid.OUT1"

    monkeypatch.setattr(rg_module, "send_whatsapp_message", fake_send)
    return sent


def test_answer_question_reply_is_sent_verbatim_from_model(monkeypatch, graph):
    monkeypatch.setattr(rg_module, "classify_intent", MagicMock(return_value=("answer_question", {})))
    monkeypatch.setattr(
        "agent.graph.nodes.answer_question.google_auth.load_google_clients",
        lambda conn: (MagicMock(), MagicMock(), MagicMock()),
    )
    monkeypatch.setattr(
        "agent.graph.nodes.answer_question.llm.answer_question",
        lambda *a, **k: "Design and Analysis of Algorithms is due 2026-09-16.",
    )
    sent = _patch_send(monkeypatch)

    asyncio.run(
        graph.ainvoke(
            _inbound("when is the algorithms assignment due?"),
            config=_base_config(),
        )
    )

    assert sent["body"] == "Design and Analysis of Algorithms is due 2026-09-16."
    assert sent["to"] == SENDER
    assert sent["access_token"] == "test-token"


def test_answer_question_no_credentials_short_circuits_before_tool_loop(monkeypatch, graph):
    monkeypatch.setattr(rg_module, "classify_intent", MagicMock(return_value=("answer_question", {})))
    monkeypatch.setattr(
        "agent.graph.nodes.answer_question.google_auth.load_google_clients",
        lambda conn: "Google access has expired — please re-run `.venv/bin/python -m agent.setup_google_auth` to re-authenticate.",
    )
    answer_question_mock = MagicMock()
    monkeypatch.setattr("agent.graph.nodes.answer_question.llm.answer_question", answer_question_mock)
    sent = _patch_send(monkeypatch)

    asyncio.run(graph.ainvoke(_inbound("what's due?"), config=_base_config()))

    answer_question_mock.assert_not_called()
    assert sent["body"].startswith("Google access has expired")


def test_unrecognized_intent_sends_fallback_reply(monkeypatch, graph):
    monkeypatch.setattr(rg_module, "classify_intent", MagicMock(return_value=("unrecognized", {})))
    sent = _patch_send(monkeypatch)

    asyncio.run(graph.ainvoke(_inbound("please submit this for me"), config=_base_config()))

    assert sent["body"] == rg_module.FALLBACK_REPLY


def test_classify_intent_failure_sends_generic_error(monkeypatch, graph):
    def raising(*args, **kwargs):
        raise RuntimeError("Gemini down")

    monkeypatch.setattr(rg_module, "classify_intent", raising)
    sent = _patch_send(monkeypatch)

    asyncio.run(graph.ainvoke(_inbound("what's due?"), config=_base_config()))

    assert sent["body"] == "Couldn't process that message right now — please try again."


def test_work_on_assignment_still_routes_through_resolve_assignment_not_answer_question(monkeypatch, graph):
    """Regression check for the spec's acceptance criterion that
    work_on_assignment is unaffected: routes through resolve_assignment_node
    (which calls agent.graph.router_graph's own google_auth), never through
    answer_question_node's tool-calling loop."""
    monkeypatch.setattr(
        rg_module, "classify_intent",
        MagicMock(return_value=("work_on_assignment", {"assignment_reference": "bio essay"})),
    )
    monkeypatch.setattr(
        rg_module.google_auth, "load_google_clients",
        lambda conn: "Google access has expired — please re-run ... to re-authenticate.",
    )
    answer_question_tool_loop = MagicMock()
    monkeypatch.setattr("agent.graph.nodes.answer_question.llm.answer_question", answer_question_tool_loop)
    sent = _patch_send(monkeypatch)

    asyncio.run(graph.ainvoke(_inbound("work on the bio essay"), config=_base_config()))

    assert sent["body"].startswith("Google access has expired")
    answer_question_tool_loop.assert_not_called()


def test_respond_to_pending_with_nothing_pending(monkeypatch, graph):
    """Regression check: respond_to_pending still resolves via
    resolve_pending_item_node (repo.list_pending_items), unaffected by the
    answer_question refactor."""
    monkeypatch.setattr(
        rg_module, "classify_intent",
        MagicMock(return_value=("respond_to_pending", {})),
    )
    monkeypatch.setattr(rg_module.repo, "list_pending_items", lambda conn, item_type=None: [])
    sent = _patch_send(monkeypatch)

    asyncio.run(graph.ainvoke(_inbound("yes"), config=_base_config()))

    assert sent["body"] == "There's nothing pending right now."


def test_run_assignment_flow_forwards_student_info_to_assignment_graph():
    assignment_graph = MagicMock()
    assignment_graph.ainvoke = AsyncMock()
    resolved = {"course_id": "c1", "coursework_id": "cw1", "course_name": "Algorithms", "title": "HW1"}

    asyncio.run(
        rg_module.run_assignment_flow(
            assignment_graph,
            resolved,
            SENDER,
            "test-token",
            "phone123",
            MagicMock(),
            "Roll number: 22-CS-045",
        )
    )

    configurable = assignment_graph.ainvoke.call_args.kwargs["config"]["configurable"]
    assert configurable["student_info"] == "Roll number: 22-CS-045"
