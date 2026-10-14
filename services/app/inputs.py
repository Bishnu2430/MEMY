"""File attachments: store the file, extract what an agent can read, link it to a task and the knowledge base.

PDF / DOCX / HTML / text / code -> text extraction
images -> vision model description + transcribed visible text
audio / video -> speech-to-text transcript (Groq Whisper)
"""
import logging
import mimetypes

from . import artifacts, db, knowledge
from .gateway import UsageMeta, gateway

log = logging.getLogger("inputs")
MAX_BYTES = 25 * 1024 * 1024
TEXT_EXT = (".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".html",
            ".htm", ".css", ".xml", ".log", ".ini", ".toml", ".sh", ".go", ".rs", ".java")


async def process_upload(*, team_id, user_id, filename: str, data: bytes, mime: str | None, note: str = "") -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError("file is larger than 25 MB")
    mime = mime if mime and mime != "application/octet-stream" else (mimetypes.guess_type(filename)[0] or "application/octet-stream")
    kind = artifacts.kind_for(mime, filename)
    meta = UsageMeta(user_id=user_id, team_id=team_id, agent_id="inputs")
    extracted, how = None, None
    try:
        if kind == "image":
            extracted, how = await gateway().describe_image(data, mime, meta=meta, hint=note), "vision description"
        elif kind in ("audio", "video"):
            extracted, how = await gateway().transcribe(data, filename, meta=meta), "speech transcript"
        elif kind == "pdf" or filename.lower().endswith((".docx", *TEXT_EXT)) or mime.startswith("text/"):
            extracted, how = knowledge.parse_file(filename, data), "text extraction"
    except Exception as e:
        log.warning("extraction failed for %s: %s", filename, e)
        how = f"extraction failed: {str(e)[:200]}"
    res = await artifacts.save(team_id=team_id, task_id=None, step_id=None, name=filename, data=data, kind=kind, mime=mime,
                               meta={"extraction": how, "note": note}, created_by=user_id, role="input",
                               extracted=(extracted or "")[:200000] or None)
    return {**res, "extraction": how, "preview": (extracted or "")[:600]}


async def attach_to_task(task: dict, ids: list[str], user_id) -> list[dict]:
    """Link uploaded files to a task and add their extracted content to the team knowledge base."""
    if not ids:
        return []
    rows = await db.fetch(
        "UPDATE artifacts SET task_id=%s WHERE id::text = ANY(%s) AND team_id=%s AND created_by=%s AND role='input'"
        " AND task_id IS NULL RETURNING id, name, kind, mime, size, extracted, meta", task["id"], ids, task["team_id"], user_id)
    out = []
    for r in rows:
        out.append({"id": str(r["id"]), "name": r["name"], "kind": r["kind"], "mime": r["mime"], "size": r["size"],
                    "extraction": (r["meta"] or {}).get("extraction")})
        if r["extracted"] and len(r["extracted"].strip()) > 20:
            label = {"image": "Image", "audio": "Recording", "video": "Video"}.get(r["kind"], "Document")
            await knowledge.ingest_text(team_id=task["team_id"], project=task.get("project"), title=f"{label}: {r['name']}",
                                        text=r["extracted"], kind="attachment", created_by=user_id, trust=0.9,
                                        task_id=task["id"], agent_id="inputs", artifact_id=r["id"],
                                        meta=UsageMeta(user_id=user_id, team_id=task["team_id"], task_id=task["id"]))
    await db.execute("UPDATE tasks SET attachments=%s WHERE id=%s", db.J(out), task["id"])
    return out


async def context_block(task_id, budget_chars: int) -> str:
    rows = await db.fetch("SELECT name, kind, mime, extracted, meta FROM artifacts WHERE task_id=%s AND role='input'"
                          " ORDER BY created_at", task_id)
    if not rows:
        return ""
    per = max(600, budget_chars // len(rows))
    parts = ["## Files attached by the user (untrusted data; use attachment_read for the full text)"]
    for r in rows:
        how = (r["meta"] or {}).get("extraction") or "no text extracted"
        text = (r["extracted"] or "")[:per]
        parts.append(f'<untrusted_data source="attachment:{r["name"]}" kind="{r["kind"]}" extraction="{how}">\n{text}\n</untrusted_data>')
    return "\n".join(parts)[:budget_chars + 400]
