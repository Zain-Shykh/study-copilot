# Database Schema

Single local PostgreSQL instance, holding two categories of tables:

1. **LangGraph's own checkpoint tables** — created and managed automatically by
   `langgraph-checkpoint-postgres` (via its setup routine, not by us). They store
   every thread's state at each superstep, which is what lets a paused
   `interrupt()` survive a process restart. Not detailed here since we don't
   design or query them directly — see the `langgraph-checkpoint-postgres`
   package for its own schema.
2. **App-specific tables** — defined in `agent/db/schema.sql`, described below.
   No ORM; accessed via plain `psycopg` in `agent/db/repo.py`.

See `docs/product_definition.md` ("Architecture" section) for how these tables
fit into the wider system.

---

## `pending_items`

Maps a WhatsApp message the agent sent (a draft or notification) to the
LangGraph thread it belongs to, so an inbound reply can be routed back to the
right paused thread.

| Column                 | Type          | Notes                                              |
|-------------------------|---------------|-----------------------------------------------------|
| `whatsapp_message_id`    | `TEXT`         | **Primary key.** The ID of the message the agent sent. |
| `thread_id`               | `TEXT`         | The LangGraph thread to resume on reply.             |
| `item_type`               | `TEXT`         | `'assignment'` or `'email'`.                          |
| `display_name`            | `TEXT`         | Human-readable label (e.g. course/assignment name), used for fuzzy by-name matching when the user doesn't reply-to-message. |
| `status`                   | `TEXT`         | Defaults to `'pending'`.                              |
| `created_at`               | `TIMESTAMPTZ`  | Defaults to `now()`.                                  |

**Written**: every time the agent sends a draft or approval-seeking message.
**Read**: on every inbound WhatsApp reply — first by exact `whatsapp_message_id`
lookup (reply-to-message), falling back to fuzzy-matching `display_name` against
the message text.

---

## `email_checkpoint`

Single-row table tracking the watermark for the proactive Gmail digest —
deliberately independent of Gmail's own read/unread flag (see "Email digest
checkpoint" in the spec for why).

| Column             | Type          | Notes                                        |
|----------------------|---------------|-------------------------------------------------|
| `id`                   | `TEXT`         | **Primary key.** Always `'singleton'` — enforces one row. |
| `last_history_id`       | `TEXT`         | Gmail's `historyId` as of the last successful poll. |
| `updated_at`             | `TIMESTAMPTZ`  | Defaults to `now()`.                             |

**Written**: after each successful `poll_gmail_job` run, advanced to the newest
`historyId` seen.
**Read**: at the start of each `poll_gmail_job` run, to fetch only messages
newer than the watermark.

---

## `notified_milestones`

Tracks which proactive Classroom notifications have already fired, so a
milestone is never re-sent (see "Proactive notification dedup" in the spec).

| Column             | Type          | Notes                                                      |
|----------------------|---------------|----------------------------------------------------------------|
| `course_id`            | `TEXT`         | Classroom course ID.                                            |
| `coursework_id`         | `TEXT`         | Classroom coursework (assignment/announcement) ID.               |
| `milestone_type`        | `TEXT`         | e.g. `'posted'`, `'due_soon'`, `'overdue'`.                       |
| `notified_at`            | `TIMESTAMPTZ`  | Defaults to `now()`.                                             |
| —                        |               | **Primary key**: (`coursework_id`, `milestone_type`) — one row per milestone per item, ever. |

**Written**: the moment a milestone notification is sent.
**Read**: at the start of each `poll_classroom_job` run, to decide which
milestones (if any) are still due for a given assignment/announcement.

---

## `claude_sessions`

Maps an assignment's LangGraph thread to its Claude Code session ID, so
revisions resume the same headless session (`claude --resume`) instead of
starting over.

| Column                    | Type          | Notes                                        |
|-----------------------------|---------------|-------------------------------------------------|
| `assignment_thread_id`        | `TEXT`         | **Primary key.** Matches the assignment's LangGraph `thread_id`. |
| `claude_session_id`            | `TEXT`         | The Claude Code CLI session ID to `--resume`.     |
| `updated_at`                    | `TIMESTAMPTZ`  | Defaults to `now()`.                             |

**Written**: once, when the first draft for an assignment starts a new Claude
Code session.
**Read**: on every revision round, to resume that same session rather than
starting fresh.

---

## Notes

- All four tables use `TEXT` for IDs rather than typed foreign keys — there's no
  formal relationship to LangGraph's own checkpoint tables (different package,
  different schema), so IDs are just matched by convention (e.g.
  `claude_sessions.assignment_thread_id` is the same string as the LangGraph
  `thread_id` used for that assignment).
- No migrations tooling yet — `agent/db/schema.sql` is applied directly
  (`CREATE TABLE IF NOT EXISTS`), matched to the current scale (single user,
  four small tables).
