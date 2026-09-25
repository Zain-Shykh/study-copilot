"""Headless Claude Code subprocess wrapper: new session per first draft, --resume per revision."""

import asyncio
import json
import os
from pathlib import Path

# Env vars intentionally NOT passed through to the subprocess — it has no
# business talking to Postgres, Google, WhatsApp, or Gemini, per the Claude
# Code interaction contract ("structurally incapable of sending/submitting
# anything").
_BLOCKED_ENV_PREFIXES = ("DATABASE_URL", "GOOGLE_OAUTH_", "META_", "GEMINI_")

# Process-wide, per the locked "Drafting concurrency" decision — only one
# headless Claude Code session runs at a time.
_invocation_lock = asyncio.Lock()

_TIMEOUT_SECONDS = 15 * 60

_VALID_FORMATS = {"as-is", "zip", "pdf", "docx", "pptx"}

# No Bash — investigated and explicitly rejected, see
# specs/sandboxed-bash-execution.md for why (live-tested, unreliable
# sandbox confinement for arbitrary code reading files outside the
# workspace, not just occasionally but reproducibly under the exact tool
# combination this app needs).
_TOOLS = "Read,Write,Edit,WebSearch,WebFetch"

DRAFT_PROMPT_TEMPLATE = """\
Read every file under source-material/ in this directory (including \
anything inside an extracted subfolder). task-brief.md always describes \
the assignment, including any stated submission requirements — format \
(e.g. "submit as a single PDF", "zip your .py files"), and structure (e.g. \
"include a src/ folder and a README", a required project layout). Follow \
them exactly, producing the complete, actual assignment response — not \
just a matching file type. If nothing is stated, use your judgment based \
on the nature of the response (prose -> a single Markdown file, a small \
script -> one file, a larger project -> whatever files/folders it \
actually needs).

If the assignment asks you to identify yourself in the submission or its \
filename (e.g. a roll number, student ID, or name), use exactly this: \
__STUDENT_INFO__

You have no way to run or execute anything (no Bash, no code execution) — \
only Read/Write/Edit/WebSearch/WebFetch. For a coding assignment, write \
the most careful, correct code you can by reasoning it through and \
tracing it by hand — you cannot compile, run, or test it yourself, so say \
so plainly in summary.txt rather than claiming it works.

Write your complete response under submission/ in this directory — one or \
more files, organized into subfolders if the required structure calls for \
it (e.g. submission/src/main.py, submission/tests/test_main.py), using \
whatever names/extensions fit the content (Markdown for prose, .py/.js/\
etc. for code, and so on). You cannot produce a .zip or a real .pdf/pptx/\
docx yourself, so never write one directly — instead, also write \
submission_manifest.json in this directory (not under submission/) \
describing how those files should be packaged:
  {"format": "as-is" | "zip" | "pdf" | "docx" | "pptx", "files": ["<paths relative to submission/, e.g. src/main.py>"], "output_name": "<optional, no extension>"}
- "as-is": upload each listed file unchanged (e.g. a single .py file, or \
  Classroom accepts multiple separate attachments). Only valid for flat \
  files directly under submission/ — no subfolders, since loose uploads \
  can't preserve a folder structure. Name the file itself under \
  submission/ exactly as the assignment requires — "output_name" is \
  ignored for this format.
- "zip": bundle every listed file into one .zip archive, preserving \
  whatever subfolder structure it's in under submission/. Required \
  whenever submission/ has more than a flat list of files, or the \
  assignment explicitly asks for a zip.
- "pdf" / "docx" / "pptx": convert the listed file(s) (must be Markdown/\
  text, flat, no subfolders) to that format, one output file per input \
  file listed.
Use "as-is" for a single flat file, "zip" whenever there's a real folder \
structure or the assignment explicitly asks for one, otherwise follow \
whatever specific format the assignment states. Set "output_name" only \
when the assignment specifies an exact filename for the packaged "zip"/\
"pdf"/"docx"/"pptx" output (e.g. "submit a zip named your roll number") — \
omit it otherwise.

When done, also write a short plain-text summary (sources used, key \
assumptions made, anything you couldn't find or access) to summary.txt in \
this directory.
"""

_NO_STUDENT_INFO = (
    "not configured — if the assignment needs one, say so in summary.txt instead of guessing"
)


def _build_draft_prompt(student_info: str) -> str:
    return DRAFT_PROMPT_TEMPLATE.replace("__STUDENT_INFO__", student_info or _NO_STUDENT_INFO)


REVISE_PROMPT_TEMPLATE = """\
The user reviewed your submission and asked for these changes:

{feedback}

Update the file(s)/folder(s) under submission/ in this directory to \
address the feedback (add/remove/rename/reorganize as needed — it doesn't \
have to match the previous structure). Re-write submission_manifest.json \
to match whatever's now in submission/, keeping the same format unless the \
feedback or the new structure implies the required format itself changed. \
When done, overwrite summary.txt with an updated short plain-text summary \
(sources used, key assumptions made, anything you couldn't find or access, \
and what changed in this revision).
"""


def _validate_manifest(workspace_dir: Path) -> dict | str:
    """Returns the parsed manifest dict on success, or an error string on
    any violation of the contract described in DRAFT_PROMPT_TEMPLATE/REVISE_PROMPT_TEMPLATE."""
    submission_dir = workspace_dir / "submission"
    if not submission_dir.is_dir() or not any(submission_dir.iterdir()):
        return "Claude Code finished without writing anything under submission/"

    manifest_path = workspace_dir / "submission_manifest.json"
    if not manifest_path.exists():
        return "Claude Code finished without writing submission_manifest.json"

    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return f"submission_manifest.json isn't valid JSON: {e}"

    format_ = manifest.get("format")
    if format_ not in _VALID_FORMATS:
        return f'submission_manifest.json has an invalid "format": {format_!r}'

    files = manifest.get("files")
    if not files or not isinstance(files, list):
        return 'submission_manifest.json has an empty or missing "files" list'

    output_name = manifest.get("output_name")
    if output_name is not None and (not isinstance(output_name, str) or not output_name.strip()):
        return 'submission_manifest.json has an invalid "output_name"'

    for rel_path in files:
        if "/" in rel_path or "\\" in rel_path:
            if format_ != "zip":
                return (
                    f'submission_manifest.json lists a nested path "{rel_path}" '
                    f'under format "{format_}" — only "zip" may list nested paths'
                )
        if not (submission_dir / rel_path).is_file():
            return f'submission_manifest.json lists "{rel_path}", which doesn\'t exist under submission/'

    return manifest


def _build_success_result(workspace_dir: Path, manifest: dict, session_id: str | None) -> dict:
    submission_dir = workspace_dir / "submission"
    submission_files = [submission_dir / rel_path for rel_path in manifest["files"]]
    summary_path = workspace_dir / "summary.txt"
    summary_text = summary_path.read_text() if summary_path.exists() else ""
    return {
        "success": True,
        "session_id": session_id,
        "submission_files": submission_files,
        "manifest": manifest,
        "summary_text": summary_text,
    }


async def run_claude_code(
    workspace_dir: Path,
    *,
    resume_session_id: str | None = None,
    feedback: str | None = None,
    student_info: str = "",
) -> dict:
    """Runs a headless Claude Code session scoped to workspace_dir via
    --tools=_TOOLS (Read/Write/Edit/WebSearch/WebFetch only — no Bash, no
    code execution of any kind; see the module comment above _TOOLS for
    why), no Google credentials in its environment. A fresh submission
    when resume_session_id is None (student_info, if set,
    is given to it for filenames/output naming the assignment asks to be
    personalized); otherwise resumes that session with feedback as a
    revision request.

    Returns {"success": True, "session_id": ..., "submission_files": [Path, ...],
    "manifest": {...}, "summary_text": ...} on success, or
    {"success": False, "error": ...} on a nonzero exit, or a
    submission/submission_manifest.json contract violation (missing
    submission/, missing/unparseable manifest, unrecognized format, a
    listed file that doesn't exist, or a nested path under a non-"zip"
    format) — same "reported as a failed run" handling as a missing
    draft.md was in Phase 2. On a timeout, the process is killed, but
    workspace_dir is checked for an already-valid manifest before giving
    up — live-observed that the CLI's conversation can finish (with a
    fully valid submission written to disk) well before the process
    itself exits, and discarding that as a failure would throw away real,
    completed work. Only reports {"success": False, "error": "Claude Code
    timed out"} if the manifest isn't valid even after the kill.
    session_id is always None in that recovered-on-timeout case (the
    session id is only ever known via the CLI's own stdout, which a
    killed process never got to print) — a subsequent revise_node call
    starts a fresh session rather than --resume-ing.
    """
    prompt = (
        _build_draft_prompt(student_info)
        if resume_session_id is None
        else REVISE_PROMPT_TEMPLATE.format(feedback=feedback)
    )

    subprocess_env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(prefix) for prefix in _BLOCKED_ENV_PREFIXES)
    }

    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--tools",
        _TOOLS,
        "--allowedTools",
        _TOOLS,
    ]
    if resume_session_id:
        args += ["--resume", resume_session_id]

    async with _invocation_lock:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=workspace_dir,
                env=subprocess_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                # The CLI's own conversation can finish (files written,
                # manifest/summary on disk) well before the process itself
                # exits and hands control back — live-observed: a fully
                # valid submission sitting on disk, discarded as a false
                # "timed out" failure because we never checked. If the
                # deliverables are actually there and valid, use them
                # instead of throwing away real, completed work.
                manifest_or_error = _validate_manifest(workspace_dir)
                if isinstance(manifest_or_error, dict):
                    return _build_success_result(workspace_dir, manifest_or_error, session_id=None)
                return {"success": False, "error": "Claude Code timed out"}
        except OSError as e:
            return {"success": False, "error": f"Failed to start Claude Code: {e}"}

        if process.returncode != 0:
            return {"success": False, "error": stderr.decode(errors="replace") or "Claude Code exited with an error"}

        manifest_or_error = _validate_manifest(workspace_dir)
        if isinstance(manifest_or_error, str):
            return {"success": False, "error": manifest_or_error}

        try:
            session_id = json.loads(stdout.decode())["session_id"]
        except (json.JSONDecodeError, KeyError):
            session_id = None

        return _build_success_result(workspace_dir, manifest_or_error, session_id)
