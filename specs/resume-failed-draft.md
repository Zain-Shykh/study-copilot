# Resuming a draft after a failed/interrupted attempt

## 0. Problem

Confirmed by tracing the actual code (agent/graph/assignment_graph.py,
agent/graph/router_graph.py): today, any drafting failure that isn't
recovered by the existing "check disk before giving up on a timeout" fix
means starting completely over, for two independent reasons:

1. **`session_id` is never captured on failure.** `draft_node` only
   includes `"session_id"` in its return dict when
   `result["success"]` is true; `run_claude_code` itself only extracts
   `session_id` from stdout in its success branch. `save_session_node`
   only writes to `claude_sessions` when `state.get("session_id")` is
   truthy — so a failed attempt leaves nothing in the DB to resume from,
   even though the underlying Claude Code session (and whatever partial
   work it did) still exists and is resumable via `--resume` in principle.
2. **A retry never checks for one anyway.** `run_assignment_flow`
   (agent/graph/router_graph.py) always invokes the assignment graph with
   fresh initial state; `draft_node` always calls `run_claude_code` with
   `resume_session_id=None`. There's no lookup against `claude_sessions`
   before starting a "fresh" draft.

Net effect: every retry pays the full time/token cost again from zero,
even for a failure that happened after most of the real work was already
done (e.g. a genuine timeout mid-way through a large project, or a crash
after most files were written).

---

## 1. Design

### 1.1 `agent/graph/nodes/claude_code.py`

**Capture `session_id` whenever stdout is available, not just on
success.** Currently `_build_success_result` is the only place
`session_id` gets threaded into the result; the manifest-invalid and
nonzero-exit branches never look at `stdout` at all. Change: extract
`session_id` from `stdout` (when `communicate()` returned it — i.e. every
path except the `OSError`-before-start case and the timeout case,
where stdout was never captured) right after `communicate()` succeeds,
once, and include it in every returned dict, success or failure:
```python
try:
    session_id = json.loads(stdout.decode())["session_id"]
except (json.JSONDecodeError, KeyError):
    session_id = None
```
moved up to run unconditionally after `communicate()` returns (still
inside the lock, before the `returncode != 0` check), and both the
nonzero-exit and manifest-invalid failure dicts gain `"session_id":
session_id`. The already-shipped timeout-recovery path is unaffected —
`session_id` stays `None` there, as already documented (a killed process
never gets to print its final stdout JSON).

**A third prompt mode: continuing an interrupted attempt.** `revise_node`
already resumes a session with user feedback
(`REVISE_PROMPT_TEMPLATE.format(feedback=...)`). Resuming after a failure
is different — there's no user feedback, just "finish what you started."
Reuse the existing `resume_session_id`/`feedback` parameters with a third
combination, no signature change needed:
```python
if resume_session_id is None:
    prompt = _build_draft_prompt(student_info)
elif feedback is not None:
    prompt = REVISE_PROMPT_TEMPLATE.format(feedback=feedback)
else:
    prompt = RESUME_AFTER_FAILURE_PROMPT
```
```python
RESUME_AFTER_FAILURE_PROMPT = """\
Your previous attempt at this assignment was interrupted before finishing \
(e.g. a timeout or crash) — you may already have useful work in \
submission/ from before. Check what's already there, keep what's still \
correct, finish anything incomplete, and make sure submission_manifest.json \
and summary.txt are both written correctly before you're done — the run \
isn't considered complete without them.
"""
```

### 1.2 `agent/graph/assignment_graph.py` — `draft_node`

```python
async def draft_node(state: AssignmentState, config: RunnableConfig) -> dict:
    dest_dir = paths.assignment_dir(state["course_name"], state["title"])
    student_info = config["configurable"].get("student_info", "")

    pool = config["configurable"]["pool"]
    thread_id = config["configurable"]["thread_id"]
    with pool.connection() as conn:
        prior_session_id = repo.get_claude_session(conn, thread_id)

    result = await claude_code.run_claude_code(
        dest_dir, resume_session_id=prior_session_id, student_info=student_info
    )

    if not result["success"] and prior_session_id:
        # The saved session itself may be what's broken (e.g. unresumable,
        # or the same structural issue that failed last time) — fall back
        # to one fresh attempt rather than leaving the user stuck retrying
        # an identically-broken resume forever.
        result = await claude_code.run_claude_code(dest_dir, student_info=student_info)

    if not result["success"]:
        return {"failure_text": f'Drafting "{state["title"]}" failed: {result["error"]}',
                "session_id": result.get("session_id")}

    return {
        "submission_files": [str(p) for p in result["submission_files"]],
        "manifest": result["manifest"],
        "summary_text": result["summary_text"],
        "session_id": result["session_id"],
    }
```
- First attempt for a thread: `prior_session_id` is `None`, behaves exactly
  as today (fresh draft).
- Retry after a failure that did capture a session_id: resumes it with
  `RESUME_AFTER_FAILURE_PROMPT` instead of starting over.
- If *that* resume itself fails: one automatic fresh-draft fallback in the
  same call, so a single retry request from the user doesn't get stuck
  repeating an identically-broken resume — this costs at most one extra
  `run_claude_code` invocation (and its own 15-minute budget) only in the
  already-degraded case where the resume attempt failed.
- Every returned failure dict now includes `session_id` (possibly `None`),
  so `save_session_node` (unchanged — already unconditionally wired after
  `draft_node`, already saves whenever `state.get("session_id")` is
  truthy) persists it for the *next* retry, continuing the chain.

`revise_node` is unaffected — its own resume-with-feedback path and
session lookup are unchanged.

### 1.3 No graph-wiring changes

`draft_node` → `save_session_node` → `relay_node` is already the
unconditional edge sequence regardless of success/failure (confirmed:
`route_after_ingest` sends to `draft_node` on ingest success, `draft_node`
unconditionally flows to `save_session_node`, and only `route_after_relay`
branches on `failure_text`). No new nodes, no new edges.

---

## 2. What this does and doesn't fix

- **Fixes**: a retry after a genuine failure (timeout the disk-check
  didn't recover, a crash, an API error) resumes from where the session
  left off — including whatever files it had already written — instead of
  starting over from zero.
- **Doesn't fix**: `ingest_node` still re-runs in full on every retry
  (re-fetches materials from Classroom/Drive). This is deliberately out of
  scope — re-ingestion is idempotent and cheap relative to drafting, and
  skipping it risks resuming against stale materials if the assignment
  was edited between attempts.
- **Doesn't fix**: no explicit "start completely fresh, discard the saved
  session" user command. The one-level automatic fallback (§1.2) handles
  the most likely stuck case (the saved session itself is broken); a
  second consecutive failure would still attempt to resume whatever
  session the fresh-fallback attempt produced (if it captured one), same
  chaining behavior as `revise_node` already has for the success path.
  Not adding a manual override now — flag as a possible future follow-up
  if it turns out to matter in practice, not something to build for a
  hypothetical.

---

## 3. Acceptance criteria

1. ✅ Unit tests: `run_claude_code` returns a non-`None` `session_id` in the
   nonzero-exit and manifest-invalid failure branches when `stdout` parses
   with one; `draft_node` looks up and passes `resume_session_id` when one
   is saved for the thread; a failed resume triggers exactly one
   fresh-draft fallback attempt within the same `draft_node` call; a
   failure dict's `session_id` (found or not) flows through to
   `save_session_node`. 271/271 passing (13 new/updated).
2. ✅ `pytest -q` passes.
3. ✅ Live verification: rather than forcing a fresh failure, reused the
   real, already-saved session id in `claude_sessions` from an earlier
   successful run (`assignment:872230715595:878591141374` →
   `79588299-2626-4e52-9500-5bb730f3b72f`) and called `run_claude_code`
   directly with `resume_session_id` set and no feedback — confirmed via
   the CLI's own local session transcript that it appended to the *same*
   session file (not a new one) and correctly recognized the prior work:
   its own final response was *"Everything from the prior run is already
   complete and correct... No changes needed — the run was already
   finished."* — exactly the intended `RESUME_AFTER_FAILURE_PROMPT`
   behavior.

---

## 4. Files changed

| File | Change |
|---|---|
| `agent/graph/nodes/claude_code.py` | `session_id` extraction moved to run unconditionally after `communicate()` succeeds, included in every result dict (not just success); new `RESUME_AFTER_FAILURE_PROMPT`; prompt selection gains the `resume_session_id` set + `feedback` unset case |
| `agent/graph/assignment_graph.py` | `draft_node` looks up `repo.get_claude_session` before drafting, passes it as `resume_session_id`, falls back to one fresh attempt if that resume itself fails, and includes `session_id` in its failure return |
| `tests/test_claude_code.py`, `tests/test_assignment_graph.py` | New/updated tests per §3.1 |
| `specs/phase-3-approval-submission.md` | **Recommend updating** the `run_claude_code`/`draft_node` code blocks and their "Post-launch fix" notes to reflect this |
