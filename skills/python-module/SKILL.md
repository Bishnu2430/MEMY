---
name: python-module
description: How to build and test Python code in the sandbox - snippets (main.py + test_main.py) and full project workspaces (FastAPI apps).
---
# Python code that passes verification
Available packages in the sandbox (no network): standard library, fastapi, pydantic, httpx, sqlalchemy, pytest.

## Project work (the task has a project)
1. Write files into the workspace with `mcp__workspace__write_file`, keeping a real layout:
   `backend/app/main.py` (with `app = FastAPI()`) and `backend/tests/test_main.py`.
2. Tests import the module by its path: `from backend.app.main import app`, then
   `from fastapi.testclient import TestClient; client = TestClient(app)`.
3. Run `sandbox_test_project`. It runs ruff (syntax), bandit (security) and pytest over the whole workspace.
4. Read the result. Fix only what failed, write the changed file again, then run the project tests again.
   Never re-run a check without changing a file first.

## Snippets (no project)
`sandbox_run_python` with `code` (saved as main.py) and `tests` (saved as test_main.py, importing from `main`).

## Always
- Keep state in memory (dicts or in-memory SQLite) so tests are self-contained.
- 3-8 focused tests: happy path, an edge case, an error case (a 4xx status or `pytest.raises`).
- Save the main module with `artifact_save` too, and show the final code in a fenced python block.
