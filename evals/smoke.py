# /// script
# dependencies = ["httpx"]
# ///
"""Quick end-to-end check: one manual mention task. Usage: uv run evals/smoke.py "@generalist what is 2+2?" """
import sys

sys.path.insert(0, __file__.rsplit("evals", 1)[0] + "evals")
from _client import Client  # noqa: E402

c = Client("admin")
text = sys.argv[1] if len(sys.argv) > 1 else "@generalist In one sentence, what is a vector database?"
team = c.team(sys.argv[2] if len(sys.argv) > 2 else "platform")
t = c.post("/tasks", {"team_id": team, "text": text})
print("task", t["id"], t["status"])
t = c.wait(t["id"])
print("STATUS:", t["status"], "| cost:", t["cost"])
for s in t["steps"]:
    print(f"- {s['key']} @{s['agent_id']} {s['status']} attempts={s['attempts']} verification={s['verification'] and s['verification']['passed']}")
print("RESULT:\n", (t["result"] or t.get("error") or "")[:3000])
