# /// script
# dependencies = ["httpx"]
# ///
"""End-to-end checks for v0.1.1 features through the API (same calls the dashboard makes).

  uv run evals/features.py [attachments knowledge mcp workspace studio]
"""
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import Client  # noqa: E402

results = {}


def png(w=96, h=64):
    """A tiny chart-like PNG: three coloured bars, so the vision model has something to describe."""
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            bar = x // 32
            height = [50, 30, 15][bar]
            row += bytes((220, 60, 50) if bar == 0 and y > h - height else (60, 140, 220) if bar == 1 and y > h - height
                         else (70, 180, 90) if bar == 2 and y > h - height else (250, 250, 250))
        rows.append(b"\x00" + bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def check(name, ok, detail):
    results[name] = (ok, detail)
    print(("PASS " if ok else "FAIL ") + f"{name}: {detail}")


def attachments_and_knowledge(c: Client, team: str):
    up1 = c.http.post("/uploads", data={"team_id": team, "note": "sales by region"},
                      files={"file": ("sales_chart.png", png(), "image/png")}).json()
    spec = ("# Checkout spec\nOrders over $50 ship free. Carts expire after 30 minutes of inactivity.\n"
            "Payment providers: Stripe only in v1. Tax is calculated at checkout from the shipping address.")
    up2 = c.http.post("/uploads", data={"team_id": team}, files={"file": ("checkout_spec.md", spec.encode(), "text/markdown")}).json()
    check("upload_image", up1.get("kind") == "image" and len(up1.get("preview", "")) > 30,
          f"{up1.get('extraction')}: {up1.get('preview', '')[:120]!r}")
    check("upload_text", "ship free" in up2.get("preview", ""), f"{up2.get('extraction')}")
    t = c.post("/tasks", {"team_id": team, "text": "@generalist Using the attached files: what is the free-shipping "
                          "threshold, how long do carts last, and what does the attached chart show? Be brief.",
                          "attachment_ids": [up1["artifact_id"], up2["artifact_id"]]})
    t = c.wait(t["id"], timeout=300)
    ans = (t.get("result") or "").lower()
    check("attachment_answer", t["status"] == "completed" and "50" in ans and "30" in ans, ans[:200].replace("\n", " "))
    know = c.get(f"/tasks/{t['id']}/knowledge")
    kinds = sorted({k["kind"] for k in know})
    evs = [e for e in c.get(f"/tasks/{t['id']}/events") if e["type"] == "knowledge_stored"]
    check("knowledge_recorded", {"attachment", "agent_record", "task_knowledge"} <= set(kinds) and len(evs) >= 3,
          f"kinds={kinds} events={len(evs)}")


def mcp(c: Client, team: str):
    r = c.post("/mcp/probe", {"transport": "stdio", "command": "python", "args": ["-m", "mcp_server_time"]})
    check("mcp_probe", r.get("ok") and len(r.get("tools", [])) >= 1, f"tools={[t['name'] for t in r.get('tools', [])]}")
    tools = c.get("/tools")
    servers = {s["id"]: s["status"] for s in tools["servers"]}
    check("mcp_servers", all(k in servers for k in ("time", "notes", "workspace", "fetch")), f"{servers}")


def workspace(c: Client, team: str):
    t = c.post("/tasks", {"team_id": team, "project": "featurecheck", "text":
                          "@coder Write a tiny Python module price.py with add_tax(amount, rate) and a pytest test, "
                          "and write both into the project workspace."})
    t = c.wait(t["id"], timeout=400)
    files = c.get("/workspace/files", team_id=team, project="featurecheck")
    check("workspace_files", t["status"] == "completed" and len(files) >= 1, f"status={t['status']} files={[f['path'] for f in files]}")


def studio(c: Client, team: str):
    status = c.get("/media/status")
    check("media_status", status.get("configured"), f"tier={status.get('tier')} used={status.get('character_count')}/{status.get('character_limit')}")
    jobs = {k: c.post("/media/jobs", {"team_id": team, "kind": k, "params": p})["id"] for k, p in {
        "speech": {"text": "Welcome to the Nimbus store.", "voice": "George"},
        "image": {"prompt": "a minimalist sneaker on a pastel background", "aspect_ratio": "1:1"},
        "diagram": {"source": "flowchart LR\n  Cart --> Checkout --> Payment --> Confirmation"}}.items()}
    deadline = time.time() + 180
    while time.time() < deadline:
        rows = {str(j["id"]): j for j in c.get("/media/jobs", team_id=team)}
        if all(rows[str(i)]["status"] in ("completed", "failed") for i in jobs.values()):
            break
        time.sleep(3)
    for k, i in jobs.items():
        j = rows[str(i)]
        expect_paid = k == "image" and status.get("tier") == "free"
        ok = (j["status"] == "completed") or (expect_paid and "Pro plan" in (j.get("error") or ""))
        check(f"studio_{k}", ok, f"{j['status']} {j.get('artifact_name') or ''} {(j.get('error') or '')[:120]}")


if __name__ == "__main__":
    want = set(sys.argv[1:])
    c = Client("admin")
    team = c.team("platform")
    for name, fn in [("attachments", attachments_and_knowledge), ("mcp", mcp), ("workspace", workspace), ("studio", studio)]:
        if not want or name in want:
            print(f"\n=== {name} ===")
            try:
                fn(c, team)
            except Exception as e:
                check(name, False, f"{type(e).__name__}: {e}")
    print("\n=== summary ===")
    for k, (ok, d) in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {k}")
