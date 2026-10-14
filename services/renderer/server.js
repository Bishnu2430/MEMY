// Minimal HTTP wrapper around mermaid-cli (mmdc). POST /render {source} -> {ok, svg} | {ok:false, error}
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFile } = require("child_process");

const MMDC = "/home/mermaidcli/node_modules/.bin/mmdc";
let queue = Promise.resolve();

function render(source) {
  return new Promise((resolve) => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mmd-"));
    const input = path.join(dir, "in.mmd");
    const output = path.join(dir, "out.svg");
    fs.writeFileSync(input, source);
    execFile(MMDC, ["-p", "/puppeteer-config.json", "-i", input, "-o", output, "-b", "transparent"],
      { timeout: 60000 }, (err, stdout, stderr) => {
        let result;
        if (err || !fs.existsSync(output)) {
          const msg = (stderr || stdout || String(err)).split("\n").filter((l) => !l.includes("at ")).join("\n");
          result = { ok: false, error: msg.slice(0, 2000) };
        } else {
          result = { ok: true, svg: fs.readFileSync(output, "utf8") };
        }
        fs.rmSync(dir, { recursive: true, force: true });
        resolve(result);
      });
  });
}

http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") { res.end("ok"); return; }
  if (req.method !== "POST" || req.url !== "/render") { res.statusCode = 404; res.end(); return; }
  let body = "";
  req.on("data", (c) => { body += c; if (body.length > 200000) req.destroy(); });
  req.on("end", () => {
    let source = "";
    try { source = JSON.parse(body).source || ""; } catch { /* ignore */ }
    // Serialise renders: one headless Chromium at a time keeps memory bounded.
    queue = queue.then(() => render(source)).then((r) => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(r));
    });
  });
}).listen(8080, () => console.log("renderer listening on 8080"));
