"""Unit tests for agent/google_auth.py: credential load/refresh and the
client-builder helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.auth.exceptions import RefreshError

from agent import google_auth


def _fake_creds(expired=False, refresh_token=None):
    return SimpleNamespace(expired=expired, refresh_token=refresh_token, refresh=MagicMock())


class TestGetCredentials:
    def test_raises_when_no_credentials_stored(self, monkeypatch):
        monkeypatch.setattr(google_auth.repo, "get_google_credentials", lambda conn: None)

        with pytest.raises(RuntimeError, match="setup_google_auth"):
            google_auth.get_credentials(MagicMock())

    def test_refreshes_and_persists_when_expired_with_refresh_token(self, monkeypatch):
        creds = _fake_creds(expired=True, refresh_token="rt")
        monkeypatch.setattr(google_auth.repo, "get_google_credentials", lambda conn: creds)
        save_mock = MagicMock()
        monkeypatch.setattr(google_auth.repo, "save_google_credentials", save_mock)
        conn = MagicMock()

        result = google_auth.get_credentials(conn)

        assert result is creds
        creds.refresh.assert_called_once()
        save_mock.assert_called_once_with(conn, creds)

    def test_does_not_refresh_when_not_expired(self, monkeypatch):
        creds = _fake_creds(expired=False, refresh_token="rt")
        monkeypatch.setattr(google_auth.repo, "get_google_credentials", lambda conn: creds)
        save_mock = MagicMock()
        monkeypatch.setattr(google_auth.repo, "save_google_credentials", save_mock)

        result = google_auth.get_credentials(MagicMock())

        assert result is creds
        creds.refresh.assert_not_called()
        save_mock.assert_not_called()

    def test_does_not_refresh_when_expired_but_no_refresh_token(self, monkeypatch):
        creds = _fake_creds(expired=True, refresh_token=None)
        monkeypatch.setattr(google_auth.repo, "get_google_credentials", lambda conn: creds)
        save_mock = MagicMock()
        monkeypatch.setattr(google_auth.repo, "save_google_credentials", save_mock)

        google_auth.get_credentials(MagicMock())

        creds.refresh.assert_not_called()
        save_mock.assert_not_called()


class TestClientBuilders:
    def test_build_gmail_client(self, monkeypatch):
        build_mock = MagicMock(return_value="gmail-service")
        monkeypatch.setattr(google_auth, "build", build_mock)
        creds = _fake_creds()

        result = google_auth.build_gmail_client(creds)

        assert result == "gmail-service"
        build_mock.assert_called_once_with("gmail", "v1", credentials=creds)

    def test_build_classroom_client(self, monkeypatch):
        build_mock = MagicMock(return_value="classroom-service")
        monkeypatch.setattr(google_auth, "build", build_mock)
        creds = _fake_creds()

        result = google_auth.build_classroom_client(creds)

        assert result == "classroom-service"
        build_mock.assert_called_once_with("classroom", "v1", credentials=creds)

    def test_build_drive_client(self, monkeypatch):
        build_mock = MagicMock(return_value="drive-service")
        monkeypatch.setattr(google_auth, "build", build_mock)
        creds = _fake_creds()

        result = google_auth.build_drive_client(creds)

        assert result == "drive-service"
        build_mock.assert_called_once_with("drive", "v3", credentials=creds)


class TestGetGoogleClients:
    def test_refreshes_credentials_once_and_builds_all_three_clients(self, monkeypatch):
        creds = _fake_creds()
        monkeypatch.setattr(google_auth, "get_credentials", MagicMock(return_value=creds))
        monkeypatch.setattr(google_auth, "build_gmail_client", lambda c: ("gmail", c))
        monkeypatch.setattr(google_auth, "build_classroom_client", lambda c: ("classroom", c))
        monkeypatch.setattr(google_auth, "build_drive_client", lambda c: ("drive", c))

        result = google_auth.get_google_clients(MagicMock())

        assert result == (("gmail", creds), ("classroom", creds), ("drive", creds))
        google_auth.get_credentials.assert_called_once()


class TestLoadGoogleClients:
    def test_returns_clients_tuple_on_success(self, monkeypatch):
        sentinel = ("gmail", "classroom", "drive")
        monkeypatch.setattr(google_auth, "get_google_clients", lambda conn: sentinel)

        result = google_auth.load_google_clients(MagicMock())

        assert result == sentinel

    def test_returns_message_string_on_runtime_error(self, monkeypatch):
        def raising(conn):
            raise RuntimeError("no creds")

        monkeypatch.setattr(google_auth, "get_google_clients", raising)

        result = google_auth.load_google_clients(MagicMock())

        assert result == google_auth.NOT_AUTHENTICATED_MESSAGE

    def test_returns_message_string_on_refresh_error(self, monkeypatch):
        def raising(conn):
            raise RefreshError("revoked")

        monkeypatch.setattr(google_auth, "get_google_clients", raising)

        result = google_auth.load_google_clients(MagicMock())

        assert result == google_auth.NOT_AUTHENTICATED_MESSAGE
