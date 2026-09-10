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


def save_claude_session(conn: psycopg.Connection, assignment_thread_id: str, claude_session_id: str) -> None:
    """Inserts or updates the Claude Code session ID for an assignment
    thread, for a future Phase 3 `--resume` revision loop to read back."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO claude_sessions (assignment_thread_id, claude_session_id, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (assignment_thread_id) DO UPDATE
                SET claude_session_id = EXCLUDED.claude_session_id,
                    updated_at = now()
            """,
            (assignment_thread_id, claude_session_id),
        )
    conn.commit()


def get_claude_session(conn: psycopg.Connection, assignment_thread_id: str) -> str | None:
    """Returns the stored Claude Code session ID for an assignment thread,
    for a --resume revision call, or None if no draft has been produced yet."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claude_session_id FROM claude_sessions WHERE assignment_thread_id = %s",
            (assignment_thread_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def create_pending_item(
    conn: psycopg.Connection, message_id: str, thread_id: str, item_type: str, display_name: str
) -> None:
    """Records that whatsapp_message_id is awaiting a reply that should
    resume thread_id. item_type is 'assignment' or 'email' (schema CHECK)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pending_items (whatsapp_message_id, thread_id, item_type, display_name)
            VALUES (%s, %s, %s, %s)
            """,
            (message_id, thread_id, item_type, display_name),
        )
    conn.commit()


def get_pending_item(conn: psycopg.Connection, message_id: str) -> dict | None:
    """Looks up a pending item by the WhatsApp message it's attached to.
    Returns None if there's no row, or the row exists but is already
    resolved (a reply to a stale/superseded message)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT whatsapp_message_id, thread_id, item_type, display_name
            FROM pending_items WHERE whatsapp_message_id = %s AND status = 'pending'
            """,
            (message_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"message_id": row[0], "thread_id": row[1], "item_type": row[2], "display_name": row[3]}


def list_pending_items(conn: psycopg.Connection, item_type: str) -> list[dict]:
    """All currently-pending items of one type, for fuzzy by-name matching
    when a reply doesn't use WhatsApp's reply-to-message feature."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT whatsapp_message_id, thread_id, item_type, display_name
            FROM pending_items WHERE item_type = %s AND status = 'pending'
            """,
            (item_type,),
        )
        rows = cur.fetchall()
    return [
        {"message_id": r[0], "thread_id": r[1], "item_type": r[2], "display_name": r[3]}
        for r in rows
    ]


def close_pending_item(conn: psycopg.Connection, message_id: str) -> None:
    """Marks a pending item resolved, so a later reply to the same message
    (or a stale by-name match) never double-resumes the thread."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE pending_items SET status = 'resolved' WHERE whatsapp_message_id = %s",
            (message_id,),
        )
    conn.commit()
