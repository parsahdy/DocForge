"""Embedding. A fake embedder lets us study a slow, flaky dependency without paying for one."""

from __future__ import annotations

import hashlib
import math
import random
import time
from typing import Protocol

from app.core.errors import TransientError


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    def __init__(
        self,
        dim: int,
        latency_s: float = 0.0,
        error_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.dim = dim
        self.latency_s = latency_s
        self.error_rate = error_rate
        self._rng = rng or random.Random()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.latency_s > 0:
            time.sleep(self.latency_s)
        if self.error_rate > 0 and self._rng.random() < self.error_rate:
            raise TransientError("EMBED_RATE_LIMITED", "fake embedder: 429 Too Many Requests")
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        """Deterministic: the same text always gives the same vector."""
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        generator = random.Random(seed)
        values = [generator.random() - 0.5 for _ in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]