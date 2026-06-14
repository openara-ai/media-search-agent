"""S-5.2 — learned reranking at the serving seam (spec 06).

Covers the seam logic — flag-off byte-identical (INV-3), permutation-only reorder
(INV-6), fail-safe to the heuristic on any error (FR-14) — and the startup gate/load
wiring (a corrupt model still lets MSA start on the heuristic, AC-06.8). Model *quality*
is never asserted (INV-2). Runs only when msa_ranker is installed (optional — ADR-013).
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("msa_ranker")

from fastapi.testclient import TestClient  # noqa: E402

from msa_apps.search_api.app import create_app  # noqa: E402
from msa_query.query_engine.engine import _learned_rerank  # noqa: E402
from msa_ranker.features import QueryContext  # noqa: E402
from msa_ranker.model import train_logreg  # noqa: E402


class _FakeRanker:
    """Scores each candidate by its precomputed features['sim'] so order is controllable."""

    def __init__(self, *, raises=False, wrong_count=False, non_numeric=False, const=None):
        self.raises, self.wrong_count, self.non_numeric = raises, wrong_count, non_numeric
        self.const = const

    def score(self, candidates, ctx, *, now):
        if self.raises:
            raise ValueError("boom")
        if self.non_numeric:
            return ["x"] * len(candidates)  # TypeError on negate — caught by the guard
        if self.const is not None:
            return [self.const] * len(candidates)  # e.g. NaN/inf — caught by isfinite guard
        out = [float(c.get("features", {}).get("sim", 0.0)) for c in candidates]
        return out[:-1] if self.wrong_count else out


def _assert_untouched(r2, original_ids=("a", "b", "c")):
    # The error/fallback paths must leave the candidate dicts byte-identical (no mutation).
    assert [m["id"] for m in r2] == list(original_ids)
    assert all("features" not in m and "heuristic_score" not in m for m in r2)
    assert [m["score"] for m in r2] == [0.9, 0.8, 0.7]  # heuristic scores unchanged


def _cands():
    ranked = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.8}, {"id": "c", "score": 0.7}]
    feats = [{"sim": 0.1}, {"sim": 0.9}, {"sim": 0.5}]  # learned order by sim desc: b, c, a
    return ranked, feats


# ---------------------------------------------------------------- seam logic
def test_flag_off_is_byte_identical():
    # INV-3 — ranker None ⇒ inputs unchanged, served False, nothing mutated on the dicts.
    ranked, feats = _cands()
    r2, f2, served = _learned_rerank(ranked, feats, None, None, None)
    assert served is False
    assert [m["id"] for m in r2] == ["a", "b", "c"]
    assert f2 == feats
    assert all("heuristic_score" not in m and "features" not in m for m in r2)


def test_learned_reorders_as_permutation():
    # INV-6 — output is a permutation (no add/drop); ordered by learned score; NN1 preserved.
    ranked, feats = _cands()
    r2, f2, served = _learned_rerank(ranked, feats, QueryContext(), 0.0, _FakeRanker())
    assert served is True
    assert {m["id"] for m in r2} == {"a", "b", "c"} and len(r2) == len(f2) == 3
    assert [m["id"] for m in r2] == ["b", "c", "a"]  # sim 0.9, 0.5, 0.1
    assert [f["sim"] for f in f2] == [0.9, 0.5, 0.1]  # feature_list stays aligned
    assert [m["heuristic_score"] for m in r2] == [0.8, 0.7, 0.9]  # original score preserved
    assert [m["score"] for m in r2] == [0.9, 0.5, 0.1]  # served (learned) score


def test_scorer_raises_falls_back_untouched():
    # FR-14 — any scoring error ⇒ heuristic order, served False, dicts UNTOUCHED.
    ranked, feats = _cands()
    r2, f2, served = _learned_rerank(ranked, feats, QueryContext(), 0.0, _FakeRanker(raises=True))
    assert served is False and f2 == feats
    _assert_untouched(r2)


def test_score_count_mismatch_falls_back_untouched():
    ranked, feats = _cands()
    r2, _, served = _learned_rerank(
        ranked, feats, QueryContext(), 0.0, _FakeRanker(wrong_count=True)
    )
    assert served is False
    _assert_untouched(r2)


def test_non_numeric_scores_fall_back_not_500():
    # The sort runs inside the guard — non-numeric scores fall back instead of raising
    # (which would have turned /search into a 500).
    ranked, feats = _cands()
    r2, _, served = _learned_rerank(
        ranked, feats, QueryContext(), 0.0, _FakeRanker(non_numeric=True)
    )
    assert served is False
    _assert_untouched(r2)


def test_nan_or_inf_scores_fall_back_not_500():
    # NaN/inf don't raise on negate or sort — the isfinite guard must catch them, else
    # they'd reach the response JSON serializer and 500 every request.
    for bad in (float("nan"), float("inf"), float("-inf")):
        ranked, feats = _cands()
        r2, _, served = _learned_rerank(
            ranked, feats, QueryContext(), 0.0, _FakeRanker(const=bad)
        )
        assert served is False, f"{bad} should fall back"
        _assert_untouched(r2)


def test_unavailable_features_skip_learned():
    ranked, feats = _cands()
    feats = [feats[0], None, feats[2]]  # any missing feature → skip learned entirely
    r2, _, served = _learned_rerank(ranked, feats, QueryContext(), 0.0, _FakeRanker())
    assert served is False and [m["id"] for m in r2] == ["a", "b", "c"]


# ---------------------------------------------------------------- startup gate/load wiring
def _deploy(model_dir, *, beats=True):
    model_dir.mkdir(parents=True, exist_ok=True)
    model = train_logreg([[1.0] + [0.0] * 15, [0.0] * 16], [1, 0], seed=0)
    art = model.save(model_dir / "model.json")
    sha = hashlib.sha256(art.read_bytes()).hexdigest()
    (model_dir / "manifest.json").write_text(
        json.dumps(
            {
                "model_id": "m1",
                "artifact": "model.json",
                "feature_set_version": model.feature_set_version,
                "artifact_sha": sha,
                "beats_baseline": beats,
            }
        )
    )
    return model_dir


def _app(tmp_path, *, enable, model_dir):
    cfg = SimpleNamespace(
        sqlite_path=str(tmp_path / "media.sqlite"),
        log_dir=tmp_path / "logs",
        ranker=SimpleNamespace(
            event_logging=False,
            ledger_dir=str(tmp_path / "ledger"),
            enable_learning_to_rank=enable,
            ltr_model_dir=str(model_dir),
        ),
    )
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    qe = MagicMock()
    qe.search.return_value = []
    return create_app(config_override=cfg, query_engine_override=qe, reset_dependencies=True)


def test_startup_loads_gated_model(tmp_path):
    app = _app(tmp_path, enable=True, model_dir=_deploy(tmp_path / "model"))
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ready"
        assert app.state.ranker is not None  # gate passed → loaded
        assert app.state.ranker_model_id == "m1"  # id stashed for per-model attribution


def test_startup_corrupt_model_starts_on_heuristic(tmp_path):
    # AC-06.8 — gate passes (sha matches) but load() raises on the corrupt bytes; MSA still
    # starts and ranker is None (the exception never reaches the lifespan).
    md = _deploy(tmp_path / "bad")
    (md / "model.json").write_text("{ not valid json")
    m = json.loads((md / "manifest.json").read_text())
    m["artifact_sha"] = hashlib.sha256((md / "model.json").read_bytes()).hexdigest()
    (md / "manifest.json").write_text(json.dumps(m))
    app = _app(tmp_path, enable=True, model_dir=md)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ready"  # MSA started
        assert app.state.ranker is None


def test_flag_off_loads_no_model(tmp_path):
    # INV-3 — master flag off ⇒ no model loaded even when a valid one is present.
    app = _app(tmp_path, enable=False, model_dir=_deploy(tmp_path / "model"))
    with TestClient(app) as client:
        assert app.state.ranker is None
