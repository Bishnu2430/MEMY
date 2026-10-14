# /// script
# dependencies = ["httpx", "pillow"]
# ///
"""Team project simulation: a small e-commerce app ("Nimbus Store") built by a real team through the app.

Everything goes through the same HTTP API the dashboard uses, signed in as each person, so every task, ledger entry,
knowledge record, approval, schedule and usage row in the app is genuine. Demo users and their passwords are generated
once and stored in the git-ignored .env (DEMO_* entries).

  uv run evals/simulate_ecommerce.py
"""
import io
import json
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE, Client, env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "nimbus-store"
PEOPLE = {  # key: (display name, role in Storefront, job)
    "priya": ("Priya Shah", "admin", "Product lead"),
    "marco": ("Marco Rossi", "member", "Backend developer"),
    "lena": ("Lena Park", "member", "Frontend developer"),
    "sam": ("Sam Okafor", "viewer", "Stakeholder"),
}
log_lines: list[str] = []


def say(msg: str):
    print(msg, flush=True)
    log_lines.append(msg)


class User(Client):
    """Client signed in as a demo user (credentials from DEMO_* in .env)."""

    def __init__(self, key: str):
        e = env()
        self.http = httpx.Client(base_url=BASE, timeout=180)
        r = self.http.post("/auth/login", json={"email": e[f"DEMO_{key.upper()}_EMAIL"], "password": e[f"DEMO_{key.upper()}_PASSWORD"]})
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['token']}"
        self.me = self.get("/me")
        self.key = key


def ensure_demo_users(admin: Client, team_id: str):
    e = env()
    new = []
    for key, (name, role, _) in PEOPLE.items():
        if f"DEMO_{key.upper()}_PASSWORD" not in e:
            new.append((key, f"{key}@nimbus.local", secrets.token_urlsafe(12)))
    if new:
        with open(ROOT / ".env", "a", encoding="utf-8", newline="\n") as f:
            f.write("\n# Demo team for the e-commerce simulation (local only)\n")
            for key, email, pw in new:
                f.write(f"DEMO_{key.upper()}_EMAIL={email}\nDEMO_{key.upper()}_PASSWORD={pw}\n")
    e = env()
    for key, (name, role, _) in PEOPLE.items():
        admin.post("/admin/users", {"email": e[f"DEMO_{key.upper()}_EMAIL"], "name": name,
                                    "password": e[f"DEMO_{key.upper()}_PASSWORD"], "team_id": team_id, "role": role})


def wireframe_png() -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (900, 600), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 900, 70], fill="#1f2937")
    d.text((24, 26), "NIMBUS   |   Shop   New   Sale", fill="white")
    d.text((760, 26), "Cart (2)", fill="white")
    for i in range(3):
        x = 30 + i * 290
        d.rectangle([x, 100, x + 260, 420], outline="#9ca3af", width=2)
        d.rectangle([x + 15, 115, x + 245, 290], fill="#e5e7eb")
        d.text((x + 20, 305), ["Cloud Runner", "Trail Glide", "City Knit"][i], fill="black")
        d.text((x + 20, 330), ["$89.00", "$119.00", "$74.00"][i], fill="#374151")
        d.rectangle([x + 20, 365, x + 240, 400], fill="#c2603f")
        d.text((x + 85, 377), "Add to cart", fill="white")
    d.rectangle([600, 450, 870, 580], outline="#c2603f", width=2)
    d.text((615, 465), "Cart drawer: items, qty +/-", fill="black")
    d.text((615, 490), "Subtotal, free shipping over $50", fill="black")
    d.rectangle([615, 530, 855, 565], fill="#1f2937")
    d.text((690, 541), "Checkout", fill="white")
    d.text((30, 470), "Wireframe v1 - product grid + cart drawer", fill="#6b7280")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PRD = """# Nimbus Store — Product Requirements (MVP)

Owner: Priya Shah (Product). Status: approved for build.

## Goal
Sell a small catalogue of sneakers online with a fast, simple checkout.

## Scope (MVP)
1. **Catalogue** — list products with name, price, image URL, stock. Prices are integers in cents (USD).
2. **Cart** — anonymous cart identified by a cart id; add item, change quantity, remove item. Carts expire after 30 minutes idle.
3. **Checkout** — collects email + shipping address; validates stock; creates an order; payment is a stub in v1.
4. **Orders** — order has id, email, items, subtotal_cents, shipping_cents, total_cents, status (pending, paid, shipped).

## Business rules
- Free shipping when subtotal >= 5000 cents ($50); otherwise flat 599 cents.
- A product cannot be added beyond available stock.
- Order totals are computed server-side only.

## API (REST, JSON)
- `GET /products` — list products
- `POST /carts` — create cart → `{cart_id}`
- `POST /carts/{cart_id}/items` — `{product_id, quantity}`
- `GET /carts/{cart_id}` — items, subtotal, shipping, total
- `POST /checkout` — `{cart_id, email, address}` → order

## Frontend pages
Product grid, cart drawer, checkout form, order confirmation.

## Non-goals (v1)
Accounts, real payments, discounts, reviews.
"""


def run_task(u: User, text: str, attach: list[str] | None = None, timeout=900, agent=None) -> dict:
    t = u.post("/tasks", {"team_id": TEAM, "text": text, "project": PROJECT, "attachment_ids": attach or [],
                          **({"agent_id": agent} if agent else {})})
    say(f"  → {PEOPLE[u.key][0]} started: {text[:90]}{'…' if len(text) > 90 else ''}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = u.get(f"/tasks/{t['id']}")
        if cur["status"] in ("completed", "failed", "needs_attention", "cancelled", "paused_budget", "awaiting_user"):
            break
        time.sleep(4)
    cur["elapsed_s"] = int(time.time() - t0)
    say(f"  ✓ {cur['status']:<15} {cur['elapsed_s']:>4}s  ${cur['cost']['cost_usd']:.4f}  "
        f"agents={sorted({s['agent_id'] for s in cur['steps']})}  · {cur['title'][:60]}")
    return cur


def wave(jobs):
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        return [f.result() for f in [ex.submit(*j) for j in jobs]]


def main():
    global TEAM
    started = time.time()
    admin = Client("admin")
    say("## 1. Team setup (organisation owner)")
    teams = {t["slug"]: t["id"] for t in admin.get("/teams")}
    if "storefront" not in teams:
        TEAM = str(admin.post("/teams", {"name": "Storefront", "slug": "storefront", "budget_usd_per_task": 1.5})["id"])
        say("  created team Storefront")
    else:
        TEAM = teams["storefront"]
        say("  team Storefront already exists")
    ensure_demo_users(admin, TEAM)
    say(f"  people: " + ", ".join(f"{n} ({r})" for n, r, _ in PEOPLE.values()))
    for s in admin.get("/skills"):
        if not s["enabled"]:
            admin.post(f"/skills/{s['name']}/approve")
            say(f"  approved updated skill content: {s['name']}")

    u = {k: User(k) for k in PEOPLE}
    priya, marco, lena, sam = u["priya"], u["marco"], u["lena"], u["sam"]

    say("\n## 2. Team context (Priya, team admin)")
    priya.post("/instructions", {"scope_type": "team", "scope_id": TEAM, "body":
        "Storefront team: Nimbus Store e-commerce MVP. Backend: Python 3.12 + FastAPI, SQLite. Frontend: React. "
        "Prices are integer cents (USD). Follow the PRD in team knowledge. Keep code small, typed and tested."})
    up = priya.http.post("/knowledge/upload", data={"team_id": TEAM}, files={"file": ("nimbus-prd.md", PRD.encode(), "text/markdown")}).json()
    say(f"  uploaded PRD to knowledge ({up.get('chunks')} chunks)")
    priya.post("/knowledge/text", {"team_id": TEAM, "title": "Nimbus brand guidelines", "text":
        "Nimbus brand: friendly, light, urban. Primary colour clay #c2603f, ink #1f2937, background #faf9f5. "
        "Logo idea: a small cloud merging into a sneaker sole. Typeface: a geometric sans. Tone: short, warm sentences."})
    say("  added brand guidelines note")

    say("\n## 3. Schedules (Priya)")
    existing = {s["name"] for s in priya.get("/schedules") if str(s["team_id"]) == TEAM}
    for name, cron, agent, req in [
        ("Weekly product summary", "0 9 * * 1", "generalist",
         "Summarise what the Storefront team built and decided this week, using our team knowledge. List open questions."),
        ("Daily backend security review", "30 8 * * 1-5", "reviewer",
         "Review the latest Nimbus backend code recorded in team knowledge for security issues; report only real findings."),
        ("Monthly roadmap digest", "0 10 1 * *", "generalist",
         "Write a short roadmap digest for Nimbus Store from the PRD non-goals and open questions in knowledge."),
    ]:
        if name not in existing:
            priya.post("/schedules", {"team_id": TEAM, "name": name, "cron": cron, "request": req, "agent_id": agent})
            say(f"  created schedule '{name}' ({cron})")

    say("\n## 4. Access control checks (each person, through the API)")
    checks = []

    def expect(who, label, code, want):
        ok = code == want
        checks.append((who, label, code, want, ok))
        say(f"  {'PASS' if ok else 'FAIL'}  {who:<12} {label:<46} HTTP {code} (expected {want})")
    bob, alice = Client("bob"), Client("alice")
    expect("Sam (viewer)", "reads Storefront tasks", sam.status("GET", "/tasks", params={"team_id": TEAM}), 200)
    expect("Sam (viewer)", "creates a task", sam.status("POST", "/tasks", json={"team_id": TEAM, "text": "@generalist hi"}), 403)
    expect("Sam (viewer)", "uploads knowledge", sam.status("POST", "/knowledge/text", json={"team_id": TEAM, "title": "x", "text": "y"}), 403)
    expect("Sam (viewer)", "edits an agent", sam.status("PUT", "/agents/coder", json={"yaml": "id: coder"}), 403)
    expect("Lena (member)", "edits an agent", lena.status("PUT", "/agents/coder", json={"yaml": "id: coder"}), 403)
    expect("Lena (member)", "adds an MCP server", lena.status("POST", "/mcp/servers", json={"id": "x", "name": "x", "transport": "stdio"}), 403)
    expect("Lena (member)", "pauses the team (kill switch)", lena.status("POST", "/admin/controls", json={"scope": f"team:{TEAM}", "paused": True}), 403)
    expect("Bob (Research)", "reads Storefront tasks", bob.status("GET", "/tasks", params={"team_id": TEAM}), 403)
    expect("Bob (Research)", "searches Storefront knowledge", bob.status("GET", "/knowledge/search", params={"team_id": TEAM, "q": "checkout"}), 403)
    expect("Alice (Platform)", "starts a Storefront task", alice.status("POST", "/tasks", json={"team_id": TEAM, "text": "@generalist hi"}), 403)
    platform = next(t["id"] for t in admin.get("/teams") if t["slug"] == "platform")
    expect("Priya (admin)", "reads another team (Platform)", priya.status("GET", "/tasks", params={"team_id": platform}), 403)

    say("\n## 5. Build (agents working in parallel waves)")
    up_wf = lena.http.post("/uploads", data={"team_id": TEAM, "note": "storefront wireframe"},
                           files={"file": ("storefront-wireframe.png", wireframe_png(), "image/png")}).json()
    say(f"  Lena attached a wireframe — vision read it: {up_wf.get('preview', '')[:110]!r}…")
    tasks = []
    say("  wave 1: plan, backend, research")
    tasks += wave([
        (run_task, priya, "Using the Nimbus PRD in our team knowledge, write the MVP build plan (data model, API endpoints, "
                          "frontend pages, risks) and draw an ER diagram of the data model."),
        (run_task, marco, "@coder Build the Nimbus Store backend from the PRD as one FastAPI module: products (seed 3 sneakers), "
                          "carts with items and quantity, checkout creating an order, integer cents, free shipping at 5000 cents "
                          "else 599, stock validation. Use an in-memory SQLite or dicts. Write pytest tests with TestClient "
                          "(including the free-shipping threshold and an out-of-stock error). Save to the project workspace as "
                          "backend/app/main.py and backend/tests/test_main.py."),
        (run_task, priya, "@researcher Research how to reduce cart abandonment for small online stores using "
                          "https://en.wikipedia.org/wiki/Shopping_cart_software and cite the source."),
    ])
    say("  wave 2: frontend from wireframe, checkout sequence diagram, brand")
    tasks += wave([
        (run_task, lena, "@frontend Build the React storefront from the attached wireframe and our backend API: ProductGrid, "
                         "CartDrawer (quantity +/-, subtotal, free-shipping hint) and a CheckoutForm with validation, plus a small "
                         "api.js client. Read backend/app/main.py in the workspace first so endpoints match. Write files under "
                         "frontend/src/.", [up_wf["artifact_id"]]),
        (run_task, marco, "@diagrammer Draw a sequence diagram of the Nimbus checkout: browser, React app, FastAPI, SQLite, "
                          "payment stub, including the stock check and the order confirmation."),
        (run_task, priya, "@designer Design the Nimbus logo as an SVG following our brand guidelines in knowledge (cloud merging "
                          "into a sneaker sole, clay #c2603f). Also try generating a hero image of a sneaker on a pastel background."),
    ])
    say("  wave 3: review, launch voiceover, announcement (needs approval)")

    def announce():
        t = lena.post("/tasks", {"team_id": TEAM, "project": PROJECT,
                                 "text": "@ops Post 'Nimbus Store MVP is ready for review: backend, storefront and logo are in the "
                                         "nimbus-store project' to the #storefront channel."})
        say("  → Lena Park started: @ops post the MVP-ready announcement")
        for _ in range(120):
            pend = [a for a in priya.get("/approvals") if a["task_id"] == t["id"]]
            if pend:
                code = sam.status("POST", f"/approvals/{pend[0]['id']}", json={"decision": "approve"})
                checks.append(("Sam (viewer)", "approves Lena's action", code, 403, code == 403))
                say(f"  {'PASS' if code == 403 else 'FAIL'}  Sam (viewer)  tries to approve it → HTTP {code} (expected 403)")
                priya.post(f"/approvals/{pend[0]['id']}", {"decision": "approve"})
                say(f"  Priya (team admin) approved: {pend[0]['tool']} {json.dumps(pend[0]['args'])[:100]}")
                break
            time.sleep(3)
        cur = lena.wait(t["id"], timeout=400)
        cur["elapsed_s"] = 0
        say(f"  ✓ {cur['status']:<15} announcement task")
        return cur
    tasks += wave([
        (run_task, marco, "@reviewer Review the Nimbus backend code recorded in our team knowledge (the coder's work record) "
                          "for bugs and security issues. Report file, line, severity and a concrete fix."),
        (run_task, priya, "@narrator Write and voice a 12-second launch announcement for Nimbus Store (warm, short), using a "
                          "friendly voice."),
        (announce,),
    ])
    sched = next((s for s in priya.get("/schedules") if s["name"] == "Weekly product summary" and str(s["team_id"]) == TEAM), None)
    if sched:
        priya.post(f"/schedules/{sched['id']}/run")
        say("  triggered schedule 'Weekly product summary' now (runs as Priya)")

    say("\n## 6. What the app now holds")
    time.sleep(30)  # let the scheduled run get picked up
    report = collect(admin, priya, tasks, checks, started)
    out = ROOT / "evals" / "reports" / "ecommerce-simulation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    say(f"\nReport written to {out.relative_to(ROOT)}")


def collect(admin: Client, priya: User, tasks, checks, started) -> str:
    know = priya.get("/knowledge/sources", team_id=TEAM)
    files = priya.get("/workspace/files", team_id=TEAM, project=PROJECT)
    scheds = [s for s in priya.get("/schedules") if str(s["team_id"]) == TEAM]
    usage = admin.get("/usage", days=1)
    access = admin.get("/access")
    all_tasks = priya.get("/tasks", team_id=TEAM, limit=50)
    by_kind: dict = {}
    for k in know:
        by_kind[k["kind"]] = by_kind.get(k["kind"], 0) + 1
    team_name = next(t["name"] for t in access["teams"] if t["id"] == TEAM)
    L = [f"# Nimbus Store — team simulation report", "",
         f"Run through the app API as real signed-in users · {time.strftime('%Y-%m-%d %H:%M')} · "
         f"{int(time.time() - started)}s wall time", "", "## People and access", "",
         "| Person | Job | Role in Storefront |", "|---|---|---|"]
    e = env()
    for key, (name, role, job) in PEOPLE.items():
        L.append(f"| {name} ({e[f'DEMO_{key.upper()}_EMAIL']}) | {job} | {role} |")
    L += ["", "### Access checks", "", "| Who | Tried to | Result | Expected | |", "|---|---|---|---|---|"]
    for who, label, code, want, ok in checks:
        L.append(f"| {who} | {label} | HTTP {code} | {want} | {'pass' if ok else '**FAIL**'} |")
    L += ["", f"## Tasks ({len(all_tasks)} in {team_name})", "", "| Task | Started by | Status | Agents | Steps | Cost |", "|---|---|---|---|---|---|"]
    for t in all_tasks:
        L.append(f"| {t['title'][:70]} | {t['created_by_name']} | {t['status']} | {t.get('requested_agent') or 'auto'} | "
                 f"{t['steps_done']}/{t['steps']} | ${t['cost_usd']:.4f} |")
    L += ["", "## Knowledge base", "", "| Kind | Entries |", "|---|---|"]
    L += [f"| {k} | {n} |" for k, n in sorted(by_kind.items(), key=lambda x: -x[1])]
    L += ["", "## Project workspace files", ""] + [f"- `{f['path']}` ({f['size']} bytes)" for f in files]
    L += ["", "## Schedules", ""] + [f"- **{s['name']}** — `{s['cron']}` — next run {s['next_run_at'][:16]}"
                                      f"{' — last run ' + s['last_run_at'][:16] if s.get('last_run_at') else ''}" for s in scheds]
    tt = usage["totals"]
    L += ["", "## Usage (last 24h, all teams)", "",
          f"- cost ${tt['cost_usd']:.4f} · tokens in {tt['tokens_in']:,} / out {tt['tokens_out']:,} · model calls {tt['llm_calls']}"
          f" · tool calls {tt['tool_calls']} · media {tt.get('media_calls', 0)} · rate-limited retries {tt.get('rate_limited', 0)}"]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
