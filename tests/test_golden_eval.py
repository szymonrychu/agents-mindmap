"""The golden-set harness (eval/run.py) over the seeded fixture with the fake embedder. A fake
embedder gives lexical-driven recall, so this checks the harness and the fusion, not the model:
hybrid is at least lexical-only, and lexical-only meets the fake floors of eval/golden.yaml.
The live floors are checked on demand against TEI (`eval/run.py --embedder-url`)."""

import importlib.util
import sys
from pathlib import Path

from fake_embedder import FakeEmbedder

ROOT = Path(__file__).resolve().parent.parent


def load_run():
    spec = importlib.util.spec_from_file_location("golden_run", ROOT / "eval" / "run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


async def test_golden_set_fake_embedder(pool, db_url):
    run = load_run()
    golden = run.load_golden(ROOT / "eval" / "golden.yaml")
    assert 20 <= len(golden.queries) <= 30
    assert {q.lang for q in golden.queries} == {"en", "pl"}
    assert golden.floors["live"] == {"recall_at_10": 0.7, "mrr": 0.5}
    assert golden.floors["fake"] == {"recall_at_10": 0.5, "mrr": 0.3}
    ids = run.seed(db_url, golden)
    assert len(ids) == len(golden.nodes)
    fake = FakeEmbedder()
    await run.embed_all(pool, fake)
    lexical = await run.evaluate(pool, golden, ids, None, mode="lexical")
    hybrid = await run.evaluate(pool, golden, ids, fake, mode="hybrid")
    assert hybrid.recall_at_10 >= lexical.recall_at_10
    assert hybrid.mrr >= lexical.mrr
    assert lexical.recall_at_10 >= golden.floors["fake"]["recall_at_10"]
    assert lexical.mrr >= golden.floors["fake"]["mrr"]
    assert 0 <= hybrid.recall_at_10 <= 1 and 0 <= hybrid.mrr <= 1


def test_metrics_math():
    run = load_run()
    result = run.score([["a", "x", "b"], ["y", "z"]], [{"a", "b"}, {"q"}])
    assert result.recall_at_10 == 0.5 and result.mrr == 0.5
