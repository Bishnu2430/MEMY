# Changelog

## v0.1.1

### Removed
- **Local LLM (Ollama).** It was the performance bottleneck on CPU-only hardware (about 2 minutes per call) and is
  gone entirely. All chat models are hosted on Groq. Search embeddings now come from a small ONNX embedding service
  (`bge-small-en-v1.5`, 384-d); existing embeddings are migrated in place and re-embedded on write.

### Fixed — tasks that looked stuck
- When every hosted model hit its tokens-per-minute limit, the gateway silently fell back to the CPU model. The
  gateway now tracks provider rate-limit headers in a table shared by all processes, sends each call to a model with
  capacity, overflows across all three Groq models, and shows any wait in the live trace
  ("Waiting 12s for model capacity").
- Cancelling a task now stops its running steps within seconds and rejects its pending approvals.
- Workers run up to three steps concurrently, so one long step no longer blocks the queue.
- Prompts are trimmed harder (older tool results collapse), keeping calls inside free-tier budgets.
- The web proxy re-resolves the API container, so recreating it no longer causes HTTP 502 on sign-in.

### Added
- **ElevenLabs suite:** speech with voice choice and previews, sound effects, music, transcription, image and video
  generation (create-then-poll), all stored and playable. Plan limits are reported in plain language.
- **Studio page:** generate, render and play media; credits meter; team gallery with players and lightbox.
- **Designer agent** (images, video, SVG illustrations) and **Frontend Developer agent**; narrator upgraded.
- **File attachments:** attach images, PDFs, documents, code and audio to any task (button, drag-and-drop or paste).
  Text is extracted, images are described by a vision model, audio is transcribed; every agent on the task sees
  them and can read them in full.
- **Knowledge recording:** every accepted step writes an agent work record, and every finished task writes
  distilled task knowledge (summary, facts, decisions, open questions). Attachments and fetched pages are stored
  with provenance (task, step, agent). Each write appears live in the trace and on the task's Knowledge tab.
- **MCP:** project workspace server (team-scoped files), official fetch server, connection test before saving,
  enable/disable/remove servers, encrypted secrets injected as environment variables or HTTP headers, revoked
  grants are never re-added on restart.
- **Projects page:** browse files agents wrote, with syntax highlighting.
- **Access matrix** on Admin, team creation, plain-language capability legend per role.
- **Usage charts:** tokens per day by model (legend, hover tooltip, table view), cost by agent, activity breakdown.
- **Stop / Run again / Copy:** stop any unfinished task from chat, the task page or the live trace (running steps stop
  within seconds); run a finished task again with the same files; copy buttons on every code block, answer, file and
  raw event; download answers as Markdown and diagrams as SVG.
- **Team simulation:** `evals/simulate_ecommerce.py` — a four-person team (admin, two members, a viewer) builds a small
  e-commerce app through the API, with access checks, schedules, approvals and a generated report.

### Changed
- Themed dropdowns and team switcher replace native selects (readable in dark mode, keyboard accessible).
- Steps can never outlive their task: an orchestrator sweep closes orphaned steps, and the live view only shows active work.
- Agent edits check permission before validating input; invalid configuration returns a readable 400, never a 500.
- Human-readable labels everywhere: tools, models, statuses, verification checks, actors, schedules (cron in plain
  language), audit actions; raw identifiers appear only as secondary detail.
- Friendlier errors (server unreachable, wrong password, missing plan permissions) and confirmation for destructive actions.

## v0.1.0
- First working prototype of the design: master node (router, supervisor, guards, scheduler), checkpointed agent
  runtime with lease-based hand-off, hybrid knowledge search under row-level security, tool gateway with MCP
  pinning and approvals, verification pipeline, web dashboard with live trace, terminal client and evals.
