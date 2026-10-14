// Human-readable labels for every identifier the UI shows. Raw ids appear only as secondary detail.

const ACRONYMS = { mcp: "MCP", api: "API", llm: "LLM", svg: "SVG", pdf: "PDF", url: "URL", id: "ID", ai: "AI", tts: "TTS",
  stt: "STT", sql: "SQL", json: "JSON", http: "HTTP", ui: "UI", cli: "CLI", gpt: "GPT", oss: "OSS", usd: "USD", kb: "KB",
  rbac: "RBAC", sse: "SSE", jwt: "JWT", rls: "RLS", ok: "OK", io: "IO", js: "JS", css: "CSS", html: "HTML", yaml: "YAML" };

export function titleCase(s) {
  if (!s) return "";
  const words = String(s).replace(/[._\-/:]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().split(/\s+/);
  return words.map((w, i) => ACRONYMS[w.toLowerCase()] || (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase())).join(" ");
}

const BUILTIN = {
  "knowledge.search": "Knowledge search", "knowledge.remember": "Propose memory", "web.fetch": "Fetch web page",
  "sandbox.run_python": "Run Python in sandbox", "artifact.save": "Save file", "diagram.render": "Render diagram",
  "audio.speak": "Text to speech", "audio.sound_effect": "Sound effect", "audio.music": "Compose music",
  "audio.transcribe": "Transcribe audio", "media.voices": "List voices", "image.generate": "Generate image",
  "video.generate": "Generate video", "image.svg": "SVG illustration", "attachment.read": "Read attachment",
  "skill.load": "Load skill", "ledger.note": "Ledger note", "team.post_message": "Post team message",
};
const SERVER = { workspace: "Workspace", notes: "Team notes", time: "Time", fetch: "Web fetch" };

/** "mcp.workspace.write_file" -> "Workspace · Write file", "knowledge.search" -> "Knowledge search", "mcp.time.*" -> "Time · all tools" */
export function toolLabel(name) {
  if (!name) return "";
  const n = String(name).replace(/__/g, ".");
  if (BUILTIN[n]) return BUILTIN[n];
  if (n.startsWith("mcp.")) {
    const [, server, ...rest] = n.split(".");
    const tool = rest.join(".");
    return `${SERVER[server] || titleCase(server)} · ${tool === "*" ? "all tools" : titleCase(tool)}`;
  }
  return titleCase(n);
}

const MODELS = {
  "strong-api": "GPT-OSS 120B", "code-api": "GPT-OSS 120B (code)", "fast-api": "GPT-OSS 20B", "reviewer-api": "Qwen 3.8 27B", router: "Router model",
  supervisor: "Supervisor model", verifier: "Verifier model", bulk: "Bulk model", vision: "Vision model",
  embedding: "Embeddings (bge-small)", transcribe: "Whisper (speech to text)",
  "openai/gpt-oss-120b": "GPT-OSS 120B", "openai/gpt-oss-20b": "GPT-OSS 20B", "qwen/qwen3.8-27b": "Qwen 3.8 27B",
  "elevenlabs/tts": "ElevenLabs speech", "elevenlabs/sound": "ElevenLabs sound effects", "elevenlabs/music": "ElevenLabs music",
  "elevenlabs/stt": "ElevenLabs transcription", "elevenlabs/image": "ElevenLabs image", "elevenlabs/video": "ElevenLabs video",
  "deterministic checks": "Automated checks",
};
export function modelLabel(alias, name) {
  if (name && MODELS[name]) return MODELS[name];
  if (alias && MODELS[alias]) return MODELS[alias];
  if (alias && alias.includes(".")) return toolLabel(alias); // tool names land in the model column of usage rows
  return name && name !== "-" ? titleCase(name.split("/").pop()) : titleCase(alias);
}

export const PROVIDER = { openai_compat: "Groq", groq: "Groq", embedder: "Local embedder", elevenlabs: "ElevenLabs", tool: "Tool" };
export const providerLabel = (p) => PROVIDER[p] || titleCase(p);

export const USAGE_KIND = { llm: "Model calls", embed: "Embeddings", tool: "Tool calls", media: "Media generation" };

const ROLES = { supervisor: "Supervisor", router: "Router", verifier: "Verifier", recorder: "Knowledge recorder", gateway: "Model gateway",
  system: "System", inputs: "File reader", studio: "Studio" };
/** Event actors: roles, agents (@id), workers (Worker 2373) or a person. */
export function actorLabel(actor, agentIds = []) {
  if (!actor) return "";
  if (ROLES[actor]) return ROLES[actor];
  if (/^[0-9a-f]{12}-\d+$/.test(actor)) return `Worker ${actor.slice(0, 4)}`;
  if (/^[0-9a-f-]{36}$/.test(actor)) return "You";
  if (MODELS[actor]) return MODELS[actor];
  return `@${actor}`;
}
export const workerLabel = (w) => (w && /^[0-9a-f]{12}-\d+$/.test(w) ? `Worker ${w.slice(0, 4)}` : w || "");

export const PROFILE = { none: "No checks", light: "Basic checks", "code-default": "Code checks + review", research: "Citation checks",
  diagram: "Diagram compiles", media: "Media produced", strict: "Strict checks + review" };

export const ARTIFACT_KIND = { code: "Code", markdown: "Document", mermaid: "Mermaid source", diagram: "Diagram", image: "Image",
  audio: "Audio", video: "Video", pdf: "PDF", text: "Text", json: "JSON", file: "File" };

export const auditLabel = (a) => titleCase(String(a || "").replace(".", " "));
export const roleLabel = (r) => titleCase(r);

const DOW = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
/** Plain-language reading of common cron expressions; falls back to the raw expression. */
export function cronLabel(expr) {
  const p = String(expr || "").trim().split(/\s+/);
  if (p.length !== 5) return expr;
  const [mi, h, dom, mon, dow] = p;
  const time = /^\d+$/.test(mi) && /^\d+$/.test(h) ? `${h.padStart(2, "0")}:${mi.padStart(2, "0")} UTC` : null;
  if (mi.startsWith("*/") && h === "*" && dom === "*" && mon === "*" && dow === "*") return `Every ${mi.slice(2)} minutes`;
  if (/^\d+$/.test(mi) && h === "*" && dom === "*" && mon === "*" && dow === "*") return `Every hour at :${mi.padStart(2, "0")}`;
  if (time && dom === "*" && mon === "*" && dow === "*") return `Every day at ${time}`;
  if (time && dom === "*" && mon === "*" && /^\d$/.test(dow)) return `Every ${DOW[+dow % 7]} at ${time}`;
  if (time && dom === "*" && mon === "*" && dow === "1-5") return `Weekdays at ${time}`;
  if (time && /^\d+$/.test(dom) && mon === "*" && dow === "*") return `Monthly on day ${dom} at ${time}`;
  return expr;
}
