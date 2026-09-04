"""Google OAuth credential load/refresh/persist, and API client builders."""

import psycopg
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build

from agent.db import repo

NOT_AUTHENTICATED_MESSAGE = (
    "Google access has expired — please re-run "
    "`.venv/bin/python -m agent.setup_google_auth` to re-authenticate."
)


def get_credentials(conn: psycopg.Connection) -> Credentials:
    """Loads the stored Google credentials, refreshing (and persisting the
    refresh) if the access token has expired.

    Raises RuntimeError if no credentials have been bootstrapped yet.
    Raises google.auth.exceptions.RefreshError if the refresh token itself
    has been revoked/expired — not retried here, left for the caller to
    treat as an auth error.
    """
    creds = repo.get_google_credentials(conn)
    if creds is None:
        raise RuntimeError(
            "No Google credentials found — run "
            "`.venv/bin/python -m agent.setup_google_auth` first."
        )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        repo.save_google_credentials(conn, creds)

    return creds


def build_gmail_client(creds: Credentials) -> Resource:
    return build("gmail", "v1", credentials=creds)


def build_classroom_client(creds: Credentials) -> Resource:
    return build("classroom", "v1", credentials=creds)


def get_google_clients(conn: psycopg.Connection) -> tuple[Resource, Resource]:
    """Convenience helper for graph nodes: refreshes credentials once, then
    builds both the Gmail and Classroom clients from them."""
    creds = get_credentials(conn)
    return build_gmail_client(creds), build_classroom_client(creds)


def load_google_clients(conn: psycopg.Connection) -> tuple[Resource, Resource] | str:
    """Builds (gmail_service, classroom_service) for a graph node to use.

    On failure (never bootstrapped, or the refresh token was revoked/
    expired) returns the user-facing reply text instead of raising — both
    cases converge on the same message, since the fix is identical
    (re-run the bootstrap CLI).
    """
    try:
        return get_google_clients(conn)
    except (RuntimeError, RefreshError):
        return NOT_AUTHENTICATED_MESSAGE
