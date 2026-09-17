"""Unit tests for agent/scheduler/jobs.py: proactive Gmail/Classroom polling,
milestone dedup, and the in-process outage-notification dedup flags."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent.scheduler import jobs


def _pool_with_conn() -> tuple[MagicMock, MagicMock]:
    pool = MagicMock()
    conn = pool.connection.return_value.__enter__.return_value
    return pool, conn


def _reset_outage_flags(monkeypatch):
    monkeypatch.setattr(jobs, "_gmail_outage", False)
    monkeypatch.setattr(jobs, "_classroom_outage", False)


GMAIL_KWARGS = dict(
    genai_client=MagicMock(),
    gemini_model="gemini-x",
    whatsapp_access_token="token",
    whatsapp_phone_number_id="phone123",
    my_whatsapp_number="923115224115",
)


class TestPollGmailJob:
    def test_no_prior_checkpoint_establishes_baseline_without_sending(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        gmail_service = MagicMock()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (gmail_service, None, None))
        monkeypatch.setattr(jobs.repo, "get_email_checkpoint", lambda c: None)
        monkeypatch.setattr(jobs, "get_current_history_id", lambda svc: "1000")
        save_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "save_email_checkpoint", save_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        save_mock.assert_called_once_with(conn, "1000")
        send_mock.assert_not_awaited()

    def test_expired_checkpoint_rebaselines_without_sending(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        gmail_service = MagicMock()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (gmail_service, None, None))
        monkeypatch.setattr(jobs.repo, "get_email_checkpoint", lambda c: "stale-id")
        monkeypatch.setattr(jobs, "get_new_message_ids", lambda svc, checkpoint: None)
        monkeypatch.setattr(jobs, "get_current_history_id", lambda svc: "2000")
        save_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "save_email_checkpoint", save_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        save_mock.assert_called_once_with(conn, "2000")
        send_mock.assert_not_awaited()

    def test_new_messages_sends_digest_and_advances_checkpoint(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        gmail_service = MagicMock()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (gmail_service, None, None))
        monkeypatch.setattr(jobs.repo, "get_email_checkpoint", lambda c: "old-id")
        monkeypatch.setattr(jobs, "get_new_message_ids", lambda svc, checkpoint: ["m1", "m2"])
        monkeypatch.setattr(jobs, "get_current_history_id", lambda svc: "3000")
        monkeypatch.setattr(jobs, "get_messages_by_id", lambda svc, ids: [{"subject": "Hi"}, {"subject": "Yo"}])
        monkeypatch.setattr(jobs, "summarize_emails", lambda client, model, messages: "digest text")
        save_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "save_email_checkpoint", save_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        send_mock.assert_awaited_once()
        body = send_mock.call_args[0][3]
        assert "2 new email(s)" in body
        assert "digest text" in body
        save_mock.assert_called_once_with(conn, "3000")

    def test_no_new_messages_still_advances_checkpoint_without_sending(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        gmail_service = MagicMock()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (gmail_service, None, None))
        monkeypatch.setattr(jobs.repo, "get_email_checkpoint", lambda c: "old-id")
        monkeypatch.setattr(jobs, "get_new_message_ids", lambda svc, checkpoint: [])
        monkeypatch.setattr(jobs, "get_current_history_id", lambda svc: "3001")
        save_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "save_email_checkpoint", save_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        send_mock.assert_not_awaited()
        save_mock.assert_called_once_with(conn, "3001")

    def test_auth_failure_sends_outage_message_once(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: "Google access has expired")
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        send_mock.assert_awaited_once()
        assert "Couldn't check Gmail" in send_mock.call_args[0][3]
        assert jobs._gmail_outage is True

    def test_repeated_failure_does_not_resend_outage_message(self, monkeypatch):
        monkeypatch.setattr(jobs, "_gmail_outage", True)
        monkeypatch.setattr(jobs, "_classroom_outage", False)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: "Google access has expired")
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        send_mock.assert_not_awaited()
        assert jobs._gmail_outage is True

    def test_recovery_after_outage_sends_back_up_message(self, monkeypatch):
        monkeypatch.setattr(jobs, "_gmail_outage", True)
        monkeypatch.setattr(jobs, "_classroom_outage", False)
        pool, conn = _pool_with_conn()
        gmail_service = MagicMock()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (gmail_service, None, None))
        monkeypatch.setattr(jobs.repo, "get_email_checkpoint", lambda c: "old-id")
        monkeypatch.setattr(jobs, "get_new_message_ids", lambda svc, checkpoint: [])
        monkeypatch.setattr(jobs, "get_current_history_id", lambda svc: "4000")
        monkeypatch.setattr(jobs.repo, "save_email_checkpoint", MagicMock())
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_gmail_job(pool, **GMAIL_KWARGS))

        send_mock.assert_awaited_once()
        assert "working again" in send_mock.call_args[0][3]
        assert jobs._gmail_outage is False


class TestFormatMilestones:
    def test_assignment_milestone_with_due_date(self):
        from datetime import datetime, timezone

        due = datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)
        item = {"course": {"name": "Algorithms"}, "courseWork": {"title": "HW1"}, "due": due}

        result = jobs._format_milestones([("due_soon", item)])

        assert result == "- [Due soon] Algorithms: HW1 — due 2026-09-20 23:59 UTC"

    def test_assignment_milestone_with_no_due_date(self):
        item = {"course": {"name": "Algorithms"}, "courseWork": {"title": "HW2"}, "due": None}

        result = jobs._format_milestones([("posted", item)])

        assert result == "- [New assignment] Algorithms: HW2 — due no due date"

    def test_announcement_milestone_truncates_to_120_chars(self):
        long_text = "x" * 200
        item = {"course": {"name": "SCD"}, "announcement": {"text": long_text}}

        result = jobs._format_milestones([("announcement_posted", item)])

        assert result == f"- [New announcement] SCD: {'x' * 120}"


CLASSROOM_KWARGS = dict(
    whatsapp_access_token="token",
    whatsapp_phone_number_id="phone123",
    my_whatsapp_number="923115224115",
)


def _stub_classroom_polling(monkeypatch, *, assignments_by_scope=None, announcements=None):
    assignments_by_scope = assignments_by_scope or {}
    monkeypatch.setattr(jobs, "list_courses", lambda svc: [{"id": "c1", "name": "Algorithms"}])
    monkeypatch.setattr(
        jobs,
        "list_assignments",
        lambda svc, courses, scope, window: assignments_by_scope.get(scope, ([], [])),
    )
    monkeypatch.setattr(jobs, "list_announcements", lambda svc, courses: announcements or [])


class TestPollClassroomJob:
    def test_no_new_milestones_sends_nothing(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (None, MagicMock(), None))
        _stub_classroom_polling(monkeypatch)
        monkeypatch.setattr(jobs.repo, "is_milestone_notified", lambda *a: False)
        record_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "record_milestone_notified", record_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        send_mock.assert_not_awaited()
        record_mock.assert_not_called()

    def test_new_posted_assignment_is_recorded_and_sent(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (None, MagicMock(), None))
        item = {
            "course": {"id": "c1", "name": "Algorithms"},
            "courseWork": {"id": "cw1", "title": "HW1"},
            "due": None,
        }
        _stub_classroom_polling(monkeypatch, assignments_by_scope={"all": ([item], [])})
        monkeypatch.setattr(jobs.repo, "is_milestone_notified", lambda *a: False)
        record_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "record_milestone_notified", record_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        record_mock.assert_called_once_with(conn, "c1", "assignment:cw1", "posted")
        send_mock.assert_awaited_once()
        assert "New assignment" in send_mock.call_args[0][3]

    def test_already_notified_milestone_is_skipped(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (None, MagicMock(), None))
        item = {
            "course": {"id": "c1", "name": "Algorithms"},
            "courseWork": {"id": "cw1", "title": "HW1"},
            "due": None,
        }
        _stub_classroom_polling(monkeypatch, assignments_by_scope={"all": ([item], [])})
        monkeypatch.setattr(jobs.repo, "is_milestone_notified", lambda *a: True)
        record_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "record_milestone_notified", record_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        record_mock.assert_not_called()
        send_mock.assert_not_awaited()

    def test_announcement_uses_prefixed_key_and_dedicated_milestone_type(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (None, MagicMock(), None))
        entry = {"course": {"id": "c1", "name": "Algorithms"}, "announcement": {"id": "a1", "text": "Midterm moved"}}
        _stub_classroom_polling(monkeypatch, announcements=[entry])
        monkeypatch.setattr(jobs.repo, "is_milestone_notified", lambda *a: False)
        record_mock = MagicMock()
        monkeypatch.setattr(jobs.repo, "record_milestone_notified", record_mock)
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        record_mock.assert_called_once_with(conn, "c1", "announcement:a1", "announcement_posted")
        send_mock.assert_awaited_once()

    def test_auth_failure_sends_outage_message_once(self, monkeypatch):
        _reset_outage_flags(monkeypatch)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: "Google access has expired")
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        send_mock.assert_awaited_once()
        assert "Couldn't check Classroom" in send_mock.call_args[0][3]
        assert jobs._classroom_outage is True

    def test_repeated_failure_does_not_resend_outage_message(self, monkeypatch):
        monkeypatch.setattr(jobs, "_gmail_outage", False)
        monkeypatch.setattr(jobs, "_classroom_outage", True)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: "Google access has expired")
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        send_mock.assert_not_awaited()
        assert jobs._classroom_outage is True

    def test_recovery_after_outage_sends_back_up_message(self, monkeypatch):
        monkeypatch.setattr(jobs, "_gmail_outage", False)
        monkeypatch.setattr(jobs, "_classroom_outage", True)
        pool, conn = _pool_with_conn()
        monkeypatch.setattr(jobs.google_auth, "load_google_clients", lambda c: (None, MagicMock(), None))
        _stub_classroom_polling(monkeypatch)
        monkeypatch.setattr(jobs.repo, "is_milestone_notified", lambda *a: False)
        monkeypatch.setattr(jobs.repo, "record_milestone_notified", MagicMock())
        send_mock = AsyncMock()
        monkeypatch.setattr(jobs, "send_whatsapp_message", send_mock)

        asyncio.run(jobs.poll_classroom_job(pool, **CLASSROOM_KWARGS))

        send_mock.assert_awaited_once()
        assert "working again" in send_mock.call_args[0][3]
        assert jobs._classroom_outage is False
