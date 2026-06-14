"""Reranker shim: /search returns a search_id + logs events; /track/open records the
label; the event_logging kill-switch (ADR-014) suppresses all writes.

These run only when msa_ranker is installed (it's optional — ADR-013)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("msa_ranker")

from fastapi.testclient import TestClient  # noqa: E402

from msa_apps.search_api.app import create_app  # noqa: E402
from msa_ranker.ledger import LedgerWriter  # noqa: E402


def _read_ledger(ledger_dir):
    events = []
    for p in sorted(Path(ledger_dir).glob("events-*.jsonl")):
        events += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    return events


def _make_client(tmp_path, *, event_logging=True, with_features=True):
    cfg = SimpleNamespace(
        sqlite_path=str(tmp_path / "media.sqlite"),  # not created → GPS enrichment skipped
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        ranker=SimpleNamespace(event_logging=event_logging, ledger_dir=str(tmp_path / "ledger")),
    )
    for d in (cfg.thumb_dir, cfg.face_thumb_dir, cfg.log_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    qe = MagicMock()
    qe.search.return_value = [
        {"id": "m1", "path": None, "thumbnail": None, "why": None, "score": 0.9,
         "heuristic_score": 0.9, "features": {"sim": 0.9} if with_features else None,
         "tags": [], "type": "image"},
        {"id": "m2", "path": None, "thumbnail": None, "why": None, "score": 0.4,
         "heuristic_score": 0.4, "features": {"sim": 0.4} if with_features else None,
         "tags": [], "type": "image"},
    ]
    app = create_app(config_override=cfg, query_engine_override=qe, reset_dependencies=True)
    ledger_dir = tmp_path / "ledger"
    app.state.ledger_writer = LedgerWriter(ledger_dir, event_logging=event_logging)
    return TestClient(app), ledger_dir


def test_search_returns_search_id_and_logs_events(tmp_path):
    client, ledger_dir = _make_client(tmp_path)
    resp = client.post("/search", json={"q": "beach"})
    assert resp.status_code == 200
    sid = resp.json()["search_id"]
    assert sid
    events = _read_ledger(ledger_dir)
    assert [e["ev"] for e in events] == ["search", "shown", "shown"]
    assert all(e["search_id"] == sid for e in events)
    shown = [e for e in events if e["ev"] == "shown"]
    assert [e["position"] for e in shown] == [0, 1]
    assert all("heuristic_score" in e for e in shown)  # NN1


def test_track_open_records_label(tmp_path):
    client, ledger_dir = _make_client(tmp_path)
    sid = client.post("/search", json={"q": "beach"}).json()["search_id"]
    r = client.post("/track/open", json={"search_id": sid, "media_id": "m1"})
    assert r.status_code == 204
    opens = [e for e in _read_ledger(ledger_dir) if e["ev"] == "open"]
    assert len(opens) == 1
    assert opens[0]["media_id"] == "m1" and opens[0]["search_id"] == sid


def test_event_logging_off_writes_nothing(tmp_path):
    # ADR-014 kill-switch.
    client, ledger_dir = _make_client(tmp_path, event_logging=False)
    resp = client.post("/search", json={"q": "beach"})
    assert resp.status_code == 200 and resp.json()["search_id"]
    client.post("/track/open", json={"search_id": "x", "media_id": "m1"})
    assert _read_ledger(ledger_dir) == []


def test_features_unavailable_skips_logging(tmp_path):
    # #B: extractor unavailable (features=None) → skip logging, don't write empty rows.
    client, ledger_dir = _make_client(tmp_path, with_features=False)
    resp = client.post("/search", json={"q": "beach"})
    assert resp.status_code == 200 and resp.json()["search_id"]  # search still works
    assert _read_ledger(ledger_dir) == []  # nothing logged (no corrupt empty-feature rows)
