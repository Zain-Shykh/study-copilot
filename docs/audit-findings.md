# Documentation Audit — Findings (2026-09-24)

Cross-checked every file in `docs/` and `specs/` against the actual code in
`agent/`/`tests/` as of commit `4763829` (email drafting spec + implementation,
branch `worktree-spec-email-drafting`). Scope: documentation drift only — not
a code-quality review.

## Actively misleading (fix these)

### 1. `CLAUDE.md:14-15`
> "This repo currently contains only the scaffold: every file under `agent/`
> has a one-line docstring naming its purpose but no implementation yet."

Describes the repo's state before Phase 0 started. Reality: Phases 0-4 and
the email-drafting feature are all fully implemented — `agent/` has ~25
real modules, `tests/` has 17 files and 232 passing tests.

### 2. `CLAUDE.md:80` and `README.md:164`
> "No test runner, linter, or formatter is configured yet."

`pyproject.toml` has `[tool.pytest.ini_options]` / `testpaths = ["tests"]`;
the suite runs via `.venv/bin/python -m pytest -q` (232 passed). Both docs
are stale on this specific line.

### 3. `docs/ARCHITECTURE.md:183`
> `| Email-draft | \`email:<uuid>\` | **Not implemented yet** — scaffold only |`

`agent/graph/email_graph.py` is now a full implementation (draft → relay →
interrupt → parse review → revise/send, 214 lines, matches
`specs/email-drafting.md` §7 exactly). The doc's own diagrams (§1 system
overview, §2 router graph, §3 assignment graph) have no corresponding "§4
Email graph" section either — the email flow that exists in code today has
zero diagram coverage in this file.

## Stale but low-impact

### 4. `README.md:87-89`
> "This creates the app's five tables (`pending_items`, `email_checkpoint`,
> `notified_milestones`, `claude_sessions`, `oauth_credentials`)."

Undercounts — `agent/db/schema.sql` has **six** tables; missing
`processed_messages` (webhook delivery dedup, added after the five-table
count was written). `docs/database_schema.md` already documents all six
correctly ("All six tables..." in its Notes section) — this drift is
README-only. An earlier commit (`34511f4`, "Fix stale docs: table counts")
fixed this elsewhere but missed README.md.

### 5. `docs/implementation_plan.md`
Never mentions that email drafting — deferred in the Phase 3 section
(lines 218-224, correctly, with a pointer to
`specs/phase-3-approval-submission.md` Decision #1) — was later built via
a standalone spec (`specs/email-drafting.md`) outside the 0-4 phase
sequence. Reading implementation_plan.md alone gives no indication email
drafting now exists.

## Not real drift (noted for completeness only)

### 6. `specs/tool-calling-read-answers.md:472`
> "The email-draft graph (`email_graph.py`) — still an empty scaffold,
> untouched."

This is a dated, point-in-time spec's "Out of scope" note — accurate when
written, before email-drafting was scoped. Specs in this repo are historical
snapshots by convention, not living docs, so this isn't drift in the sense
items 1-5 are. Listed only for completeness.

## Clean — no drift found

- **`docs/database_schema.md`** — matches `agent/db/schema.sql` exactly:
  all six tables, all columns, the `item_type` CHECK constraint (already
  allowed `'email'` before email drafting used it).
- **`docs/product_definition.md`** — scope, permission matrix, and all 10
  locked decisions still hold against current code; its module-layout
  listing already named `email_graph.py` as a per-email-draft graph.
- **`specs/email-drafting.md`** — spot-checked against the actual
  implementation line-by-line: `agent/db/repo.py::list_pending_items`
  (§2), `agent/graph/nodes/gmail.py`'s `get_message_body`/`send_message`
  (§3), `agent/llm.py::draft_email` (§4), `agent/graph/router_graph.py`'s
  `resolve_email_node`/`_dispatch_pending_resume` (§6), and the full
  `agent/graph/email_graph.py` node/edge structure (§7). Implementation
  matches the approved spec exactly — no deviation.
- **`docs/ARCHITECTURE.md`** §1-§3 (system diagram, router graph, assignment
  graph) — accurate against current code; only gap is the missing email-graph
  section (see #3 above).
- No feature was found implemented in `agent/` without a corresponding spec
  in `specs/` — email drafting is the only feature added since the last
  audit, and it's fully spec'd.

## Recommended fixes (not applied — this is a read-only audit)

1. Rewrite `CLAUDE.md`'s "What this is" scaffold paragraph to reflect the
   current build state, or drop it (it was Phase-0-era orientation text).
2. Drop or replace the "No test runner..." line in `CLAUDE.md` and
   `README.md` with the actual `pytest` invocation.
3. Update `docs/ARCHITECTURE.md`'s Thread-ID summary row for Email-draft,
   and add a §4 diagram for the email graph matching §2/§3's style.
4. Fix README.md's table count to six, add `processed_messages`.
5. Add a one-line pointer in `docs/implementation_plan.md`'s Phase 3
   section noting email drafting was completed via `specs/email-drafting.md`.
