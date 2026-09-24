"""Unit tests for agent/graph/nodes/answer_question.py: answer_question_node
and its five tool closures (get_courses, get_all_assignments,
get_missing_assignments, get_announcements, get_recent_emails). See
specs/tool-calling-read-answers.md."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from agent.graph.nodes import answer_question as aq_module
from agent.graph.nodes import classroom, gmail


def _http_error(status: int = 403) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Error"
    return HttpError(resp, b'{"error": {"message": "denied"}}')


def _make_config() -> dict:
    return {
        "configurable": {
            "pool": MagicMock(),
            "genai_client": MagicMock(),
            "gemini_model": "gemini-x",
        }
    }


def _stub_clients(monkeypatch, gmail_service=None, classroom_service=None):
    gmail_service = gmail_service or MagicMock(name="gmail_service")
    classroom_service = classroom_service or MagicMock(name="classroom_service")
    drive_service = MagicMock(name="drive_service")
    monkeypatch.setattr(
        aq_module.google_auth,
        "load_google_clients",
        lambda conn: (gmail_service, classroom_service, drive_service),
    )
    return gmail_service, classroom_service, drive_service


def _capture_tools(monkeypatch, reply_text: str = "the answer") -> dict:
    """Patches llm.answer_question so the real Gemini tool-calling loop
    never runs; captures the tool callables the node built so the test can
    invoke them directly."""
    captured: dict = {}

    def fake_answer_question(client, model, text, tools, **kwargs):
        captured["tools"] = {t.__name__: t for t in tools}
        captured["text"] = text
        return reply_text

    monkeypatch.setattr(aq_module.llm, "answer_question", fake_answer_question)
    return captured


class TestAnswerQuestionNodeAuth:
    def test_returns_auth_error_string_without_calling_llm(self, monkeypatch):
        monkeypatch.setattr(
            aq_module.google_auth,
            "load_google_clients",
            lambda conn: "Google access has expired — please re-run ...",
        )
        fake_answer_question = MagicMock()
        monkeypatch.setattr(aq_module.llm, "answer_question", fake_answer_question)

        result = aq_module.answer_question_node({"inbound_text": "what's due?"}, _make_config())

        assert result == {"reply_text": "Google access has expired — please re-run ..."}
        fake_answer_question.assert_not_called()

    def test_catches_exception_from_answer_question(self, monkeypatch):
        _stub_clients(monkeypatch)

        def raising(*args, **kwargs):
            raise RuntimeError("model exhausted remote calls")

        monkeypatch.setattr(aq_module.llm, "answer_question", raising)

        result = aq_module.answer_question_node({"inbound_text": "what's due?"}, _make_config())

        assert result == {"reply_text": "Couldn't process that message right now — please try again."}

    def test_returns_model_reply_text_on_success(self, monkeypatch):
        _stub_clients(monkeypatch)
        _capture_tools(monkeypatch, reply_text="You have 2 assignments due.")

        result = aq_module.answer_question_node({"inbound_text": "how many assignments?"}, _make_config())

        assert result == {"reply_text": "You have 2 assignments due."}


class TestGetCourses:
    def test_success(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(
            classroom, "list_courses",
            lambda svc: [{"id": "c1", "name": "Algorithms"}, {"id": "c2", "name": "SCD"}],
        )

        aq_module.answer_question_node({"inbound_text": "list courses"}, _make_config())

        assert captured["tools"]["get_courses"]() == [{"name": "Algorithms"}, {"name": "SCD"}]

    def test_http_error_returns_error_payload(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(classroom, "list_courses", MagicMock(side_effect=_http_error()))

        aq_module.answer_question_node({"inbound_text": "list courses"}, _make_config())

        result = captured["tools"]["get_courses"]()
        assert len(result) == 1
        assert "error" in result[0]


class TestGetAllAssignments:
    def test_shapes_items_and_surfaces_unavailable_courses(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        due = datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)
        courses = [{"id": "c1", "name": "Algorithms"}]
        assignments = [
            {
                "course": {"id": "c1", "name": "Algorithms"},
                "courseWork": {"id": "cw1", "title": "HW1"},
                "due": due,
            },
            {
                "course": {"id": "c1", "name": "Algorithms"},
                "courseWork": {"id": "cw2", "title": "HW2 (no due date)"},
                "due": None,
            },
        ]
        list_assignments_mock = MagicMock(return_value=(assignments, ["DS-Lab C"]))
        monkeypatch.setattr(classroom, "list_courses", lambda svc: courses)
        monkeypatch.setattr(classroom, "list_assignments", list_assignments_mock)

        aq_module.answer_question_node({"inbound_text": "what's due?"}, _make_config())

        result = captured["tools"]["get_all_assignments"]()
        assert result["assignments"] == [
            {"course": "Algorithms", "title": "HW1", "due": due.isoformat()},
            {"course": "Algorithms", "title": "HW2 (no due date)", "due": None},
        ]
        assert result["unavailable_courses"] == ["DS-Lab C"]
        # No time-window filtering: scope="all", window_hours=0 (spec §3).
        args, kwargs = list_assignments_mock.call_args
        assert args[1] == courses
        assert kwargs["scope"] == "all"
        assert kwargs["window_hours"] == 0

    def test_omits_unavailable_courses_key_when_nothing_failed(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(classroom, "list_courses", lambda svc: [])
        monkeypatch.setattr(classroom, "list_assignments", lambda *a, **k: ([], []))

        aq_module.answer_question_node({"inbound_text": "what's due?"}, _make_config())

        result = captured["tools"]["get_all_assignments"]()
        assert result == {"assignments": []}
        assert "unavailable_courses" not in result

    def test_http_error_returns_error_dict(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(classroom, "list_courses", MagicMock(side_effect=_http_error()))

        aq_module.answer_question_node({"inbound_text": "what's due?"}, _make_config())

        result = captured["tools"]["get_all_assignments"]()
        assert "error" in result


class TestGetMissingAssignments:
    def test_calls_list_assignments_with_missing_scope(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        list_assignments_mock = MagicMock(return_value=([], []))
        monkeypatch.setattr(classroom, "list_courses", lambda svc: [])
        monkeypatch.setattr(classroom, "list_assignments", list_assignments_mock)

        aq_module.answer_question_node({"inbound_text": "what am I missing?"}, _make_config())

        captured["tools"]["get_missing_assignments"]()
        args, kwargs = list_assignments_mock.call_args
        assert kwargs["scope"] == "missing"


class TestGetAnnouncements:
    def test_no_course_name_returns_across_all_courses(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        courses = [{"id": "c1", "name": "Algorithms"}, {"id": "c2", "name": "SCD"}]
        monkeypatch.setattr(classroom, "list_courses", lambda svc: courses)
        list_announcements_mock = MagicMock(
            return_value=[
                {
                    "course": {"name": "Algorithms"},
                    "announcement": {"text": "Midterm moved", "creationTime": "2026-09-10T00:00:00Z"},
                }
            ]
        )
        monkeypatch.setattr(classroom, "list_announcements", list_announcements_mock)

        aq_module.answer_question_node({"inbound_text": "latest announcement?"}, _make_config())

        result = captured["tools"]["get_announcements"]()
        assert result == [{"course": "Algorithms", "text": "Midterm moved", "posted": "2026-09-10T00:00:00Z"}]
        # Called with every course when none was named.
        assert list_announcements_mock.call_args[0][1] == courses

    def test_course_name_filters_case_insensitively(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        courses = [{"id": "c1", "name": "Design and Analysis of Algorithms"}, {"id": "c2", "name": "SCD"}]
        monkeypatch.setattr(classroom, "list_courses", lambda svc: courses)
        list_announcements_mock = MagicMock(return_value=[])
        monkeypatch.setattr(classroom, "list_announcements", list_announcements_mock)

        aq_module.answer_question_node({"inbound_text": "latest in algorithms?"}, _make_config())

        captured["tools"]["get_announcements"](course_name="algorithms")
        called_courses = list_announcements_mock.call_args[0][1]
        assert called_courses == [{"id": "c1", "name": "Design and Analysis of Algorithms"}]

    def test_unmatched_course_name_returns_error_without_calling_list_announcements(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(classroom, "list_courses", lambda svc: [{"id": "c1", "name": "SCD"}])
        list_announcements_mock = MagicMock()
        monkeypatch.setattr(classroom, "list_announcements", list_announcements_mock)

        aq_module.answer_question_node({"inbound_text": "latest in nonexistent?"}, _make_config())

        result = captured["tools"]["get_announcements"](course_name="Quantum Basketweaving")
        assert result == [{"error": 'No course matching "Quantum Basketweaving"'}]
        list_announcements_mock.assert_not_called()

    def test_http_error_from_list_courses(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(classroom, "list_courses", MagicMock(side_effect=_http_error()))

        aq_module.answer_question_node({"inbound_text": "announcements?"}, _make_config())

        result = captured["tools"]["get_announcements"]()
        assert len(result) == 1 and "error" in result[0]

    def test_http_error_from_list_announcements(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(classroom, "list_courses", lambda svc: [{"id": "c1", "name": "SCD"}])
        monkeypatch.setattr(classroom, "list_announcements", MagicMock(side_effect=_http_error()))

        aq_module.answer_question_node({"inbound_text": "announcements?"}, _make_config())

        result = captured["tools"]["get_announcements"]()
        assert len(result) == 1 and "error" in result[0]


class TestGetRecentEmails:
    def test_forwards_filters_and_shapes_query(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        build_query_mock = MagicMock(return_value="from:prof@uni.edu subject:midterm")
        list_messages_mock = MagicMock(return_value=[{"from": "prof@uni.edu", "subject": "Midterm", "date": "", "snippet": ""}])
        monkeypatch.setattr(gmail, "build_query", build_query_mock)
        monkeypatch.setattr(gmail, "list_messages", list_messages_mock)

        aq_module.answer_question_node({"inbound_text": "emails from prof about midterm"}, _make_config())

        result = captured["tools"]["get_recent_emails"](
            max_results=5, unread_only=True, sender="prof@uni.edu", subject_contains="midterm"
        )
        assert result == [{"from": "prof@uni.edu", "subject": "Midterm", "date": "", "snippet": ""}]
        build_query_mock.assert_called_once_with(
            {"email_sender": "prof@uni.edu", "email_subject": "midterm", "after_date": None, "before_date": None},
            unread_only=True,
        )
        list_messages_mock.assert_called_once_with(
            list_messages_mock.call_args[0][0], "from:prof@uni.edu subject:midterm", 5
        )

    def test_forwards_date_range(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        build_query_mock = MagicMock(return_value="after:1758657000 before:1758743400")
        monkeypatch.setattr(gmail, "build_query", build_query_mock)
        monkeypatch.setattr(gmail, "list_messages", MagicMock(return_value=[]))

        aq_module.answer_question_node({"inbound_text": "emails from yesterday"}, _make_config())

        captured["tools"]["get_recent_emails"](after_date="2026-09-24", before_date="2026-09-25")
        build_query_mock.assert_called_once_with(
            {"email_sender": None, "email_subject": None, "after_date": "2026-09-24", "before_date": "2026-09-25"},
            unread_only=False,
        )

    def test_defaults_unset_filters_to_none(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        build_query_mock = MagicMock(return_value="")
        monkeypatch.setattr(gmail, "build_query", build_query_mock)
        monkeypatch.setattr(gmail, "list_messages", MagicMock(return_value=[]))

        aq_module.answer_question_node({"inbound_text": "recent emails"}, _make_config())

        captured["tools"]["get_recent_emails"]()
        build_query_mock.assert_called_once_with(
            {"email_sender": None, "email_subject": None, "after_date": None, "before_date": None}, unread_only=False
        )

    def test_http_error_returns_error_payload(self, monkeypatch):
        _stub_clients(monkeypatch)
        captured = _capture_tools(monkeypatch)
        monkeypatch.setattr(gmail, "build_query", MagicMock(return_value=""))
        monkeypatch.setattr(gmail, "list_messages", MagicMock(side_effect=_http_error()))

        aq_module.answer_question_node({"inbound_text": "recent emails"}, _make_config())

        result = captured["tools"]["get_recent_emails"]()
        assert len(result) == 1 and "error" in result[0]
