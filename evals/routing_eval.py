# /// script
# dependencies = ["httpx"]
# ///
"""Routing accuracy on the labelled set (auto-routing becomes default at >= 85% top-1 on 100 tasks).
Usage: uv run evals/routing_eval.py [--limit N]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import Client  # noqa: E402

limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
cases = [json.loads(l) for l in (Path(__file__).parent / "routing.jsonl").read_text().splitlines() if l.strip()][:limit]
c = Client("admin")
team = c.team("platform")
correct, asked = 0, 0
for case in cases:
    d = c.post("/route", {"team_id": team, "text": case["text"]})
    ok = d["agent_id"] == case["agent"]
    correct += ok
    asked += d["mode"] == "ask"
    print(f"{'OK ' if ok else 'XX '} {d['mode']:6} {d['agent_id']:11} (want {case['agent']:11}) conf={d['confidence']:.2f}  {case['text'][:60]}")
n = len(cases)
print(f"\ntop-1 accuracy: {correct}/{n} = {correct / n:.0%} · asked user on {asked} · target 85%")
