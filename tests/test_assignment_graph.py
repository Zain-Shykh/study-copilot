"""Unit tests for every node function in agent/graph/assignment_graph.py,
plus integration tests running the compiled graph end-to-end through both
interrupt() pauses (draft review, submit confirm) with an in-memory
checkpointer — covering the ingest -> draft -> review -> submit -> Drive
upload lifecycle (Phases 2-3), which previously had zero test coverage."""

import asyncio
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

from agent.graph import assignment_graph as ag


def _config(**overrides) -> dict:
    configurable = {
        "pool": MagicMock(),
        "thread_id": "assignment:c1:cw1",
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


STATE = {
    "course_id": "c1",
    "course_name": "Algorithms",
    "coursework_id": "cw1",
    "title": "HW1",
    "sender": "923115224115",
}


class TestIngestNode:
    def test_auth_failure_returns_not_ingested(self, monkeypatch):
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: "auth message")

        result = asyncio.run(ag.ingest_node(dict(STATE), _config()))

        assert result == {"ingested": False, "failure_text": "auth message"}

    def test_success_returns_ingested_true_and_unsupported_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        ingest_mock = AsyncMock(return_value={"success": True, "unsupported_files": ["video.mov"]})
        monkeypatch.setattr(ag.ingestion, "ingest_assignment", ingest_mock)

        result = asyncio.run(ag.ingest_node(dict(STATE), _config()))

        assert result == {"ingested": True, "unsupported_files": ["video.mov"]}
        ingest_mock.assert_awaited_once_with("classroom", "drive", "c1", "cw1", tmp_path)

    def test_failure_with_failed_file_includes_filename_in_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("g", "c", "d"))
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(
            ag.ingestion,
            "ingest_assignment",
            AsyncMock(return_value={"success": False, "failed_file": "Rubric.pdf", "error": "Drive 500"}),
        )

        result = asyncio.run(ag.ingest_node(dict(STATE), _config()))

        assert result == {
            "ingested": False,
            "failure_text": 'Couldn\'t ingest "Rubric.pdf": Drive 500. Send "work on HW1" again to retry.',
        }

    def test_failure_without_failed_file_uses_generic_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("g", "c", "d"))
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(
            ag.ingestion,
            "ingest_assignment",
            AsyncMock(return_value={"success": False, "failed_file": None, "error": "Classroom API down"}),
        )

        result = asyncio.run(ag.ingest_node(dict(STATE), _config()))

        assert result == {
            "ingested": False,
            "failure_text": 'Couldn\'t load the assignment details: Classroom API down. '
            'Send "work on HW1" again to retry.',
        }


class TestRouteAfterIngest:
    def test_routes_to_draft_when_ingested(self):
        assert ag.route_after_ingest({"ingested": True}) == "draft_node"

    def test_routes_to_relay_when_not_ingested(self):
        assert ag.route_after_ingest({"ingested": False}) == "relay_node"


class TestDraftNode:
    def test_success_stringifies_submission_file_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        submission_file = tmp_path / "submission" / "main.py"
        monkeypatch.setattr(
            ag.claude_code,
            "run_claude_code",
            AsyncMock(
                return_value={
                    "success": True,
                    "submission_files": [submission_file],
                    "manifest": {"format": "as-is", "files": ["main.py"]},
                    "summary_text": "Done.",
                    "session_id": "sess1",
                }
            ),
        )

        result = asyncio.run(ag.draft_node(dict(STATE), _config()))

        assert result == {
            "submission_files": [str(submission_file)],
            "manifest": {"format": "as-is", "files": ["main.py"]},
            "summary_text": "Done.",
            "session_id": "sess1",
        }

    def test_failure_returns_failure_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(
            ag.claude_code, "run_claude_code", AsyncMock(return_value={"success": False, "error": "timed out"})
        )

        result = asyncio.run(ag.draft_node(dict(STATE), _config()))

        assert result == {"failure_text": 'Drafting "HW1" failed: timed out'}

    def test_student_info_is_forwarded_to_claude_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        run_mock = AsyncMock(
            return_value={
                "success": True,
                "submission_files": [tmp_path / "submission" / "main.py"],
                "manifest": {"format": "as-is", "files": ["main.py"]},
                "summary_text": "Done.",
                "session_id": "sess1",
            }
        )
        monkeypatch.setattr(ag.claude_code, "run_claude_code", run_mock)

        asyncio.run(ag.draft_node(dict(STATE), _config(student_info="Roll number: 22-CS-045")))

        run_mock.assert_awaited_once_with(tmp_path, student_info="Roll number: 22-CS-045")

    def test_missing_student_info_defaults_to_empty_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        run_mock = AsyncMock(
            return_value={
                "success": True,
                "submission_files": [tmp_path / "submission" / "main.py"],
                "manifest": {"format": "as-is", "files": ["main.py"]},
                "summary_text": "Done.",
                "session_id": "sess1",
            }
        )
        monkeypatch.setattr(ag.claude_code, "run_claude_code", run_mock)

        asyncio.run(ag.draft_node(dict(STATE), _config()))

        run_mock.assert_awaited_once_with(tmp_path, student_info="")


class TestReviseNode:
    def test_resumes_session_with_feedback(self, tmp_path, monkeypatch):
        config = _config()
        conn = _conn_of(config)
        monkeypatch.setattr(ag.repo, "get_claude_session", lambda c, thread_id: "sess1")
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        run_mock = AsyncMock(
            return_value={
                "success": True,
                "submission_files": [tmp_path / "submission" / "main.py"],
                "manifest": {"format": "as-is", "files": ["main.py"]},
                "summary_text": "Revised.",
                "session_id": "sess1",
            }
        )
        monkeypatch.setattr(ag.claude_code, "run_claude_code", run_mock)
        state = dict(STATE, review_feedback="Add a conclusion.")

        result = asyncio.run(ag.revise_node(state, config))

        assert result["summary_text"] == "Revised."
        run_mock.assert_awaited_once_with(tmp_path, resume_session_id="sess1", feedback="Add a conclusion.")

    def test_failure_returns_failure_text(self, tmp_path, monkeypatch):
        config = _config()
        monkeypatch.setattr(ag.repo, "get_claude_session", lambda c, thread_id: "sess1")
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(
            ag.claude_code, "run_claude_code", AsyncMock(return_value={"success": False, "error": "crashed"})
        )
        state = dict(STATE, review_feedback="fix it")

        result = asyncio.run(ag.revise_node(state, config))

        assert result == {"failure_text": 'Revising "HW1" failed: crashed'}


class TestSaveSessionNode:
    def test_no_session_id_does_not_touch_pool(self):
        config = _config()

        result = ag.save_session_node({}, config)

        assert result == {}
        config["configurable"]["pool"].connection.assert_not_called()

    def test_session_id_present_is_saved(self):
        config = _config()
        conn = _conn_of(config)
        save_mock = MagicMock()
        with_patch = ag.repo.save_claude_session
        try:
            ag.repo.save_claude_session = save_mock
            result = ag.save_session_node({"session_id": "sess1"}, config)
        finally:
            ag.repo.save_claude_session = with_patch

        assert result == {}
        save_mock.assert_called_once_with(conn, "assignment:c1:cw1", "sess1")


class TestRelayNode:
    def test_failure_text_sends_failure_message_only(self, monkeypatch):
        send_mock = AsyncMock(return_value="wamid.OUT1")
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        upload_mock = MagicMock()
        monkeypatch.setattr(ag, "_upload_to_drive", upload_mock)
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)
        state = dict(STATE, failure_text="Something broke")

        result = asyncio.run(ag.relay_node(state, _config()))

        assert result == {}
        send_mock.assert_awaited_once_with("test-token", "phone123", "923115224115", "Something broke")
        upload_mock.assert_not_called()
        create_mock.assert_not_called()

    def test_failure_text_send_error_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        state = dict(STATE, failure_text="Something broke")

        result = asyncio.run(ag.relay_node(state, _config()))

        assert result == {}

    def test_success_uploads_to_drive_then_sends_summary_and_creates_pending_item(self, monkeypatch):
        send_mock = AsyncMock(return_value="wamid.OUT1")
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        upload_mock = MagicMock(return_value="https://drive.google.com/x")
        monkeypatch.setattr(ag, "_upload_to_drive", upload_mock)
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)
        config = _config()
        conn = _conn_of(config)
        state = dict(
            STATE,
            manifest={"format": "as-is", "files": ["main.py"]},
            submission_files=["/tmp/x/submission/main.py"],
            summary_text="Great job.",
            unsupported_files=[],
        )

        result = asyncio.run(ag.relay_node(state, config))

        assert result == {}
        upload_mock.assert_called_once_with("drive", Path("/tmp/x/submission/main.py"))
        send_mock.assert_awaited_once_with(
            "test-token",
            "phone123",
            "923115224115",
            "Great job.\n\nmain.py: https://drive.google.com/x\n\nReply approve, suggest changes, or say reject.",
        )
        create_mock.assert_called_once_with(conn, "wamid.OUT1", "assignment:c1:cw1", "assignment", "Algorithms — HW1")

    def test_unsupported_files_are_prefixed_to_summary(self, monkeypatch):
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(ag, "_upload_to_drive", lambda svc, f: "https://drive.google.com/x")
        monkeypatch.setattr(ag.repo, "create_pending_item", MagicMock())
        state = dict(
            STATE,
            manifest={"format": "as-is", "files": ["main.py"]},
            submission_files=["/tmp/x/submission/main.py"],
            summary_text="Great job.",
            unsupported_files=["video.mov"],
        )

        asyncio.run(ag.relay_node(state, _config()))

        body = ag.send_whatsapp_message.call_args[0][3]
        assert body.startswith("Skipped unsupported material(s): video.mov\n\nGreat job.")

    def test_empty_summary_defaults_to_draft_ready(self, monkeypatch):
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(ag, "_upload_to_drive", lambda svc, f: "https://drive.google.com/x")
        monkeypatch.setattr(ag.repo, "create_pending_item", MagicMock())
        state = dict(
            STATE,
            manifest={"format": "as-is", "files": ["main.py"]},
            submission_files=["/tmp/x/submission/main.py"],
            summary_text="",
            unsupported_files=[],
        )

        asyncio.run(ag.relay_node(state, _config()))

        body = ag.send_whatsapp_message.call_args[0][3]
        assert body.startswith("Draft ready.")

    def test_links_text_lists_each_manifest_file(self, monkeypatch):
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        links = iter(["https://drive.google.com/a", "https://drive.google.com/b"])
        monkeypatch.setattr(ag, "_upload_to_drive", lambda svc, f: next(links))
        monkeypatch.setattr(ag.repo, "create_pending_item", MagicMock())
        state = dict(
            STATE,
            manifest={"format": "zip", "files": ["src/main.py", "README.md"]},
            submission_files=["/tmp/x/submission/src/main.py", "/tmp/x/submission/README.md"],
            summary_text="Done.",
            unsupported_files=[],
        )

        asyncio.run(ag.relay_node(state, _config()))

        body = ag.send_whatsapp_message.call_args[0][3]
        assert "src/main.py: https://drive.google.com/a" in body
        assert "README.md: https://drive.google.com/b" in body

    def test_drive_auth_failure_stops_before_summary(self, monkeypatch):
        send_mock = AsyncMock(return_value="wamid.OUT1")
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: "auth message")
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)
        state = dict(
            STATE,
            manifest={"format": "as-is", "files": ["main.py"]},
            submission_files=["/tmp/x/submission/main.py"],
            summary_text="Great job.",
            unsupported_files=[],
        )

        result = asyncio.run(ag.relay_node(state, _config()))

        assert result == {}
        send_mock.assert_not_awaited()
        create_mock.assert_not_called()

    def test_drive_upload_error_stops_before_summary(self, monkeypatch):
        send_mock = AsyncMock(return_value="wamid.OUT1")
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))

        def raising(svc, f):
            raise RuntimeError("Drive quota exceeded")

        monkeypatch.setattr(ag, "_upload_to_drive", raising)
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)
        state = dict(
            STATE,
            manifest={"format": "as-is", "files": ["main.py"]},
            submission_files=["/tmp/x/submission/main.py"],
            summary_text="Great job.",
            unsupported_files=[],
        )

        result = asyncio.run(ag.relay_node(state, _config()))

        assert result == {}
        send_mock.assert_not_awaited()
        create_mock.assert_not_called()

    def test_summary_send_error_skips_pending_item_creation(self, monkeypatch):
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
        monkeypatch.setattr(ag, "_upload_to_drive", lambda svc, f: "https://drive.google.com/x")
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)
        state = dict(
            STATE,
            manifest={"format": "as-is", "files": ["main.py"]},
            submission_files=["/tmp/x/submission/main.py"],
            summary_text="Great job.",
            unsupported_files=[],
        )

        result = asyncio.run(ag.relay_node(state, _config()))

        assert result == {}
        create_mock.assert_not_called()


class TestRouteAfterRelay:
    def test_no_failure_goes_to_await_review(self):
        assert ag.route_after_relay({}) == "await_review_node"

    def test_failure_ends(self):
        assert ag.route_after_relay({"failure_text": "x"}) == END


class TestParseReviewNode:
    def test_closes_pending_item_and_returns_decision(self, monkeypatch):
        monkeypatch.setattr(
            ag.llm, "parse_review_reply", lambda client, model, text: ("revise", "Add more detail")
        )
        close_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "close_pending_item", close_mock)
        config = _config(_pending_item_message_id="wamid.X")
        conn = _conn_of(config)
        state = dict(STATE, review_reply_text="please add more detail")

        result = ag.parse_review_node(state, config)

        assert result == {"review_decision": "revise", "review_feedback": "Add more detail"}
        close_mock.assert_called_once_with(conn, "wamid.X")


class TestRouteAfterReview:
    @pytest.mark.parametrize(
        "decision,expected",
        [("approve", "ask_submit_node"), ("revise", "revise_node"), ("reject", END)],
    )
    def test_routes_by_decision(self, decision, expected):
        assert ag.route_after_review({"review_decision": decision}) == expected


class TestAskSubmitNode:
    def test_sends_question_and_creates_pending_item(self, monkeypatch):
        send_mock = AsyncMock(return_value="wamid.OUT2")
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)
        config = _config()
        conn = _conn_of(config)

        result = asyncio.run(ag.ask_submit_node(dict(STATE), config))

        assert result == {}
        send_mock.assert_awaited_once_with("test-token", "phone123", "923115224115", ag._submit_question_text("HW1"))
        create_mock.assert_called_once_with(conn, "wamid.OUT2", "assignment:c1:cw1", "assignment", "Algorithms — HW1")

    def test_send_error_skips_pending_item_creation(self, monkeypatch):
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        create_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "create_pending_item", create_mock)

        result = asyncio.run(ag.ask_submit_node(dict(STATE), _config()))

        assert result == {}
        create_mock.assert_not_called()


class TestParseSubmitNode:
    def test_decline_closes_pending_item(self, monkeypatch):
        monkeypatch.setattr(ag.llm, "parse_confirmation_reply", lambda client, model, question, reply: "decline")
        close_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "close_pending_item", close_mock)
        config = _config(_pending_item_message_id="wamid.X")
        conn = _conn_of(config)
        state = dict(STATE, submit_reply_text="no thanks")

        result = ag.parse_submit_node(state, config)

        assert result == {"submit_decision": "decline"}
        close_mock.assert_called_once_with(conn, "wamid.X")

    def test_confirm_leaves_pending_item_open(self, monkeypatch):
        monkeypatch.setattr(ag.llm, "parse_confirmation_reply", lambda client, model, question, reply: "confirm")
        close_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "close_pending_item", close_mock)
        state = dict(STATE, submit_reply_text="yes")

        result = ag.parse_submit_node(state, _config(_pending_item_message_id="wamid.X"))

        assert result == {"submit_decision": "confirm"}
        close_mock.assert_not_called()


class TestRouteAfterSubmit:
    def test_confirm_goes_to_submission_prep(self):
        assert ag.route_after_submit({"submit_decision": "confirm"}) == "submission_prep_node"

    def test_decline_ends(self):
        assert ag.route_after_submit({"submit_decision": "decline"}) == END


class TestPackageSubmission:
    def test_as_is_returns_flat_paths_unchanged(self, tmp_path):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()

        result = ag._package_submission(
            {"format": "as-is", "files": ["main.py", "utils.py"]}, submission_dir, tmp_path, "HW1"
        )

        assert result == [submission_dir / "main.py", submission_dir / "utils.py"]

    def test_zip_bundles_nested_files_preserving_structure(self, tmp_path):
        submission_dir = tmp_path / "submission"
        (submission_dir / "src").mkdir(parents=True)
        (submission_dir / "src" / "main.py").write_text("print(1)")
        (submission_dir / "README.md").write_text("# readme")

        result = ag._package_submission(
            {"format": "zip", "files": ["src/main.py", "README.md"]}, submission_dir, tmp_path, "HW 1"
        )

        assert result == [tmp_path / "hw-1.zip"]
        with zipfile.ZipFile(result[0]) as zf:
            assert set(zf.namelist()) == {"src/main.py", "README.md"}

    def test_pdf_format_converts_each_file_with_pdf_engine_flag(self, tmp_path, monkeypatch):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()
        (submission_dir / "essay.md").write_text("# Essay")
        convert_mock = MagicMock()
        monkeypatch.setattr(ag.pypandoc, "convert_file", convert_mock)

        result = ag._package_submission({"format": "pdf", "files": ["essay.md"]}, submission_dir, tmp_path, "HW1")

        assert result == [tmp_path / "essay.pdf"]
        convert_mock.assert_called_once_with(
            str(submission_dir / "essay.md"),
            "pdf",
            outputfile=str(tmp_path / "essay.pdf"),
            extra_args=["--pdf-engine=wkhtmltopdf"],
        )

    def test_docx_format_converts_without_pdf_engine_flag(self, tmp_path, monkeypatch):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()
        (submission_dir / "essay.md").write_text("# Essay")
        convert_mock = MagicMock()
        monkeypatch.setattr(ag.pypandoc, "convert_file", convert_mock)

        result = ag._package_submission({"format": "docx", "files": ["essay.md"]}, submission_dir, tmp_path, "HW1")

        assert result == [tmp_path / "essay.docx"]
        assert convert_mock.call_args[1]["extra_args"] == []

    def test_zip_output_name_overrides_title_derived_name(self, tmp_path):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()
        (submission_dir / "main.py").write_text("print(1)")

        result = ag._package_submission(
            {"format": "zip", "files": ["main.py"], "output_name": "22-CS-045"},
            submission_dir,
            tmp_path,
            "HW 1",
        )

        assert result == [tmp_path / "22-cs-045.zip"]

    def test_pdf_output_name_overrides_stem_for_single_file(self, tmp_path, monkeypatch):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()
        (submission_dir / "essay.md").write_text("# Essay")
        monkeypatch.setattr(ag.pypandoc, "convert_file", MagicMock())

        result = ag._package_submission(
            {"format": "pdf", "files": ["essay.md"], "output_name": "22-CS-045"},
            submission_dir,
            tmp_path,
            "HW1",
        )

        assert result == [tmp_path / "22-cs-045.pdf"]

    def test_pdf_output_name_ignored_when_multiple_files(self, tmp_path, monkeypatch):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()
        (submission_dir / "a.md").write_text("a")
        (submission_dir / "b.md").write_text("b")
        monkeypatch.setattr(ag.pypandoc, "convert_file", MagicMock())

        result = ag._package_submission(
            {"format": "pdf", "files": ["a.md", "b.md"], "output_name": "22-CS-045"},
            submission_dir,
            tmp_path,
            "HW1",
        )

        assert result == [tmp_path / "a.pdf", tmp_path / "b.pdf"]

    def test_as_is_ignores_output_name(self, tmp_path):
        submission_dir = tmp_path / "submission"
        submission_dir.mkdir()

        result = ag._package_submission(
            {"format": "as-is", "files": ["main.py"], "output_name": "22-CS-045"},
            submission_dir,
            tmp_path,
            "HW1",
        )

        assert result == [submission_dir / "main.py"]


class TestUploadToDrive:
    def test_creates_then_fetches_web_view_link(self, monkeypatch, tmp_path):
        captured = {}

        class FakeMediaFileUpload:
            def __init__(self, path):
                captured["path"] = path

        monkeypatch.setattr(ag, "MediaFileUpload", FakeMediaFileUpload)
        drive_service = MagicMock()
        drive_service.files.return_value.create.return_value.execute.return_value = {"id": "fid1"}
        drive_service.files.return_value.get.return_value.execute.return_value = {
            "webViewLink": "https://drive.google.com/fid1"
        }
        file_path = tmp_path / "hw1.pdf"

        result = ag._upload_to_drive(drive_service, file_path)

        assert result == "https://drive.google.com/fid1"
        assert captured["path"] == str(file_path)
        drive_service.files.return_value.create.assert_called_once_with(
            body={"name": "hw1.pdf"}, media_body=drive_service.files.return_value.create.call_args[1]["media_body"], fields="id"
        )
        drive_service.files.return_value.get.assert_called_once_with(fileId="fid1", fields="webViewLink")


class TestSubmissionPrepNode:
    def test_auth_failure_returns_failure_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: "auth message")
        state = dict(STATE, manifest={"format": "as-is", "files": ["main.py"]})

        result = asyncio.run(ag.submission_prep_node(state, _config()))

        assert result == {"failure_text": 'Couldn\'t prepare "HW1" for submission: auth message'}

    def test_package_failure_returns_failure_text_with_retry_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("g", "c", "drive"))

        def raising(manifest, submission_dir, dest_dir, title):
            raise ValueError("bad manifest")

        monkeypatch.setattr(ag, "_package_submission", raising)
        state = dict(STATE, manifest={"format": "as-is", "files": ["main.py"]})

        result = asyncio.run(ag.submission_prep_node(state, _config()))

        assert result == {
            "failure_text": 'Couldn\'t prepare "HW1" for submission: bad manifest. '
            'Reply "yes" again to retry — nothing was lost.'
        }

    def test_upload_failure_returns_failure_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("g", "c", "drive"))
        monkeypatch.setattr(ag, "_package_submission", lambda *a: [tmp_path / "out.pdf"])

        def raising(drive_service, file_path):
            raise RuntimeError("Drive quota exceeded")

        monkeypatch.setattr(ag, "_upload_to_drive", raising)
        state = dict(STATE, manifest={"format": "as-is", "files": ["main.py"]})

        result = asyncio.run(ag.submission_prep_node(state, _config()))

        assert result == {
            "failure_text": 'Couldn\'t prepare "HW1" for submission: Drive quota exceeded. '
            'Reply "yes" again to retry — nothing was lost.'
        }

    def test_success_closes_pending_item_and_returns_links(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("g", "c", "drive"))
        monkeypatch.setattr(ag, "_package_submission", lambda *a: [tmp_path / "out.pdf"])
        monkeypatch.setattr(ag, "_upload_to_drive", lambda svc, f: "https://drive.google.com/x")
        close_mock = MagicMock()
        monkeypatch.setattr(ag.repo, "close_pending_item", close_mock)
        config = _config(_pending_item_message_id="wamid.SUBMIT")
        conn = _conn_of(config)
        state = dict(STATE, manifest={"format": "as-is", "files": ["main.py"]})

        result = asyncio.run(ag.submission_prep_node(state, config))

        assert result == {
            "final_files": [str(tmp_path / "out.pdf")],
            "drive_links": ["https://drive.google.com/x"],
        }
        close_mock.assert_called_once_with(conn, "wamid.SUBMIT")


class TestRelaySubmitNode:
    def test_failure_text_sends_failure_message(self, monkeypatch):
        send_mock = AsyncMock()
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        state = dict(STATE, failure_text="Drive upload failed")

        result = asyncio.run(ag.relay_submit_node(state, _config()))

        assert result == {}
        send_mock.assert_awaited_once_with("test-token", "phone123", "923115224115", "Drive upload failed")

    def test_success_sends_formatted_links_and_turn_in_instructions(self, monkeypatch):
        send_mock = AsyncMock()
        monkeypatch.setattr(ag, "send_whatsapp_message", send_mock)
        state = dict(
            STATE,
            final_files=["/tmp/x/out.pdf"],
            drive_links=["https://drive.google.com/x"],
        )

        asyncio.run(ag.relay_submit_node(state, _config()))

        body = send_mock.call_args[0][3]
        assert "- out.pdf: https://drive.google.com/x" in body
        assert "Turn In" in body

    def test_send_error_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(side_effect=_http_status_error()))
        state = dict(STATE, final_files=["/tmp/x/out.pdf"], drive_links=["https://drive.google.com/x"])

        result = asyncio.run(ag.relay_submit_node(state, _config()))

        assert result == {}


# --- Integration tests: compiled graph through both interrupt() pauses ---

THREAD_ID = "assignment:c1:cw1"


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


def _stub_common_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: ("gmail", "classroom", "drive"))
    monkeypatch.setattr(ag.paths, "assignment_dir", lambda course, title: tmp_path)
    monkeypatch.setattr(
        ag.ingestion, "ingest_assignment", AsyncMock(return_value={"success": True, "unsupported_files": []})
    )
    monkeypatch.setattr(
        ag.claude_code,
        "run_claude_code",
        AsyncMock(
            return_value={
                "success": True,
                "submission_files": [tmp_path / "submission" / "main.py"],
                "manifest": {"format": "as-is", "files": ["main.py"]},
                "summary_text": "Draft complete.",
                "session_id": "sess1",
            }
        ),
    )
    monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))
    monkeypatch.setattr(ag.repo, "create_pending_item", MagicMock())
    monkeypatch.setattr(ag.repo, "close_pending_item", MagicMock())
    monkeypatch.setattr(ag.repo, "save_claude_session", MagicMock())
    monkeypatch.setattr(ag, "_package_submission", lambda *a: [tmp_path / "hw1.pdf"])
    monkeypatch.setattr(ag, "_upload_to_drive", lambda svc, f: "https://drive.google.com/hw1")


@pytest.fixture
def graph():
    return ag.build_assignment_graph(InMemorySaver())


class TestAssignmentGraphIntegration:
    def test_full_happy_path_approve_then_confirm_uploads_to_drive(self, tmp_path, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch, tmp_path)
        monkeypatch.setattr(ag.llm, "parse_review_reply", lambda client, model, text: ("approve", None))
        monkeypatch.setattr(ag.llm, "parse_confirmation_reply", lambda client, model, question, reply: "confirm")

        async def run():
            await graph.ainvoke(dict(STATE), config=_graph_config())
            snapshot = await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})
            assert snapshot.next == ("await_review_node",)

            await graph.ainvoke(
                Command(resume="approve"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            snapshot = await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})
            assert snapshot.next == ("await_submit_node",)

            await graph.ainvoke(
                Command(resume="yes"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert final_snapshot.values["drive_links"] == ["https://drive.google.com/hw1"]
        assert ag.send_whatsapp_message.await_count >= 3  # draft ready, submit question, final links

    def test_reject_review_ends_without_asking_to_submit(self, tmp_path, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch, tmp_path)
        monkeypatch.setattr(ag.llm, "parse_review_reply", lambda client, model, text: ("reject", None))

        async def run():
            await graph.ainvoke(dict(STATE), config=_graph_config())
            await graph.ainvoke(
                Command(resume="not good enough"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert final_snapshot.values.get("submit_decision") is None

    def test_decline_submit_ends_without_uploading(self, tmp_path, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch, tmp_path)
        monkeypatch.setattr(ag.llm, "parse_review_reply", lambda client, model, text: ("approve", None))
        monkeypatch.setattr(ag.llm, "parse_confirmation_reply", lambda client, model, question, reply: "decline")

        async def run():
            await graph.ainvoke(dict(STATE), config=_graph_config())
            await graph.ainvoke(
                Command(resume="approve"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            await graph.ainvoke(
                Command(resume="no"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert final_snapshot.values.get("drive_links") is None

    def test_revise_review_loops_back_through_draft_node(self, tmp_path, monkeypatch, graph):
        _stub_common_dependencies(monkeypatch, tmp_path)
        decisions = iter([("revise", "Add a conclusion."), ("approve", None)])
        monkeypatch.setattr(
            ag.llm, "parse_review_reply", lambda client, model, text: next(decisions)
        )
        monkeypatch.setattr(ag.llm, "parse_confirmation_reply", lambda client, model, question, reply: "confirm")

        async def run():
            await graph.ainvoke(dict(STATE), config=_graph_config())
            await graph.ainvoke(
                Command(resume="please add a conclusion"),
                config=_graph_config(_pending_item_message_id="wamid.OUT1"),
            )
            snapshot = await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})
            # revise -> draft_node -> save_session -> relay -> await_review again
            assert snapshot.next == ("await_review_node",)

            await graph.ainvoke(
                Command(resume="approve"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            await graph.ainvoke(
                Command(resume="yes"), config=_graph_config(_pending_item_message_id="wamid.OUT1")
            )
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        assert ag.claude_code.run_claude_code.await_count == 2  # initial draft + one revision

    def test_ingest_failure_ends_without_review_interrupt(self, tmp_path, monkeypatch, graph):
        monkeypatch.setattr(ag.google_auth, "load_google_clients", lambda conn: "Google access has expired")
        monkeypatch.setattr(ag, "send_whatsapp_message", AsyncMock(return_value="wamid.OUT1"))

        async def run():
            await graph.ainvoke(dict(STATE), config=_graph_config())
            return await graph.aget_state({"configurable": {"thread_id": THREAD_ID}})

        final_snapshot = asyncio.run(run())

        assert final_snapshot.next == ()
        ag.send_whatsapp_message.assert_awaited_once_with(
            "test-token", "phone123", "923115224115", "Google access has expired"
        )
