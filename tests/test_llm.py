"""Unit tests for agent/llm.py: classify_intent (narrowed to 4 intents,
per specs/tool-calling-read-answers.md) and the new answer_question."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import llm


def _response_with_function_call(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(function_calls=[SimpleNamespace(name=name, args=args)])


def _response_with_text(text: str | None) -> SimpleNamespace:
    return SimpleNamespace(text=text)


class TestClassifyIntent:
    def test_answer_question_no_args(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "route_message", {"intent": "answer_question"}
        )

        intent, args = llm.classify_intent(client, "gemini-x", "what's due?")

        assert intent == "answer_question"
        assert args == {}

    def test_work_on_assignment_extracts_reference(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "route_message",
            {"intent": "work_on_assignment", "assignment_reference": "bio essay"},
        )

        intent, args = llm.classify_intent(client, "gemini-x", "work on the bio essay")

        assert intent == "work_on_assignment"
        assert args == {"assignment_reference": "bio essay"}

    def test_respond_to_pending_extracts_pending_item_reference(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "route_message",
            {"intent": "respond_to_pending", "pending_item_reference": "algorithms"},
        )

        intent, args = llm.classify_intent(client, "gemini-x", "approve the algorithms draft")

        assert intent == "respond_to_pending"
        assert args == {"pending_item_reference": "algorithms"}

    def test_unrecognized(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "route_message", {"intent": "unrecognized"}
        )

        intent, args = llm.classify_intent(client, "gemini-x", "turn in my assignment")

        assert intent == "unrecognized"
        assert args == {}

    @patch("agent.llm.time.sleep")
    def test_retries_transient_errors_then_succeeds(self, mock_sleep):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("503"),
            RuntimeError("503"),
            _response_with_function_call("route_message", {"intent": "answer_question"}),
        ]

        intent, args = llm.classify_intent(client, "gemini-x", "hi")

        assert intent == "answer_question"
        assert client.models.generate_content.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("agent.llm.time.sleep")
    def test_raises_after_max_attempts(self, mock_sleep):
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("still down")

        with pytest.raises(RuntimeError, match="still down"):
            llm.classify_intent(client, "gemini-x", "hi")

        assert client.models.generate_content.call_count == 3


class TestAnswerQuestion:
    def test_returns_final_text(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_text(
            "You have 2 assignments due."
        )

        result = llm.answer_question(client, "gemini-x", "how many assignments?", tools=[])

        assert result == "You have 2 assignments due."

    def test_passes_tools_and_max_remote_calls(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_text("ok")

        def a_tool():
            """A tool."""

        llm.answer_question(client, "gemini-x", "hi", tools=[a_tool], max_remote_calls=7)

        _, kwargs = client.models.generate_content.call_args
        config = kwargs["config"]
        assert config.tools == [a_tool]
        assert config.automatic_function_calling.maximum_remote_calls == 7

    def test_default_max_remote_calls_is_four(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_text("ok")

        llm.answer_question(client, "gemini-x", "hi", tools=[])

        _, kwargs = client.models.generate_content.call_args
        assert kwargs["config"].automatic_function_calling.maximum_remote_calls == 4

    def test_raises_runtime_error_on_empty_text(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_text("")

        with pytest.raises(RuntimeError, match="no final text"):
            llm.answer_question(client, "gemini-x", "hi", tools=[])

    def test_raises_runtime_error_on_none_text(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_text(None)

        with pytest.raises(RuntimeError, match="no final text"):
            llm.answer_question(client, "gemini-x", "hi", tools=[])

    @patch("agent.llm.time.sleep")
    def test_retries_transient_errors_then_succeeds(self, mock_sleep):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("429"),
            _response_with_text("answer after retry"),
        ]

        result = llm.answer_question(client, "gemini-x", "hi", tools=[])

        assert result == "answer after retry"
        assert client.models.generate_content.call_count == 2
