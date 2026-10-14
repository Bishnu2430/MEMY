"""LLM supervisor: plans the ledger, reviews step output against acceptance criteria and writes
corrections. Scheduling, retries and budgets live in the orchestrator as plain code."""
import json
import re

import httpx

from . import artifacts, db
from .agents import get_spec
from .config import settings
from .gateway import UsageMeta, gateway
from .router import invocable_agents
from .tasks import files_note
from .tools import render_mermaid


async def plan(task: dict, hint: dict | None = None, on_wait=None) -> dict:
    agents = await invocable_agents(task["team_id"])
    ids = [a["id"] for a in agents]
    catalog = "\n".join(f"- {a['id']}: {a['description']} (mastery: {', '.join(a.get('mastery') or [])})" for a in agents)
    schema = {"type": "object", "properties": {
        "goal": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "key": {"type": "string"}, "title": {"type": "string"}, "instruction": {"type": "string"},
            "agent_id": {"type": "string", "enum": ids},
            "depends_on": {"type": "array", "items": {"type": "string"}}},
            "required": ["key", "title", "instruction", "agent_id", "depends_on"]}}},
        "required": ["goal", "acceptance_criteria", "steps"]}
    meta = UsageMeta(user_id=task["created_by"], team_id=task["team_id"], agent_id="supervisor", task_id=task["id"])
    res = await gateway().chat("supervisor", [
        {"role": "system", "content": "You are the supervisor of a team of AI agents. You turn a request into a task "
         "ledger: a goal, 2-4 checkable acceptance criteria and the minimum number of steps (at most 4). Each step is "
         "done by exactly one agent and one agent normally gets one step (e.g. the coder writes AND tests the code in a "
         "single step). Never add separate review, testing or verification steps: every step is verified automatically. A step can read the outputs of the steps listed in its depends_on; steps without "
         "dependencies run in parallel. Keys are s1, s2, ... Instructions must be self-contained."},
        {"role": "user", "content": f"Agents:\n{catalog}\n\nRequest:\n<request>\n{task['request']}{files_note(task)}\n</request>"
         + (f"\n\nRouter hint: {json.dumps(hint)}" if hint else "")}],
        json_schema=schema, meta=meta, max_tokens=3500, temperature=0.1, on_wait=on_wait)
    p = res.parsed or {}
    raw = (p.get("steps") or [])[:4]
    keymap = {(s.get("key") or f"s{i + 1}"): f"s{i + 1}" for i, s in enumerate(raw)}
    steps = []
    for i, s in enumerate(raw):
        deps = [keymap[d] for d in (s.get("depends_on") or []) if d in keymap and int(keymap[d][1:]) < i + 1]
        steps.append({"key": f"s{i + 1}", "title": (s.get("title") or f"Step {i + 1}")[:200],
                      "instruction": s.get("instruction") or task["request"],
                      "agent_id": s.get("agent_id") if s.get("agent_id") in ids else ids[0],
                      "depends_on": sorted(set(deps))})
    if not steps:
        steps = [{"key": "s1", "title": "Handle the request", "instruction": task["request"],
                  "agent_id": (hint or {}).get("agent_id") or ids[0], "depends_on": []}]
    return {"goal": p.get("goal") or task["request"][:300],
            "acceptance": [c for c in (p.get("acceptance_criteria") or [])][:5] or ["The request is fully answered"],
            "steps": steps}


# ---------------- deterministic verification ----------------
CODE_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.S)
URL = re.compile(r"https?://[^\s)\]>\"'`]+")


def norm_url(u: str) -> str:
    u = u.strip().rstrip(".,;:!?")
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"\.git$", "", u)
    u = re.sub(r"^https?://(www\.)?", "", u.lower())
    return u.rstrip("/")


async def verify(task: dict, step: dict, spec: dict) -> dict:
    profile = spec.get("verification", "light")
    out = step.get("output") or ""
    arts = await artifacts.for_step(step["id"])
    result = {"profile": profile, "passed": True, "checks": []}

    def check(name, ok, detail=""):
        result["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:1500]})
        if not ok:
            result["passed"] = False

    if profile == "none":
        return result
    check("non_empty_output", len(out.strip()) > 0 and not out.startswith("(the agent returned an empty answer)"))

    project_files = {}
    if profile in ("code-default", "strict") and task.get("project"):
        from .tools import workspace_files
        project_files = workspace_files(task["team_id"], task.get("project"))
    if profile in ("code-default", "strict") and any(re.search(r"(^|/)test_[^/]*\.py$", p) for p in project_files):
        # The project has its own test suite: run it exactly as a developer would.
        from .tools import sandbox_check
        r = await sandbox_check(project_files)
        result["sandbox"] = r
        check("static_errors", not r.get("ruff", {}).get("errors"), r.get("ruff", {}).get("errors"))
        check("security_high", not [i for i in r.get("bandit", {}).get("issues", []) if i.get("severity") == "HIGH"], r.get("bandit"))
        check("tests", r.get("tests", {}).get("passed", False), r.get("tests", {}).get("output", ""))
    elif profile in ("code-default", "strict"):
        code, tests = None, None
        for a in arts:
            if a["kind"] == "code":
                body = open(a["storage_path"]).read()
                if a["name"].startswith("test_") or "_test_" in a["name"] or a["name"].endswith("_test.py"):
                    tests = body
                else:
                    code = body
        if code is None:
            blocks = CODE_BLOCK.findall(out)
            code = next((b for b in blocks if "def test_" not in b), None)
            tests = tests or next((b for b in blocks if "def test_" in b), None)
        if not code:
            check("code_present", False, "no Python code found in artifacts or answer")
        else:
            files = {"main.py": code}
            if tests:
                files["test_main.py"] = tests if "import main" in tests or "from main" in tests else "from main import *\n" + tests
            try:
                async with httpx.AsyncClient(timeout=120) as c:
                    r = (await c.post(f"{settings.sandbox_url}/check", json={"files": files, "timeout": 30})).json()
            except Exception as e:
                r = {"error": str(e)}
            result["sandbox"] = r
            if "error" in r:
                check("sandbox", False, r["error"])
            else:
                check("static_errors", not r.get("ruff", {}).get("errors"), r.get("ruff", {}).get("errors"))
                check("security_high", not [i for i in r.get("bandit", {}).get("issues", []) if i.get("severity") == "HIGH"],
                      r.get("bandit"))
                if tests:
                    check("tests", r.get("tests", {}).get("passed", False), r.get("tests", {}).get("output", ""))
                else:
                    check("runs", r.get("run", {}).get("exit_code", 1) == 0, r.get("run", {}).get("stderr", ""))

    if profile == "research":
        prose = re.sub(r"```.*?```|`[^`]*`", " ", out, flags=re.S)  # URLs inside code are not citations
        urls = {norm_url(u) for u in URL.findall(prose)}
        cites = re.findall(r"\[S\d+\]", prose)
        check("has_citations", bool(urls or cites), "answer must cite stored sources ([S1] or URLs)")
        if urls:
            stored = {norm_url(r["url"]) for r in await db.fetch(
                "SELECT url FROM sources WHERE team_id=%s AND url IS NOT NULL AND status='live'", task["team_id"])}
            missing = [u for u in urls if not any(u == s or u.startswith(s + "/") or s.startswith(u + "/") for s in stored)]
            check("citations_stored", not missing, f"cited but not stored/live: {missing[:5]}")

    if profile == "diagram":
        has_svg = any(a["kind"] == "diagram" for a in arts)
        if not has_svg:
            m = MERMAID_BLOCK.search(out)
            if m:
                r = await render_mermaid(m.group(1))
                if r.get("ok"):
                    await artifacts.save(team_id=task["team_id"], task_id=task["id"], step_id=step["id"],
                                         name="diagram.svg", data=r["svg"], kind="diagram")
                    has_svg = True
                else:
                    check("mermaid_compiles", False, r.get("error"))
        check("diagram_rendered", has_svg, "no compiled diagram artifact")

    if profile == "media":
        has_media = any(a["kind"] in ("audio", "image", "video", "diagram") for a in arts)
        blocked = await db.fetchval(
            "SELECT count(*) FROM events WHERE step_id=%s AND type='tool_result' AND payload->>'result' LIKE %s",
            step["id"], "%PAID_PLAN_REQUIRED%")
        check("media_produced", has_media or blocked,
              "no audio/image/video artifact was produced" if not blocked else
              "ElevenLabs feature needs a paid plan; the limitation was reported")
    return result


async def review(task: dict, step: dict, spec: dict, verification: dict, on_wait=None) -> dict:
    """Second-model review for code and strict profiles; other profiles rely on deterministic checks."""
    if spec.get("verification") not in ("code-default", "strict"):
        return {"verdict": "accept", "issues": [], "notes": "deterministic checks only", "llm": False}
    schema = {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "description": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["severity", "description", "fix"]}},
        "notes": {"type": "string"}}, "required": ["verdict", "issues", "notes"]}
    meta = UsageMeta(user_id=task["created_by"], team_id=task["team_id"], agent_id="verifier", task_id=task["id"],
                     step_id=step["id"])
    res = await gateway().chat("verifier", [
        {"role": "system", "content": "You are a strict code reviewer. Judge the step output against the acceptance "
         "criteria and the automated check results. Only flag real defects. high = wrong behaviour, failing tests or "
         "security hole."},
        {"role": "user", "content": f"Acceptance criteria: {json.dumps(task.get('acceptance') or [])}\n"
         f"Step: {step['title']}\n{step['instruction']}\n\nAutomated checks: {json.dumps(verification.get('checks'))[:3000]}"
         f"\n\nStep output:\n{(step.get('output') or '')[:6000]}"}],
        json_schema=schema, meta=meta, max_tokens=2000, temperature=0, on_wait=on_wait)
    d = res.parsed or {"verdict": "accept", "issues": [], "notes": "review unparseable"}
    d["llm"] = True
    d["model"] = res.alias
    return d


async def synthesize(task: dict, steps: list[dict], on_wait=None) -> str:
    if len(steps) == 1:
        return steps[0].get("output") or ""
    body = "\n\n".join(f"### {s['title']} (@{s['agent_id']})\n{(s.get('output') or '')[:3000]}" for s in steps)
    meta = UsageMeta(user_id=task["created_by"], team_id=task["team_id"], agent_id="supervisor", task_id=task["id"])
    try:
        res = await gateway().chat("supervisor", [
            {"role": "system", "content": "You are the supervisor. Write the final answer for the user from the step "
             "outputs. Keep every citation and artifact mention. Be concise and well structured."},
            {"role": "user", "content": f"Request: {task['request']}\n\nStep outputs:\n{body}"}],
            meta=meta, max_tokens=2500, on_wait=on_wait)
        return res.content.strip() + "\n\n---\n<details><summary>Step outputs</summary>\n\n" + body + "\n</details>"
    except Exception:
        return body


async def step_spec(step: dict) -> dict:
    return await get_spec(step["agent_id"], step["agent_version"]) or {}
