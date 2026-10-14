"""Routing: explicit @agent mention, or two-stage auto routing (embedding shortlist -> LLM pick)."""
import re

from . import db
from .agents import list_agents
from .config import settings
from .gateway import UsageMeta, gateway

MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_-]{1,40})\b")


def parse_mention(text: str) -> str | None:
    m = MENTION.search(text or "")
    return m.group(1) if m else None


async def invocable_agents(team_id) -> list[dict]:
    paused = {r["scope"][6:] for r in await db.fetch("SELECT scope FROM controls WHERE paused AND scope LIKE 'agent:%%'")}
    return [a for a in await list_agents([team_id]) if a["id"] not in paused]


async def shortlist(text: str, team_id, k: int = 3, meta: UsageMeta | None = None) -> list[dict]:
    agents = await invocable_agents(team_id)
    ids = [a["id"] for a in agents]
    sims: dict[str, float] = {}
    try:
        q = (await gateway().embed([text], kind="query", meta=meta))[0]
        vec = "[" + ",".join(f"{x:.6f}" for x in q) + "]"
        for r in await db.fetch("SELECT agent_id, 1 - (embedding <=> %s::vector) AS sim FROM agent_cards"
                                " WHERE agent_id = ANY(%s) AND embedding IS NOT NULL", vec, ids):
            sims[r["agent_id"]] = float(r["sim"])
    except Exception:
        pass
    words = set(re.findall(r"[a-z]{3,}", text.lower()))
    for a in agents:  # small lexical boost on mastery tags and names
        tags = set(" ".join(a.get("mastery") or []).lower().replace("-", " ").split()) | {a["id"]}
        sims[a["id"]] = sims.get(a["id"], 0.0) + 0.03 * len(words & tags)
    ranked = sorted(agents, key=lambda a: sims.get(a["id"], 0), reverse=True)[:k]
    catch_all = next((a for a in agents if a["id"] == "generalist"), None)
    if catch_all and catch_all not in ranked:
        ranked.append(catch_all)  # general questions always have a home
    return [{"id": a["id"], "name": a["name"], "description": a["description"], "mastery": a.get("mastery"),
             "examples": (a.get("examples") or [])[:3], "similarity": round(sims.get(a["id"], 0), 4)} for a in ranked]


async def route(text: str, team_id, meta: UsageMeta | None = None) -> dict:
    sl = await shortlist(text, team_id, meta=meta)
    if not sl:
        return {"mode": "none", "agent_id": None, "confidence": 0, "reason": "no invocable agents", "shortlist": []}
    all_ids = [a["id"] for a in await invocable_agents(team_id)]
    schema = {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["single", "split"]},
        "agent_id": {"type": "string", "enum": [a["id"] for a in sl]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"}}, "required": ["mode", "agent_id", "confidence", "reason"]}
    cards = "\n".join(f"- id={a['id']}: {a['description']} | mastery: {', '.join(a['mastery'] or [])} | e.g. "
                      f"{' / '.join(a['examples'])}" for a in sl)
    prompt = (f"User request:\n<request>\n{text[:3000]}\n</request>\n\nCandidate agents:\n{cards}\n\n"
              "Pick the single best agent. Use mode='split' only if the request clearly asks for several different "
              "deliverables that need different specialists (for example research AND code AND a diagram). "
              "confidence is 0..1: how sure you are this agent is the right one.")
    try:
        res = await gateway().chat("router", [
            {"role": "system", "content": "You are a precise request router for a team of AI agents. Answer in JSON."},
            {"role": "user", "content": prompt}], json_schema=schema, meta=meta, max_tokens=1200, temperature=0)
        d = res.parsed or {}
    except Exception as e:
        d = {"mode": "single", "agent_id": sl[0]["id"], "confidence": 0.4, "reason": f"router model failed: {e}"}
    if d.get("agent_id") not in all_ids:
        d["agent_id"] = sl[0]["id"]
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    close = len(sl) > 1 and sl[0]["similarity"] - sl[1]["similarity"] < 0.01
    ask = d.get("mode") != "split" and (conf < settings.router_threshold or (close and conf < 0.75))
    return {"mode": "ask" if ask else d.get("mode", "single"), "agent_id": d["agent_id"], "confidence": conf,
            "reason": d.get("reason", ""), "shortlist": sl}


async def pick_alternative(title: str, instruction: str, team_id, exclude: list[str], meta: UsageMeta | None = None) -> dict:
    """Reassignment target: another agent that can genuinely do this step, or none (then escalate)."""
    cands = [a for a in await shortlist(f"{title}. {instruction}", team_id, k=6, meta=meta) if a["id"] not in exclude]
    if not cands:
        return {"agent_id": None, "reason": "no other agents"}
    schema = {"type": "object", "properties": {
        "agent_id": {"type": "string", "enum": [a["id"] for a in cands] + ["none"]},
        "reason": {"type": "string"}}, "required": ["agent_id", "reason"]}
    cards = "\n".join(f"- id={a['id']}: {a['description']} | mastery: {', '.join(a['mastery'] or [])}" for a in cands)
    try:
        res = await gateway().chat("router", [
            {"role": "system", "content": "A step failed with its first agent. Choose another agent ONLY if it is genuinely "
             "capable of this exact work; otherwise answer none so a person takes over."},
            {"role": "user", "content": f"Step: {title}\n{instruction[:2000]}\n\nCandidates:\n{cards}"}],
            json_schema=schema, meta=meta, max_tokens=800, temperature=0)
        d = res.parsed or {}
    except Exception as e:
        return {"agent_id": None, "reason": f"router failed: {e}"}
    if d.get("agent_id") in (None, "none"):
        return {"agent_id": None, "reason": d.get("reason", "")}
    return d
