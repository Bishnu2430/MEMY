"""Tool gateway: every built-in and MCP call passes here for grants, schema validation,
approval policy, loop detection, metering and the event log."""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import jsonschema

from .. import artifacts, db, knowledge, media, scraping
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
                                         progress=progress, task_id=ctx.task["id"], step_id=ctx.step["id"],
                                         agent_id=ctx.spec["id"])
    if not res.get("ok"):
        return f"FETCH FAILED: {res.get('error')}"
    head = (f"Fetched '{res.get('title')}' status={res.get('status', 'existing')} trust={res.get('trust')} "
            f"source_id={res.get('source_id') or res.get('near_duplicate_of')}")
    if res.get("status") == "quarantined":
        return head + "\nThis page failed the quality filter and was quarantined; do not rely on it."
    return head + f'\n<untrusted_data source="{a["url"]}">\n{res.get("content", "")[:3500]}\n</untrusted_data>'


def workspace_files(team_id, project: str | None) -> dict[str, str]:
    """Source files of a team's project workspace (the same folder the workspace MCP server writes to)."""
    from pathlib import Path
    clean = "".join(c for c in (project or "default") if c.isalnum() or c in "-_") or "default"
    root = Path(settings.artifact_dir) / "workspace" / str(team_id) / clean
    out, total = {}, 0
    if root.exists():
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix in (".py", ".toml", ".cfg", ".ini", ".txt", ".json") and total < 1_500_000:
                text = f.read_text(errors="replace")
                out[str(f.relative_to(root)).replace("\\", "/")] = text
                total += len(text)
    return out


async def sandbox_check(files: dict[str, str]) -> dict:
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{settings.sandbox_url}/check", json={"files": files, "timeout": 90})
    return r.json()


async def _sandbox_project(ctx: ToolContext, a: dict) -> str:
    files = workspace_files(ctx.team_id, ctx.task.get("project"))
    if not any(p.endswith(".py") for p in files):
        return "The project workspace has no Python files yet. Write them with mcp__workspace__write_file first."
    data = await sandbox_check(files)
    ctx.extra.setdefault("sandbox_runs", []).append(data)
    tests = data.get("tests") or {}
    head = (f"Project checks on {sum(p.endswith('.py') for p in files)} Python files: "
            f"syntax errors {len(data.get('ruff', {}).get('errors', []))}, "
            f"high-severity security issues {sum(i.get('severity') == 'HIGH' for i in data.get('bandit', {}).get('issues', []))}, "
            f"tests {'PASSED' if tests.get('passed') else 'FAILED' if tests else 'not found (add tests/test_*.py)'}.")
    return head + "\n" + json.dumps(data)[:5000]


async def _sandbox_run(ctx: ToolContext, a: dict) -> str:
    files = {"main.py": a["code"]}
    if a.get("tests"):
        files["test_main.py"] = a["tests"]
    files.update({k: v for k, v in (a.get("files") or {}).items() if isinstance(v, str)})
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
    note = ""
    if a["name"].endswith(".py"):
        try:
            compile(a["content"], a["name"], "exec")
        except SyntaxError as e:
            note = (f"\nWARNING: {a['name']} has a syntax error at line {e.lineno}: {e.msg}. The file looks incomplete or "
                    "broken — save the COMPLETE corrected file again (do not save partial files).")
    return f"Saved artifact {res['name']} ({res['size']} bytes, {a['content'].count(chr(10)) + 1} lines) id={res['artifact_id']}{note}"


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


async def _progress(ctx: ToolContext, tool: str, msg: str):
    await emit("tool_progress", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={"tool": tool, "message": msg})


async def _store_media(ctx: ToolContext, name: str, data: bytes, kind: str, mime: str, info: dict) -> dict:
    res = await media.store(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"],
                            user_id=ctx.task["created_by"], name=name, data=data, kind=kind, mime=mime, info=info)
    await emit("artifact_saved", task_id=ctx.task["id"], step_id=ctx.step["id"], actor=ctx.spec["id"],
               payload={**res, **{k: v for k, v in info.items() if k != "prompt"}})
    return res


def _media_error(e: Exception, what: str) -> str:
    msg = str(e)
    if "PAID_PLAN_REQUIRED" in msg:
        return (f"{msg} Do not retry {what}. Tell the user this needs a paid ElevenLabs plan"
                + (" and, if a visual is still useful, create an SVG illustration with image_svg instead." if what in ("image", "video") else "."))
    return f"MEDIA ERROR ({what}): {msg}"


async def _audio_speak(ctx: ToolContext, a: dict) -> str:
    text = a["text"][:2500]
    await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"], name="narration_script.md",
                         data=text, kind="markdown")
    await _progress(ctx, "audio.speak", f"synthesising {len(text)} characters (~{len(text)} credits)")
    try:
        data, info = await media.speech(text, a.get("voice"), meta=ctx.meta)
    except media.MediaError as e:
        return _media_error(e, "speech")
    res = await _store_media(ctx, a.get("name") or "narration", data, "audio", "audio/mpeg", info)
    return f"Speech generated with voice {info['voice']}: artifact {res['artifact_id']} ({info['characters']} characters)."


async def _audio_sound(ctx: ToolContext, a: dict) -> str:
    await _progress(ctx, "audio.sound_effect", f"generating sound effect: {a['prompt'][:80]}")
    try:
        data, info = await media.sound_effect(a["prompt"], a.get("duration_seconds"), meta=ctx.meta)
    except media.MediaError as e:
        return _media_error(e, "sound effect")
    res = await _store_media(ctx, a.get("name") or "sound_effect", data, "audio", "audio/mpeg", info)
    return f"Sound effect generated: artifact {res['artifact_id']}."


async def _audio_music(ctx: ToolContext, a: dict) -> str:
    await _progress(ctx, "audio.music", f"composing {a.get('length_seconds', 30)}s of music")
    try:
        data, info = await media.music(a["prompt"], int(a.get("length_seconds", 30)), meta=ctx.meta)
    except media.MediaError as e:
        return _media_error(e, "music")
    res = await _store_media(ctx, a.get("name") or "music", data, "audio", "audio/mpeg", info)
    return f"Music generated: artifact {res['artifact_id']} ({info['length_s']}s)."


async def _audio_transcribe(ctx: ToolContext, a: dict) -> str:
    row = await db.fetchrow("SELECT * FROM artifacts WHERE id::text=%s AND team_id=%s", a["artifact_id"], ctx.team_id)
    if not row or not (row["mime"].startswith("audio/") or row["mime"].startswith("video/")):
        return "No audio or video artifact with that id in this team."
    from pathlib import Path
    data = Path(row["storage_path"]).read_bytes()
    await _progress(ctx, "audio.transcribe", f"transcribing {row['name']}")
    try:
        out = await media.transcribe(data, row["name"], meta=ctx.meta)
    except media.MediaError as e:
        return _media_error(e, "transcription")
    await artifacts.save(team_id=ctx.team_id, task_id=ctx.task["id"], step_id=ctx.step["id"],
                         name=f"{row['name']}.transcript.md", data=out["text"], kind="markdown")
    return f"Transcript ({out.get('language')}):\n{out['text'][:4000]}"


async def _media_voices(ctx: ToolContext, a: dict) -> str:
    try:
        vs = await media.voices()
    except media.MediaError as e:
        return _media_error(e, "voices")
    return "\n".join(f"- {v['name']} ({v.get('labels', {}).get('gender', '')} {v.get('labels', {}).get('accent', '')})".rstrip()
                     for v in vs[:30])


async def _image_generate(ctx: ToolContext, a: dict) -> str:
    async def progress(msg):
        await _progress(ctx, "image.generate", msg)
    try:
        data, mime, info = await media.image(a["prompt"], a.get("model"), a.get("aspect_ratio", "1:1"), progress, ctx.meta)
    except media.MediaError as e:
        return _media_error(e, "image")
    res = await _store_media(ctx, a.get("name") or "image", data, "image", mime, info)
    return f"Image generated: artifact {res['artifact_id']} ({mime})."


async def _video_generate(ctx: ToolContext, a: dict) -> str:
    async def progress(msg):
        await _progress(ctx, "video.generate", msg)
    try:
        data, mime, info = await media.video(a["prompt"], a.get("model"), int(a.get("duration_seconds", 4)),
                                             progress=progress, meta=ctx.meta)
    except media.MediaError as e:
        return _media_error(e, "video")
    res = await _store_media(ctx, a.get("name") or "video", data, "video", mime, info)
    return f"Video generated: artifact {res['artifact_id']} ({mime})."


async def _image_svg(ctx: ToolContext, a: dict) -> str:
    import xml.etree.ElementTree as ET
    svg = a["svg"].strip()
    if "<script" in svg.lower() or "javascript:" in svg.lower() or "onload=" in svg.lower():
        return "SVG REJECTED: scripts and event handlers are not allowed."
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        return f"SVG PARSE ERROR: {e}. Fix the markup and call image_svg again."
    if not root.tag.endswith("svg"):
        return "SVG ERROR: the root element must be <svg>."
    res = await _store_media(ctx, (a.get("name") or "illustration").removesuffix(".svg") + ".svg", svg.encode(), "image",
                             "image/svg+xml", {"source": "svg", "description": a.get("description", "")})
    return f"Illustration saved: artifact {res['artifact_id']} (SVG)."


async def _attachment_read(ctx: ToolContext, a: dict) -> str:
    rows = await db.fetch("SELECT id, name, mime, extracted FROM artifacts WHERE task_id=%s AND role='input' ORDER BY created_at",
                          ctx.task["id"])
    if not rows:
        return "This task has no attached files."
    want = (a.get("name") or "").lower()
    pick = next((r for r in rows if want and (want in r["name"].lower() or want == str(r["id"]))), None)
    if not pick:
        return "Attached files: " + ", ".join(f"{r['name']} ({r['mime']})" for r in rows) + ". Pass one of these names."
    text = pick["extracted"] or "(no text could be extracted from this file)"
    start = int(a.get("offset", 0))
    chunk = text[start:start + 6000]
    more = f"\n[... {len(text) - start - 6000} more chars; call again with offset={start + 6000}]" if len(text) > start + 6000 else ""
    return f'<untrusted_data source="attachment:{pick["name"]}">\n{chunk}\n</untrusted_data>{more}'


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
    Tool("sandbox.run_python", "Run a quick Python snippet in an isolated sandbox with no network. code is saved as "
         "main.py and tests as test_main.py (tests import from main); extra files may be passed as {path: content}. "
         "For project code, prefer sandbox_test_project.",
         _obj({"code": {"type": "string"}, "tests": {"type": "string"}, "files": {"type": "object"}}, ["code"]), _sandbox_run),
    Tool("sandbox.test_project", "Run ruff, bandit and pytest over the whole project workspace (folders kept, so "
         "`from backend.app.main import app` works). Call it after writing files with mcp__workspace__write_file.",
         _obj({}, []), _sandbox_project),
    Tool("artifact.save", "Save a file (code, markdown, json) as a task artifact.",
         _obj({"name": {"type": "string"}, "content": {"type": "string"}, "kind": {"type": "string"}},
              ["name", "content"]), _artifact_save),
    Tool("diagram.render", "Compile Mermaid source to SVG. Returns parse errors to fix, up to 3 attempts.",
         _obj({"source": {"type": "string"}, "name": {"type": "string"}}, ["source"]), _diagram_render),
    Tool("audio.speak", "Text to speech with ElevenLabs; stores a playable MP3. voice is a voice name from media_voices "
         "(default George). Costs ~1 credit per character.",
         _obj({"text": {"type": "string"}, "voice": {"type": "string"}, "name": {"type": "string"}}, ["text"]), _audio_speak),
    Tool("audio.sound_effect", "Generate a sound effect from a description with ElevenLabs (0.5-30 s).",
         _obj({"prompt": {"type": "string"}, "duration_seconds": {"type": "number"}, "name": {"type": "string"}},
              ["prompt"]), _audio_sound),
    Tool("audio.music", "Compose music from a prompt with ElevenLabs Music (3-300 s). Needs a paid ElevenLabs plan.",
         _obj({"prompt": {"type": "string"}, "length_seconds": {"type": "integer"}, "name": {"type": "string"}},
              ["prompt"]), _audio_music),
    Tool("audio.transcribe", "Transcribe an audio/video artifact (e.g. an attached recording) to text with ElevenLabs Scribe.",
         _obj({"artifact_id": {"type": "string"}}, ["artifact_id"]), _audio_transcribe),
    Tool("media.voices", "List available ElevenLabs voices (names to pass to audio_speak).", _obj({}, []), _media_voices),
    Tool("image.generate", "Generate an image from a prompt with the ElevenLabs Image API (needs a Pro plan). "
         "aspect_ratio e.g. 1:1, 16:9, 4:3.",
         _obj({"prompt": {"type": "string"}, "aspect_ratio": {"type": "string"}, "name": {"type": "string"}},
              ["prompt"]), _image_generate),
    Tool("video.generate", "Generate a short video (4-8 s) from a prompt with the ElevenLabs Video API (needs a Pro plan; "
         "expensive, always needs approval).",
         _obj({"prompt": {"type": "string"}, "duration_seconds": {"type": "integer"}, "name": {"type": "string"}},
              ["prompt"]), _video_generate, side_effect=True),
    Tool("image.svg", "Save an SVG illustration you write yourself (logos, icons, simple product art, UI mockups). "
         "Pass complete <svg> markup with a viewBox; no scripts.",
         _obj({"svg": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"}}, ["svg"]), _image_svg),
    Tool("attachment.read", "Read the text extracted from a file the user attached to this task (PDF, doc, image "
         "description, audio transcript). Call without a name to list attachments.",
         _obj({"name": {"type": "string"}, "offset": {"type": "integer"}}, []), _attachment_read, untrusted_output=True),
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


async def resolve_tools(spec: dict, team_id, extra: list[str] | None = None) -> dict[str, ResolvedTool]:
    """Least privilege: a tool is usable only if the agent spec lists it, it is enabled and
    pinned, and (for MCP) the team holds a grant."""
    out: dict[str, ResolvedTool] = {}
    approval = set(spec.get("approval_required", []))
    grants = await db.fetch("SELECT tool, needs_approval FROM tool_grants WHERE team_id=%s", team_id)
    for name in list(dict.fromkeys(spec.get("tools", {}).get("builtin", []) + (extra or []))):
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
            ok, result = await mcp_client.call(server, tool, args, context={
                "team_id": ctx.team_id, "project": ctx.task.get("project") or "default", "task_id": ctx.task["id"]})
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
