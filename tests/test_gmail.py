"""Unit tests for agent/graph/nodes/gmail.py's message-metadata capture and
the new get_message_body/send_message functions added for email drafting."""

import base64
from email import message_from_bytes
from unittest.mock import MagicMock

from agent.graph.nodes import gmail


def _service_returning(payload: dict) -> MagicMock:
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = payload
    return service


class TestGetMessageMetadata:
    def test_list_messages_includes_thread_id_and_message_id_header(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "m1"}]
        }
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "m1",
            "threadId": "t1",
            "snippet": "hi",
            "payload": {
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Subject", "value": "Hello"},
                    {"name": "Date", "value": "Mon"},
                    {"name": "Message-ID", "value": "<abc@mail.gmail.com>"},
                ]
            },
        }

        result = gmail.list_messages(service, "q", 5)

        assert result == [
            {
                "id": "m1",
                "thread_id": "t1",
                "from": "a@b.com",
                "subject": "Hello",
                "date": "Mon",
                "snippet": "hi",
                "message_id_header": "<abc@mail.gmail.com>",
            }
        ]


class TestGetMessageBody:
    def test_returns_text_plain_body(self):
        body_b64 = base64.urlsafe_b64encode(b"Hello there").decode()
        service = _service_returning(
            {"payload": {"mimeType": "text/plain", "body": {"data": body_b64}}}
        )

        result = gmail.get_message_body(service, "m1")

        assert result == "Hello there"

    def test_finds_text_plain_nested_in_multipart(self):
        body_b64 = base64.urlsafe_b64encode(b"Nested body").decode()
        service = _service_returning(
            {
                "payload": {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode()}},
                        {"mimeType": "text/plain", "body": {"data": body_b64}},
                    ],
                }
            }
        )

        result = gmail.get_message_body(service, "m1")

        assert result == "Nested body"

    def test_returns_empty_string_when_no_text_plain_part(self):
        service = _service_returning(
            {"payload": {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>hi</p>").decode()}}}
        )

        result = gmail.get_message_body(service, "m1")

        assert result == ""


class TestSendMessage:
    def _sent_raw(self, service: MagicMock) -> bytes:
        _, kwargs = service.users.return_value.messages.return_value.send.call_args
        return base64.urlsafe_b64decode(kwargs["body"]["raw"])

    def test_sends_new_message_with_to_and_subject(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent1"}

        result = gmail.send_message(service, "to@x.com", "Subject line", "Body text")

        assert result == "sent1"
        mime = message_from_bytes(self._sent_raw(service))
        assert mime["To"] == "to@x.com"
        assert mime["Subject"] == "Subject line"
        assert mime.get_payload() == "Body text"
        _, kwargs = service.users.return_value.messages.return_value.send.call_args
        assert "threadId" not in kwargs["body"]

    def test_sets_reply_headers_and_thread_id_when_given(self):
        service = MagicMock()
        service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent1"}

        gmail.send_message(
            service, "to@x.com", "Re: Subject", "Body",
            in_reply_to_header="<orig@mail.gmail.com>", thread_id="t1",
        )

        mime = message_from_bytes(self._sent_raw(service))
        assert mime["In-Reply-To"] == "<orig@mail.gmail.com>"
        assert mime["References"] == "<orig@mail.gmail.com>"
        _, kwargs = service.users.return_value.messages.return_value.send.call_args
        assert kwargs["body"]["threadId"] == "t1"


class TestBuildDateFilter:
    def test_no_dates_returns_empty_string(self):
        assert gmail.build_date_filter(None, None) == ""

    def test_after_date_and_before_date_become_epoch_seconds_in_user_timezone(self):
        # 2026-09-24 00:00:00 and 2026-09-25 00:00:00 in Asia/Karachi (UTC+5)
        query = gmail.build_date_filter("2026-09-24", "2026-09-25")
        assert query == "after:1790190000 before:1790276400"

    def test_after_date_only(self):
        assert gmail.build_date_filter("2026-09-25", None) == "after:1790276400"

    def test_before_date_only(self):
        assert gmail.build_date_filter(None, "2026-09-25") == "before:1790276400"
