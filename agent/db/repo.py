"""Thin data-access functions (psycopg) for the app-specific Postgres tables."""

import json

import psycopg
from google.oauth2.credentials import Credentials


def get_google_credentials(conn: psycopg.Connection) -> Credentials | None:
    """Loads the stored Google OAuth credentials, if any.

    Returns None if no row exists (not yet bootstrapped via
    `agent.setup_google_auth`).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT credentials_json FROM oauth_credentials WHERE provider = 'google'"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Credentials.from_authorized_user_info(json.loads(row[0]))


def save_google_credentials(conn: psycopg.Connection, creds: Credentials) -> None:
    """Inserts or updates the stored Google OAuth credentials."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO oauth_credentials (provider, credentials_json, updated_at)
            VALUES ('google', %s, now())
            ON CONFLICT (provider) DO UPDATE
                SET credentials_json = EXCLUDED.credentials_json,
                    updated_at = now()
            """,
            (creds.to_json(),),
        )
    conn.commit()
