import { useState } from "react";
import { api, money, num } from "../api.js";
import { Bar, Status, useInterval } from "../components.jsx";

function Breakdown({ title, rows }) {
  const max = Math.max(...rows.map((r) => r.cost_usd || 0), 0.000001);
  const maxTok = Math.max(...rows.map((r) => r.tokens || 0), 1);
  return (
    <div className="card">
      <h3>{title}</h3>
      <table><thead><tr><th></th><th>Cost</th><th></th><th>Tokens</th><th>Calls</th></tr></thead><tbody>
        {rows.map((r) => (
          <tr key={r.key + (r.provider || "")}><td>{r.key}{r.provider ? <span className="muted small"> · {r.provider}</span> : ""}</td>
            <td>{money(r.cost_usd)}</td><td className="w40"><Bar value={r.cost_usd > 0 ? r.cost_usd : r.tokens} max={r.cost_usd > 0 ? max : maxTok} /></td>
            <td>{num(r.tokens)}</td><td>{r.calls ?? ""}{r.errors ? <span className="warn-text"> ({r.errors} err)</span> : ""}</td></tr>))}
      </tbody></table>
    </div>
  );
}

export default function Usage() {
  const [days, setDays] = useState(7);
  const [d, setD] = useState(null);
  useInterval(() => api(`/usage?days=${days}`).then(setD).catch(() => {}), 5000, [days]);
  if (!d) return <div className="muted">Loading…</div>;
  const q = Object.fromEntries((d.quality || []).map((r) => [r.agent, r]));
  const r = d.routing || {};
  return (
    <div>
      <div className="page-head"><h2>Usage</h2>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[1, 7, 30].map((x) => <option key={x} value={x}>last {x} days</option>)}</select></div>
      <div className="grid4">
        <div className="card stat"><div className="muted small">Cost</div><div className="big">{money(d.totals.cost_usd)}</div></div>
        <div className="card stat"><div className="muted small">Tokens in / out</div><div className="big">{num(d.totals.tokens_in)} / {num(d.totals.tokens_out)}</div></div>
        <div className="card stat"><div className="muted small">LLM calls</div><div className="big">{num(d.totals.llm_calls)}</div></div>
        <div className="card stat"><div className="muted small">Tool calls</div><div className="big">{num(d.totals.tool_calls)}</div></div>
      </div>
      <div className="grid3">
        <Breakdown title="By agent" rows={d.by_agent} />
        <Breakdown title="By model" rows={d.by_model} />
        <Breakdown title="By team" rows={d.by_team} />
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Quality per agent</h3>
          <table><thead><tr><th>Agent</th><th>Steps</th><th>Done</th><th>First-pass verification</th><th>Retry rate</th></tr></thead><tbody>
            {d.steps.map((s) => (
              <tr key={s.agent}><td>@{s.agent}</td><td>{s.steps}</td><td>{s.done}</td>
                <td>{q[s.agent] ? `${Math.round(q[s.agent].first_pass_rate * 100)}%` : "—"}</td><td>{Math.round(s.retry_rate * 100)}%</td></tr>))}
          </tbody></table>
          <div className="small muted">Routing: {r.auto} auto · {r.manual} manual · {r.asked} asked the user · {r.overrides} overrides
            ({r.auto ? Math.round((r.overrides / r.auto) * 100) : 0}% override rate)</div>
        </div>
        <div className="card">
          <h3>Latency (ms)</h3>
          <table><thead><tr><th>Kind</th><th>Model / tool</th><th>n</th><th>p50</th><th>p95</th></tr></thead><tbody>
            {d.latency.map((l) => <tr key={l.kind + l.key}><td>{l.kind}</td><td>{l.key}</td><td>{l.n}</td><td>{num(l.p50)}</td><td>{num(l.p95)}</td></tr>)}
          </tbody></table>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Budget burn (active tasks)</h3>
          <table><tbody>{d.budgets.map((b) => (
            <tr key={b.id}><td><a href={`#/tasks/${b.id}`}>{b.title}</a></td><td><Status s={b.status} /></td>
              <td className="w40"><Bar value={Math.max(b.cost_usd / b.budget_usd, b.tokens / b.budget_tokens)} max={1} /></td>
              <td className="small">{money(b.cost_usd)} / {money(b.budget_usd)}</td></tr>))}
          </tbody></table>
          {d.budgets.length === 0 && <div className="muted">No active tasks.</div>}
        </div>
        <div className="card">
          <h3>Most expensive tasks</h3>
          <table><tbody>{d.top_tasks.map((t) => <tr key={t.id}><td><a href={`#/tasks/${t.id}`}>{t.title}</a></td><td>{money(t.cost_usd)}</td><td>{num(t.tokens)} tok</td></tr>)}</tbody></table>
        </div>
      </div>
    </div>
  );
}
