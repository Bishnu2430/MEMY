"""ElevenLabs media: speech, voices, sound effects, music, transcription, image and video generation.

Every result is downloaded immediately (ElevenLabs content URLs expire after ~1 hour) and stored as an
artifact. Plan limits surface as MediaError with a readable message instead of a raw HTTP error.
"""
import asyncio
import logging
import time

import httpx

from . import artifacts, db
from .config import settings
from .gateway import UsageMeta
from .security import decrypt

log = logging.getLogger("media")
API = "https://api.elevenlabs.io"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # "George" premade voice
TTS_MODEL = "eleven_multilingual_v2"
IMAGE_MODELS = ["gemini-2.5-flash-image", "gpt-image-1", "gemini-3.1-flash-lite-image", "bytedance-seedream-5-lite"]
VIDEO_MODELS = ["veo-3.1-fast-generate-001", "bytedance-seedance-v2-mini", "veo-3.1-generate-001"]
_voice_cache: tuple[float, list] = (0.0, [])
_sub_cache: tuple[float, dict] = (0.0, {})


class MediaError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str = "media_error"):
        super().__init__(message)
        self.status, self.code = status, code


async def api_key() -> str:
    if settings.elevenlabs_api_key:
        return settings.elevenlabs_api_key
    row = await db.fetchrow("SELECT ciphertext, nonce FROM credentials WHERE provider='elevenlabs'")
    if not row:
        raise MediaError("ElevenLabs is not configured: add ELEVENLABS_API_KEY to .env or a key under Admin > Provider keys.",
                         code="not_configured")
    return decrypt(row["ciphertext"], row["nonce"])


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, headers={"xi-api-key": await api_key()}, timeout=httpx.Timeout(180, connect=15))


def _raise_for(r: httpx.Response, what: str):
    if r.status_code < 400:
        return
    try:
        detail = r.json().get("detail")
    except Exception:
        detail = r.text[:300]
    code = detail.get("status") if isinstance(detail, dict) else None
    msg = detail.get("message") if isinstance(detail, dict) else str(detail)
    if r.status_code == 402 or code in ("paid_plan_required", "payment_required"):
        tier = _sub_cache[1].get("tier", "current")
        raise MediaError(f"PAID_PLAN_REQUIRED: ElevenLabs {what} needs a paid plan (this workspace is on the {tier} tier).",
                         402, "paid_plan_required")
    if code == "quota_exceeded" or "quota" in str(msg).lower():
        raise MediaError(f"ElevenLabs credit quota exceeded for {what}: {msg}", r.status_code, "quota_exceeded")
    if r.status_code in (401, 403):
        if any(w in what for w in ("image", "video", "music")):
            tier = _sub_cache[1].get("tier", "current")
            raise MediaError(f"PAID_PLAN_REQUIRED: ElevenLabs {what} is not available to this API key. It needs the "
                             f"'Image & Video' (Flows) permission on the key and a Pro plan or above (this workspace is "
                             f"on the {tier} tier).", r.status_code, "feature_unavailable")
        raise MediaError(f"ElevenLabs rejected the key for {what} (missing permission?): {msg}", r.status_code, "permission")
    if r.status_code == 429:
        raise MediaError(f"ElevenLabs rate limit reached for {what}; try again shortly.", 429, "rate_limited")
    raise MediaError(f"ElevenLabs {what} failed ({r.status_code}): {msg}", r.status_code)


async def _meter(meta: UsageMeta | None, kind: str, units: int, latency_ms: int, status: str):
    m = meta or UsageMeta()
    await db.execute(
        "INSERT INTO usage_records(user_id, team_id, agent_id, task_id, step_id, kind, model, provider, tokens_in,"
        " latency_ms, status) VALUES (%s,%s,%s,%s,%s,'media',%s,'elevenlabs',%s,%s,%s)",
        m.user_id, m.team_id, m.agent_id, m.task_id, m.step_id, f"elevenlabs/{kind}", units, latency_ms, status)


# ---------------- account ----------------
async def subscription(force: bool = False) -> dict:
    global _sub_cache
    if not force and time.time() - _sub_cache[0] < 60 and _sub_cache[1]:
        return _sub_cache[1]
    async with await _client() as c:
        r = await c.get("/v1/user/subscription")
    _raise_for(r, "subscription lookup")
    d = r.json()
    out = {"tier": d.get("tier"), "character_count": d.get("character_count"), "character_limit": d.get("character_limit"),
           "next_reset_unix": d.get("next_character_count_reset_unix"), "status": d.get("status")}
    _sub_cache = (time.time(), out)
    return out


async def voices() -> list[dict]:
    global _voice_cache
    if time.time() - _voice_cache[0] < 600 and _voice_cache[1]:
        return _voice_cache[1]
    async with await _client() as c:
        r = await c.get("/v2/voices", params={"page_size": 50})
    _raise_for(r, "voice list")
    out = [{"voice_id": v["voice_id"], "name": v["name"], "category": v.get("category"),
            "preview_url": v.get("preview_url"), "labels": v.get("labels") or {}} for v in r.json().get("voices", [])]
    _voice_cache = (time.time(), out)
    return out


async def resolve_voice(voice: str | None) -> tuple[str, str]:
    if not voice:
        return DEFAULT_VOICE, "George"
    for v in await voices():
        if voice in (v["voice_id"], v["name"]) or v["name"].lower().startswith(voice.lower()):
            return v["voice_id"], v["name"]
    return voice, voice


# ---------------- audio ----------------
async def speech(text: str, voice: str | None = None, model_id: str | None = None, meta: UsageMeta | None = None) -> tuple[bytes, dict]:
    voice_id, voice_name = await resolve_voice(voice)
    t0 = time.time()
    async with await _client() as c:
        r = await c.post(f"/v1/text-to-speech/{voice_id}", params={"output_format": "mp3_44100_128"},
                         json={"text": text[:5000], "model_id": model_id or TTS_MODEL})
    await _meter(meta, "tts", len(text), int((time.time() - t0) * 1000), "ok" if r.status_code < 400 else "error")
    _raise_for(r, "text to speech")
    return r.content, {"voice": voice_name, "voice_id": voice_id, "model": model_id or TTS_MODEL, "characters": len(text)}


async def sound_effect(prompt: str, duration_s: float | None = None, meta: UsageMeta | None = None) -> tuple[bytes, dict]:
    body = {"text": prompt[:1000], "prompt_influence": 0.4}
    if duration_s:
        body["duration_seconds"] = max(0.5, min(30.0, float(duration_s)))
    t0 = time.time()
    async with await _client() as c:
        r = await c.post("/v1/sound-generation", json=body)
    await _meter(meta, "sound", int(body.get("duration_seconds", 5)), int((time.time() - t0) * 1000),
                 "ok" if r.status_code < 400 else "error")
    _raise_for(r, "sound effects")
    return r.content, {"prompt": prompt, "duration_s": body.get("duration_seconds")}


async def music(prompt: str, length_s: int = 30, meta: UsageMeta | None = None) -> tuple[bytes, dict]:
    ms = int(max(3, min(300, length_s)) * 1000)
    t0 = time.time()
    async with await _client() as c:
        r = await c.post("/v1/music", json={"prompt": prompt[:2000], "music_length_ms": ms})
    await _meter(meta, "music", ms // 1000, int((time.time() - t0) * 1000), "ok" if r.status_code < 400 else "error")
    _raise_for(r, "music generation")
    return r.content, {"prompt": prompt, "length_s": ms // 1000}


async def transcribe(data: bytes, filename: str, meta: UsageMeta | None = None) -> dict:
    t0 = time.time()
    async with await _client() as c:
        r = await c.post("/v1/speech-to-text", data={"model_id": "scribe_v1"}, files={"file": (filename, data)})
    await _meter(meta, "stt", len(data) // 16000, int((time.time() - t0) * 1000), "ok" if r.status_code < 400 else "error")
    _raise_for(r, "speech to text")
    d = r.json()
    return {"text": d.get("text", ""), "language": d.get("language_code")}


# ---------------- image & video (async flows: create, then poll) ----------------
async def _flow(kind: str, body: dict, interval: float, timeout_s: int, progress=None, meta: UsageMeta | None = None):
    t0 = time.time()
    async with await _client() as c:
        r = await c.post(f"/v1/flows/{kind}", json=body)
        _raise_for(r, f"{kind} generation")
        gen_id = r.json()["id"]
        if progress:
            await progress(f"{kind} generation {gen_id} submitted ({body['model_id']})")
        status, last = "pending", None
        while time.time() - t0 < timeout_s:
            await asyncio.sleep(interval)
            g = await c.get(f"/v1/flows/{kind}/{gen_id}")
            if g.status_code == 429:
                interval = min(interval * 2, 60)
                continue
            _raise_for(g, f"{kind} status")
            last = g.json()
            status = last.get("status")
            if progress:
                await progress(f"{kind} {status} ({int(time.time() - t0)}s)")
            if status in ("completed", "failed"):
                break
        if status != "completed":
            await _meter(meta, kind, 0, int((time.time() - t0) * 1000), "error")
            reason = (last or {}).get("error") or (last or {}).get("failure_reason") or status
            raise MediaError(f"{kind} generation {status}: {reason}")
        dl = await httpx.AsyncClient(timeout=180).get(last["content_url"])
        dl.raise_for_status()
    await _meter(meta, kind, 1, int((time.time() - t0) * 1000), "ok")
    return dl.content, last.get("content_mime_type") or dl.headers.get("content-type", "application/octet-stream"), gen_id


async def image(prompt: str, model_id: str | None = None, aspect_ratio: str = "1:1", progress=None,
                meta: UsageMeta | None = None) -> tuple[bytes, str, dict]:
    model_id = model_id or IMAGE_MODELS[0]
    data, mime, gid = await _flow("image", {"model_id": model_id, "prompt": prompt[:4000], "aspect_ratio": aspect_ratio},
                                  2.0, 240, progress, meta)
    return data, mime, {"prompt": prompt, "model": model_id, "aspect_ratio": aspect_ratio, "generation_id": gid}


async def video(prompt: str, model_id: str | None = None, duration_secs: int = 4, resolution: str = "720p",
                aspect_ratio: str = "16:9", progress=None, meta: UsageMeta | None = None) -> tuple[bytes, str, dict]:
    model_id = model_id or VIDEO_MODELS[0]
    body = {"model_id": model_id, "prompt": prompt[:4000], "aspect_ratio": aspect_ratio, "resolution": resolution,
            "duration_secs": duration_secs}
    data, mime, gid = await _flow("video", body, 10.0, 900, progress, meta)
    return data, mime, {"prompt": prompt, "model": model_id, "duration_secs": duration_secs, "generation_id": gid}


# ---------------- storage ----------------
EXT = {"audio/mpeg": ".mp3", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4",
       "image/svg+xml": ".svg"}


async def store(*, team_id, task_id, step_id, user_id, name: str, data: bytes, kind: str, mime: str, info: dict) -> dict:
    if "." not in name[-6:]:
        name += EXT.get(mime, "")
    return await artifacts.save(team_id=team_id, task_id=task_id, step_id=step_id, name=name, data=data, kind=kind,
                                mime=mime, meta=info, created_by=user_id)
