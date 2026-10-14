# Memy — multi-agent orchestration platform

A self-hosted platform where a team works with a set of specialised AI agents through one chat-first dashboard.
Agents share one knowledge base and one task ledger, so any agent (or person) can pick up any task, and every
step is checkpointed, verified, costed and recorded as knowledge.

The design rationale lives in [`Multi-Agent Orchestration Platform — Design & Decisions.pdf`](Multi-Agent%20Orchestration%20Platform%20—%20Design%20&%20Decisions.pdf);
this README covers what is built and how to run it.

## Quick start

Requirements: Docker Desktop (8 GB RAM is enough — there is no local LLM), a [Groq](https://console.groq.com) API key,
and optionally an [ElevenLabs](https://elevenlabs.io) API key for speech and media.

```bash
cp .env.example .env        # then fill in the secrets, seeded users and GROQ_API_KEY / ELEVENLABS_API_KEY
docker compose up -d --build
```

Open **http://localhost:8080** and sign in with a user from `.env`. First start takes a few minutes (image builds).

## What runs

| Service | Role |
|---|---|
| `web` | React dashboard behind nginx (the only port published to the network: 8080) |
| `api` | FastAPI master node: auth, REST + SSE, routing, approvals, media studio, uploads (also on `127.0.0.1:8000` for the CLI) |
| `orchestrator` | Deterministic control loop: routing, supervisor planning, dispatch, verification, retries → reassignment → escalation, guards, scheduler, knowledge recording |
| `worker` ×2 | Agent runtime: claims steps with leases, runs each agent as a LangGraph graph with Postgres checkpoints (3 concurrent steps per worker) |
| `db` | PostgreSQL 17 + pgvector: users, ledger, events, knowledge (row-level security per team), checkpoints, usage |
| `embedder` | Small ONNX embedding model (`bge-small-en-v1.5`, 384-d) — not an LLM |
| `sandbox` | Code runner on an internal network with no internet, read-only filesystem, resource limits |
| `renderer` | Mermaid → SVG renderer |

All language models are hosted (Groq). Each model has its own tokens-per-minute budget; the gateway reads the
provider's rate-limit headers, routes each call to a model with capacity and shows any wait in the live trace.

## Using it

- **Chat** — describe a task, optionally `@mention` an agent and attach files (images, PDFs, docs, code, audio).
  The router picks an agent, or the supervisor plans multi-agent work. Each answer shows the live trace while it
  runs and the knowledge it saved afterwards.
- **Live trace** — which agent, on which model, is doing what right now, with live tokens and cost.
- **Tasks** — each task's ledger (goal, acceptance criteria, steps, verification), event timeline, inputs,
  outputs and the knowledge it recorded. Retry, reassign or take over any step.
- **Approvals** — side-effecting actions (posting messages, deleting workspace files, video generation, …) wait
  here with the exact tool and arguments.
- **Projects** — files agents wrote into the team's project workspace (through the workspace MCP server).
- **Studio** — generate and play speech, sound effects, music, images, video, transcripts and diagrams.
- **Knowledge** — search, add documents/pages/notes, review memory proposals, inspect every stored chunk and where it came from.
- **Agents** — edit agent configuration (YAML), compare and restore versions, clone for a team, pause, try in a playground.
- **Tools & MCP** — connect MCP servers (local or remote), test before saving, pin tool definitions, grant per team, manage secrets.
- **Usage** — cost and tokens over time by model and agent, response times, quality per agent, budget use.
- **Admin** — access matrix, people and roles, team budgets, provider keys (encrypted), instruction pool, kill switch, audit log.

Terminal client: `uv run cli/hub.py login`, then `chat "…"`, `run @coder "…"`, `live`, `approvals`, `approve <id>`.

## Agents

| Agent | Does | Model |
|---|---|---|
| `@generalist` | questions, explanations, summaries | GPT-OSS 20B |
| `@researcher` | cited research from web pages and team knowledge | GPT-OSS 20B |
| `@coder` | Python modules with tests, run in the sandbox, written to the project workspace | GPT-OSS 120B |
| `@frontend` | React / HTML / CSS UI written to the project workspace | GPT-OSS 120B |
| `@reviewer` | code and security review | Qwen 3.8 27B |
| `@diagrammer` | Mermaid diagrams compiled to SVG with repair loop | GPT-OSS 20B |
| `@designer` | images and video (ElevenLabs), SVG illustrations | GPT-OSS 120B |
| `@narrator` | speech, sound effects, music, transcription (ElevenLabs) | GPT-OSS 20B |
| `@ops` | team announcements (approval required), shared notes, time | GPT-OSS 20B |

Agents are YAML files in `agents/`; editing one in the UI creates a new version. Seed files update an agent only
if nobody has edited it in the UI since.

## ElevenLabs plan limits

Speech, voices, sound effects and transcription work on the free plan. Image, video and music generation require
a paid plan **and** the *Image & Video* permission on the API key; on lower tiers the platform reports this clearly
and agents fall back to SVG illustrations.

## Verifying it works

```bash
uv run evals/run_all.py      # team isolation, MCP pinning, budget stop, approvals, prompt injection, worker-kill hand-off
uv run evals/features.py     # attachments, knowledge recording, MCP probe, project workspace, studio
uv run evals/routing_eval.py # routing accuracy on a labelled set
uv run evals/simulate_ecommerce.py  # a 4-person team builds a small e-commerce app through the app; writes evals/reports/
```

## Repository layout

```
agents/      seed agent configurations (YAML)
skills/      Agent Skills folders (SKILL.md)
infra/       model aliases, scraping policy, db init, MCP demo config
services/    app/ (api, orchestrator, worker, gateway, runtime, tools, MCP servers), sandbox/, renderer/, embedder/
web/         React + Vite dashboard
cli/         terminal client
evals/       end-to-end checks against the running stack
```

See [CHANGELOG.md](CHANGELOG.md) for what changed between versions.
