const KEY = "memy_token";

export const getToken = () => {
  try { return localStorage.getItem(KEY); } catch { return null; }
};
export const setToken = (t) => {
  try { t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY); } catch { /* storage unavailable */ }
};

export async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const r = await fetch(`/api${path}`, { method, headers, body: payload });
  if (r.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("memy-logout"));
  }
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${r.status}`);
  return data;
}

export function stream(path, onEvent, after = 0) {
  const sep = path.includes("?") ? "&" : "?";
  const es = new EventSource(`/api${path}${sep}token=${encodeURIComponent(getToken() || "")}&after=${after}`);
  es.onmessage = (m) => {
    try { onEvent(JSON.parse(m.data)); } catch { /* ignore malformed */ }
  };
  return () => es.close();
}

export const artifactUrl = (id) => `/api/artifacts/${id}?token=${encodeURIComponent(getToken() || "")}`;

export const money = (v) => `$${Number(v || 0).toFixed(Number(v || 0) < 0.01 ? 5 : 4)}`;
export const num = (v) => Number(v || 0).toLocaleString();
export const ago = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts).toLocaleDateString();
};
export const elapsed = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Math.floor((Date.now() - new Date(ts).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};

const short = (v, n = 90) => {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s && s.length > n ? s.slice(0, n) + "…" : s;
};

/** Human description of what an event means for the live trace. */
export function describe(e) {
  if (!e) return "";
  const p = e.payload || {};
  switch (e.type) {
    case "task_created": return `Task created (${p.mode}${p.agent ? ` → @${p.agent}` : ""})`;
    case "routing_start": return `Router choosing an agent with ${p.model}`;
    case "routing_decision": return `Routed: ${p.mode} → @${p.agent_id} (confidence ${Number(p.confidence).toFixed(2)})`;
    case "routing_needs_user": return "Router unsure — waiting for you to pick an agent";
    case "planning_start": return `Supervisor planning with ${p.model}`;
    case "plan_created": return `Plan ready: ${p.steps?.length} step(s)`;
    case "step_planned": return `Assigned to @${p.agent_id} (${p.how})`;
    case "step_dispatched": return `Step ${p.key} dispatched to @${p.agent}`;
    case "step_claimed": return `Claimed by worker ${p.worker}${p.resumed_from ? ` (took over from ${p.resumed_from})` : ""}`;
    case "resumed_by": return `Resumed ${p.from_checkpoint ? "from checkpoint" : "from ledger"} on ${p.worker}`;
    case "llm_start": return `Thinking with ${p.model} (${p.model_name}) · turn ${p.turn}`;
    case "llm_turn": return p.tool_calls?.length ? `Decided to call ${p.tool_calls.map((t) => t.name).join(", ")}` : `Wrote an answer (${p.tokens_out} tokens)`;
    case "tool_call": return `Running ${p.tool}(${short(p.args)})`;
    case "tool_result": return `${p.tool} → ${p.status} in ${p.latency_ms} ms`;
    case "tool_progress": return `${p.tool}: ${p.message}`;
    case "tool_call_invalid": return `Invalid tool call: ${short(p.errors)}`;
    case "approval_requested": return `Waiting for approval: ${p.tool}(${short(p.args)})`;
    case "approval_decided": return `Approval ${p.decision}: ${p.tool}`;
    case "step_waiting_approval": return "Paused until a person approves";
    case "artifact_saved": return `Saved artifact ${p.name}`;
    case "step_output": return "Step output submitted for review";
    case "review_start": return `Verifying (${p.profile}) with ${p.model}`;
    case "verification": return `Verification ${p.accepted ? "passed" : "failed"}${p.first_pass ? " (first pass)" : ""}`;
    case "step_revision": return `Revision requested (attempt ${p.attempt}): ${short(p.correction, 120)}`;
    case "step_reassigned": return `Reassigned @${p.from} → @${p.to}`;
    case "step_done": return `Step ${p.key} done`;
    case "step_failed": return `Step failed: ${short(p.error || p.reason)}`;
    case "halted": return `Guard fired: ${p.guard} ${p.detail ? `— ${short(p.detail)}` : ""}`;
    case "budget_paused": return `Budget stop: ${p.detail}`;
    case "stalled": return "Step stalled — lease released for another worker";
    case "escalated": return `Escalated to a person: ${short(p.detail, 120)}`;
    case "synthesis_start": return `Supervisor writing the final answer with ${p.model}`;
    case "task_completed": return `Task completed · ${money(p.cost?.cost_usd)}`;
    case "ledger_note": return `Ledger note: ${short(p.note, 120)}`;
    case "team_message": return `Posted to #${p.channel}: ${short(p.text)}`;
    default: return e.type.replaceAll("_", " ");
  }
}

export const ACTIVE = ["routing", "planning", "running", "finalizing"];
