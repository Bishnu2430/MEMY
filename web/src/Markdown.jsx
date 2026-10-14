import { useEffect, useId, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import "katex/dist/katex.min.css";
import "highlight.js/styles/github-dark.css";
import CopyButton from "./copy.jsx";

const LANG_LABEL = { python: "Python", py: "Python", js: "JavaScript", javascript: "JavaScript", jsx: "JSX", ts: "TypeScript",
  typescript: "TypeScript", tsx: "TSX", bash: "Shell", sh: "Shell", sql: "SQL", json: "JSON", yaml: "YAML", yml: "YAML",
  html: "HTML", css: "CSS", mermaid: "Mermaid", toml: "TOML", go: "Go", rust: "Rust", java: "Java", diff: "Diff" };

let mermaidPromise = null;
const loadMermaid = () => {
  if (!mermaidPromise) {
    mermaidPromise = import("mermaid").then((m) => {
      m.default.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral",
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif" });
      return m.default;
    });
  }
  return mermaidPromise;
};

export function Mermaid({ code }) {
  const id = "m" + useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    let cancelled = false;
    loadMermaid()
      .then((m) => m.render(`${id}${Date.now()}`, code))
      .then((r) => { if (!cancelled) { setSvg(r.svg); setErr(""); } })
      .catch((e) => { if (!cancelled) setErr(String(e?.message || e)); });
    return () => { cancelled = true; };
  }, [code, id]);
  if (err) return (
    <div className="mermaid-error">
      <div className="muted small">Mermaid could not render this diagram: {err.split("\n")[0]}</div>
      <pre><code>{code}</code></pre>
    </div>
  );
  if (!svg) return <div className="muted small">Rendering diagram…</div>;
  return (
    <div className="mermaid-wrap">
      <div className="codeblock-bar"><span>Diagram</span><CopyButton text={code} label="Copy source" />
        <a className="copy-btn" download="diagram.svg" href={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}>Download SVG</a></div>
      <div className="mermaid-box" dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}

const toText = (node) =>
  node?.type === "text" ? node.value : (node?.children || []).map(toText).join("");

// LLMs often emit \( \) and \[ \] delimiters, or a one-line $$…$$ meant as display math;
// remark-math expects $ for inline and $$ on their own lines for display.
const normalizeMath = (s) =>
  (s || "")
    .replace(/^[ \t]*\$\$([^\n]+?)\$\$[ \t]*$/gm, (_, m) => `\n$$\n${m.trim()}\n$$\n`)
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => `\n$$\n${m.trim()}\n$$\n`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => `$${m.trim()}$`);

const components = {
  pre({ node, children, ...props }) {
    const code = node?.children?.[0];
    const cls = code?.properties?.className || [];
    if (cls.includes("language-mermaid")) return <Mermaid code={toText(code).trim()} />;
    const lang = (cls.find((c) => String(c).startsWith("language-")) || "").replace("language-", "");
    const text = toText(code).replace(/\n$/, "");
    const lines = text.split("\n").length;
    return (
      <div className="codeblock">
        <div className="codeblock-bar">
          <span>{LANG_LABEL[lang] || lang || "Text"}</span>
          <span className="faint">{lines} line{lines === 1 ? "" : "s"}</span>
          <CopyButton text={text} label="Copy code" />
        </div>
        <pre {...props}>{children}</pre>
      </div>
    );
  },
  a({ node, ...props }) {
    return <a target="_blank" rel="noreferrer" {...props} />;
  },
  table({ node, ...props }) {
    return <div className="table-wrap"><table {...props} /></div>;
  },
};

export default function Markdown({ children, className = "" }) {
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex, [rehypeHighlight, { detect: true, plainText: ["mermaid", "text", "txt"] }]]}
        components={components}
      >
        {normalizeMath(typeof children === "string" ? children : String(children ?? ""))}
      </ReactMarkdown>
    </div>
  );
}
