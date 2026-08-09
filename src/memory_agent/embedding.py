"""Embedders and the token counter.

Both are pluggable and both have a dependency-free fallback, because spec NF4
requires the agent to run fully offline and NF6 requires losing the vector path
to degrade recall rather than break it.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol


class Embedder(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic, dependency-free, offline.

    A hashed bag-of-tokens projected onto the unit sphere. It captures lexical
    overlap and nothing deeper - two paraphrases with no shared words look
    unrelated to it. That is a real limitation, not a hidden one: it exists so
    the service runs and is testable anywhere, and so a missing model degrades
    recall quality visibly instead of taking the server down. Use
    SentenceTransformerEmbedder for real semantic recall.
    """

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions
        self.name = f"hashing-{dimensions}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        tokens = _tokenize(text)
        if not tokens:
            return vec
        counts: dict[str, int] = {}
        for tok in tokens:
            counts[tok] = counts.get(tok, 0) + 1
        for tok, n in counts.items():
            digest = hashlib.blake2b(tok.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            # sublinear term frequency, as in tf-idf: the tenth occurrence of a
            # word says much less than the second
            vec[idx] += sign * (1.0 + math.log(n))
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


class SentenceTransformerEmbedder:
    """The real one. Imported lazily so the package works without torch."""

    def __init__(self, model: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        self._model = SentenceTransformer(model)
        self.name = model
        self.dimensions = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.encode(texts, normalize_embeddings=True)]


def build_embedder(provider: str, model: str, dimensions: int) -> Embedder:
    """Fall back rather than fail. A store that will not start because a model is
    missing is worse than one whose recall is weaker and says so."""
    if provider == "hashing":
        return HashingEmbedder(dimensions)
    try:
        return SentenceTransformerEmbedder(model)
    except Exception:
        return HashingEmbedder(dimensions)


def serialize(vector: list[float]) -> bytes:
    """Pack for sqlite-vec. float32 little-endian, which is what vec0 expects."""
    return struct.pack(f"<{len(vector)}f", *vector)


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


def _tokenize(text: str) -> list[str]:
    out, cur = [], []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
class TokenCounter:
    """Counts tokens for the recall budget.

    Spec F1 requires context_block never to exceed max_tokens. With a real
    tokenizer that is exact. Without one the fallback deliberately OVER-counts,
    because the guarantee is an upper bound: over-counting returns slightly less
    memory than it could, while under-counting breaks the promise the whole
    budget mechanism exists to make.
    """

    def __init__(self, encoding: str = "cl100k_base"):
        self._enc = None
        self.exact = False
        try:
            import tiktoken  # noqa: PLC0415

            self._enc = tiktoken.get_encoding(encoding)
            self.exact = True
        except Exception:
            pass

    def count(self, text: str) -> int:
        if self._enc is not None:
            return len(self._enc.encode(text))
        if not text:
            return 0
        # ~3 chars/token is conservative for English (real ratio is nearer 4),
        # and whitespace-separated chunks set a floor for symbol-heavy text.
        return max(len(text) // 3, len(text.split())) + 1
