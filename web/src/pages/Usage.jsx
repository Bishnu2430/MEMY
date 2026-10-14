import { useMemo, useState } from "react";
import { api, money, num } from "../api.js";
import { Bar, useAgents, useInterval, Status } from "../components.jsx";
import { USAGE_KIND, modelLabel, providerLabel } from "../labels.js";
import Select from "../select.jsx";

const SLOTS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

/** Daily tokens stacked by model. Fixed slot per model (by total, max 3 + Other); legend + hover tooltip + table view. */
function DailyTokens({ rows, period }) {
  const [hover, setHover] = useState(null);
  const [table, setTable] = useState(false);
  const { days, series, max } = useMemo(() => {
    const totals = {};
    rows.forEach((r) => { totals[r.key] = (totals[r.key] || 0) + r.tokens; });
    const ranked = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
    const keep = ranked.slice(0, 3);
    const series = [...keep, ...(ranked.length > 3 ? ["other"] : [])];
    const byDay = {};
    rows.forEach((r) => {
      const k = keep.includes(r.key) ? r.key : "other";
      const d = (byDay[r.day] = byDay[r.day] || { day: r.day, total: 0 });
      d[k] = (d[k] || 0) + r.tokens; d.total += r.tokens;
    });
    // Every day of the period gets a slot, so gaps read as "no usage" rather than disappearing.
    const days = [];
    for (let i = period - 1; i >= 0; i--) {
      const key = new Date(Date.now() - i * 86400000).toISOString().slice(0, 10);
      days.push(byDay[key] || { day: key, total: 0 });
    }
    return { days, series, max: Math.max(1, ...days.map((d) => d.total)) };
  }, [rows, period]);
  if (!rows.length) return <div className="muted small">No model usage in this period.</div>;
  const W = 640, H = 220, L = 52, B = 26, T = 8;
  const bw = Math.min(46, (W - L - 10) / days.length - 8);
  const step = (W - L - 10) / days.length;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const ticks = [0, 0.5, 1].map((f) => Math.round(max * f));
  const color = (k) => (k === "other" ? "var(--series-other)" : SLOTS[series.indexOf(k)]);
  return (
    <div className="chart">
      <div className="chart-head">
        <div className="legend inline" aria-label="Legend">
          {series.map((k) => <span key={k}><i className="swatch" style={{ background: color(k) }} />{k === "other" ? "Other models" : modelLabel(k)}</span>)}
        </div>
        <button className="link small right" onClick={() => setTable(!table)}>{table ? "Show chart" : "Show table"}</button>
      </div>
      {table ? (
        <table><thead><tr><th>Day</th>{series.map((k) => <th key={k}>{k === "other" ? "Other" : modelLabel(k)}</th>)}<th>Total</th></tr></thead>
          <tbody>{days.map((d) => <tr key={d.day}><td>{d.day}</td>{series.map((k) => <td key={k}>{num(d[k] || 0)}</td>)}<td><b>{num(d.total)}</b></td></tr>)}</tbody></table>
      ) : (
        <div className="chart-wrap" onMouseLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Tokens per day by model">
            {ticks.map((t) => <g key={t}><line x1={L} x2={W - 4} y1={y(t)} y2={y(t)} className="grid" />
              <text x={L - 6} y={y(t) + 4} className="axis" textAnchor="end">{t >= 1000 ? `${Math.round(t / 1000)}k` : t}</text></g>)}
            {days.map((d, i) => {
              let acc = 0;
              const x = L + i * step + (step - bw) / 2;
              return (
                <g key={d.day} onMouseEnter={() => setHover(d.total ? { d, x: x + bw / 2 } : null)}>
                  <rect x={L + i * step} y={T} width={step} height={H - T - B} fill="transparent" />
                  {series.map((k) => {
                    const v = d[k] || 0;
                    if (!v) return null;
                    const top = y(acc + v), h = y(acc) - y(acc + v);
                    acc += v;
                    return <rect key={k} x={x} y={top + 1} width={bw} height={Math.max(0, h - 2)} rx={3} fill={color(k)} />;
                  })}
                  {(days.length <= 10 || i % Math.ceil(days.length / 10) === 0) &&
                    <text x={x + bw / 2} y={H - 8} className="axis" textAnchor="middle">{new Date(d.day + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })}</text>}
                </g>
              );
            })}
          </svg>
          {hover && (
            <div className="tooltip" style={{ left: `${(hover.x / W) * 100}%` }}>
              <b>{new Date(hover.d.day + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</b>
              {series.filter((k) => hover.d[k]).map((k) => <div key={k}><i className="swatch" style={{ background: color(k) }} />{k === "other" ? "Other" : modelLabel(k)} <b>{num(hover.d[k])}</b></div>)}
              <div className="faint">Total {num(hover.d.total)} tokens</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Single-series horizontal bars: cost (or tokens when cost is zero) per agent, value labelled at the bar end. */
function AgentBars({ rows, names }) {
  const useCost = rows.some((r) => r.cost_usd > 0);
  const val = (r) => (useCost ? r.cost_usd : r.tokens);
  const max = Math.max(...rows.map(val), 1e-9);
  return (
    <div className="hbars">
      {rows.slice(0, 10).map((r) => (
        <div key={r.key} className="hbar" title={`${num(r.tokens)} tokens · ${money(r.cost_usd)} · ${r.calls} calls`}>
          <span className="hbar-label">{names[r.key] || (r.key === "-" ? "Unattributed" : `@${r.key}`)}</span>
          <span className="hbar-track"><span className="hbar-fill" style={{ width: `${(val(r) / max) * 100}%` }} /></span>
          <span className="hbar-value">{useCost ? money(r.cost_usd) : `${num(r.tokens)} tok`}</span>
        </div>
      ))}
    </div>
  );
}

const ROLE_NAMES = { router: "Router", supervisor: "Supervisor", verifier: "Verifier", recorder: "Knowledge recorder",
  inputs: "File reader", studio: "Studio" };

export default function Usage() {
  const [days, setDays] = useState(7);
  const [d, setD] = useState(null);
  const agents = useAgents();
  useInterval(() => api(`/usage?days=${days}`).then(setD).catch(() => {}), 6000, [days]);
  if (!d) return <div className="muted">Loading…</div>;
  const names = { ...ROLE_NAMES, ...Object.fromEntries(agents.map((a) => [a.id, a.name])) };
  const q = Object.fromEntries((d.quality || []).map((r) => [r.agent, r]));
  const r = d.routing || {};
  const t = d.totals;
  return (
    <div>
      <div className="page-head"><h2>Usage</h2>
        <Select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
          {[1, 7, 30].map((x) => <option key={x} value={x}>Last {x === 1 ? "24 hours" : `${x} days`}</option>)}</Select>
        <span className="muted small">every model call, tool call and media generation is metered</span></div>
      <div className="grid4">
        <div className="card stat"><div className="muted small">Estimated cost</div><div className="big">{money(t.cost_usd)}</div></div>
        <div className="card stat"><div className="muted small">Tokens (in / out)</div><div className="big">{num(t.tokens_in)} <span className="faint">/</span> {num(t.tokens_out)}</div></div>
        <div className="card stat"><div className="muted small">Model calls</div><div className="big">{num(t.llm_calls)}</div>
          {t.rate_limited > 0 && <div className="faint tiny">{num(t.rate_limited)} rate-limited attempts retried</div>}</div>
        <div className="card stat"><div className="muted small">Tool calls · media</div><div className="big">{num(t.tool_calls)} <span className="faint">·</span> {num(t.media_calls)}</div></div>
      </div>
      {(d.daily_quota || []).length > 0 && (
        <div className="card">
          <h3>Daily model quota</h3>
          <p className="muted small">Groq's free tier allows a fixed number of tokens per model on a rolling 24-hour window. When a model runs
            out, work moves to the others; when all are out, tasks stop with a clear message instead of waiting.</p>
          <div className="quota">
            {d.daily_quota.map((m) => {
              const pct = m.used / m.limit;
              return (
                <div key={m.model} className="quota-row">
                  <span className="hbar-label">{modelLabel(m.model)}</span>
                  <Bar value={m.used} max={m.limit} label={`${num(m.used)} of ${num(m.limit)} tokens in the last 24 h`} />
                  <span className="small nowrap">{num(m.used)} / {num(m.limit)} <span className={pct >= 0.95 ? "error" : pct >= 0.8 ? "warn-text" : "faint"}>
                    {pct >= 0.95 ? "exhausted" : pct >= 0.8 ? "nearly used" : `${Math.round((1 - pct) * 100)}% left`}</span></span>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div className="grid2">
        <div className="card"><h3>Tokens per day by model</h3><DailyTokens rows={d.by_day_model || []} period={days === 1 ? 1 : days} /></div>
        <div className="card"><h3>{d.by_agent.some((x) => x.cost_usd > 0) ? "Cost by agent" : "Tokens by agent"}</h3><AgentBars rows={d.by_agent} names={names} /></div>
      </div>
      <div className="grid2">
        <div className="card">
          <h3>By model</h3>
          <table><thead><tr><th>Model</th><th>Provider</th><th>Calls</th><th>Tokens</th><th>Cost</th><th title="Failed or rate-limited attempts">Errors</th></tr></thead>
            <tbody>{d.by_model.map((m) => (
              <tr key={m.key + m.provider}><td>{modelLabel(m.key)}</td><td className="small">{providerLabel(m.provider)}</td><td>{num(m.calls)}</td>
                <td>{num(m.tokens)}</td><td>{money(m.cost_usd)}</td><td>{m.errors ? <span className="warn-text">{m.errors}</span> : "—"}</td></tr>))}</tbody></table>
          <h4>By activity</h4>
          <table><tbody>{(d.by_kind || []).map((k) => <tr key={k.key}><td>{USAGE_KIND[k.key] || k.key}</td><td>{num(k.calls)}</td><td>{money(k.cost_usd)}</td></tr>)}</tbody></table>
        </div>
        <div className="card">
          <h3>Response times</h3>
          <table><thead><tr><th>Activity</th><th>Model / tool</th><th title="Number of successful calls">Calls</th><th title="Median">Median</th><th title="95th percentile">95th pct</th></tr></thead>
            <tbody>{d.latency.map((l) => (
              <tr key={l.kind + l.key}><td className="small">{USAGE_KIND[l.kind] || l.kind}</td><td>{modelLabel(l.key)}</td><td>{num(l.n)}</td>
                <td>{fmtMs(l.p50)}</td><td>{fmtMs(l.p95)}</td></tr>))}</tbody></table>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <h3>Quality per agent</h3>
          <table><thead><tr><th>Agent</th><th>Steps</th><th>Completed</th><th title="Accepted by verification without any retry">First-pass verified</th><th title="Steps that needed a retry or reassignment">Needed a retry</th></tr></thead>
            <tbody>{d.steps.map((s) => (
              <tr key={s.agent}><td>{names[s.agent] || `@${s.agent}`}</td><td>{s.steps}</td><td>{s.done}</td>
                <td>{q[s.agent] ? `${Math.round(q[s.agent].first_pass_rate * 100)}%` : "—"}</td><td>{Math.round(s.retry_rate * 100)}%</td></tr>))}</tbody></table>
          <p className="small muted">Routing: {num(r.auto)} automatic, {num(r.manual)} by @mention, {num(r.asked)} asked the user to choose,
            {" "}{num(r.overrides)} overridden by a person ({r.auto ? Math.round((r.overrides / r.auto) * 100) : 0}% override rate).</p>
        </div>
        <div className="card">
          <h3>Budget use (active tasks)</h3>
          {d.budgets.length === 0 && <div className="muted small">No active tasks.</div>}
          <table><tbody>{d.budgets.map((b) => {
            const used = Math.max(b.cost_usd / b.budget_usd, b.tokens / b.budget_tokens);
            const label = used >= 1 ? "At limit" : used >= 0.8 ? "Near limit" : "On track";
            return (
              <tr key={b.id}><td><a href={`#/tasks/${b.id}`}>{b.title}</a></td><td><Status s={b.status} /></td>
                <td className="w40"><div className="bar" title={`${Math.round(used * 100)}% of budget`}><div style={{ width: `${Math.min(100, used * 100)}%` }} className={used >= 1 ? "over" : used >= 0.8 ? "warnbar" : ""} /></div></td>
                <td className="small nowrap">{label} · {Math.round(used * 100)}%</td></tr>);
          })}</tbody></table>
          <h3 style={{ marginTop: 16 }}>Most expensive tasks</h3>
          <table><tbody>{d.top_tasks.map((x) => <tr key={x.id}><td><a href={`#/tasks/${x.id}`}>{x.title}</a></td><td>{money(x.cost_usd)}</td><td className="small">{num(x.tokens)} tokens</td></tr>)}</tbody></table>
        </div>
      </div>
    </div>
  );
}

const fmtMs = (ms) => (ms == null ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
