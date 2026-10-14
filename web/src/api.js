import { PROFILE, modelLabel, toolLabel, workerLabel } from "./labels.js";
export { modelLabel } from "./labels.js";
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
  let r;
  try {
    r = await fetch(`/api${path}`, { method, headers, body: payload });
  } catch {
    throw new Error("Can't reach the server. Check that the stack is running, then try again.");
  }
  if (r.status >= 502 && r.status <= 504) throw new Error("The server is restarting or unreachable. Try again in a few seconds.");
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
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
};

const short = (v, n = 90) => {
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s && s.length > n ? s.slice(0, n) + "…" : s;
};


/** Human description of what an event means for the live trace. */
export function describe(e) {
  if (!e) return "";
  const p = e.payload || {};
  const m = modelLabel(p.model, p.model_name);
  switch (e.type) {
    case "task_created": return `Task created${p.agent ? ` for @${p.agent}` : " — auto-routing"}${p.attachments ? ` · ${p.attachments} file(s) attached` : ""}`;
    case "routing_start": return `Choosing the best agent (${m})`;
    case "routing_decision": return p.mode === "split" ? `Needs several agents — handing to the supervisor to plan`
      : `Routed to @${p.agent_id} · ${Math.round(Number(p.confidence) * 100)}% confident`;
    case "routing_needs_user": return "Not sure which agent fits — waiting for you to choose";
    case "routing_user_choice": return `You chose @${p.chosen}`;
    case "planning_start": return `Supervisor is planning the steps (${m})`;
    case "plan_created": return `Plan ready: ${p.steps?.length} step(s) — ${(p.steps || []).map((s) => "@" + s.agent_id).join(" → ")}`;
    case "step_planned": return `Assigned to @${p.agent_id}${p.how === "manual" ? " (you mentioned it)" : p.how === "user_choice" ? " (your choice)" : ""}`;
    case "step_dispatched": return `Step ${p.key} queued for @${p.agent}`;
    case "step_claimed": return p.resumed_from ? `${workerLabel(p.worker)} took over from ${workerLabel(p.resumed_from)}` : `Started on ${workerLabel(p.worker)}`;
    case "resumed_by": return `Resumed ${p.from_checkpoint ? "from its last checkpoint" : "from the ledger"} on ${workerLabel(p.worker)}`;
    case "llm_start": return `Thinking · ${m} · turn ${p.turn}`;
    case "llm_turn": return p.tool_calls?.length ? `Decided to use ${p.tool_calls.map((t) => toolLabel(t.name)).join(", ")}` : `Wrote an answer (${num(p.tokens_out)} tokens)`;
    case "output_truncated": return `Reply was cut off at the output limit (${m}); asking for smaller pieces`;
    case "rate_limit_wait": return `Waiting ${Math.round(p.seconds)}s for model capacity (provider rate limit)`;
    case "tool_call": return `${toolLabel(p.tool)} ${short(p.args, 80)}`;
    case "tool_result": return `${toolLabel(p.tool)} ${p.status === "ok" ? "finished" : "failed"} in ${num(p.latency_ms)} ms`;
    case "tool_progress": return `${toolLabel(p.tool)}: ${p.message}`;
    case "tool_call_invalid": return `Rejected an invalid tool call: ${short(p.errors)}`;
    case "approval_requested": return `Needs your approval: ${toolLabel(p.tool)}`;
    case "approval_decided": return `${toolLabel(p.tool)} was ${p.decision}`;
    case "step_waiting_approval": return "Paused until someone approves";
    case "artifact_saved": return `Produced ${p.name}`;
    case "knowledge_stored": return `Saved to knowledge: ${short(p.title, 90)}`;
    case "recording_start": return "Recording what this task established in the knowledge base";
    case "step_output": return "Answer submitted for verification";
    case "review_start": return p.model === "deterministic checks" ? `Running ${(PROFILE[p.profile] || p.profile).toLowerCase()}` : `Reviewing with ${m} (${(PROFILE[p.profile] || p.profile).toLowerCase()})`;
    case "verification": return p.accepted ? `Verified${p.first_pass ? " on the first try" : ""}` : "Verification failed — sending back with notes";
    case "step_revision": return `Revision ${p.attempt}: ${short(p.correction?.split("\n")[1] || p.correction, 110)}`;
    case "step_reassigned": return `Reassigned from @${p.from} to @${p.to}`;
    case "step_done": return `Step ${p.key} complete`;
    case "step_failed": return `Step failed: ${short(p.error || p.reason)}`;
    case "step_cancelled": return "Step stopped (task cancelled)";
    case "halted": return `Safety guard stopped the step: ${{ loop: "repeated identical tool call", budget: "budget", paused: "kill switch", steps_per_task: "too many steps", config: "configuration" }[p.guard] || p.guard}${p.detail ? ` — ${short(p.detail)}` : ""}`;
    case "budget_paused": return `Paused at the budget limit: ${p.detail}`;
    case "stalled": return "No progress for a while — handing the step to another worker";
    case "escalated": return `Needs a person: ${short(p.detail, 120)}`;
    case "synthesis_start": return `Writing the final answer (${m})`;
    case "task_completed": return `Done · ${money(p.cost?.cost_usd)}`;
    case "task_cancelled": return "Task cancelled";
    case "ledger_note": return `Note: ${short(p.note, 120)}`;
    case "team_message": return `Posted to #${p.channel}: ${short(p.text)}`;
    case "kill_switch": return `Kill switch ${p.paused ? "engaged" : "released"} (${p.scope === "all" ? "everything" : p.scope.split(":")[0]})`;
    default: return e.type.replaceAll("_", " ");
  }
}

export const STATUS = {
  preparing: ["Preparing", "Linking attached files"],
  routing: ["Routing", "Choosing the right agent"],
  planning: ["Planning", "The supervisor is splitting the work into steps"],
  running: ["Working", "Agents are working on it"],
  finalizing: ["Finishing", "Writing the final answer and saving knowledge"],
  completed: ["Done", "Finished and verified"],
  awaiting_user: ["Your input", "Pick which agent should handle this"],
  needs_attention: ["Needs attention", "A step could not be completed automatically"],
  paused_budget: ["Budget limit", "Paused because the task budget was reached"],
  cancelled: ["Cancelled", "Stopped by a person"],
  failed: ["Failed", "Could not be completed"],
  pending: ["Waiting", "Waiting for earlier steps"],
  ready: ["Queued", "Waiting for a free worker"],
  waiting_approval: ["Needs approval", "Waiting for someone to approve an action"],
  review: ["Checking", "Queued for verification"],
  reviewing: ["Checking", "Being verified"],
  done: ["Done", "Verified and accepted"],
  escalated: ["Needs a person", "Retries and reassignment did not resolve it"],
  paused: ["Paused", "Paused by the kill switch or budget"],
  live: ["Live", "Searchable by agents"],
  quarantined: ["Quarantined", "Failed the quality filter; hidden from agents"],
  superseded: ["Superseded", "Replaced by a newer source"],
  approved: ["Approved", ""],
  rejected: ["Rejected", ""],
  needs_review: ["Needs review", "Changed since approval; blocked until an admin re-approves"],
  queued: ["Queued", ""],
  active: ["Active", ""],
  disabled: ["Disabled", ""],
};

export const KNOWLEDGE_KIND = {
  agent_record: "Agent work record", task_knowledge: "Task knowledge", attachment: "Attached file", url: "Web page",
  upload: "Uploaded document", text: "Note", memory: "Approved memory", task_summary: "Task summary",
};

export const ACTIVE = ["preparing", "routing", "planning", "running", "finalizing"];
export const TERMINAL = ["completed", "cancelled", "failed"];
export const canStop = (status) => !!status && !TERMINAL.includes(status);

export const CHECK_LABEL = {
  non_empty_output: "has an answer", code_present: "code present", static_errors: "no syntax errors",
  security_high: "no high-severity security issues", tests: "tests pass", runs: "code runs", sandbox: "sandbox reachable",
  has_citations: "cites sources", citations_stored: "cited sources are stored", mermaid_compiles: "diagram compiles",
  diagram_rendered: "diagram rendered", media_produced: "media produced",
};
