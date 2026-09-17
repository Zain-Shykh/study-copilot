"""Unit tests for agent/graph/nodes/claude_code.py: the headless Claude Code
subprocess wrapper and its submission_manifest.json contract validation."""

import asyncio
import json
from pathlib import Path

from agent.graph.nodes import claude_code


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.killed = False
        self.waited = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True


def _patch_subprocess(monkeypatch, process: FakeProcess) -> dict:
    captured: dict = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return captured


def _valid_workspace(tmp_path: Path, fmt: str = "as-is", files=None) -> Path:
    files = files or ["main.py"]
    (tmp_path / "submission").mkdir()
    for f in files:
        (tmp_path / "submission" / f).write_text("content")
    manifest = {"format": fmt, "files": files}
    (tmp_path / "submission_manifest.json").write_text(json.dumps(manifest))
    return tmp_path


class TestValidateManifest:
    def test_missing_submission_dir(self, tmp_path):
        result = claude_code._validate_manifest(tmp_path)

        assert result == "Claude Code finished without writing anything under submission/"

    def test_empty_submission_dir(self, tmp_path):
        (tmp_path / "submission").mkdir()

        result = claude_code._validate_manifest(tmp_path)

        assert result == "Claude Code finished without writing anything under submission/"

    def test_missing_manifest_file(self, tmp_path):
        (tmp_path / "submission").mkdir()
        (tmp_path / "submission" / "main.py").write_text("x")

        result = claude_code._validate_manifest(tmp_path)

        assert result == "Claude Code finished without writing submission_manifest.json"

    def test_invalid_json(self, tmp_path):
        (tmp_path / "submission").mkdir()
        (tmp_path / "submission" / "main.py").write_text("x")
        (tmp_path / "submission_manifest.json").write_text("{not json")

        result = claude_code._validate_manifest(tmp_path)

        assert result.startswith("submission_manifest.json isn't valid JSON")

    def test_invalid_format(self, tmp_path):
        _valid_workspace(tmp_path, fmt="doc")

        result = claude_code._validate_manifest(tmp_path)

        assert result == 'submission_manifest.json has an invalid "format": \'doc\''

    def test_missing_files_list(self, tmp_path):
        (tmp_path / "submission").mkdir()
        (tmp_path / "submission" / "main.py").write_text("x")
        (tmp_path / "submission_manifest.json").write_text(json.dumps({"format": "as-is"}))

        result = claude_code._validate_manifest(tmp_path)

        assert result == 'submission_manifest.json has an empty or missing "files" list'

    def test_nested_path_under_non_zip_format_is_rejected(self, tmp_path):
        (tmp_path / "submission" / "src").mkdir(parents=True)
        (tmp_path / "submission" / "src" / "main.py").write_text("x")
        manifest = {"format": "as-is", "files": ["src/main.py"]}
        (tmp_path / "submission_manifest.json").write_text(json.dumps(manifest))

        result = claude_code._validate_manifest(tmp_path)

        assert 'only "zip" may list nested paths' in result

    def test_nested_path_under_zip_format_is_allowed(self, tmp_path):
        (tmp_path / "submission" / "src").mkdir(parents=True)
        (tmp_path / "submission" / "src" / "main.py").write_text("x")
        manifest = {"format": "zip", "files": ["src/main.py"]}
        (tmp_path / "submission_manifest.json").write_text(json.dumps(manifest))

        result = claude_code._validate_manifest(tmp_path)

        assert result == manifest

    def test_listed_file_does_not_exist(self, tmp_path):
        (tmp_path / "submission").mkdir()
        (tmp_path / "submission" / "main.py").write_text("x")
        manifest = {"format": "as-is", "files": ["missing.py"]}
        (tmp_path / "submission_manifest.json").write_text(json.dumps(manifest))

        result = claude_code._validate_manifest(tmp_path)

        assert "missing.py" in result and "doesn't exist" in result

    def test_valid_manifest_returns_parsed_dict(self, tmp_path):
        workspace = _valid_workspace(tmp_path)

        result = claude_code._validate_manifest(workspace)

        assert result == {"format": "as-is", "files": ["main.py"]}


class TestRunClaudeCode:
    def test_success_fresh_draft_returns_full_result(self, tmp_path, monkeypatch):
        workspace = _valid_workspace(tmp_path)
        (workspace / "summary.txt").write_text("Used the textbook.")
        process = FakeProcess(stdout=json.dumps({"session_id": "sess1"}).encode(), returncode=0)
        captured = _patch_subprocess(monkeypatch, process)

        result = asyncio.run(claude_code.run_claude_code(workspace))

        assert result == {
            "success": True,
            "session_id": "sess1",
            "submission_files": [workspace / "submission" / "main.py"],
            "manifest": {"format": "as-is", "files": ["main.py"]},
            "summary_text": "Used the textbook.",
        }
        assert claude_code.DRAFT_PROMPT in captured["args"]
        assert "--resume" not in captured["args"]
        assert captured["kwargs"]["cwd"] == workspace

    def test_revision_uses_resume_and_feedback_prompt(self, tmp_path, monkeypatch):
        workspace = _valid_workspace(tmp_path)
        process = FakeProcess(stdout=json.dumps({"session_id": "sess2"}).encode(), returncode=0)
        captured = _patch_subprocess(monkeypatch, process)

        result = asyncio.run(
            claude_code.run_claude_code(workspace, resume_session_id="sess1", feedback="Add a conclusion.")
        )

        assert result["success"] is True
        args = captured["args"]
        assert "--resume" in args
        assert args[args.index("--resume") + 1] == "sess1"
        prompt = args[args.index("-p") + 1]
        assert "Add a conclusion." in prompt

    def test_blocked_env_vars_are_stripped(self, tmp_path, monkeypatch):
        workspace = _valid_workspace(tmp_path)
        process = FakeProcess(stdout=json.dumps({"session_id": "s"}).encode(), returncode=0)
        captured = _patch_subprocess(monkeypatch, process)
        monkeypatch.setenv("DATABASE_URL", "postgres://secret")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
        monkeypatch.setenv("META_APP_SECRET", "secret")
        monkeypatch.setenv("GEMINI_API_KEY", "secret")
        monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")

        asyncio.run(claude_code.run_claude_code(workspace))

        env = captured["kwargs"]["env"]
        assert "DATABASE_URL" not in env
        assert "GOOGLE_OAUTH_CLIENT_SECRET" not in env
        assert "META_APP_SECRET" not in env
        assert "GEMINI_API_KEY" not in env
        assert env.get("SOME_OTHER_VAR") == "keep-me"

    def test_nonzero_exit_returns_stderr_as_error(self, tmp_path, monkeypatch):
        process = FakeProcess(stderr=b"boom", returncode=1)
        _patch_subprocess(monkeypatch, process)

        result = asyncio.run(claude_code.run_claude_code(tmp_path))

        assert result == {"success": False, "error": "boom"}

    def test_nonzero_exit_with_no_stderr_uses_generic_message(self, tmp_path, monkeypatch):
        process = FakeProcess(stderr=b"", returncode=1)
        _patch_subprocess(monkeypatch, process)

        result = asyncio.run(claude_code.run_claude_code(tmp_path))

        assert result == {"success": False, "error": "Claude Code exited with an error"}

    def test_manifest_contract_violation_is_reported_as_failure(self, tmp_path, monkeypatch):
        # returncode 0 but nothing written under submission/
        process = FakeProcess(stdout=b"{}", returncode=0)
        _patch_subprocess(monkeypatch, process)

        result = asyncio.run(claude_code.run_claude_code(tmp_path))

        assert result == {
            "success": False,
            "error": "Claude Code finished without writing anything under submission/",
        }

    def test_unparseable_stdout_session_id_falls_back_to_none(self, tmp_path, monkeypatch):
        workspace = _valid_workspace(tmp_path)
        process = FakeProcess(stdout=b"not json at all", returncode=0)
        _patch_subprocess(monkeypatch, process)

        result = asyncio.run(claude_code.run_claude_code(workspace))

        assert result["success"] is True
        assert result["session_id"] is None

    def test_missing_summary_file_defaults_to_empty_string(self, tmp_path, monkeypatch):
        workspace = _valid_workspace(tmp_path)
        process = FakeProcess(stdout=json.dumps({"session_id": "s"}).encode(), returncode=0)
        _patch_subprocess(monkeypatch, process)

        result = asyncio.run(claude_code.run_claude_code(workspace))

        assert result["summary_text"] == ""

    def test_process_start_oserror_is_reported_as_failure(self, tmp_path, monkeypatch):
        async def raising(*args, **kwargs):
            raise OSError("claude: command not found")

        monkeypatch.setattr(claude_code.asyncio, "create_subprocess_exec", raising)

        result = asyncio.run(claude_code.run_claude_code(tmp_path))

        assert result["success"] is False
        assert "claude: command not found" in result["error"]

    def test_timeout_kills_process_and_reports_failure(self, tmp_path, monkeypatch):
        process = FakeProcess(returncode=0)
        _patch_subprocess(monkeypatch, process)

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(claude_code.asyncio, "wait_for", fake_wait_for)

        result = asyncio.run(claude_code.run_claude_code(tmp_path))

        assert result == {"success": False, "error": "Claude Code timed out"}
        assert process.killed is True
        assert process.waited is True
