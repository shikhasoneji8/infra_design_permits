"""One thin model client. OpenRouter for chat and embeddings; hashing fallback so the
loop still runs (and the demo still resumes) with no network at all."""
from __future__ import annotations

import hashlib
import json
import math
import re

from . import config as C

_client = None


def _openai():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=C.OPENROUTER_API_KEY, base_url=C.OPENROUTER_BASE_URL,
                         default_headers={"HTTP-Referer": "https://github.com/shikhasoneji8/infra_design_permits",
                                          "X-Title": "permit-harness"})
    return _client


def have_llm() -> bool:
    return bool(C.OPENROUTER_API_KEY)


def chat(model: str, system: str, user: str, json_mode: bool = False, temperature: float = 0.2,
         max_tokens: int = 4000) -> str:
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    r = _openai().chat.completions.create(
        model=model, temperature=temperature, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs)
    return r.choices[0].message.content or ""


def extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    return json.loads(text[start:end + 1])


# ------------------------------------------------------------ embeddings ----
def embed(texts: list[str]) -> list[list[float]]:
    if have_llm():
        try:
            r = _openai().embeddings.create(model=C.EMBEDDING_MODEL, input=texts)
            return [d.embedding for d in r.data]
        except Exception as e:  # noqa: BLE001
            print(f"[embed] OpenRouter embeddings failed ({e}); using local hashing embeddings")
    return [_hash_embed(t) for t in texts]


def _hash_embed(text: str, dims: int = C.EMBEDDING_DIMS) -> list[float]:
    """Deterministic bag-of-words hashing embedding. Crude, but cosine similarity on
    shared vocabulary ('generators', 'east', 'wetland') is enough to make retrieval
    meaningful when no embedding API is reachable."""
    v = [0.0] * dims
    toks = re.findall(r"[a-z0-9]+", text.lower())
    grams = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
    for g in grams:
        h = int(hashlib.md5(g.encode()).hexdigest(), 16)
        v[h % dims] += 1.0 if (h >> 8) % 2 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]
