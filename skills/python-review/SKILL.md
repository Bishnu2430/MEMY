---
name: python-review
description: Checklist for reviewing Python code for correctness, error handling, typing and performance.
---
# Python review checklist
1. Correctness: off-by-one, None handling, mutable default arguments, integer vs float division.
2. Errors: bare `except`, swallowed exceptions, missing context managers for files/sockets.
3. Types: public functions have type hints; return types match all branches.
4. Concurrency: shared mutable state, blocking calls inside `async def`.
5. Performance: quadratic loops over large inputs, repeated I/O in loops.
Report: `file:line [severity] problem -> fix`.
