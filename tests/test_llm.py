"""Unit tests for agent/llm.py: classify_intent (per
specs/tool-calling-read-answers.md and specs/email-drafting.md) and the
answer_question/draft_email generation functions."""

from datetime import datetime, timezone
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

    def test_draft_email_extracts_reference_and_topic(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "route_message",
            {"intent": "draft_email", "email_reference": "the registrar", "email_topic": "ask about my transcript"},
        )

        intent, args = llm.classify_intent(client, "gemini-x", "reply to the registrar about my transcript")

        assert intent == "draft_email"
        assert args == {"email_reference": "the registrar", "email_topic": "ask about my transcript"}

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
    def test_raises_after_max_attempts_and_fallback(self, mock_sleep):
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("still down")

        with pytest.raises(RuntimeError, match="still down"):
            llm.classify_intent(client, "gemini-x", "hi")

        # 3 primary attempts + 3 fallback-model attempts
        assert client.models.generate_content.call_count == 6
        assert client.models.generate_content.call_args.kwargs["model"] == llm.FALLBACK_MODEL

    @patch("agent.llm.time.sleep")
    def test_falls_back_to_gemma_when_primary_exhausted(self, mock_sleep):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("503"),
            RuntimeError("503"),
            RuntimeError("503"),
            _response_with_function_call("route_message", {"intent": "answer_question"}),
        ]

        intent, args = llm.classify_intent(client, "gemini-x", "hi")

        assert intent == "answer_question"
        assert client.models.generate_content.call_count == 4
        assert client.models.generate_content.call_args.kwargs["model"] == llm.FALLBACK_MODEL

    @patch("agent.llm.time.sleep")
    def test_fallback_model_itself_retries_on_transient_failure(self, mock_sleep):
        """The fallback model has shown the same transient-failure pattern
        as the primary in practice, so it must get its own retries rather
        than a single unretried attempt."""
        client = MagicMock()
        client.models.generate_content.side_effect = [
            RuntimeError("503"),  # primary attempt 1
            RuntimeError("503"),  # primary attempt 2
            RuntimeError("503"),  # primary attempt 3 - exhausted
            RuntimeError("500"),  # fallback attempt 1
            _response_with_function_call("route_message", {"intent": "answer_question"}),  # fallback attempt 2
        ]

        intent, args = llm.classify_intent(client, "gemini-x", "hi")

        assert intent == "answer_question"
        assert client.models.generate_content.call_count == 5
        assert client.models.generate_content.call_args.kwargs["model"] == llm.FALLBACK_MODEL


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

    def test_includes_actual_todays_date_in_system_instruction(self, monkeypatch):
        """Regression test: the model used to guess "today" itself and got
        it wrong differently on every call (e.g. computing a different
        "next 2 weeks" window each time) — the real date must be injected
        so relative-time questions are answered consistently and correctly."""

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 17, tzinfo=timezone.utc)

        monkeypatch.setattr(llm, "datetime", _FixedDatetime)
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_text("ok")

        llm.answer_question(client, "gemini-x", "what's due in the next 2 weeks?", tools=[])

        _, kwargs = client.models.generate_content.call_args
        assert "2026-09-17" in kwargs["config"].system_instruction

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


class TestDraftEmail:
    def test_returns_subject_and_body(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "record_draft", {"subject": "Transcript request", "body": "Could you let me know when it'll be ready?"}
        )

        subject, body = llm.draft_email(client, "gemini-x", topic="ask when my transcript will be ready")

        assert subject == "Transcript request"
        assert body == "Could you let me know when it'll be ready?"

    def test_includes_original_email_context_for_a_reply(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "record_draft", {"subject": "Re: Transcript", "body": "..."}
        )

        llm.draft_email(
            client, "gemini-x", topic="ask when it'll be ready",
            original_subject="Transcript", original_body="Your transcript is being processed.",
        )

        _, kwargs = client.models.generate_content.call_args
        assert "Your transcript is being processed." in kwargs["contents"]
        assert "Transcript" in kwargs["contents"]

    def test_includes_prior_draft_and_feedback_on_a_revision(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "record_draft", {"subject": "S", "body": "Shorter body."}
        )

        llm.draft_email(
            client, "gemini-x", topic="ask for an extension",
            prior_subject="S", prior_body="A much longer previous draft.",
            feedback="make it shorter",
        )

        _, kwargs = client.models.generate_content.call_args
        assert "A much longer previous draft." in kwargs["contents"]
        assert "make it shorter" in kwargs["contents"]

    def test_forces_record_draft_function_call(self):
        client = MagicMock()
        client.models.generate_content.return_value = _response_with_function_call(
            "record_draft", {"subject": "S", "body": "B"}
        )

        llm.draft_email(client, "gemini-x", topic="hello")

        _, kwargs = client.models.generate_content.call_args
        assert kwargs["config"].tool_config.function_calling_config.allowed_function_names == ["record_draft"]

    @patch("agent.llm.time.sleep")
    def test_raises_after_max_attempts_and_fallback(self, mock_sleep):
        client = MagicMock()
        client.models.generate_content.side_effect = RuntimeError("still down")

        with pytest.raises(RuntimeError, match="still down"):
            llm.draft_email(client, "gemini-x", topic="hi")

        # 3 primary attempts + 3 fallback-model attempts
        assert client.models.generate_content.call_count == 6
        assert client.models.generate_content.call_args.kwargs["model"] == llm.FALLBACK_MODEL
