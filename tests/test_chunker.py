"""The chunker: chunk 0 = topic + growth texts + first notes paragraph, heading-aware sections,
a token budget of 350 with a 50-token overlap inside a long run of text."""

from agents_mindmap.embeddings import CHUNK_TOKENS, OVERLAP_TOKENS, approx_tokens, chunk_node


def words(prefix: str, n: int) -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


NOTES = (
    "First paragraph about pgvector.\n\nSecond paragraph.\n\n# Section A\n\n"
    + words("alpha", 600)
    + "\n\n## Section B\n\nshort b text\n\n"
    + words("beta", 40)
)


def test_chunk_budget_and_overlap():
    assert (CHUNK_TOKENS, OVERLAP_TOKENS) == (350, 50)
    chunks = chunk_node("Topic here", ["grow one", "grow two"], NOTES)
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert approx_tokens(c.text) <= CHUNK_TOKENS, c.ord
    head = chunks[0].text
    assert head.startswith("Topic here")
    assert "grow one" in head and "grow two" in head
    assert "First paragraph about pgvector." in head
    assert "Second paragraph." not in head
    assert "Second paragraph." in chunks[1].text and "Section A" not in chunks[1].text
    # heading-aware: a section's chunks start with its heading and never mix two sections
    alpha = [c for c in chunks if "alpha" in c.text]
    beta = [c for c in chunks if "beta" in c.text or "short b text" in c.text]
    assert len(alpha) >= 3
    assert all(c.text.startswith("# Section A") and "Section B" not in c.text for c in alpha)
    assert beta and all(c.text.startswith("## Section B") for c in beta)
    assert "short b text" in beta[0].text and "beta39" in beta[-1].text
    # every word of the long run lands in some chunk; neighbours share about 50 tokens
    seen = {w for c in alpha for w in c.text.split() if w.startswith("alpha")}
    assert seen == set(words("alpha", 600).split())
    for a, b in zip(alpha, alpha[1:], strict=False):
        a_words = [w for w in a.text.split() if w.startswith("alpha")]
        b_words = [w for w in b.text.split() if w.startswith("alpha")]
        shared = [w for w in b_words if w in a_words]
        assert shared == b_words[: len(shared)] == a_words[-len(shared) :]
        assert OVERLAP_TOKENS <= approx_tokens(" ".join(shared)) <= OVERLAP_TOKENS + 10


def test_chunk_empty_notes_and_huge_word():
    (only,) = chunk_node("Lonely topic", [], "")
    assert only.ord == 0 and only.text == "Lonely topic"
    chunks = chunk_node("t", ["g"], "x" * 5000)
    assert len(chunks) > 1
    assert all(approx_tokens(c.text) <= CHUNK_TOKENS for c in chunks)
    assert sum(c.text.count("x") for c in chunks) >= 5000


def test_chunk_head_over_budget_is_split():
    growth = [words(f"g{i}x", 40) for i in range(10)]
    chunks = chunk_node("big head", growth, "tail paragraph")
    assert chunks[0].text.startswith("big head")
    assert all(approx_tokens(c.text) <= CHUNK_TOKENS for c in chunks)
    assert any("tail paragraph" in c.text for c in chunks)
