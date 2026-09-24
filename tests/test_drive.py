"""Unit tests for agent/graph/nodes/drive.py: attachment resolution by
mimeType (native export / Pandoc conversion / passthrough / zip / unsupported)."""

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.graph.nodes import drive


def _service_returning(name: str, mime_type: str) -> MagicMock:
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {
        "name": name,
        "mimeType": mime_type,
    }
    return service


class TestDownloadToBuffer:
    def test_reads_chunks_until_done(self, monkeypatch):
        class FakeDownloader:
            def __init__(self, buffer, request):
                self.buffer = buffer
                self.calls = 0

            def next_chunk(self, num_retries=3):
                self.calls += 1
                self.buffer.write(b"chunk")
                return None, self.calls >= 2

        monkeypatch.setattr(drive, "MediaIoBaseDownload", FakeDownloader)

        result = drive._download_to_buffer(object())

        assert result == b"chunkchunk"


class TestResolveAttachment:
    def test_native_export_google_doc_writes_markdown(self, tmp_path, monkeypatch):
        service = _service_returning("Essay", "application/vnd.google-apps.document")
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: b"# content")

        result = drive.resolve_attachment(service, "fid1", tmp_path)

        assert result == tmp_path / "essay.md"
        assert result.read_bytes() == b"# content"
        service.files.return_value.export.assert_called_once_with(
            fileId="fid1", mimeType="text/markdown"
        )

    def test_pandoc_convertible_mimetype_converts_and_cleans_up_tempfile(self, tmp_path, monkeypatch):
        service = _service_returning(
            "Notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: b"binary content")
        convert_mock = MagicMock()
        monkeypatch.setattr(drive.pypandoc, "convert_file", convert_mock)

        result = drive.resolve_attachment(service, "fid2", tmp_path)

        assert result == tmp_path / "notesdocx.md"
        tmp_src, target_fmt = convert_mock.call_args[0]
        assert target_fmt == "md"
        assert convert_mock.call_args[1]["outputfile"] == str(tmp_path / "notesdocx.md")
        assert tmp_src.endswith(".docx")
        assert not Path(tmp_src).exists()  # cleaned up after conversion

    def test_passthrough_pdf_writes_raw_bytes_under_original_name(self, tmp_path, monkeypatch):
        service = _service_returning("scan.pdf", "application/pdf")
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: b"%PDF-1.4")

        result = drive.resolve_attachment(service, "fid3", tmp_path)

        assert result == tmp_path / "scan.pdf"
        assert result.read_bytes() == b"%PDF-1.4"

    def test_passthrough_image_writes_raw_bytes(self, tmp_path, monkeypatch):
        service = _service_returning("photo.jpg", "image/jpeg")
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: b"\xff\xd8\xff")

        result = drive.resolve_attachment(service, "fid4", tmp_path)

        assert result == tmp_path / "photo.jpg"
        assert result.read_bytes() == b"\xff\xd8\xff"

    def test_unsupported_mimetype_returns_none(self, tmp_path):
        service = _service_returning("video.mp4", "video/mp4")

        result = drive.resolve_attachment(service, "fid5", tmp_path)

        assert result is None

    def test_zip_extracts_contents_into_subfolder(self, tmp_path, monkeypatch):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("util.py", "def helper(): pass")
            zf.writestr("src/main.py", "print('hi')")
        service = _service_returning("search.zip", "application/x-zip-compressed")
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: buf.getvalue())

        result = drive.resolve_attachment(service, "fid6", tmp_path)

        assert result == tmp_path / "search"
        assert (result / "util.py").read_text() == "def helper(): pass"
        assert (result / "src" / "main.py").read_text() == "print('hi')"

    def test_zip_application_zip_mimetype_also_handled(self, tmp_path, monkeypatch):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.txt", "x")
        service = _service_returning("bundle.zip", "application/zip")
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: buf.getvalue())

        result = drive.resolve_attachment(service, "fid7", tmp_path)

        assert result == tmp_path / "bundle"
        assert (result / "a.txt").exists()

    def test_zip_slip_path_traversal_is_rejected(self, tmp_path, monkeypatch):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.txt", "pwned")
        service = _service_returning("evil.zip", "application/zip")
        monkeypatch.setattr(drive, "_download_to_buffer", lambda req: buf.getvalue())

        with pytest.raises(ValueError, match="Unsafe path in zip"):
            drive.resolve_attachment(service, "fid8", tmp_path)
