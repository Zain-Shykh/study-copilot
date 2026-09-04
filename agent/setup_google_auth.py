"""One-time interactive Google OAuth bootstrap.

Run once, before starting the server for the first time:

    .venv/bin/python -m agent.setup_google_auth

Opens a browser for consent, then stores the resulting credentials in
Postgres (`oauth_credentials` table) for the running app to use. Unlike
`scripts/verify_google_oauth.py` (Phase 0's disposable check, which caches to
a local file), this is a permanent part of the app and writes to the same
database the server reads from at runtime.
"""

import sys

import psycopg
from google_auth_oauthlib.flow import InstalledAppFlow

from agent.config import load_settings
from agent.db import repo

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


def build_client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def main() -> None:
    settings = load_settings()

    client_config = build_client_config(
        settings.google_oauth_client_id, settings.google_oauth_client_secret
    )
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    try:
        with psycopg.connect(settings.database_url) as conn:
            repo.save_google_credentials(conn, creds)
    except psycopg.OperationalError as e:
        has_refresh_token = creds.refresh_token is not None
        print(f"Google consent succeeded, but storing it in Postgres failed: {e}", file=sys.stderr)
        print(
            f"Refresh token was {'obtained' if has_refresh_token else 'NOT obtained'} "
            "— fix the database connection and re-run this command "
            "rather than re-consenting if a refresh token was obtained.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Google credentials stored.")


if __name__ == "__main__":
    main()
