"""Unit tests for agent/graph/nodes/ingestion.py: task-brief formatting and
the fail-fast material-ingestion flow."""

import asyncio
from unittest.mock import MagicMock

from agent.graph.nodes import ingestion


class TestBuildTaskBrief:
    def test_title_and_description(self):
        result = ingestion.build_task_brief({"title": "Bio Essay", "description": "Write 500 words."})

        assert result == "# Bio Essay\n\nWrite 500 words.\n"

    def test_title_only_no_description(self):
        result = ingestion.build_task_brief({"title": "Bio Essay"})

        assert result == "# Bio Essay\n"

    def test_missing_keys_default_to_empty(self):
        result = ingestion.build_task_brief({})

        assert result == "# \n"


def _coursework_service(courseWork: dict | None = None, error: Exception | None = None) -> MagicMock:
    service = MagicMock()
    get_mock = service.courses.return_value.courseWork.return_value.get.return_value
    if error is not None:
        get_mock.execute.side_effect = error
    else:
        get_mock.execute.return_value = courseWork
    return service


class TestIngestAssignment:
    def test_classroom_lookup_failure_returns_fail_fast_with_no_failed_file(self, tmp_path):
        classroom_service = _coursework_service(error=RuntimeError("Classroom API down"))

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result == {"success": False, "failed_file": None, "error": "Classroom API down"}

    def test_writes_task_brief_with_no_materials(self, tmp_path):
        classroom_service = _coursework_service({"title": "Essay", "description": "Do it.", "materials": []})

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result == {"success": True, "ingested_files": [], "unsupported_files": []}
        assert (tmp_path / "source-material" / "task-brief.md").read_text() == "# Essay\n\nDo it.\n"

    def test_resolved_drive_file_is_ingested(self, tmp_path, monkeypatch):
        courseWork = {
            "title": "Essay",
            "materials": [
                {"driveFile": {"driveFile": {"id": "fid1", "title": "Rubric.pdf"}}},
            ],
        }
        classroom_service = _coursework_service(courseWork)
        drive_service = MagicMock()
        monkeypatch.setattr(
            ingestion.drive_node,
            "resolve_attachment",
            lambda svc, fid, dest: tmp_path / "source-material" / "rubric.pdf",
        )

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, drive_service, "c1", "cw1", tmp_path)
        )

        assert result == {"success": True, "ingested_files": ["Rubric.pdf"], "unsupported_files": []}

    def test_unresolvable_drive_file_is_unsupported_not_a_failure(self, tmp_path, monkeypatch):
        courseWork = {
            "title": "Essay",
            "materials": [
                {"driveFile": {"driveFile": {"id": "fid1", "title": "video.mov"}}},
            ],
        }
        classroom_service = _coursework_service(courseWork)
        monkeypatch.setattr(ingestion.drive_node, "resolve_attachment", lambda svc, fid, dest: None)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result == {"success": True, "ingested_files": [], "unsupported_files": ["video.mov"]}

    def test_drive_resolution_error_fails_fast_with_failed_file_name(self, tmp_path, monkeypatch):
        courseWork = {
            "title": "Essay",
            "materials": [
                {"driveFile": {"driveFile": {"id": "fid1", "title": "Rubric.pdf"}}},
            ],
        }
        classroom_service = _coursework_service(courseWork)

        def raising(svc, fid, dest):
            raise RuntimeError("Drive 500")

        monkeypatch.setattr(ingestion.drive_node, "resolve_attachment", raising)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result == {"success": False, "failed_file": "Rubric.pdf", "error": "Drive 500"}

    def test_drive_file_falls_back_to_id_when_title_missing(self, tmp_path, monkeypatch):
        courseWork = {
            "title": "Essay",
            "materials": [{"driveFile": {"driveFile": {"id": "fid1"}}}],
        }
        classroom_service = _coursework_service(courseWork)
        monkeypatch.setattr(ingestion.drive_node, "resolve_attachment", lambda svc, fid, dest: None)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result["unsupported_files"] == ["fid1"]

    def test_link_material_is_unsupported(self, tmp_path):
        courseWork = {
            "title": "Essay",
            "materials": [{"link": {"url": "https://example.com/reading"}}],
        }
        classroom_service = _coursework_service(courseWork)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result["unsupported_files"] == ["https://example.com/reading"]

    def test_youtube_video_material_is_unsupported(self, tmp_path):
        courseWork = {
            "title": "Essay",
            "materials": [{"youtubeVideo": {"title": "Intro Lecture"}}],
        }
        classroom_service = _coursework_service(courseWork)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result["unsupported_files"] == ["Intro Lecture"]

    def test_form_material_is_unsupported(self, tmp_path):
        courseWork = {
            "title": "Essay",
            "materials": [{"form": {"title": "Feedback Form"}}],
        }
        classroom_service = _coursework_service(courseWork)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result["unsupported_files"] == ["Feedback Form"]

    def test_multiple_materials_mixed_outcomes(self, tmp_path, monkeypatch):
        courseWork = {
            "title": "Essay",
            "materials": [
                {"driveFile": {"driveFile": {"id": "fid1", "title": "Rubric.pdf"}}},
                {"driveFile": {"driveFile": {"id": "fid2", "title": "video.mov"}}},
                {"link": {"url": "https://example.com"}},
            ],
        }
        classroom_service = _coursework_service(courseWork)

        def fake_resolve(svc, fid, dest):
            return dest / "rubric.pdf" if fid == "fid1" else None

        monkeypatch.setattr(ingestion.drive_node, "resolve_attachment", fake_resolve)

        result = asyncio.run(
            ingestion.ingest_assignment(classroom_service, MagicMock(), "c1", "cw1", tmp_path)
        )

        assert result["ingested_files"] == ["Rubric.pdf"]
        assert result["unsupported_files"] == ["video.mov", "https://example.com"]
