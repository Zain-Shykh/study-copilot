"""Unit tests for agent/graph/recovery.py: the startup scan that finds
assignment/email threads stuck mid-node (not cleanly paused, not terminal)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent.graph import recovery


def _conn_with_thread_ids(assignment_ids: list[str] | None = None, email_ids: list[str] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    results_by_prefix = {"assignment:%": assignment_ids or [], "email:%": email_ids or []}

    def _execute(_query, params):
        cursor.fetchall.return_value = [(t,) for t in results_by_prefix[params[0]]]

    cursor.execute.side_effect = _execute
    return conn


def _snapshot(next_=(), tasks=None, values=None):
    return SimpleNamespace(next=next_, tasks=tasks or [], values=values or {})


def _idle_graph() -> MagicMock:
    graph = MagicMock()
    graph.aget_state = AsyncMock()
    return graph


def test_returns_empty_list_when_no_threads():
    conn = _conn_with_thread_ids()
    assignment_graph = _idle_graph()
    email_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == []
    assignment_graph.aget_state.assert_not_called()
    email_graph.aget_state.assert_not_called()


def test_skips_terminal_assignment_thread():
    conn = _conn_with_thread_ids(assignment_ids=["assignment:c1:cw1"])
    assignment_graph = MagicMock()
    assignment_graph.aget_state = AsyncMock(return_value=_snapshot(next_=()))
    email_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == []


def test_skips_thread_legitimately_paused_at_an_interrupt():
    conn = _conn_with_thread_ids(assignment_ids=["assignment:c1:cw1"])
    task = SimpleNamespace(interrupts=(SimpleNamespace(value={"kind": "draft_review"}),))
    assignment_graph = MagicMock()
    assignment_graph.aget_state = AsyncMock(
        return_value=_snapshot(next_=("await_review_node",), tasks=[task], values={"title": "Essay"})
    )
    email_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == []


def test_includes_assignment_thread_stuck_mid_node():
    conn = _conn_with_thread_ids(assignment_ids=["assignment:c1:cw1"])
    task = SimpleNamespace(interrupts=())
    assignment_graph = MagicMock()
    assignment_graph.aget_state = AsyncMock(
        return_value=_snapshot(next_=("draft_node",), tasks=[task], values={"title": "Bio Essay"})
    )
    email_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == ["Bio Essay"]


def test_includes_email_thread_stuck_mid_node_using_recipient_display():
    conn = _conn_with_thread_ids(email_ids=["email:abc123"])
    task = SimpleNamespace(interrupts=())
    email_graph = MagicMock()
    email_graph.aget_state = AsyncMock(
        return_value=_snapshot(next_=("send_node",), tasks=[task], values={"recipient_display": "registrar@school.edu"})
    )
    assignment_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == ["registrar@school.edu"]


def test_skips_email_thread_paused_at_its_own_interrupt():
    conn = _conn_with_thread_ids(email_ids=["email:abc123"])
    task = SimpleNamespace(interrupts=(SimpleNamespace(value={"kind": "email_review"}),))
    email_graph = MagicMock()
    email_graph.aget_state = AsyncMock(
        return_value=_snapshot(next_=("await_review_node",), tasks=[task], values={"recipient_display": "x@y.com"})
    )
    assignment_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == []


def test_falls_back_to_thread_id_when_label_missing():
    conn = _conn_with_thread_ids(assignment_ids=["assignment:c1:cw1"])
    assignment_graph = MagicMock()
    assignment_graph.aget_state = AsyncMock(return_value=_snapshot(next_=("draft_node",), tasks=[], values={}))
    email_graph = _idle_graph()

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == ["assignment:c1:cw1"]


def test_checks_every_thread_independently_across_both_prefixes():
    conn = _conn_with_thread_ids(
        assignment_ids=["assignment:c1:cw1", "assignment:c2:cw2"], email_ids=["email:abc"]
    )
    assignment_graph = MagicMock()
    assignment_graph.aget_state = AsyncMock(
        side_effect=[
            _snapshot(next_=()),  # terminal
            _snapshot(next_=("draft_node",), tasks=[SimpleNamespace(interrupts=())], values={"title": "C"}),
        ]
    )
    email_graph = MagicMock()
    email_graph.aget_state = AsyncMock(
        return_value=_snapshot(
            next_=("send_node",), tasks=[SimpleNamespace(interrupts=())], values={"recipient_display": "e@x.com"}
        )
    )

    result = asyncio.run(recovery.scan_for_interrupted_threads(assignment_graph, email_graph, conn))

    assert result == ["C", "e@x.com"]
    assert assignment_graph.aget_state.await_count == 2
    assert email_graph.aget_state.await_count == 1
