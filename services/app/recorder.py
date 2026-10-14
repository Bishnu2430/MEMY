"""Records what agents did into the team knowledge base, so later tasks (and people) can find it.

- After every accepted step: a deterministic step record (instruction, outcome, artifacts, checks).
- After every completed task: a distilled entry (summary, facts, decisions, open questions) written by a model.
Each write emits a `knowledge_stored` event, shown live in the task trace.
"""
import logging

from . import db, knowledge
from .gateway import UsageMeta, gateway

log = logging.getLogger("recorder")

DISTILL_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "facts": {"type": "array", "items": {"type": "string"}},
    "decisions": {"type": "array", "items": {"type": "string"}},
    "open_questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "facts", "decisions", "open_questions"]}


async def record_step(task: dict, step: dict):
    arts = await db.fetch("SELECT id, name, kind FROM artifacts WHERE step_id=%s AND role='output' ORDER BY created_at",
                          step["id"])
    checks = (step.get("verification") or {}).get("checks") or []
    text = "\n".join([
        f"# {task['title']} — step {step['key']}: {step['title']}",
        f"Agent: @{step['agent_id']} (v{step.get('agent_version')}) · attempts: {step['attempts'] + 1}",
        f"Requested by task: {task['request'][:600]}",
        "", "## Instruction", step["instruction"][:1500],
        "", "## Outcome", (step.get("output") or "")[:4000],
        "", "## Artifacts", *([f"- {a['name']} ({a['kind']}, id {a['id']})" for a in arts] or ["- none"]),
        "", "## Checks", *([f"- {'pass' if c['ok'] else 'fail'}: {c['name']}" for c in checks] or ["- none"]),
    ])
    try:
        await knowledge.ingest_text(team_id=task["team_id"], project=task.get("project"),
                                    title=f"{task['title'][:80]} · {step['key']} by @{step['agent_id']}", text=text,
                                    kind="agent_record", created_by=task["created_by"], trust=0.75,
                                    task_id=task["id"], step_id=step["id"], agent_id=step["agent_id"],
                                    meta=UsageMeta(user_id=task["created_by"], team_id=task["team_id"], task_id=task["id"]))
    except Exception as e:
        log.warning("step record failed: %s", e)


async def record_task(task: dict, steps: list[dict], result: str, on_wait=None):
    meta = UsageMeta(user_id=task["created_by"], team_id=task["team_id"], agent_id="recorder", task_id=task["id"])
    body = "\n\n".join(f"[{s['key']} @{s['agent_id']}] {(s.get('output') or '')[:2500]}" for s in steps)
    try:
        res = await gateway().chat("bulk", [
            {"role": "system", "content": "You maintain a team knowledge base. Distil what this finished task established "
             "so a teammate can reuse it later. Facts must be concrete and self-contained (names, numbers, file names, "
             "endpoints). Decisions are choices that were made and why. Keep each item to one sentence."},
            {"role": "user", "content": f"Task request: {task['request'][:1500]}\n\nStep outputs:\n{body[:9000]}"}],
            json_schema=DISTILL_SCHEMA, meta=meta, max_tokens=1500, temperature=0.1, on_wait=on_wait)
        d = res.parsed or {}
    except Exception as e:
        log.warning("distillation failed, storing the raw result: %s", e)
        d = {"summary": result[:1500], "facts": [], "decisions": [], "open_questions": []}

    def section(title, items):
        return [f"## {title}", *[f"- {i}" for i in items]] if items else []
    text = "\n".join([f"# {task['title']}", "", d.get("summary", ""), "",
                      *section("Facts", d.get("facts")), *section("Decisions", d.get("decisions")),
                      *section("Open questions", d.get("open_questions")),
                      "", f"Agents: {', '.join(sorted({'@' + s['agent_id'] for s in steps}))}"])
    try:
        await knowledge.ingest_text(team_id=task["team_id"], project=task.get("project"), title=f"Task knowledge: {task['title'][:90]}",
                                    text=text, kind="task_knowledge", created_by=task["created_by"], trust=0.8,
                                    task_id=task["id"], agent_id="recorder", meta=meta)
    except Exception as e:
        log.warning("task knowledge ingest failed: %s", e)
