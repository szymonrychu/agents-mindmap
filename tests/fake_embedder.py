"""A deterministic bag-of-words embedder for tests: no network, no model. Each word hashes to
one of 1024 dimensions; vectors are L2-normalised, so cosine similarity tracks word overlap."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import zlib
from collections.abc import Callable

from agents_mindmap.embeddings import EmbedderUnavailable

DIM = 1024
WORD = re.compile(r"\w+")


def vectorize(text: str) -> list[float]:
    v = [0.0] * DIM
    for word in WORD.findall(text.lower()):
        v[zlib.crc32(word.encode()) % DIM] += 1.0
    if not any(v):
        v[0] = 1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class FakeEmbedder:
    """`embed` records each call; `fail` raises EmbedderUnavailable, `delay` sleeps first and
    `on_call(texts, query)` (sync or async) runs before the vectors are returned."""

    def __init__(
        self, *, fail: bool = False, delay: float = 0.0, on_call: Callable | None = None
    ) -> None:
        self.fail = fail
        self.delay = delay
        self.on_call = on_call
        self.calls: list[tuple[list[str], bool]] = []

    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        self.calls.append((list(texts), query))
        if self.on_call is not None:
            result = self.on_call(texts, query)
            if inspect.isawaitable(result):
                await result
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise EmbedderUnavailable("fake embedder is down")
        return [vectorize(t) for t in texts]

    async def aclose(self) -> None:
        return None
