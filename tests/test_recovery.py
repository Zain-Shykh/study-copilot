"""Unit tests for agent/graph/recovery.py: the startup scan that finds
assignment threads stuck mid-node (not cleanly paused, not terminal)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent.graph import recovery


def _conn_with_thread_ids(thread_ids: list[str]) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(t,) for t in thread_ids]
    return conn


def _snapshot(next_=(), tasks=None, values=None):
    return SimpleNamespace(next=next_, tasks=tasks or [], values=values or {})


def test_returns_empty_list_when_no_assignment_threads():
    conn = _conn_with_thread_ids([])
    graph = MagicMock()
    graph.aget_state = AsyncMock()

    result = asyncio.run(recovery.scan_for_interrupted_assignments(graph, conn))

    assert result == []
    graph.aget_state.assert_not_called()


def test_skips_terminal_threads():
    conn = _conn_with_thread_ids(["assignment:c1:cw1"])
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_snapshot(next_=()))

    result = asyncio.run(recovery.scan_for_interrupted_assignments(graph, conn))

    assert result == []


def test_skips_threads_legitimately_paused_at_an_interrupt():
    conn = _conn_with_thread_ids(["assignment:c1:cw1"])
    task = SimpleNamespace(interrupts=(SimpleNamespace(value={"kind": "draft_review"}),))
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        return_value=_snapshot(next_=("await_review_node",), tasks=[task], values={"title": "Essay"})
    )

    result = asyncio.run(recovery.scan_for_interrupted_assignments(graph, conn))

    assert result == []


def test_includes_threads_stuck_mid_node_not_at_an_interrupt():
    conn = _conn_with_thread_ids(["assignment:c1:cw1"])
    task = SimpleNamespace(interrupts=())
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        return_value=_snapshot(next_=("draft_node",), tasks=[task], values={"title": "Bio Essay"})
    )

    result = asyncio.run(recovery.scan_for_interrupted_assignments(graph, conn))

    assert result == ["Bio Essay"]


def test_falls_back_to_thread_id_when_title_missing():
    conn = _conn_with_thread_ids(["assignment:c1:cw1"])
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=_snapshot(next_=("draft_node",), tasks=[], values={}))

    result = asyncio.run(recovery.scan_for_interrupted_assignments(graph, conn))

    assert result == ["assignment:c1:cw1"]


def test_checks_every_thread_independently():
    conn = _conn_with_thread_ids(["assignment:c1:cw1", "assignment:c2:cw2", "assignment:c3:cw3"])
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        side_effect=[
            _snapshot(next_=()),  # terminal
            _snapshot(next_=("await_review_node",), tasks=[SimpleNamespace(interrupts=(1,))], values={"title": "A"}),
            _snapshot(next_=("draft_node",), tasks=[SimpleNamespace(interrupts=())], values={"title": "C"}),
        ]
    )

    result = asyncio.run(recovery.scan_for_interrupted_assignments(graph, conn))

    assert result == ["C"]
    assert graph.aget_state.await_count == 3
