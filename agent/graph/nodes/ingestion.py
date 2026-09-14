"""Assignment material ingestion: task-brief + attachment resolution, Pandoc conversion, fail-fast on any parse failure."""

from pathlib import Path

from agent.graph.nodes import drive as drive_node


def build_task_brief(courseWork: dict) -> str:
    """Formats the courseWork title + description into Markdown."""
    title = courseWork.get("title", "")
    description = courseWork.get("description", "")
    lines = [f"# {title}"]
    if description:
        lines.append("")
        lines.append(description)
    return "\n".join(lines) + "\n"


async def ingest_assignment(
    classroom_service, drive_service, course_id: str, coursework_id: str, dest_dir: Path
) -> dict:
    """Assembles source-material/ for one assignment. Stops on the first
    real failure (fail-fast, per spec §0.1) — never drafts from partial
    materials. Returns:
      {"success": True, "ingested_files": [...], "unsupported_files": [...]}
      or
      {"success": False, "failed_file": <name or None>, "error": <str>}
    """
    try:
        courseWork = (
            classroom_service.courses()
            .courseWork()
            .get(courseId=course_id, id=coursework_id)
            .execute(num_retries=3)
        )
    except Exception as e:  # noqa: BLE001 - Classroom HttpError or transport error
        return {"success": False, "failed_file": None, "error": str(e)}

    source_material_dir = dest_dir / "source-material"
    source_material_dir.mkdir(parents=True, exist_ok=True)
    (source_material_dir / "task-brief.md").write_text(build_task_brief(courseWork))

    ingested_files: list[str] = []
    unsupported_files: list[str] = []

    for material in courseWork.get("materials", []):
        if "driveFile" in material:
            drive_file = material["driveFile"].get("driveFile", {})
            file_id = drive_file.get("id")
            file_name = drive_file.get("title", file_id)
            try:
                resolved_path = drive_node.resolve_attachment(
                    drive_service, file_id, source_material_dir
                )
            except Exception as e:  # noqa: BLE001 - Drive HttpError or Pandoc conversion error
                return {"success": False, "failed_file": file_name, "error": str(e)}

            if resolved_path is None:
                unsupported_files.append(file_name)
            else:
                ingested_files.append(file_name)
        elif "link" in material:
            unsupported_files.append(material["link"].get("url", "link"))
        elif "youtubeVideo" in material:
            unsupported_files.append(material["youtubeVideo"].get("title", "YouTube video"))
        elif "form" in material:
            unsupported_files.append(material["form"].get("title", "form"))

    return {"success": True, "ingested_files": ingested_files, "unsupported_files": unsupported_files}
