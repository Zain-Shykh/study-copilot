# Bash execution for assignment drafting — investigated and rejected

**Status: rejected.** This document is a decision record, not an
implementation spec — Bash was investigated as a way to let Claude Code
compile/run/verify code during assignment drafting, live-tested
extensively, and ultimately **not implemented**, because the one
enforcement mechanism available (`--restricted`) turned out not to
reliably confine what Bash-executed code can read, under the exact tool
combination this app needs. What shipped instead is in §4: a smaller, fully
safe fix that stops Claude Code from wasting turns repeatedly retrying a
Bash it doesn't have, plus PPTX submission support (which never needed
Bash in the first place).

The locked "no Bash" decision from `specs/phase-3-approval-submission.md`
(Decision #8) and CLAUDE.md's architecture description ("no Bash, no
broader filesystem... structurally incapable of sending/submitting
anything") **stand, unchanged.**

---

## 1. What went wrong without it (the motivating problem)

Live-testing a real assignment (Artificial Intelligence (BSSE) — Assignment
01, a full Berkeley CS188 Pacman "search" project: 60+ files including an
autograder and test cases) surfaced two structural problems, both traced to
the complete absence of any code-execution capability:

1. **No way to bulk-move unmodified files.** Most of a real coding
   assignment's skeleton (support files the student doesn't need to touch)
   just needs to be carried into `submission/` unchanged. Without Bash, the
   only way to do that is `Read` the whole file, then `Write` it back out
   byte-for-byte — for a 60-file project, a lot of the 15-minute
   `_TIMEOUT_SECONDS` budget goes to this instead of the actual assignment
   work.
2. **No way to verify its own work.** Many assignment briefs explicitly say
   things like *"Run all provided test cases to verify your solutions"* —
   with `Read`/`Write`/`Edit`/`WebSearch`/`WebFetch` and no code execution
   at all, Claude Code can reason about code but can never confirm it
   actually runs, let alone runs correctly.

Both problems are real, and neither is fixed by what shipped instead (§4)
— §5 covers what that leaves open.

---

## 2. The investigation: a promising mechanism, then a reversal

### 2.1 First round: `--restricted` looked like a real fix

The `claude` CLI (v2.1.282) has a `--restricted` mode, backed by
`bubblewrap` (confirmed installed on this machine at `/usr/bin/bwrap`),
that's supposed to confine file/Bash tools to the working directory. A
first round of live tests (reproduced multiple times, including the
critical ones three times) found:

1. With `--restricted --tools Bash` and an outside file at a sibling path:
   `cat <outside-path>` was blocked (`permission_denials` showed the Bash
   call denied, response said "The command was blocked by the sandbox"),
   and — this looked like the important part — `python3 -c
   "print(open('<outside-path>').read())"` was **also** blocked,
   identically. That seemed to rule out a shallow "check the shell command
   text" implementation in favor of real path-level enforcement.
2. Without `--restricted`, there was no confinement at all for *any* tool
   — a plain `--allowedTools Read` (matching production at the time) read
   an arbitrary absolute path outside its working directory with zero
   denial. (This remains true and unrelated to Bash — see §5.)
3. `--restricted` is stricter by default even for already-allowed tools —
   `--permission-mode acceptEdits` was needed to let ordinary
   `Write`/`mkdir`/`cp`/`mv` proceed without hanging on approval.
4. A **bare wildcard `Bash(*)`** allow-pattern was found to disable the
   sandbox — a subsequent `cat <outside-path>` succeeded when `Bash(*)` was
   allowed, but stayed blocked with a pinned pattern like `Bash(python3 *)`
   (fixed program name, wildcard only for arguments). This looked like a
   clean, learnable rule: never use a bare wildcard, always pin the exact
   program name.
5. Running a just-built binary needed its own pinned pattern, `Bash(./*)`
   (relative-path execution) — tested with a compile-then-run chain
   (`g++ -o sol sol.cpp && ./sol`), and the directory-escape check still
   held with `Bash(./*)` allowed (a `../../` escape attempt via `cat` was
   still blocked).
6. Chained commands are evaluated per-subcommand, not as one prefix match
   — `g++ -o sol sol.cpp && ./sol` with only `Bash(g++ *)` allowed compiled
   but was separately denied at `./sol`.

Based on this, a full implementation plan was drafted: `--restricted
--tools "Read,Write,Edit,WebSearch,WebFetch,Bash" --permission-mode
acceptEdits --allowedTools` with a curated, pinned command list (`g++`,
`gcc`, `make`, `python3`, `python`, `./*` — covering the assignment types
this app actually sees: C++, Python, and PDF/DOCX/PPTX/`.ipynb`
generation, the latter of which turned out to need no Bash at all, see §4.2).

### 2.2 Second round: re-testing the *actual* production combination broke it

The plan's own acceptance criteria required re-verifying confinement with
the real, full allow-list before shipping — not just the narrower ad-hoc
patterns tested one at a time above. That re-test is what reversed the
decision:

1. With the **actual production tool list**
   (`--tools "Read,Write,Edit,WebSearch,WebFetch,Bash"`, matching what the
   app would really need — every earlier "it's blocked" result had used
   `--tools Bash` *alone*) plus the real pinned allow-list, `python3 -c
   "print(open('<outside-path>').read())"` **succeeded** — it printed the
   file. `cat <outside-path>` was *still* blocked in this exact same run.
2. Narrowing it down: even the minimal `--tools "Read,Bash"` reproduced the
   leak. So did `--tools "Write,Bash"`.
3. The decisive finding: **re-running the original "safe" baseline —
   `--tools "Bash"` alone, the exact configuration §2.1's point 1 was built
   on — also leaked**, on a plain re-run with no configuration change at
   all.

That last point is what makes this a rejection rather than a
configuration puzzle to solve: the same exact setup produced different
results on different runs. The protection isn't unreliable *for a specific
tool combination* — it's inconsistent, full stop. The one thing that held
consistently across every single test, safe or leaking: a plain `cat
<path>` was always blocked. That's most likely a narrow pattern-match on a
few well-known file-reading commands, not the real OS-level containment
§2.1 assumed it was.

### 2.3 Why this rules out shipping Bash, not just this specific design

A security boundary that sometimes holds and sometimes doesn't isn't a
security boundary — it's worse than not having the capability at all,
because it would create false confidence. Given:
- `subprocess_env` already strips `DATABASE_URL`/`GOOGLE_OAUTH_*`/
  `META_*`/`GEMINI_*` from environment *variables*, but that does nothing
  to stop Bash-executed Python from reading the literal `.env` *file* on
  disk by absolute path, or any other file the OS user can read;
- `WebFetch` is already an allowed tool, giving a real (not hypothetical)
  path to actually exfiltrate whatever gets read;

...granting Bash under this CLI version's `--restricted` mode was judged
not safe to ship, regardless of which specific command allow-list or tool
combination was chosen. No further tuning was attempted.

---

## 3. What this means for "will Claude Code get stuck retrying Bash"

A real, separate problem observed in both live transcripts of the
motivating failure (§1): Claude Code repeatedly tried `Bash` (`cp -r`,
`shutil.copytree`, etc.), got denied each time, and retried several
variations before giving up — wasted turns and time, independent of
whether Bash is ever actually granted.

This turned out to be fixable with **no security tradeoff at all** and
**no Bash**. The distinction is between two different CLI flags:
- `--allowedTools` — which specific actions are pre-approved without a
  prompt.
- `--tools` — which tools are *offered to the model as existing at all*.

Production code (before this investigation) passed `--allowedTools` but
never passed `--tools`, so Bash remained *visible* as an option (just
denied on each attempt) — matching exactly the repeated-retry pattern
observed. Live-tested fix: pass `--tools` explicitly, without Bash in it.
With `--tools "Read,Write,Edit,WebSearch,WebFetch"` set, a prompt that
explicitly asked it to run `ls -la` and a checksum tool produced **zero**
Bash attempts — the model's own response: *"I can't run `ls -la` or a
checksum tool either way, since no shell/bash tool is available in this
environment"* — and it moved on immediately. Not "denied and retried" —
"never tried."

---

## 4. What actually shipped

### 4.1 `agent/graph/nodes/claude_code.py` — explicit `--tools`, no Bash

```python
_TOOLS = "Read,Write,Edit,WebSearch,WebFetch"

args = [
    "claude", "-p", prompt, "--output-format", "json",
    "--tools", _TOOLS,
    "--allowedTools", _TOOLS,
]
```
This replaces the previous:
```python
args = [
    "claude", "-p", prompt, "--output-format", "json",
    "--allowedTools", "Read,Write,Edit,WebSearch,WebFetch",
]
```
No `--restricted`, no `--permission-mode`, no Bash pattern list — none of
it survived the investigation. `_TIMEOUT_SECONDS` is unchanged at `15 * 60`
(never raised in this round either).

`DRAFT_PROMPT_TEMPLATE` gained one paragraph making the limitation
explicit to the model, rather than letting it discover it by trial and
error: *"You have no way to run or execute anything (no Bash, no code
execution)... write the most careful, correct code you can by reasoning it
through and tracing it by hand... say so plainly in summary.txt rather than
claiming it works."*

### 4.2 PPTX submission support — unrelated to Bash, shipped alongside

`_VALID_FORMATS` gained `"pptx"`: `{"as-is", "zip", "pdf", "docx", "pptx"}`.
This never needed Bash — PDF/DOCX/PPTX generation is handled entirely
server-side by `agent/graph/assignment_graph.py`'s `_package_submission`,
via the same `pypandoc.convert_file` call already used for `pdf`/`docx`
(the branch is already generic on `format_`, so no code change was needed
there — only widening `_VALID_FORMATS` upstream). Live-verified: a small
Markdown file converted to a real, valid `.pptx` (28KB, non-empty) via
`pypandoc.convert_file(src, "pptx", ...)`.

`DRAFT_PROMPT_TEMPLATE`'s manifest schema and format-bullet list mention
`"pptx"` alongside `"pdf"`/`"docx"` throughout.

---

## 5. What's still open

- **Coding assignments still can't be self-verified.** This isn't a new
  loss — it's the status quo from before this investigation. Claude Code
  writes code by reasoning alone, same as before; it just now says so
  explicitly in `summary.txt` (§4.1) instead of silently not mentioning it.
- **Read/Write/Edit are still not actually confined to `workspace_dir`.**
  §2.1 point 2 remains true and unresolved — a plain `Read`/`Write`/`Edit`
  call *can* reach an absolute path outside the workspace if given one;
  nothing currently stops that except that there's no reason for it to try.
  This predates this investigation and is out of scope for what shipped
  here, but is worth its own look at some point, independent of Bash.
- **Whether the original timeout (Assignment 01) is now avoidable at all**
  is unknown without Bash. The `Edit`-permission stall (a separate,
  already-merged fix from before this investigation) was the actual root
  cause of that specific timeout, not fundamentally the lack of bulk-copy
  speed — so it's untested whether a re-run, with the `Edit` fix but no
  Bash, now completes within 15 minutes.

---

## 6. Files changed/created — summary

| File | Change |
|---|---|
| `agent/graph/nodes/claude_code.py` | `_TOOLS` constant; subprocess args pass `--tools` explicitly (no Bash) alongside `--allowedTools`; `_VALID_FORMATS` gains `"pptx"`; `DRAFT_PROMPT_TEMPLATE` documents the no-execution limitation and the pptx format option; `run_claude_code`'s docstring corrected |
| `tests/test_claude_code.py` | Replaced the (reverted) Bash/`--restricted` tests with one asserting `--tools`/`--allowedTools` both omit Bash and no `--restricted`/`--permission-mode` flags are passed; added a pptx-format-is-valid manifest test |
| `specs/phase-3-approval-submission.md` | **Recommend updating** the `run_claude_code` code block (§4) and its "Post-launch fix" notes to reflect the explicit `--tools` flag and pptx support, and to link here for the rejected Bash investigation |

No changes to `agent/graph/assignment_graph.py` (§4.2 — pptx needed no
code change there), `docs/product_definition.md`, or `CLAUDE.md` — the "no
Bash" architecture description in both remains accurate as written.
