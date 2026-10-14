"""Shared helpers for eval scripts: reads seeded credentials from .env, talks to the API via the web proxy."""
import os
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.environ.get("MEMY_URL", "http://localhost:8000/api")


def env() -> dict:
    out = {}
    for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class Client:
    def __init__(self, who: str = "admin"):
        e = env()
        self.http = httpx.Client(base_url=BASE, timeout=120)
        r = self.http.post("/auth/login", json={"email": e[f"SEED_{who.upper()}_EMAIL"],
                                                "password": e[f"SEED_{who.upper()}_PASSWORD"]})
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['token']}"
        self.me = self.get("/me")

    def team(self, slug: str) -> str:
        return next(t["id"] for t in self.me["teams"] if t["slug"] == slug)

    def status(self, method: str, path: str, **kw) -> int:
        """HTTP status of a request (used for access-control checks)."""
        return self.http.request(method, path, **kw).status_code

    def get(self, path, **params):
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None, **kw):
        r = self.http.post(path, json=body, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text}")
        return r.json()

    def wait(self, task_id: str, timeout: int = 600, until=("completed", "failed", "needs_attention", "cancelled",
                                                          "paused_budget", "awaiting_user")) -> dict:
        t0 = time.time()
        seen = 0
        while time.time() - t0 < timeout:
            for e in self.get(f"/tasks/{task_id}/events", after=seen):
                seen = e["id"]
                p = e["payload"]
                extra = p.get("tool") or p.get("model") or p.get("agent_id") or ""
                print(f"  [{e['type']}] {e['actor'][:14]} {extra}")
            t = self.get(f"/tasks/{task_id}")
            if t["status"] in until:
                return t
            time.sleep(2)
        raise TimeoutError(task_id)
