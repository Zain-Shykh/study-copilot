"""Phase 0 verification: completes the Google OAuth flow and prints the Gmail
profile address it grants access to. Disposable — not imported by agent/.
"""

import os
import pathlib
import sys

import dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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
TOKEN_CACHE_PATH = pathlib.Path(__file__).parent / ".google_token.json"


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


def load_or_run_flow(client_id: str, client_secret: str) -> Credentials:
    creds = None
    if TOKEN_CACHE_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_CACHE_PATH), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

    if not creds or not creds.valid:
        client_config = build_client_config(client_id, client_secret)
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_CACHE_PATH.write_text(creds.to_json())

    return creds


def main() -> None:
    dotenv.load_dotenv()

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id:
        print("Missing required env var: GOOGLE_OAUTH_CLIENT_ID", file=sys.stderr)
        sys.exit(1)
    if not client_secret:
        print("Missing required env var: GOOGLE_OAUTH_CLIENT_SECRET", file=sys.stderr)
        sys.exit(1)

    try:
        creds = load_or_run_flow(client_id, client_secret)
    except Exception as e:
        print(f"Google OAuth flow failed: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
    except HttpError as e:
        print(f"Gmail API call failed: {e.status_code} {e.reason}", file=sys.stderr)
        sys.exit(1)

    print(f"Gmail profile: {profile['emailAddress']}")


if __name__ == "__main__":
    main()
