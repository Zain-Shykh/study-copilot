"""Drive API calls: download/export attachments, upload the approved final file."""

import io
import tempfile
from pathlib import Path

import pypandoc
from googleapiclient.http import MediaIoBaseDownload

from agent.workspace.paths import slugify

# Verify text/markdown export is actually supported for native Google Docs on
# the live API before relying on this — see spec §0.5. Fall back to
# text/html or text/plain + Pandoc if it isn't.
NATIVE_EXPORT_MIMETYPES = {
    "application/vnd.google-apps.document": "text/markdown",
    "application/vnd.google-apps.presentation": "text/markdown",
    "application/vnd.google-apps.spreadsheet": "text/markdown",
}

PANDOC_CONVERTIBLE_MIMETYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/vnd.oasis.opendocument.text",  # .odt
    "application/rtf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
}

PASSTHROUGH_PREFIXES = ("application/pdf", "image/")


def _download_to_buffer(request) -> bytes:
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk(num_retries=3)
    return buffer.getvalue()


def resolve_attachment(drive_service, drive_file_id: str, dest_dir: Path) -> Path | None:
    """Fetches file metadata, then resolves the file into dest_dir based on
    its mimeType. Returns the written path, or None if the mimeType is
    unsupported (not a failure — caller notes it and moves on). Raises on
    any real Drive/Pandoc error — caller treats that as a fail-fast abort."""
    metadata = (
        drive_service.files()
        .get(fileId=drive_file_id, fields="name,mimeType")
        .execute(num_retries=3)
    )
    name = metadata["name"]
    mime_type = metadata["mimeType"]

    if mime_type in NATIVE_EXPORT_MIMETYPES:
        export_mime = NATIVE_EXPORT_MIMETYPES[mime_type]
        content = _download_to_buffer(
            drive_service.files().export(fileId=drive_file_id, mimeType=export_mime)
        )
        dest_path = dest_dir / f"{slugify(name)}.md"
        dest_path.write_bytes(content)
        return dest_path

    if mime_type in PANDOC_CONVERTIBLE_MIMETYPES:
        content = _download_to_buffer(drive_service.files().get_media(fileId=drive_file_id))
        suffix = Path(name).suffix or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            dest_path = dest_dir / f"{slugify(name)}.md"
            pypandoc.convert_file(tmp_path, "md", outputfile=str(dest_path))
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        return dest_path

    if mime_type.startswith(PASSTHROUGH_PREFIXES):
        content = _download_to_buffer(drive_service.files().get_media(fileId=drive_file_id))
        dest_path = dest_dir / name
        dest_path.write_bytes(content)
        return dest_path

    return None
