"""Knowledge base: ingestion, chunking, hybrid retrieval (FTS + vector + RRF) under RLS."""
import hashlib
import io
import logging
import re

from . import db
from .events import audit
from .gateway import UsageMeta, gateway

log = logging.getLogger("knowledge")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def chunk_text(text: str, max_chars: int = 2400, overlap: int = 300) -> list[str]:
    """Chunk by structure: split on headings / blank lines / defs, pack to ~600 tokens with overlap."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    blocks = re.split(r"\n(?=#{1,6} )|\n\s*\n|\n(?=def |class |async def )", text)
    chunks, cur = [], ""
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        while len(b) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(b[:max_chars])
            b = b[max_chars - overlap:]
        if len(cur) + len(b) + 2 > max_chars and cur:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + b if overlap else b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    return chunks


def parse_file(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in d.paragraphs)
    if name.endswith((".html", ".htm")):
        import trafilatura
        return trafilatura.extract(data.decode("utf-8", "ignore"), output_format="markdown") or ""
    return data.decode("utf-8", "ignore")


async def ingest_text(*, team_id, title: str, text: str, kind: str = "text", project: str | None = None,
                      url: str | None = None, domain: str | None = None, created_by=None, trust: float = 0.8,
                      status: str = "live", scores: dict | None = None, simhash: int | None = None,
                      meta: UsageMeta | None = None) -> dict:
    h = content_hash(text)
    existing = await db.fetchrow("SELECT id, status FROM sources WHERE team_id=%s AND content_hash=%s", team_id, h)
    if existing:
        return {"source_id": str(existing["id"]), "duplicate": True, "status": existing["status"]}
    chunks = chunk_text(text)
    embeddings = await gateway().embed(chunks, kind="document", meta=meta) if chunks else []
    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT INTO sources(team_id, project, kind, url, domain, title, content_hash, simhash, scores, trust_score,"
            " status, excerpt, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (team_id, project, kind, url, domain, title[:300], h, simhash, db.J(scores or {}), trust, status,
             text[:1200], created_by))
        sid = (await cur.fetchone())["id"]
        for i, (c, e) in enumerate(zip(chunks, embeddings)):
            await conn.execute(
                "INSERT INTO chunks(source_id, team_id, project, ord, content, embedding, embedding_model)"
                " VALUES (%s,%s,%s,%s,%s,%s::vector,%s)",
                (sid, team_id, project, i, c, _vec(e), gateway().embedding_model))
    return {"source_id": str(sid), "chunks": len(chunks), "status": status, "duplicate": False}


def _vec(e: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


async def search(query: str, team_ids: list, *, k: int = 8, project: str | None = None,
                 meta: UsageMeta | None = None) -> list[dict]:
    """Hybrid search inside the caller's team scope (RLS enforced by the app_scoped role)."""
    if not query.strip() or not team_ids:
        return []
    qvec = _vec((await gateway().embed([query], kind="query", meta=meta))[0])
    async with db.scoped(team_ids) as conn:
        await conn.execute("SET LOCAL hnsw.ef_search = 100", prepare=False)
        try:
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order", prepare=False)
        except Exception:
            pass
        proj_sql = " AND (c.project IS NULL OR c.project = %(project)s)" if project else ""
        params = {"q": query, "v": qvec, "project": project}
        cur = await conn.execute(
            "SELECT c.id FROM chunks c JOIN sources s ON s.id=c.source_id WHERE s.status='live'"
            " AND c.tsv @@ websearch_to_tsquery('english', %(q)s)" + proj_sql +
            " ORDER BY ts_rank_cd(c.tsv, websearch_to_tsquery('english', %(q)s)) DESC LIMIT 50", params)
        fts = [r["id"] for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT c.id FROM chunks c JOIN sources s ON s.id=c.source_id WHERE s.status='live'" + proj_sql +
            " ORDER BY c.embedding <=> %(v)s::vector LIMIT 50", params)
        vec = [r["id"] for r in await cur.fetchall()]
        scores: dict = {}
        for ranked in (fts, vec):
            for rank, cid in enumerate(ranked):
                scores[cid] = scores.get(cid, 0) + 1.0 / (60 + rank + 1)
        top = sorted(scores, key=scores.get, reverse=True)[:k]
        if not top:
            return []
        cur = await conn.execute(
            "SELECT c.id, c.content, c.source_id, s.title, s.url, s.trust_score, s.kind FROM chunks c"
            " JOIN sources s ON s.id=c.source_id WHERE c.id = ANY(%s)", (top,))
        rows = {r["id"]: r for r in await cur.fetchall()}
    out = []
    for cid in top:
        r = rows.get(cid)
        if r:
            out.append({"chunk_id": str(cid), "source_id": str(r["source_id"]), "title": r["title"], "url": r["url"],
                        "trust": round(float(r["trust_score"]), 2), "kind": r["kind"], "content": r["content"],
                        "score": round(scores[cid], 4)})
    return out


def wrap_untrusted(results: list[dict], max_chars: int) -> str:
    """Retrieved content is data, never instructions."""
    parts, used = [], 0
    for i, r in enumerate(results, 1):
        body = r["content"]
        block = (f'<untrusted_data id="S{i}" source="{r.get("url") or r["title"]}" source_id="{r["source_id"]}" '
                 f'trust="{r["trust"]}">\n{body}\n</untrusted_data>')
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


async def propose_memory(*, team_id, agent_id, task_id, fact: str, source_url: str | None) -> str:
    pid = await db.fetchval(
        "INSERT INTO memory_proposals(team_id, agent_id, task_id, fact, source_url) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        team_id, agent_id, task_id, fact[:2000], source_url)
    return str(pid)


async def decide_memory(proposal_id, decision: str, user_id) -> dict:
    p = await db.fetchrow("SELECT * FROM memory_proposals WHERE id=%s AND status='pending'", proposal_id)
    if not p:
        return {"ok": False, "error": "not pending"}
    status = "approved" if decision == "approve" else "rejected"
    await db.execute("UPDATE memory_proposals SET status=%s, reviewed_by=%s, reviewed_at=now() WHERE id=%s",
                     status, user_id, proposal_id)
    result = {"ok": True, "status": status}
    if status == "approved":
        result.update(await ingest_text(team_id=p["team_id"], title=f"Memory: {p['fact'][:80]}", text=p["fact"],
                                        kind="memory", url=p["source_url"], created_by=user_id, trust=0.85))
    await audit(user_id, f"memory.{status}", str(proposal_id))
    return result
