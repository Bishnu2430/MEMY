import hashlib
import mimetypes
import re
from pathlib import Path

from . import db
from .config import settings

KIND_MIME = {"code": "text/x-python", "markdown": "text/markdown", "diagram": "image/svg+xml",
             "mermaid": "text/plain", "audio": "audio/mpeg", "text": "text/plain", "json": "application/json"}


async def save(*, team_id, task_id, step_id, name: str, data: bytes | str, kind: str, mime: str | None = None) -> dict:
    if isinstance(data, str):
        data = data.encode()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "artifact"
    h = hashlib.sha256(data).hexdigest()
    d = Path(settings.artifact_dir) / str(team_id) / str(task_id or "none")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{h[:12]}-{safe}"
    path.write_bytes(data)
    mime = mime or KIND_MIME.get(kind) or mimetypes.guess_type(safe)[0] or "application/octet-stream"
    aid = await db.fetchval(
        "INSERT INTO artifacts(task_id, step_id, team_id, name, kind, mime, storage_path, content_hash, size)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        task_id, step_id, team_id, safe, kind, mime, str(path), h, len(data))
    return {"artifact_id": str(aid), "name": safe, "kind": kind, "size": len(data)}


async def read(artifact_id) -> tuple[dict, bytes] | None:
    row = await db.fetchrow("SELECT * FROM artifacts WHERE id=%s", artifact_id)
    if not row:
        return None
    return row, Path(row["storage_path"]).read_bytes()


async def for_step(step_id) -> list[dict]:
    return await db.fetch("SELECT id, name, kind, mime, size, storage_path FROM artifacts WHERE step_id=%s ORDER BY created_at",
                          step_id)
