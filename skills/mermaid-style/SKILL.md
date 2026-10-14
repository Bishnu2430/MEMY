---
name: mermaid-style
description: Mermaid syntax rules that avoid common parse errors (quoting, node ids, arrows, diagram headers).
---
# Mermaid rules
- Start with a header: `flowchart TD`, `sequenceDiagram`, `classDiagram`, `stateDiagram-v2` or `erDiagram`.
- Node ids are simple words (A, api, db); put text in brackets: `api["API server (FastAPI)"]`.
- Quote any label containing parentheses, commas, colons or slashes.
- Flowchart arrows: `A --> B`, labelled `A -->|label| B`. Sequence arrows: `A->>B: message`.
- No trailing semicolons inside sequence diagrams; one statement per line.
