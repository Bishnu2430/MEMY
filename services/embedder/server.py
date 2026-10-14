"""Embedding service: BAAI/bge-small-en-v1.5 (384-d) via ONNX on CPU. Small and fast; not an LLM."""
from fastapi import FastAPI
from fastembed import TextEmbedding
from pydantic import BaseModel

app = FastAPI()
model = TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir="/models", threads=2)


class Req(BaseModel):
    texts: list[str]
    kind: str = "document"


@app.get("/health")
def health():
    return {"ok": True, "model": "BAAI/bge-small-en-v1.5", "dims": 384}


@app.post("/embed")
def embed(req: Req):
    gen = model.query_embed(req.texts) if req.kind == "query" else model.passage_embed(req.texts)
    return {"embeddings": [v.tolist() for v in gen]}
