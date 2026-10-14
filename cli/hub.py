# /// script
# dependencies = ["httpx", "rich"]
# ///
"""hub — terminal client for the Memy agent platform.

  uv run cli/hub.py login                      # prompts for email/password, stores a token in ~/.memy-hub.json
  uv run cli/hub.py chat "explain RRF"         # auto-routed task, follows the live trace
  uv run cli/hub.py run @coder "fix the failing test"
  uv run cli/hub.py tasks
  uv run cli/hub.py show <task-id>
  uv run cli/hub.py approvals | approve <id> | reject <id>
  uv run cli/hub.py logs --follow              # live event stream for your teams
  uv run cli/hub.py live                       # who is working on what right now
  uv run cli/hub.py teams | team <slug>        # pick the default team
"""
import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

import httpx
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.table import Table

CFG = Path.home() / ".memy-hub.json"
console = Console()


def cfg() -> dict:
    return json.loads(CFG.read_text()) if CFG.exists() else {}


def save(c: dict):
    CFG.write_text(json.dumps(c))


def client() -> httpx.Client:
    c = cfg()
    base = os.environ.get("MEMY_URL", c.get("url", "http://localhost:8080/api"))
    h = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
    return httpx.Client(base_url=base, headers=h, timeout=60)


def call(method: str, path: str, **kw):
    r = client().request(method, path, **kw)
    if r.status_code == 401:
        console.print("[red]Not logged in. Run: hub login[/red]")
        sys.exit(1)
    if r.status_code >= 400:
        console.print(f"[red]{r.status_code}: {r.text}[/red]")
        sys.exit(1)
    return r.json()


def describe(e: dict) -> str:
    p = e.get("payload") or {}
    t = e["type"]
    table = {
        "llm_start": lambda: f"thinking with {p.get('model')} ({p.get('model_name')}) turn {p.get('turn')}",
        "llm_turn": lambda: ("calls " + ", ".join(c["name"] for c in p.get("tool_calls", []))) if p.get("tool_calls")
        else f"answered ({p.get('tokens_out')} tok)",
        "tool_call": lambda: f"{p.get('tool')}({json.dumps(p.get('args'))[:80]})",
        "tool_result": lambda: f"{p.get('tool')} -> {p.get('status')} {p.get('latency_ms')}ms",
        "routing_decision": lambda: f"routed {p.get('mode')} -> @{p.get('agent_id')} ({p.get('confidence')})",
        "plan_created": lambda: f"plan: " + " | ".join(f"{s['key']} @{s['agent_id']} {s['title']}" for s in p.get("steps", [])),
        "approval_requested": lambda: f"[yellow]APPROVAL NEEDED {p.get('tool')} {json.dumps(p.get('args'))} id={p.get('approval_id')}[/yellow]",
        "verification": lambda: f"verification {'passed' if p.get('accepted') else 'failed'}",
        "task_completed": lambda: f"[green]completed ${p.get('cost', {}).get('cost_usd', 0):.5f}[/green]",
    }
    return table.get(t, lambda: t.replace("_", " "))()


def follow(task_id: str):
    seen = 0
    while True:
        for e in call("GET", f"/tasks/{task_id}/events", params={"after": seen}):
            seen = e["id"]
            console.print(f"[dim]{e['created_at'][11:19]}[/dim] [bold]{e['actor'][:14]:>14}[/bold] {describe(e)}")
        t = call("GET", f"/tasks/{task_id}")
        if t["status"] in ("completed", "failed", "needs_attention", "cancelled", "paused_budget", "awaiting_user"):
            console.rule(f"{t['status']} · ${t['cost']['cost_usd']:.5f} · {t['cost']['tokens_in'] + t['cost']['tokens_out']} tok")
            if t["status"] == "awaiting_user":
                console.print("Router unsure. Candidates: " + ", ".join(a["id"] for a in (t.get("router") or {}).get("shortlist", [])))
            if t.get("result"):
                console.print(Markdown(t["result"]))
            elif t.get("error"):
                console.print(f"[red]{t['error']}[/red]")
            return
        time.sleep(1.5)


def team_id() -> str:
    c = cfg()
    if c.get("team"):
        return c["team"]
    me = call("GET", "/me")
    return me["teams"][0]["id"]


def main():
    ap = argparse.ArgumentParser(prog="hub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login"); lg.add_argument("--url", default="http://localhost:8080/api")
    ch = sub.add_parser("chat"); ch.add_argument("text"); ch.add_argument("--no-follow", action="store_true")
    rn = sub.add_parser("run"); rn.add_argument("agent"); rn.add_argument("text")
    sub.add_parser("tasks")
    sh = sub.add_parser("show"); sh.add_argument("id")
    sub.add_parser("approvals")
    ap_ = sub.add_parser("approve"); ap_.add_argument("id")
    rj = sub.add_parser("reject"); rj.add_argument("id")
    lo = sub.add_parser("logs"); lo.add_argument("--follow", action="store_true")
    sub.add_parser("live")
    sub.add_parser("teams")
    tm = sub.add_parser("team"); tm.add_argument("slug")
    a = ap.parse_args()

    if a.cmd == "login":
        email = input("email: ")
        pw = getpass.getpass("password: ")
        r = httpx.post(f"{a.url}/auth/login", json={"email": email, "password": pw}, timeout=30)
        if r.status_code != 200:
            console.print(f"[red]login failed: {r.text}[/red]")
            return
        save({**cfg(), "url": a.url, "token": r.json()["token"]})
        console.print("[green]logged in[/green]")
    elif a.cmd in ("chat", "run"):
        text = a.text if a.cmd == "chat" else f"{a.agent if a.agent.startswith('@') else '@' + a.agent} {a.text}"
        t = call("POST", "/tasks", json={"team_id": team_id(), "text": text})
        console.print(f"task [bold]{t['id']}[/bold] ({t['status']})")
        if not getattr(a, "no_follow", False):
            follow(t["id"])
    elif a.cmd == "tasks":
        tb = Table("id", "status", "title", "steps", "cost")
        for t in call("GET", "/tasks", params={"team_id": team_id()}):
            tb.add_row(t["id"][:8], t["status"], t["title"][:60], f"{t['steps_done']}/{t['steps']}", f"${t['cost_usd']:.4f}")
        console.print(tb)
    elif a.cmd == "show":
        tid = a.id
        if len(tid) < 36:
            tid = next(t["id"] for t in call("GET", "/tasks", params={"team_id": team_id()}) if t["id"].startswith(tid))
        follow(tid)
    elif a.cmd == "approvals":
        for x in call("GET", "/approvals"):
            console.print(f"[bold]{x['id']}[/bold] @{x['agent_id']} -> {x['tool']} {json.dumps(x['args'])}  ({x['task_title']})")
    elif a.cmd in ("approve", "reject"):
        console.print(call("POST", f"/approvals/{a.id}", json={"decision": a.cmd}))
    elif a.cmd == "logs":
        after = -1
        first = True
        while True:
            # Poll the per-team stream via recent tasks' events (SSE needs a long-lived connection).
            with client().stream("GET", "/stream", params={"after": after}, timeout=None) as r:
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        e = json.loads(line[6:])
                        after = e["id"]
                        console.print(f"[dim]{e['created_at'][11:19]}[/dim] [bold]{e['actor'][:14]:>14}[/bold] {describe(e)}")
            if not a.follow and not first:
                break
            first = False
    elif a.cmd == "live":
        def render():
            d = call("GET", "/live")
            tb = Table("agent", "model", "status", "doing now", "tokens", "cost", "task")
            for s in d["steps"]:
                llm = s.get("last_llm") or {}
                u = s.get("usage") or {}
                tb.add_row(f"@{s['agent_id']}", f"{llm.get('model', '-')}", s["status"],
                           describe(s["last_event"]) if s.get("last_event") else "-",
                           str(u.get("tokens_in", 0) + u.get("tokens_out", 0)), f"${u.get('cost_usd', 0):.5f}", s["task_title"][:30])
            return tb
        with Live(render(), refresh_per_second=1) as lv:
            while True:
                time.sleep(1.5)
                lv.update(render())
    elif a.cmd == "teams":
        for t in call("GET", "/me")["teams"]:
            console.print(f"{t['slug']:12} {t['role']:8} {t['id']}")
    elif a.cmd == "team":
        t = next(t for t in call("GET", "/me")["teams"] if t["slug"] == a.slug)
        save({**cfg(), "team": t["id"]})
        console.print(f"default team: {t['name']}")


if __name__ == "__main__":
    main()
