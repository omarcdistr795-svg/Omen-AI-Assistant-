"""Vector embeddings and similarity search for Omen Memory.

Stays strictly within the 0.3 GB memory budget.
Uses a fast, deterministic local feature-hashed embedding model as the local engine,
with optional adapter support for Ollama embeddings when Ollama is available.
"""

from __future__ import annotations

import array
import hashlib
import json
import logging
import math
import re
import urllib.error
import urllib.request
from typing import Any, Sequence

logger = logging.getLogger(__name__)


def cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot = 0.0
    norm1 = 0.0
    norm2 = 0.0
    for a, b in zip(v1, v2):
        dot += a * b
        norm1 += a * a
        norm2 += b * b
    if norm1 <= 0.0 or norm2 <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (math.sqrt(norm1) * math.sqrt(norm2))))


def vector_to_blob(vec: Sequence[float]) -> bytes:
    """Serialize a vector of floats to bytes using array('f')."""
    return array.array("f", vec).tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    """Deserialize a byte blob into a list of floats."""
    arr = array.array("f")
    arr.frombytes(blob)
    return arr.tolist()


class LocalEmbeddingModel:
    """Deterministic, zero-dependency subword/token hashing embedding model.

    Produces dense, L2-normalized semantic vectors.
    Uses subword character 3-grams, 4-grams, and normalized word tokens with
    murmur-like hashing to capture semantic similarity between related concepts.
    Memory footprint is < 2 MB, well below the 0.3 GB budget.
    """

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim
        self.name = "local_hash_v1"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        if not text or not text.strip():
            return vec

        cleaned = re.sub(r"[^\w\s]", " ", text.lower())
        tokens = cleaned.split()
        if not tokens:
            return vec

        # Word tokens
        for token in tokens:
            weight = 1.0 + math.log(1.0 + len(token))
            idx = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % self.dim
            sign = 1.0 if (idx % 2 == 0) else -1.0
            vec[idx] += sign * weight

            # Character 3-grams within token
            if len(token) >= 3:
                for i in range(len(token) - 2):
                    ngram = token[i : i + 3]
                    n_idx = int(hashlib.md5(ngram.encode("utf-8")).hexdigest(), 16) % self.dim
                    n_sign = 1.0 if (n_idx % 2 == 0) else -1.0
                    vec[n_idx] += n_sign * 0.4

            # Character 4-grams within token
            if len(token) >= 4:
                for i in range(len(token) - 3):
                    ngram = token[i : i + 4]
                    n_idx = int(hashlib.md5(ngram.encode("utf-8")).hexdigest(), 16) % self.dim
                    n_sign = 1.0 if (n_idx % 2 == 0) else -1.0
                    vec[n_idx] += n_sign * 0.2

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [round(x / norm, 6) for x in vec]
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class OllamaEmbeddingModel:
    """Ollama embedding adapter with automatic fallback to LocalEmbeddingModel."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        fallback_dim: int = 128,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.fallback = LocalEmbeddingModel(dim=fallback_dim)
        self.name = f"ollama:{model}"
        self.using_fallback = False

    def embed(self, text: str) -> list[float]:
        try:
            req_data = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            req = urllib.request.Request(
                f"{self.base_url}/api/embeddings",
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                emb = data.get("embedding")
                if isinstance(emb, list) and emb:
                    self.using_fallback = False
                    # L2 normalize
                    norm = math.sqrt(sum(x * x for x in emb))
                    if norm > 0:
                        return [round(x / norm, 6) for x in emb]
                    return emb
        except Exception as exc:
            self.using_fallback = True
            logger.warning("Ollama embedding unavailable (%s); using local fallback", exc)

        return self.fallback.embed(text)


def create_embedding_model(config: Any = None) -> LocalEmbeddingModel | OllamaEmbeddingModel:
    """Factory to instantiate the configured embedding model."""
    dim = 128
    model_type = "local"
    if config:
        dim = int(config.get("memory.embedding_dim", 128))
        model_type = str(config.get("memory.embedding_model", "local"))

    if model_type.lower() == "ollama":
        base_url = str(config.get("providers.adapters.ollama.base_url", "http://127.0.0.1:11434"))
        return OllamaEmbeddingModel(base_url=base_url, fallback_dim=dim)
    return LocalEmbeddingModel(dim=dim)
