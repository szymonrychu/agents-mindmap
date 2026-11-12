"""Embeddings: the provider interface, the OpenAI-shaped client (TEI `/v1/embeddings`) and the
node chunker (spec 3.2 node_chunks, 3.3).

The chunker is markdown-aware: chunk 0 is the topic, the growth-area texts and the first notes
paragraph; the rest of the notes is split on headings (a chunk never spans two sections and
starts with its heading) and packed paragraph by paragraph into about CHUNK_TOKENS tokens, with
OVERLAP_TOKENS of the previous chunk's tail carried into the next one of the same section. Token
counts use an approximation of the model tokenizer (`approx_tokens`), kept on the safe side of
the model's 512-token input."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from agents_mindmap.settings import EMBED_DIM

CHUNK_TOKENS = 350
OVERLAP_TOKENS = 50
# a longer run of non-space characters is cut into pieces of this size (100 tokens each)
MAX_WORD_CHARS = 400
# a heading longer than this many tokens is cut, so the body keeps most of the budget
MAX_HEADING_TOKENS = CHUNK_TOKENS // 4

# Query-time embedding: the request waits at most this long, then searches lexically.
QUERY_TIMEOUT_SECONDS = 2.0
# multilingual-e5-large-instruct embeds queries with a one-line task instruction and passages
# with no prefix (model card; the TEI spike measured recall with exactly this text).
QUERY_PREFIX = (
    "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
)

_TOKEN = re.compile(r"\w+|[^\w\s]")
_HEADING = re.compile(r"^#{1,6}\s")
_BLANK = re.compile(r"\n\s*\n")


class EmbedderUnavailable(Exception):
    """The embeddings endpoint failed (network, HTTP status, malformed answer). The message
    names the status or the exception type only, never a response body."""


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        """One L2-normalised vector per text; `query` marks a search query (instruction
        prefix), otherwise the texts are passages. Raises EmbedderUnavailable."""
        ...

    async def aclose(self) -> None: ...


def _normalised(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm:
        raise EmbedderUnavailable("embeddings endpoint returned a zero vector")
    return [x / norm for x in vector]


class OpenAIShapedProvider:
    """`POST <url>/v1/embeddings` with `{"model", "input": [...]}` (TEI, OpenAI and compatible
    servers), in batches of `batch_size`; checks the dimension and normalises every vector."""

    def __init__(
        self,
        url: str,
        model: str,
        *,
        timeout: float,
        dim: int = EMBED_DIM,
        batch_size: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._dim = dim
        self._batch = batch_size
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        inputs = [QUERY_PREFIX + t if query else t for t in texts]
        out: list[list[float]] = []
        for start in range(0, len(inputs), self._batch):
            out.extend(await self._call(inputs[start : start + self._batch]))
        return out

    async def _call(self, inputs: list[str]) -> list[list[float]]:
        path = "/v1/embeddings"
        try:
            resp = await self._http.post(path, json={"model": self._model, "input": inputs})
        except httpx.HTTPError as exc:
            raise EmbedderUnavailable(f"POST {path}: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise EmbedderUnavailable(f"POST {path}: HTTP {resp.status_code}")
        try:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbedderUnavailable(
                f"POST {path}: malformed answer ({type(exc).__name__})"
            ) from None
        if len(vectors) != len(inputs) or any(len(v) != self._dim for v in vectors):
            raise EmbedderUnavailable(f"POST {path}: expected {len(inputs)} x {self._dim} vectors")
        return [_normalised(v) for v in vectors]


@dataclass(frozen=True)
class Chunk:
    ord: int
    text: str


def approx_tokens(text: str) -> int:
    """Tokenizer approximation: every word or punctuation mark is at least one token, a long
    word one per four characters (sentencepiece splits rare and Polish words finely)."""
    return sum(max(1, math.ceil(len(piece) / 4)) for piece in _TOKEN.findall(text))


def _words(text: str) -> list[str]:
    out: list[str] = []
    for word in text.split():
        out.extend(word[i : i + MAX_WORD_CHARS] for i in range(0, len(word), MAX_WORD_CHARS))
    return out


def _cost(paragraphs: list[list[str]]) -> int:
    return sum(approx_tokens(w) for p in paragraphs for w in p)


def _tail(paragraphs: list[list[str]], tokens: int) -> list[str]:
    """The last words of `paragraphs` worth at least `tokens` tokens (the overlap)."""
    flat = [w for p in paragraphs for w in p]
    taken: list[str] = []
    cost = 0
    while flat and cost < tokens:
        word = flat.pop()
        taken.insert(0, word)
        cost += approx_tokens(word)
    return taken


def _pack(heading: str, paragraphs: list[str]) -> list[str]:
    """Pack paragraphs under one heading into chunks within the token budget."""
    head_words = _words(heading)
    while head_words and _cost([head_words]) > MAX_HEADING_TOKENS:
        head_words.pop()
    heading = " ".join(head_words)
    budget = CHUNK_TOKENS - _cost([head_words])
    out: list[str] = []
    cur: list[list[str]] = []
    fresh = False  # cur holds words not yet emitted (beyond the carried overlap)

    def emit() -> None:
        body = "\n\n".join(" ".join(p) for p in cur if p)
        out.append(f"{heading}\n\n{body}" if heading else body)

    def carry() -> list[list[str]]:
        tail = _tail(cur, OVERLAP_TOKENS)
        return [tail] if tail else []

    for text in paragraphs:
        para = _words(text)
        if not para:
            continue
        if _cost(cur) + _cost([para]) <= budget:
            cur.append(para)
            fresh = True
            continue
        if _cost([para]) <= budget:
            # it fits a chunk of its own: close this one, start the next with the overlap
            emit()
            cur = carry()
            if _cost(cur) + _cost([para]) > budget:
                cur = []
            cur.append(para)
            fresh = True
            continue
        # too long for any chunk: fill the current chunk word by word, then continue
        cur.append([])
        for word in para:
            if _cost(cur) + approx_tokens(word) > budget:
                if fresh:
                    emit()
                cur = carry() if fresh else []
                cur.append([])
                fresh = False
            cur[-1].append(word)
            fresh = True
    if fresh:
        emit()
    return out


def _sections(notes: str) -> list[tuple[str, list[str]]]:
    """(heading line, paragraphs) in order; the first section has the empty heading."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body)
        sections[-1][1].extend(p.strip() for p in _BLANK.split(text) if p.strip())
        body.clear()

    for line in notes.splitlines():
        if _HEADING.match(line):
            flush()
            sections.append((line.strip(), []))
        else:
            body.append(line)
    flush()
    return sections


def chunk_node(topic: str, growth_texts: list[str], notes: str) -> list[Chunk]:
    """The chunks of one node, numbered from 0; chunk 0 starts with the topic."""
    sections = _sections(notes)
    first_heading, first_paragraphs = sections[0]
    head = [topic, *growth_texts, *first_paragraphs[:1]]
    texts = _pack(first_heading, head)
    texts += _pack(first_heading, first_paragraphs[1:])
    for heading, paragraphs in sections[1:]:
        texts += _pack(heading, paragraphs)
    return [Chunk(i, t) for i, t in enumerate(texts)]
