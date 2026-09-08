"""Path helpers for ~/agent-workspace/<course>/<assignment>/."""

import re
from pathlib import Path

WORKSPACE_ROOT = Path.home() / "agent-workspace"


def slugify(name: str) -> str:
    """Lowercases, replaces whitespace with '-', strips anything outside
    [a-z0-9-]."""
    slug = re.sub(r"\s+", "-", name.strip().lower())
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    return slug


def assignment_dir(course_name: str, assignment_name: str) -> Path:
    """Returns ~/agent-workspace/<slugify(course_name)>/<slugify(assignment_name)>/,
    creating it (and its source-material/ subdirectory) if missing. Idempotent."""
    path = WORKSPACE_ROOT / slugify(course_name) / slugify(assignment_name)
    (path / "source-material").mkdir(parents=True, exist_ok=True)
    return path
