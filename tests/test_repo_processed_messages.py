"""Unit tests for repo.mark_message_processed_if_new — the idempotency
guard behind the webhook-duplicate-reply fix."""

from unittest.mock import MagicMock

from agent.db import repo


def test_returns_true_and_commits_when_row_is_new():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("wamid.1",)

    result = repo.mark_message_processed_if_new(conn, "wamid.1")

    assert result is True
    conn.commit.assert_called_once()
    args, _ = cursor.execute.call_args
    assert args[1] == ("wamid.1",)


def test_returns_false_when_already_recorded():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None

    result = repo.mark_message_processed_if_new(conn, "wamid.1")

    assert result is False
    conn.commit.assert_called_once()
