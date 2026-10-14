import { useEffect, useState } from "react";
import { ago, api } from "../api.js";
import { useApp } from "../App.jsx";
import { fmtBytes } from "../components.jsx";
import Icon from "../icons.jsx";
import Markdown from "../Markdown.jsx";
import CopyButton from "../copy.jsx";
import Select from "../select.jsx";

const LANG = { py: "python", js: "javascript", jsx: "jsx", ts: "typescript", tsx: "tsx", sql: "sql", css: "css", html: "html",
  json: "json", yml: "yaml", yaml: "yaml", sh: "bash", toml: "toml" };

function tree(files) {
  const root = {};
  for (const f of files) {
    const parts = f.path.split("/");
    let node = root;
    parts.forEach((p, i) => { node[p] = node[p] || (i === parts.length - 1 ? { __file: f } : {}); node = node[p]; });
  }
  return root;
}

function Tree({ node, depth = 0, open, onOpen }) {
  return Object.keys(node).sort((a, b) => !!node[a].__file - !!node[b].__file || a.localeCompare(b)).map((k) => node[k].__file ? (
    <div key={k} className={`tree-file ${open === node[k].__file.path ? "active" : ""}`} style={{ paddingLeft: 10 + depth * 14 }}
      onClick={() => onOpen(node[k].__file.path)} role="button" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onOpen(node[k].__file.path)}>
      <Icon name="file" size={14} /> {k} <span className="faint tiny right">{fmtBytes(node[k].__file.size)}</span>
    </div>
  ) : (
    <div key={k}>
      <div className="tree-dir" style={{ paddingLeft: 10 + depth * 14 }}><Icon name="projects" size={14} /> {k}</div>
      <Tree node={node[k]} depth={depth + 1} open={open} onOpen={onOpen} />
    </div>
  ));
}

export default function Projects() {
  const { team } = useApp();
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState("");
  const [files, setFiles] = useState([]);
  const [open, setOpen] = useState("");
  const [content, setContent] = useState(null);

  useEffect(() => {
    if (!team) return;
    api(`/workspace/projects?team_id=${team}`).then((p) => { setProjects(p); if (p.length && !project) setProject(p[0].project); }).catch(() => {});
  }, [team]);
  useEffect(() => {
    if (!project) return;
    api(`/workspace/files?team_id=${team}&project=${encodeURIComponent(project)}`).then(setFiles).catch(() => setFiles([]));
    setOpen(""); setContent(null);
  }, [project, team]);
  const view = (path) => {
    setOpen(path);
    api(`/workspace/file?team_id=${team}&project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`)
      .then((r) => setContent(r.content)).catch((e) => setContent(`(${e.message})`));
  };
  const ext = open.split(".").pop();

  return (
    <div>
      <div className="page-head"><h2>Projects</h2>
        <span className="muted">files agents write through the workspace MCP server — scoped to this team</span></div>
      {projects.length === 0 ? (
        <div className="card muted">No project files yet. Start a task with a project name (or ask the coder or frontend agent to
          write files into the project workspace) and they will appear here.</div>
      ) : (
        <div className="grid-side">
          <div className="card list">
            <label className="field">Project
              <Select value={project} onChange={(e) => setProject(e.target.value)}>
                {projects.map((p) => <option key={p.project} value={p.project}>{p.project} · {p.files} files</option>)}
              </Select></label>
            <div className="faint tiny" style={{ margin: "4px 0 8px" }}>
              {fmtBytes(projects.find((p) => p.project === project)?.bytes)} · updated {ago(new Date((projects.find((p) => p.project === project)?.updated || 0) * 1000))}</div>
            <Tree node={tree(files)} open={open} onOpen={view} />
          </div>
          <div className="card">
            {open ? <>
              <div className="row"><b className="mono">{open}</b><span className="faint tiny">{content ? `${content.split("\n").length} lines` : ""}</span>
                {content && <span className="answer-actions right" style={{ margin: 0 }}><CopyButton text={content} label="Copy file" />
                  <a href={`data:text/plain;charset=utf-8,${encodeURIComponent(content)}`} download={open.split("/").pop()}>Download</a></span>}</div>
              {content === null ? <div className="muted">Loading…</div>
                : ext === "md" ? <Markdown>{content}</Markdown>
                : <Markdown>{"```" + (LANG[ext] || "") + "\n" + content + "\n```"}</Markdown>}
            </> : <div className="muted">Select a file to view it.</div>}
          </div>
        </div>
      )}
    </div>
  );
}
