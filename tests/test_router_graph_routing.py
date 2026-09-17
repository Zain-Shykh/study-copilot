"""Unit tests for the pure routing function route_after_classify, updated
per specs/tool-calling-read-answers.md to the narrowed 4-intent set."""

from agent.graph.router_graph import route_after_classify


def test_answer_question_routes_to_answer_question_node():
    assert route_after_classify({"intent": "answer_question"}) == "answer_question_node"


def test_work_on_assignment_routes_to_resolve_assignment():
    assert route_after_classify({"intent": "work_on_assignment"}) == "resolve_assignment"


def test_respond_to_pending_routes_to_resolve_pending_item():
    assert route_after_classify({"intent": "respond_to_pending"}) == "resolve_pending_item"


def test_unrecognized_routes_to_fallback_node():
    assert route_after_classify({"intent": "unrecognized"}) == "fallback_node"


def test_classify_intent_failure_short_circuits_to_send_reply():
    # classify_intent_node already set reply_text (its own error fallback) —
    # route_after_classify must not try to index into a missing/stale intent.
    state = {"reply_text": "Couldn't process that message right now — please try again."}
    assert route_after_classify(state) == "send_reply"
