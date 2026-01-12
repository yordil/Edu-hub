"""Text embeddings for semantic search. Bedrock Titan v2 in production; a hashing embedder for tests/offline."""

import asyncio
import hashlib
import json
import math
import os
import re
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings
from app.models.embedding import EMBEDDING_DIM

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the of to in on for and or is are was were be by with what which how does do this that it its as at from".split())


class EmbeddingUnavailable(Exception):
    pass


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class HashingEmbedder:
    """Signed feature hashing of words, word bigrams and character trigrams (so "balance" ~ "balancing").
    Lexical rather than semantic, but free, offline and deterministic."""

    model = f"hashing-v2-{EMBEDDING_DIM}"

    def _one(self, text: str) -> list[float]:
        words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
        features: list[tuple[str, float]] = [(w, 1.0) for w in words]
        features += [(f"{a} {b}", 0.7) for a, b in zip(words, words[1:])]
        features += [(f"#{w[i:i + 3]}", 0.35) for w in words if len(w) > 3 for i in range(len(w) - 2)]
        v = [0.0] * EMBEDDING_DIM
        for feature, weight in features:
            h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
            v[h % EMBEDDING_DIM] += weight if (h >> 32) & 1 else -weight
        return _normalize(v)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class BedrockTitanEmbedder:
    def __init__(self, model: str, region: str, concurrency: int = 8):
        import boto3
        from botocore.config import Config

        self.model = model
        self._client = boto3.client("bedrock-runtime", region_name=region, config=Config(retries={"max_attempts": 3}))
        self._sem = asyncio.Semaphore(concurrency)

    def _invoke(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text[:8000], "dimensions": EMBEDDING_DIM, "normalize": True})
        r = self._client.invoke_model(modelId=self.model, body=body, accept="application/json", contentType="application/json")
        return json.loads(r["body"].read())["embedding"]

    async def _one(self, text: str) -> list[float]:
        async with self._sem:
            return await asyncio.to_thread(self._invoke, text)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return list(await asyncio.gather(*(self._one(t) for t in texts)))
        except Exception as e:  # botocore raises many error types; callers only need "unavailable".
            raise EmbeddingUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "bedrock":
        # boto3 picks up a Bedrock API key from this variable.
        if s.aws_bearer_token_bedrock:
            os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", s.aws_bearer_token_bedrock)
        return BedrockTitanEmbedder(s.bedrock_embedding_model, s.aws_region)
    return HashingEmbedder()
