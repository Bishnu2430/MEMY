"""Tool gateway: every built-in and MCP call passes here for grants, schema validation,
approval policy, loop detection, metering and the event log."""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import jsonschema

from .. import artifacts, db, knowledge, scraping
from ..agents import load_skill
from ..config import settings
from ..events import emit
from ..gateway import UsageMeta, record_tool_usage
from . import mcp_client

log = logging.getLogger("tools")


@dataclass
class ToolContext:
    task: dict
    step: dict
    spec: dict
    meta: UsageMeta
    diagram_failures: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def team_id(self):
        return self.task["team_id"]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[ToolContext, dict], Awaitable[str]]
    side_effect: bool = False
    untrusted_output: bool = False


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# ---------------- built-in tool handlers ----------------
async def _knowledge_search(ctx: ToolContext, a: dict) -> str:
    res = await knowledge.search(a["query"], [ctx.team_id], k=min(int(a.get("k", 4)), 6),
                                 project=ctx.task.get("project"), meta=ctx.meta)
    if not res:
        return "No matching knowledge found in your team's scope."
    return knowledge.wrap_untrusted(res, 5000)


async def _knowledge_remember(ctx: ToolContext, a: dict) -> str:
    pid = await knowledge.propose_memory(team_id=ctx.team_id, agent_id=ctx.spec["id"], task_id=ctx.task["id"],
                                         fact=a["fact"], source_url=a.get("source_url"))
    return f"Memory proposal {pid} recorded as pending; a supervisor or person must approve it before it is shared."


async def _web_fetch(ctx: ToolContext, a: dict) -> str:
    async def progress(msg: str):
        await emit("tool_progress", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
                   payload={"tool": "web.fetch", "message": msg})
    res = await scraping.fetch_and_store(a["url"], team_id=ctx.team_id, purpose=a.get("purpose", ctx.step["instruction"]),
                                         project=ctx.task.get("project"), user_id=ctx.task["created_by"], meta=ctx.meta,
                                         progress=progress)
    if not res.get("ok"):
        return f"FETCH FAILED: {res.get('error')}"
    head = (f"Fetched '{res.get('title')}' status={res.get('status', 'existing')} trust={res.get('trust')} "
            f"source_id={res.get('source_id') or res.get('near_duplicate_of')}")
    if res.get("status") == "quarantined":
        return head + "\nThis page failed the quality filter and was quarantined; do not rely on it."
    return head + f'\n<untrusted_data source="{a["url"]}">\n{res.get("content", "")[:3500]}\n</untrusted_data>'


async def _sandbox_run(ctx: ToolContext, a: dict) -> str:
    files = {"main.py": a["code"]}
    if a.get("tests"):
        files["test_main.py"] = a["tests"]
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{settings.sandbox_url}/check" if a.get("tests") else f"{settings.sandbox_url}/run",
                         json={"files": files, "timeout": 30})
    data = r.json()
    ctx.extra.setdefault("sandbox_runs", []).append(data)
    return json.dumps(data)[:6000]


async def _artifact_save(ctx: ToolContext, a: dict) -> str:
    kind = a.get("kind") or ("code" if a["name"].endswith(".py") else "markdown")
    res = await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"], name=a["name"],
                               data=a["content"], kind=kind)
    await emit("artifact_saved", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"], payload=res)
    return f"Saved artifact {res['name']} ({res['size']} bytes) id={res['artifact_id']}"


async def render_mermaid(source: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{settings.renderer_url}/render", json={"source": source})
        return r.json()
    except Exception as e:
        return {"ok": False, "error": f"renderer unavailable: {e}"}


async def _diagram_render(ctx: ToolContext, a: dict) -> str:
    if ctx.diagram_failures >= 3:
        return "Diagram repair budget exhausted (3 attempts). Report the last error in your answer."
    res = await render_mermaid(a["source"])
    if not res.get("ok"):
        ctx.diagram_failures += 1
        return f"MERMAID PARSE ERROR (attempt {ctx.diagram_failures}/3): {res.get('error', '')[:1500]}\nFix the source and call diagram_render again."
    name = a.get("name") or "diagram"
    src = await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"],
                               name=f"{name}.mmd", data=a["source"], kind="mermaid")
    svg = await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"],
                               name=f"{name}.svg", data=res["svg"], kind="diagram")
    await emit("artifact_saved", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"], payload=svg)
    return f"Diagram compiled and stored: svg={svg['artifact_id']} source={src['artifact_id']}"


async def _audio_speak(ctx: ToolContext, a: dict) -> str:
    text = a["text"][:2500]
    script = await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"],
                                  name="narration_script.md", data=text, kind="markdown")
    key = settings.elevenlabs_api_key
    if not key:
        row = await db.fetchrow("SELECT ciphertext, nonce FROM credentials WHERE provider='elevenlabs'")
        if row:
            from ..security import decrypt
            key = decrypt(row["ciphertext"], row["nonce"])
    if not key:
        return (f"ElevenLabs is not configured, so no audio was generated. The script was saved as artifact "
                f"{script['artifact_id']}. Say so in your answer.")
    voice = a.get("voice_id") or "JBFqnCBsd6RMkjVDRZzb"
    est_credits = len(text)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                         headers={"xi-api-key": key}, json={"text": text, "model_id": "eleven_multilingual_v2"})
    if r.status_code >= 400:
        return f"ElevenLabs error {r.status_code}: {r.text[:300]}"
    audio = await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"],
                                 name="narration.mp3", data=r.content, kind="audio", mime="audio/mpeg")
    await emit("artifact_saved", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={**audio, "estimated_credits": est_credits, "latency_ms": int((time.time() - t0) * 1000)})
    return f"Audio generated: artifact {audio['artifact_id']} (~{est_credits} credits)."


async def _skill_load(ctx: ToolContext, a: dict) -> str:
    if a["name"] not in ctx.spec.get("skills", []):
        return "Skill not granted to this agent."
    md = await load_skill(a["name"])
    return md or "Skill is disabled (pending admin review of its content)."


async def _ledger_note(ctx: ToolContext, a: dict) -> str:
    await emit("ledger_note", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={"note": a["note"][:1000]})
    return "Noted in the task ledger."


async def _team_post(ctx: ToolContext, a: dict) -> str:
    await emit("team_message", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={"channel": a.get("channel", "general"), "text": a["text"][:2000]})
    return f"Posted to #{a.get('channel', 'general')}."


BUILTINS: dict[str, Tool] = {t.name: t for t in [
    Tool("knowledge.search", "Search the team knowledge base (hybrid full-text + vector). Returns cited chunks.",
         _obj({"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]), _knowledge_search,
         untrusted_output=True),
    Tool("knowledge.remember", "Propose a fact for shared memory. It stays pending until reviewed.",
         _obj({"fact": {"type": "string"}, "source_url": {"type": "string"}}, ["fact"]), _knowledge_remember),
    Tool("web.fetch", "Fetch a public web page, extract its main text, score it for quality and store it with provenance.",
         _obj({"url": {"type": "string"}, "purpose": {"type": "string"}}, ["url"]), _web_fetch, untrusted_output=True),
    Tool("sandbox.run_python", "Run Python code in an isolated sandbox with no network. If tests are given, also runs "
         "ruff, bandit and pytest (code is saved as main.py, tests as test_main.py).",
         _obj({"code": {"type": "string"}, "tests": {"type": "string"}}, ["code"]), _sandbox_run),
    Tool("artifact.save", "Save a file (code, markdown, json) as a task artifact.",
         _obj({"name": {"type": "string"}, "content": {"type": "string"}, "kind": {"type": "string"}},
              ["name", "content"]), _artifact_save),
    Tool("diagram.render", "Compile Mermaid source to SVG. Returns parse errors to fix, up to 3 attempts.",
         _obj({"source": {"type": "string"}, "name": {"type": "string"}}, ["source"]), _diagram_render),
    Tool("audio.speak", "Synthesize speech from text with ElevenLabs and store the audio.",
         _obj({"text": {"type": "string"}, "voice_id": {"type": "string"}}, ["text"]), _audio_speak),
    Tool("skill.load", "Load the full instructions of a granted skill by name.",
         _obj({"name": {"type": "string"}}, ["name"]), _skill_load),
    Tool("ledger.note", "Record a decision or open question in the shared task ledger.",
         _obj({"note": {"type": "string"}}, ["note"]), _ledger_note),
    Tool("team.post_message", "Post a message to a team channel (external side effect).",
         _obj({"text": {"type": "string"}, "channel": {"type": "string"}}, ["text"]), _team_post, side_effect=True),
]}


def fn_name(name: str) -> str:
    return name.replace(".", "__")


def _matches(pattern: str, name: str) -> bool:
    return pattern == name or (pattern.endswith("*") and name.startswith(pattern[:-1]))


@dataclass
class ResolvedTool:
    name: str
    description: str
    schema: dict
    side_effect: bool
    needs_approval: bool
    kind: str  # builtin | mcp
    untrusted_output: bool = False


async def resolve_tools(spec: dict, team_id) -> dict[str, ResolvedTool]:
    """Least privilege: a tool is usable only if the agent spec lists it, it is enabled and
    pinned, and (for MCP) the team holds a grant."""
    out: dict[str, ResolvedTool] = {}
    approval = set(spec.get("approval_required", []))
    grants = await db.fetch("SELECT tool, needs_approval FROM tool_grants WHERE team_id=%s", team_id)
    for name in spec.get("tools", {}).get("builtin", []):
        t = BUILTINS.get(name)
        if not t:
            continue
        g = next((g for g in grants if _matches(g["tool"], name)), None)
        out[fn_name(name)] = ResolvedTool(name, t.description, t.schema, t.side_effect,
                                          name in approval or t.side_effect or bool(g and g["needs_approval"]),
                                          "builtin", t.untrusted_output)
    patterns = spec.get("tools", {}).get("mcp", [])
    if patterns:
        rows = await db.fetch("SELECT t.* FROM mcp_tools t JOIN mcp_servers s ON s.id=t.server_id WHERE s.status='active'"
                              " AND t.status='approved' AND t.pinned_hash=t.current_hash")
        for r in rows:
            name = f"mcp.{r['server_id']}.{r['name']}"
            short = f"{r['server_id']}.{r['name']}"
            if not any(_matches(p, short) or _matches(p, name) for p in patterns):
                continue
            g = next((g for g in grants if _matches(g["tool"], name)), None)
            if not g:
                continue
            out[fn_name(name)] = ResolvedTool(name, r["description"], r["input_schema"] or {"type": "object"},
                                              r["side_effect"], name in approval or short in approval or g["needs_approval"],
                                              "mcp", True)
    return out


def openai_tools(resolved: dict[str, ResolvedTool]) -> list[dict]:
    return [{"type": "function", "function": {"name": fn, "description": t.description[:1000],
                                              "parameters": t.schema}} for fn, t in resolved.items()]


def validate_args(t: ResolvedTool, args: dict) -> tuple[dict, str | None]:
    if not isinstance(args, dict) or "__invalid_json__" in args:
        return {}, "arguments were not valid JSON"
    props = (t.schema or {}).get("properties")
    if props and t.schema.get("additionalProperties") is False:
        args = {k: v for k, v in args.items() if k in props}
    try:
        jsonschema.validate(args, t.schema)
    except jsonschema.ValidationError as e:
        return args, e.message
    return args, None


async def execute(ctx: ToolContext, t: ResolvedTool, args: dict, call_id: str) -> str:
    await emit("tool_call", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={"tool": t.name, "args": args, "call_id": call_id})
    t0 = time.time()
    status = "ok"
    try:
        if t.kind == "builtin":
            result = await BUILTINS[t.name].handler(ctx, args)
        else:
            _, server, tool = t.name.split(".", 2)
            ok, result = await mcp_client.call(server, tool, args)
            status = "ok" if ok else "error"
            if t.untrusted_output:
                result = f'<untrusted_data source="mcp:{server}.{tool}">\n{result}\n</untrusted_data>'
    except Exception as e:
        log.exception("tool %s failed", t.name)
        status, result = "error", f"TOOL ERROR: {type(e).__name__}: {e}"
    latency = int((time.time() - t0) * 1000)
    await record_tool_usage(ctx.meta, t.name, latency, status)
    await emit("tool_result", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={"tool": t.name, "call_id": call_id, "status": status, "latency_ms": latency,
                        "result": result[:2000]})
    return result[:8000]
