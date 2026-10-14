"""Model gateway: one OpenAI-compatible call path for Ollama and hosted APIs.

Agents name aliases (models.yaml). The gateway resolves an alias, falls back when a
provider is unconfigured or failing (per-provider circuit breaker), and writes one
usage record per call.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx
import yaml

from . import db
from .config import settings
from .security import decrypt

log = logging.getLogger("gateway")


@dataclass
class UsageMeta:
    user_id: str | None = None
    team_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    step_id: str | None = None


@dataclass
class ChatResult:
    content: str
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    alias: str = ""
    model: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    parsed: dict | None = None


class GatewayError(Exception):
    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def _retry_after(r) -> float:
    h = r.headers.get("retry-after")
    if h:
        try:
            return float(h)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", r.text)
    if m:
        return float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + 60 * int(m.group(1) or 0)
    return 5.0


def strictify(schema):
    """Strict structured outputs need every object closed and every property required."""
    if isinstance(schema, dict):
        out = {k: strictify(v) for k, v in schema.items()}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(schema, list):
        return [strictify(x) for x in schema]
    return schema


class _Breaker:
    def __init__(self):
        self.failures: dict[str, list[float]] = {}
        self.open_until: dict[str, float] = {}

    def is_open(self, key):
        return self.open_until.get(key, 0) > time.time()

    def fail(self, key):
        now = time.time()
        hist = [t for t in self.failures.get(key, []) if now - t < 60] + [now]
        self.failures[key] = hist
        if len(hist) >= 3:
            self.open_until[key] = now + 30
            log.warning("circuit open for %s", key)

    def ok(self, key):
        self.failures.pop(key, None)


class ModelGateway:
    def __init__(self):
        with open(settings.models_file) as f:
            cfg = yaml.safe_load(f)
        self.models: dict = cfg["models"]
        self.embedding: dict = cfg["embedding"]
        self.breaker = _Breaker()
        self.limited: dict[str, float] = {}
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        self._cred_cache: dict[str, tuple[float, str | None]] = {}

    # ---------- alias resolution ----------
    def _cfg(self, alias: str) -> dict:
        seen = set()
        while alias in self.models and "alias_of" in self.models[alias]:
            if alias in seen:
                break
            seen.add(alias)
            alias = self.models[alias]["alias_of"]
        if alias not in self.models:
            raise GatewayError(f"unknown model alias {alias}")
        return {"alias": alias, **self.models[alias]}

    async def _api_key(self, cfg: dict) -> str | None:
        env = cfg.get("api_key_env")
        if env and os.environ.get(env):
            return os.environ[env]
        cred = cfg.get("credential")
        if not cred:
            return None
        cached = self._cred_cache.get(cred)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        row = await db.fetchrow("SELECT ciphertext, nonce FROM credentials WHERE provider=%s", cred)
        key = decrypt(row["ciphertext"], row["nonce"]) if row else None
        self._cred_cache[cred] = (time.time(), key)
        return key

    async def available(self, alias: str) -> bool:
        cfg = self._cfg(alias)
        if cfg["provider"] == "ollama":
            return not self.breaker.is_open(cfg["alias"])
        return bool(await self._api_key(cfg)) and not self.breaker.is_open(cfg["alias"])

    async def resolve_chain(self, alias: str) -> list[dict]:
        chain, seen, queue = [], set(), [alias]
        while queue:
            cur = queue.pop(0)
            cfg = self._cfg(cur)
            if cfg["alias"] in seen:
                continue
            seen.add(cfg["alias"])
            if await self.available(cfg["alias"]):
                chain.append(cfg)
            fb = cfg.get("fallback") or []
            queue.extend(fb if isinstance(fb, list) else [fb])
        if not any(c["provider"] == "ollama" for c in chain) and "local-fast" in self.models:
            chain.append(self._cfg("local-fast"))  # local model is always the last resort
        return chain

    async def describe(self) -> list[dict]:
        out = []
        for alias, cfg in self.models.items():
            real = self._cfg(alias)
            chain = await self.resolve_chain(alias)
            out.append({"alias": alias, "provider": real["provider"], "model": real.get("model"),
                        "configured": await self.available(real["alias"]),
                        "resolves_to": chain[0]["alias"], "family": real.get("family")})
        return out

    async def family(self, alias: str) -> str:
        return (await self.resolve_chain(alias))[0].get("family", "unknown")

    def context_chars(self, cfg: dict) -> int:
        # Hosted free tiers meter tokens per minute, so prompts stay compact regardless of window size.
        return min(int(cfg.get("context", 8192) * 3.2), int(cfg.get("prompt_budget_chars", 24000)))

    # ---------- chat ----------
    async def chat(self, alias: str, messages: list[dict], *, meta: UsageMeta | None = None,
                   tools: list | None = None, json_schema: dict | None = None,
                   temperature: float = 0.2, max_tokens: int = 1500) -> ChatResult:
        """Walk the fallback chain. A rate-limited hosted model is skipped while another hosted model
        has capacity; if all are limited briefly the gateway waits; local models are the last resort."""
        chain = await self.resolve_chain(alias)
        hosted = [c for c in chain if c["provider"] != "ollama"]
        local = [c for c in chain if c["provider"] == "ollama"]
        last_err = None
        for _ in range(4):
            for cfg in hosted:
                if self.limited.get(cfg["alias"], 0) > time.time():
                    continue
                try:
                    return await self._attempt(cfg, meta, messages, tools, json_schema, temperature, max_tokens)
                except GatewayError as e:
                    last_err = e
                    if e.status == 429:
                        self.limited[cfg["alias"]] = time.time() + (e.retry_after or 5)
            waits = [self.limited.get(c["alias"], 0) - time.time() for c in hosted]
            soonest = min(waits) if waits else 999
            if hosted and 0 < soonest <= 60:
                log.info("hosted models rate-limited; waiting %.1fs", soonest)
                await asyncio.sleep(soonest + 0.2)
                continue
            break
        for cfg in local:
            try:
                return await self._attempt(cfg, meta, messages, tools, json_schema, temperature, max_tokens)
            except GatewayError as e:
                last_err = e
        raise GatewayError(f"all models failed for {alias}: {last_err}")

    async def _attempt(self, cfg, meta, messages, tools, json_schema, temperature, max_tokens) -> ChatResult:
        try:
            res = await self._call(cfg, messages, tools, json_schema, temperature, max_tokens)
        except Exception as e:
            err = e if isinstance(e, GatewayError) else GatewayError(f"{type(e).__name__}: {e}")
            if err.status is None or err.status >= 500:
                self.breaker.fail(cfg["alias"])  # outages only; 4xx and rate limits are not outages
            await self._record(meta, "llm", cfg, 0, 0, 0, 0, "rate_limited" if err.status == 429 else "error")
            log.warning("model %s failed: %s", cfg["alias"], str(err)[:300])
            raise err
        self.breaker.ok(cfg["alias"])
        await self._record(meta, "llm", cfg, res.tokens_in, res.tokens_out, res.cost_usd, res.latency_ms, "ok")
        return res

    async def _call(self, cfg, messages, tools, json_schema, temperature, max_tokens) -> ChatResult:
        if cfg["provider"] == "ollama":
            base, key = settings.ollama_base_url.rstrip("/") + "/v1", "ollama"
        else:
            base, key = cfg["base_url"].rstrip("/"), await self._api_key(cfg)
        body = {"model": cfg["model"], "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, "stream": False}
        body.update(cfg.get("extra") or {})
        if tools:
            body["tools"] = tools
        if json_schema:
            strict = bool(cfg.get("strict_json"))
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "output", "schema": strictify(json_schema) if strict else json_schema, "strict": strict}}
        t0 = time.time()
        headers = {"Authorization": f"Bearer {key}"}
        r = await self.http.post(f"{base}/chat/completions", json=body, headers=headers)
        bad_output = "tool_use_failed" in r.text or "json_validate_failed" in r.text
        if r.status_code == 400 and json_schema and not bad_output:
            # Provider without structured outputs: fall back to prompt-level JSON.
            body.pop("response_format")
            body["messages"] = messages + [{"role": "user", "content":
                "Respond ONLY with JSON matching this schema:\n" + json.dumps(json_schema)}]
            r = await self.http.post(f"{base}/chat/completions", json=body, headers=headers)
        elif r.status_code == 400 and bad_output:
            # Malformed tool call / JSON from the model: retry once at a higher temperature.
            body["temperature"] = min(1.0, temperature + 0.4)
            r = await self.http.post(f"{base}/chat/completions", json=body, headers=headers)
        if r.status_code == 429:
            raise GatewayError(f"429: {r.text[:300]}", 429, retry_after=_retry_after(r))
        if r.status_code >= 400:
            raise GatewayError(f"{r.status_code}: {r.text[:300]}", r.status_code)
        data = r.json()
        latency = int((time.time() - t0) * 1000)
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage") or {}
        tin, tout = usage.get("prompt_tokens", 0) or 0, usage.get("completion_tokens", 0) or 0
        cost = tin / 1e6 * cfg.get("input_per_mtok", 0) + tout / 1e6 * cfg.get("output_per_mtok", 0)
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"__invalid_json__": args}
            calls.append({"id": tc.get("id") or f"call_{len(calls)}", "name": fn.get("name", ""), "arguments": args or {}})
        content = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        res = ChatResult(content=content, tool_calls=calls, tokens_in=tin, tokens_out=tout, alias=cfg["alias"],
                         model=cfg["model"], latency_ms=latency, cost_usd=cost)
        if json_schema:
            res.parsed = parse_json(content)
            if res.parsed is None:
                why = "output truncated (max_tokens)" if choice.get("finish_reason") == "length" else "invalid JSON"
                raise GatewayError(f"model returned {why}", 422)
        return res

    # ---------- embeddings ----------
    async def embed(self, texts: list[str], *, kind: str = "document", meta: UsageMeta | None = None) -> list[list[float]]:
        prefix = self.embedding.get("document_prefix" if kind == "document" else "query_prefix", "")
        out: list[list[float]] = []
        t0 = time.time()
        for i in range(0, len(texts), 32):
            batch = [prefix + t[:6000] for t in texts[i:i + 32]]
            r = await self.http.post(settings.ollama_base_url.rstrip("/") + "/api/embed",
                                     json={"model": self.embedding["model"], "input": batch, "truncate": True})
            if r.status_code >= 400:
                raise GatewayError(f"embed failed {r.status_code}: {r.text[:200]}")
            out.extend(r.json()["embeddings"])
        cfg = {"alias": "embedding", "provider": "ollama", "model": self.embedding["model"]}
        await self._record(meta, "embed", cfg, sum(len(t) // 4 for t in texts), 0, 0, int((time.time() - t0) * 1000), "ok")
        return out

    @property
    def embedding_model(self) -> str:
        return self.embedding["model"]

    async def _record(self, meta, kind, cfg, tin, tout, cost, latency, status):
        m = meta or UsageMeta()
        try:
            await db.execute(
                "INSERT INTO usage_records(user_id, team_id, agent_id, task_id, step_id, kind, model, provider, "
                "tokens_in, tokens_out, cost_usd, latency_ms, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                m.user_id, m.team_id, m.agent_id, m.task_id, m.step_id, kind, cfg.get("alias"), cfg.get("provider"),
                tin, tout, cost, latency, status)
        except Exception as e:
            log.warning("usage record failed: %s", e)


def parse_json(text: str):
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


async def record_tool_usage(meta: UsageMeta, tool: str, latency_ms: int, status: str, cost: float = 0.0):
    await db.execute(
        "INSERT INTO usage_records(user_id, team_id, agent_id, task_id, step_id, kind, model, provider, latency_ms, status, cost_usd)"
        " VALUES (%s,%s,%s,%s,%s,'tool',%s,'tool',%s,%s,%s)",
        meta.user_id, meta.team_id, meta.agent_id, meta.task_id, meta.step_id, tool, latency_ms, status, cost)


_gateway: ModelGateway | None = None


def gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
