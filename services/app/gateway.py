"""Model gateway: one OpenAI-compatible call path for every hosted model.

Agents name aliases (models.yaml). The gateway resolves an alias to a chain of hosted models,
picks the first one with rate-limit capacity (tracked from provider headers in a table shared by
every process), waits visibly when all are saturated, and writes one usage record per call.
Embeddings come from the small embedder service (not an LLM).
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import yaml

from . import db
from .config import settings
from .security import decrypt

log = logging.getLogger("gateway")
MAX_TOTAL_WAIT_S = 300


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
    truncated: bool = False


class GatewayError(Exception):
    def __init__(self, msg: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def _duration(s: str | None) -> float | None:
    """Parse provider durations like '7.66s', '2m59.5s', '350ms'."""
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def _retry_after(r) -> float:
    for h in ("retry-after", "x-ratelimit-reset-tokens"):
        v = _duration(r.headers.get(h))
        if v:
            return v
    m = re.search(r"try again in ([\dmsh.]+)", r.text)
    return (_duration(m.group(1)) if m else None) or 5.0


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


def estimate_tokens(messages: list, tools: list | None, max_tokens: int) -> int:
    chars = sum(len(m["content"]) if isinstance(m.get("content"), str) else len(json.dumps(m.get("content")))
                for m in messages)
    chars += sum(len(json.dumps(m.get("tool_calls"))) for m in messages if m.get("tool_calls"))
    chars += len(json.dumps(tools)) if tools else 0
    return int(chars / 3.4) + max_tokens


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


WaitCallback = Callable[[float, str], Awaitable[None]]


class ModelGateway:
    def __init__(self):
        with open(settings.models_file) as f:
            cfg = yaml.safe_load(f)
        self.models: dict = cfg["models"]
        self.embedding: dict = cfg["embedding"]
        self.transcription: dict = cfg.get("transcription", {})
        self.breaker = _Breaker()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))
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
        return bool(await self._api_key(cfg)) and not self.breaker.is_open(cfg["alias"])

    async def resolve_chain(self, alias: str) -> list[dict]:
        chain, seen, queue = [], set(), [alias]
        while queue:
            cfg = self._cfg(queue.pop(0))
            if cfg["alias"] in seen:
                continue
            seen.add(cfg["alias"])
            if await self.available(cfg["alias"]):
                chain.append(cfg)
            fb = cfg.get("fallback") or []
            queue.extend(fb if isinstance(fb, list) else [fb])
        if not chain:
            raise GatewayError(f"no configured model for '{alias}' (check provider API keys)")
        return chain

    async def describe(self) -> list[dict]:
        limits = {r["alias"]: r for r in await db.fetch("SELECT * FROM model_limits")}
        out = []
        for alias in self.models:
            real = self._cfg(alias)
            try:
                first = (await self.resolve_chain(alias))[0]["alias"]
            except GatewayError:
                first = None
            lim = limits.get(real.get("model")) or {}
            out.append({"alias": alias, "provider": real.get("provider_label", real["provider"]), "model": real.get("model"),
                        "configured": await self.available(real["alias"]), "resolves_to": first,
                        "family": real.get("family"), "vision": bool(real.get("vision")),
                        "tpm_limit": lim.get("limit_tokens"), "tpm_remaining": self._effective_remaining(lim) if lim else None})
        return out

    async def family(self, alias: str) -> str:
        return (await self.resolve_chain(alias))[0].get("family", "unknown")

    def context_chars(self, cfg: dict) -> int:
        # Free tiers meter tokens per minute, so prompts stay compact regardless of window size.
        return min(int(cfg.get("context", 8192) * 3.2), int(cfg.get("prompt_budget_chars", 14000)))

    # ---------- shared rate-limit state ----------
    @staticmethod
    def _effective_remaining(row: dict) -> int:
        """Token buckets refill continuously: remaining grows at limit/60 per second since the last report."""
        limit = row.get("limit_tokens") or 8000
        elapsed = max(0.0, time.time() - row["updated_at"].timestamp())
        return int(min(limit, (row.get("remaining_tokens") or 0) + elapsed * limit / 60))

    async def daily_used(self, model: str) -> int:
        """Tokens this provider model used in the last 24 h (from our own metering), all aliases combined."""
        aliases = [a for a, c in self.models.items() if c.get("model") == model]
        return await db.fetchval("SELECT coalesce(sum(tokens_in + tokens_out), 0)::int FROM usage_records"
                                 " WHERE kind='llm' AND created_at > now() - interval '24 hours' AND model = ANY(%s)", aliases) or 0

    async def daily_status(self) -> list[dict]:
        seen, out = set(), []
        for alias, c in self.models.items():
            if "alias_of" in c or c.get("model") in seen or not c.get("daily_token_limit"):
                continue
            seen.add(c["model"])
            out.append({"model": c["model"], "label": alias, "limit": c["daily_token_limit"],
                        "used": await self.daily_used(c["model"])})
        return out

    async def _capacity_wait(self, cfg: dict, need: int) -> float:
        limit_day = cfg.get("daily_token_limit")
        if limit_day and await self.daily_used(cfg["model"]) + need > limit_day * 0.98:
            return 86400.0  # daily quota spent: unusable until the rolling window frees up
        row = await db.fetchrow("SELECT * FROM model_limits WHERE alias=%s", cfg["model"])  # keyed by provider model
        if not row:
            return 0.0
        if row["blocked_until"] and row["blocked_until"].timestamp() > time.time():
            return row["blocked_until"].timestamp() - time.time()
        limit = row.get("limit_tokens") or 8000
        if need > limit:  # can never fit; let the provider decide
            return 0.0
        have = self._effective_remaining(row)
        return 0.0 if have >= need else (need - have) / (limit / 60)

    async def _save_limits(self, cfg: dict, r: httpx.Response | None, blocked_s: float | None = None):
        h = r.headers if r is not None else {}
        lim = h.get("x-ratelimit-limit-tokens")
        rem = h.get("x-ratelimit-remaining-tokens")
        await db.execute(
            "INSERT INTO model_limits(alias, limit_tokens, remaining_tokens, blocked_until, updated_at)"
            " VALUES (%s,%s,%s,%s, now()) ON CONFLICT (alias) DO UPDATE SET"
            " limit_tokens=coalesce(EXCLUDED.limit_tokens, model_limits.limit_tokens),"
            " remaining_tokens=coalesce(EXCLUDED.remaining_tokens, model_limits.remaining_tokens),"
            " blocked_until=EXCLUDED.blocked_until, updated_at=now()",
            cfg["model"], int(lim) if lim and lim.isdigit() else None,
            0 if blocked_s else (int(rem) if rem and rem.lstrip("-").isdigit() else None),
            None if not blocked_s else _ts(time.time() + blocked_s))

    # ---------- chat ----------
    async def chat(self, alias: str, messages: list[dict], *, meta: UsageMeta | None = None,
                   tools: list | None = None, json_schema: dict | None = None, temperature: float = 0.2,
                   max_tokens: int = 1200, on_wait: WaitCallback | None = None) -> ChatResult:
        """Use the first model in the alias's chain that has rate-limit capacity. When every model is
        saturated, wait (reporting the wait through on_wait) for the soonest one to refill."""
        chain = await self.resolve_chain(alias)
        need = estimate_tokens(messages, tools, max_tokens)
        fits = [c for c in chain if need <= c.get("max_request_tokens", 10**9)]
        if not fits:
            raise GatewayError(f"request of ~{need} tokens is larger than any model in '{alias}' accepts "
                               f"(max {max(c.get('max_request_tokens', 0) for c in chain)}); shorten the context", 413)
        chain = fits
        too_big: set[str] = set()
        waited, last_err = 0.0, None
        while True:
            waits = []
            for cfg in chain:
                if cfg["alias"] in too_big:
                    continue
                w = await self._capacity_wait(cfg, need)
                if w > 0.5:
                    waits.append((w, cfg))
                    continue
                try:
                    return await self._attempt(cfg, meta, messages, tools, json_schema, temperature, max_tokens)
                except GatewayError as e:
                    last_err = e
                    if e.status == 429:
                        daily = "per day" in str(e)
                        block = max(e.retry_after or 5, 600 if daily else 0)
                        await self._save_limits(cfg, None, blocked_s=block)
                        waits.append((block, cfg))
                    elif e.status == 413:
                        too_big.add(cfg["alias"])  # this model's per-request cap is lower than estimated
                    elif e.status in (401, 403):
                        raise  # bad key: no other model on this provider will do better
            if not waits or waited >= MAX_TOTAL_WAIT_S:
                raise GatewayError(f"all models failed for {alias}: {last_err}", getattr(last_err, "status", None))
            w, cfg = min(waits, key=lambda x: x[0])
            if w > 120:  # daily quotas, not per-minute buckets: waiting here would only look like a hang
                raise GatewayError(f"Daily token quota reached for every model that can serve '{alias}' "
                                   f"(provider free tier: ~200k tokens per model per day, rolling). Try again later or "
                                   f"raise the provider plan.", 429)
            w = min(max(w, 1.0), 65.0)
            if on_wait:
                await on_wait(w, f"all models for '{alias}' are at their rate limit; {cfg['model']} frees up first")
            log.info("rate-limited: waiting %.1fs for %s", w, cfg["alias"])
            await asyncio.sleep(w)
            waited += w

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

    async def _post(self, cfg, body) -> httpx.Response:
        key = await self._api_key(cfg)
        r = await self.http.post(f"{cfg['base_url'].rstrip('/')}/chat/completions", json=body,
                                 headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 429:
            await self._save_limits(cfg, r)
        return r

    async def _call(self, cfg, messages, tools, json_schema, temperature, max_tokens) -> ChatResult:
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
        r = await self._post(cfg, body)
        bad_output = "tool_use_failed" in r.text or "json_validate_failed" in r.text
        if r.status_code == 400 and json_schema and not bad_output:
            # Provider without structured outputs: fall back to prompt-level JSON.
            body.pop("response_format")
            body["messages"] = messages + [{"role": "user", "content":
                "Respond ONLY with JSON matching this schema:\n" + json.dumps(json_schema)}]
            r = await self._post(cfg, body)
        elif r.status_code == 400 and bad_output:
            # Malformed tool call / JSON from the model: retry once at a higher temperature.
            body["temperature"] = min(1.0, temperature + 0.4)
            r = await self._post(cfg, body)
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
                         model=cfg["model"], latency_ms=latency, cost_usd=cost,
                         truncated=choice.get("finish_reason") == "length")
        if json_schema:
            res.parsed = parse_json(content)
            if res.parsed is None:
                why = "output truncated (max_tokens)" if choice.get("finish_reason") == "length" else "invalid JSON"
                raise GatewayError(f"model returned {why}", 422)
        return res

    # ---------- multimodal inputs ----------
    async def describe_image(self, data: bytes, mime: str, *, meta: UsageMeta | None = None, hint: str = "") -> str:
        """Vision model reads an attached image: detailed description plus any visible text."""
        b64 = base64.b64encode(data).decode()
        res = await self.chat("vision", [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image in detail for a teammate who cannot see it: subject, layout, "
             "colours, any charts/diagrams/UI, and transcribe all visible text verbatim under a 'Text in image' heading. "
             + (f"Context from the user: {hint}" if hint else "")},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}],
            meta=meta, max_tokens=900, temperature=0.1)
        return res.content

    async def transcribe(self, data: bytes, filename: str, *, meta: UsageMeta | None = None) -> str:
        """Speech to text for attached audio (Groq Whisper)."""
        cfg = self.transcription
        key = os.environ.get(cfg.get("api_key_env", "GROQ_API_KEY"), "")
        t0 = time.time()
        r = await self.http.post(cfg.get("url", "https://api.groq.com/openai/v1/audio/transcriptions"),
                                 headers={"Authorization": f"Bearer {key}"},
                                 data={"model": cfg.get("model", "whisper-large-v3-turbo"), "response_format": "json"},
                                 files={"file": (filename, data)})
        status = "ok" if r.status_code < 400 else "error"
        await self._record(meta, "llm", {"alias": "transcribe", "provider": "openai_compat"}, 0, 0,
                           cfg.get("usd_per_hour", 0.04) * len(data) / (16000 * 3600), int((time.time() - t0) * 1000), status)
        if r.status_code >= 400:
            raise GatewayError(f"transcription failed {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json().get("text", "")

    # ---------- embeddings (small local embedding model, not an LLM) ----------
    async def embed(self, texts: list[str], *, kind: str = "document", meta: UsageMeta | None = None) -> list[list[float]]:
        out: list[list[float]] = []
        t0 = time.time()
        for i in range(0, len(texts), 64):
            r = await self.http.post(settings.embedder_url.rstrip("/") + "/embed",
                                     json={"texts": [t[:4000] for t in texts[i:i + 64]], "kind": kind})
            if r.status_code >= 400:
                raise GatewayError(f"embed failed {r.status_code}: {r.text[:200]}")
            out.extend(r.json()["embeddings"])
        await self._record(meta, "embed", {"alias": "embedding", "provider": "embedder"},
                           sum(len(t) // 4 for t in texts), 0, 0, int((time.time() - t0) * 1000), "ok")
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


def _ts(epoch: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


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


def request_limit(chain: list[dict]) -> int:
    """Largest request (prompt + output) that every model in the chain accepts."""
    return min((c.get("max_request_tokens", 10**9) for c in chain), default=10**9)
