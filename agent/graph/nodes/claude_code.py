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

_VALID_FORMATS = {"as-is", "zip", "pdf", "docx"}

DRAFT_PROMPT = """\
Read every file under source-material/ in this directory. task-brief.md \
always describes the assignment, including any stated submission \
requirements — format (e.g. "submit as a single PDF", "zip your .py \
files"), and structure (e.g. "include a src/ folder and a README", a \
required project layout). Follow them exactly, producing the complete, \
actual assignment response — not just a matching file type. If nothing is \
stated, use your judgment based on the nature of the response (prose -> a \
single Markdown file, a small script -> one file, a larger project -> \
whatever files/folders it actually needs).

Write your complete response under submission/ in this directory — one or \
more files, organized into subfolders if the required structure calls for \
it (e.g. submission/src/main.py, submission/tests/test_main.py), using \
whatever names/extensions fit the content (Markdown for prose, .py/.js/\
etc. for code, and so on). You cannot produce a .zip or a real .pdf \
yourself, so never write one directly — instead, also write \
submission_manifest.json in this directory (not under submission/) \
describing how those files should be packaged:
  {"format": "as-is" | "zip" | "pdf" | "docx", "files": ["<paths relative to submission/, e.g. src/main.py>"]}
- "as-is": upload each listed file unchanged (e.g. a single .py file, or \
  Classroom accepts multiple separate attachments). Only valid for flat \
  files directly under submission/ — no subfolders, since loose uploads \
  can't preserve a folder structure.
- "zip": bundle every listed file into one .zip archive, preserving \
  whatever subfolder structure it's in under submission/. Required \
  whenever submission/ has more than a flat list of files, or the \
  assignment explicitly asks for a zip.
- "pdf" / "docx": convert the listed file(s) (must be Markdown/text, flat, \
  no subfolders) to that format, one output file per input file listed.
Use "as-is" for a single flat file, "zip" whenever there's a real folder \
structure or the assignment explicitly asks for one, otherwise follow \
whatever specific format the assignment states.

When done, also write a short plain-text summary (sources used, key \
assumptions made, anything you couldn't find or access) to summary.txt in \
this directory.
"""

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
    any violation of the contract described in DRAFT_PROMPT/REVISE_PROMPT_TEMPLATE."""
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


async def run_claude_code(
    workspace_dir: Path, *, resume_session_id: str | None = None, feedback: str | None = None
) -> dict:
    """Runs a headless Claude Code session scoped to workspace_dir, no Bash,
    no broader filesystem, no Google credentials in its environment. A
    fresh submission when resume_session_id is None; otherwise resumes
    that session with feedback as a revision request.

    Returns {"success": True, "session_id": ..., "submission_files": [Path, ...],
    "manifest": {...}, "summary_text": ...} on success, or
    {"success": False, "error": ...} on a nonzero exit, timeout, or a
    submission/submission_manifest.json contract violation (missing
    submission/, missing/unparseable manifest, unrecognized format, a
    listed file that doesn't exist, or a nested path under a non-"zip"
    format) — same "reported as a failed run" handling as a missing
    draft.md was in Phase 2.
    """
    prompt = DRAFT_PROMPT if resume_session_id is None else REVISE_PROMPT_TEMPLATE.format(feedback=feedback)

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
        "--allowedTools",
        "Read,Write,WebSearch,WebFetch",
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
                return {"success": False, "error": "Claude Code timed out"}
        except OSError as e:
            return {"success": False, "error": f"Failed to start Claude Code: {e}"}

        if process.returncode != 0:
            return {"success": False, "error": stderr.decode(errors="replace") or "Claude Code exited with an error"}

        manifest_or_error = _validate_manifest(workspace_dir)
        if isinstance(manifest_or_error, str):
            return {"success": False, "error": manifest_or_error}
        manifest = manifest_or_error

        submission_dir = workspace_dir / "submission"
        submission_files = [submission_dir / rel_path for rel_path in manifest["files"]]

        try:
            session_id = json.loads(stdout.decode())["session_id"]
        except (json.JSONDecodeError, KeyError):
            session_id = None

        summary_path = workspace_dir / "summary.txt"
        summary_text = summary_path.read_text() if summary_path.exists() else ""

        return {
            "success": True,
            "session_id": session_id,
            "submission_files": submission_files,
            "manifest": manifest,
            "summary_text": summary_text,
        }
