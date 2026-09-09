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

DRAFT_PROMPT = """\
Read every file under source-material/ in this directory. task-brief.md
always describes the assignment; any other files are additional source
material or examples. Write your complete response to draft.md in this
directory. When done, also write a short plain-text summary (sources used,
key assumptions made, anything you couldn't find or access) to summary.txt
in this directory.
"""


async def run_claude_code(workspace_dir: Path) -> dict:
    """Runs a headless Claude Code session scoped to workspace_dir, no Bash,
    no broader filesystem, no Google credentials in its environment.

    Returns {"success": True, "session_id": ..., "draft_path": ...,
    "summary_text": ...} on success, or {"success": False, "error": ...}
    on a nonzero exit, missing draft.md, or a timeout.
    """
    subprocess_env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(prefix) for prefix in _BLOCKED_ENV_PREFIXES)
    }

    async with _invocation_lock:
        try:
            process = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                DRAFT_PROMPT,
                "--output-format",
                "json",
                "--allowedTools",
                "Read,Write,WebSearch,WebFetch",
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

        draft_path = workspace_dir / "draft.md"
        if not draft_path.exists():
            return {"success": False, "error": "Claude Code finished without producing draft.md"}

        try:
            session_id = json.loads(stdout.decode())["session_id"]
        except (json.JSONDecodeError, KeyError):
            session_id = None

        summary_path = workspace_dir / "summary.txt"
        summary_text = summary_path.read_text() if summary_path.exists() else ""

        return {
            "success": True,
            "session_id": session_id,
            "draft_path": draft_path,
            "summary_text": summary_text,
        }
