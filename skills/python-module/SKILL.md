---
name: python-module
description: How to structure a small Python module with tests that run in the sandbox (main.py + test_main.py).
---
# Python module pattern
- The sandbox saves your code as `main.py` and tests as `test_main.py`; tests must `from main import ...`.
- Keep the module importable: put demo code under `if __name__ == "__main__":`.
- Use only the standard library (the sandbox has no network and no extra packages).
- Write 3-6 focused pytest tests including an edge case and an error case (`pytest.raises`).
- Save files with artifact_save: `<module>.py` and `test_<module>.py`.
