"""Code sandbox. Runs on an internal Docker network with no egress, read-only root filesystem,
non-root user, dropped capabilities, and per-run CPU / memory / file-size / process limits."""
import json
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Job(BaseModel):
    files: dict[str, str]
    timeout: int = 30


def _limits(cpu: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))
    return apply


def _run(cmd: list[str], cwd: str, timeout: int) -> dict:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           preexec_fn=_limits(timeout), env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": cwd,
                                                             "PYTHONDONTWRITEBYTECODE": "1"})
        return {"exit_code": p.returncode, "stdout": p.stdout[-6000:], "stderr": p.stderr[-6000:]}
    except subprocess.TimeoutExpired:
        return {"exit_code": -9, "stdout": "", "stderr": f"timed out after {timeout}s"}


def _workspace(files: dict[str, str]) -> str:
    """Write files keeping their folders (backend/app/main.py stays importable as backend.app.main)."""
    d = Path(tempfile.mkdtemp(prefix="job-", dir="/tmp"))
    for name, content in list(files.items())[:300]:
        rel = Path(str(name).replace("\\", "/").lstrip("/"))
        if not rel.parts or ".." in rel.parts:
            continue
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(d)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/run")
def run(job: Job):
    d = _workspace(job.files)
    try:
        return {"run": _run(["python", "-I", "main.py"], d, min(job.timeout, 60))}
    finally:
        shutil.rmtree(d, ignore_errors=True)


@app.post("/check")
def check(job: Job):
    """Static (ruff: syntax / undefined names), security (bandit), tests (pytest) or a plain run."""
    d = _workspace(job.files)
    try:
        out: dict = {}
        r = _run(["ruff", "check", "--select", "E9,F63,F7,F82", "--output-format", "json", "--no-cache", "."], d, 30)
        try:
            issues = json.loads(r["stdout"] or "[]")
        except json.JSONDecodeError:
            issues = []
        out["ruff"] = {"errors": [f"{i['filename'].split('/')[-1]}:{i['location']['row']} {i['code']} {i['message']}"
                                  for i in issues]}
        b = _run(["bandit", "-q", "-f", "json", "-r", "."], d, 30)
        try:
            bj = json.loads(b["stdout"] or "{}")
            out["bandit"] = {"issues": [{"severity": i["issue_severity"], "text": i["issue_text"], "line": i["line_number"],
                                         "file": i["filename"].split("/")[-1]} for i in bj.get("results", [])]}
        except json.JSONDecodeError:
            out["bandit"] = {"issues": [], "error": b["stderr"][-500:]}
        if any(Path(n).name.startswith("test_") or Path(n).name.endswith("_test.py") for n in job.files):
            t = _run(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header"], d, min(job.timeout, 90))
            out["tests"] = {"passed": t["exit_code"] == 0, "output": (t["stdout"] + t["stderr"])[-4000:]}
        else:
            out["run"] = _run(["python", "-I", "main.py"], d, min(job.timeout, 60))
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)
