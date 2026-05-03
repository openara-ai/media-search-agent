"""
Tests for the process-wide shared Qdrant client singleton.

These tests verify the _blocked flag mechanism that prevents concurrent
access to the embedded Qdrant store when the indexer subprocess is running.
"""
import pytest
import threading
import msa_query.storage.qdrant_client as qc


class DummyClient:
    def __init__(self):
        self.closed = False
        self.searches = []

    def close(self):
        self.closed = True

    def search(self, collection_name, query_vector, limit, with_payload, query_filter=None):
        self.searches.append(collection_name)
        return []


@pytest.fixture(autouse=True)
def reset_singleton():
    """Restore shared-client module state after each test."""
    yield
    qc._blocked = False
    qc._shared = None


# ── get_shared_client ──────────────────────────────────────────────────────────

def test_get_shared_client_returns_none_when_blocked():
    qc._blocked = True
    qc._shared = DummyClient()
    assert qc.get_shared_client() is None


def test_get_shared_client_returns_existing_client():
    dummy = DummyClient()
    qc._shared = dummy
    qc._blocked = False
    assert qc.get_shared_client() is dummy


def test_get_shared_client_does_not_create_client_when_blocked(monkeypatch):
    created = []

    def fake_qdrant(*args, **kwargs):
        created.append(True)
        return DummyClient()

    monkeypatch.setattr(qc, "QdrantClient", fake_qdrant)
    qc._blocked = True
    qc._shared = None

    result = qc.get_shared_client()
    assert result is None
    assert created == [], "QdrantClient should not be instantiated while blocked"


# ── close_shared_client ────────────────────────────────────────────────────────

def test_close_shared_client_sets_blocked():
    qc._blocked = False
    qc._shared = None
    qc.close_shared_client()
    assert qc._blocked is True


def test_close_shared_client_closes_existing_client():
    dummy = DummyClient()
    qc._shared = dummy
    qc._blocked = False
    qc.close_shared_client()
    assert dummy.closed is True
    assert qc._shared is None


def test_close_shared_client_tolerates_no_existing_client():
    qc._shared = None
    qc._blocked = False
    qc.close_shared_client()  # must not raise
    assert qc._blocked is True


def test_close_shared_client_tolerates_close_error():
    class BadClose:
        def close(self):
            raise RuntimeError("close failed")

    qc._shared = BadClose()
    qc._blocked = False
    qc.close_shared_client()  # must not raise
    assert qc._blocked is True
    assert qc._shared is None


# ── reopen_shared_client ───────────────────────────────────────────────────────

def test_reopen_shared_client_clears_blocked():
    qc._blocked = True
    qc.reopen_shared_client()
    assert qc._blocked is False


def test_reopen_then_get_creates_new_client(monkeypatch):
    dummy = DummyClient()

    def fake_qdrant(*args, **kwargs):
        return dummy

    from types import SimpleNamespace
    monkeypatch.setattr(qc, "load_config", lambda: SimpleNamespace(qdrant_path="/tmp/qdrant_test"))
    monkeypatch.setattr(qc, "QdrantClient", fake_qdrant)

    qc._blocked = True
    qc._shared = None
    qc.reopen_shared_client()
    result = qc.get_shared_client()
    assert result is dummy


# ── close → reopen lifecycle (simulates indexer subprocess) ────────────────────

def test_indexer_lifecycle_blocks_then_restores(monkeypatch):
    dummy = DummyClient()

    def fake_qdrant(*args, **kwargs):
        return dummy

    from types import SimpleNamespace
    monkeypatch.setattr(qc, "load_config", lambda: SimpleNamespace(qdrant_path="/tmp/qdrant_test"))
    monkeypatch.setattr(qc, "QdrantClient", fake_qdrant)

    # Simulate: API has a client, indexer is about to start
    qc._blocked = False
    qc._shared = DummyClient()  # existing API client

    # Step 1: close before launching indexer subprocess
    qc.close_shared_client()
    assert qc._blocked is True
    assert qc._shared is None
    assert qc.get_shared_client() is None  # API is blocked

    # Step 2: indexer subprocess finishes, reopen
    qc.reopen_shared_client()
    assert qc._blocked is False
    result = qc.get_shared_client()  # lazily creates new client
    assert result is dummy


# ── QdrantStore.search ─────────────────────────────────────────────────────────

def test_qdrant_store_search_returns_empty_when_blocked():
    import numpy as np
    qc._blocked = True
    qc._shared = DummyClient()

    store = qc.QdrantStore()
    results = store.search("media_emb", np.zeros(512, dtype="float32"), k=5)
    assert results == []


def test_qdrant_store_search_calls_client_when_available():
    import numpy as np

    class HitPayload:
        def __init__(self):
            self.id = 1
            self.score = 0.9
            self.payload = {"path": "/img.jpg", "type": None}

    class TrackingClient:
        def close(self): pass
        def search(self, **kwargs):
            return [HitPayload()]

    qc._blocked = False
    qc._shared = TrackingClient()

    store = qc.QdrantStore()
    results = store.search("media_emb", [0.1] * 512, k=3)
    assert len(results) == 1
    assert results[0]["path"] == "/img.jpg"


# ── Thread safety: blocked flag is visible across threads ──────────────────────

def test_blocked_flag_visible_across_threads():
    qc._blocked = False
    qc._shared = DummyClient()

    results = []

    def reader():
        results.append(qc.get_shared_client())

    qc.close_shared_client()  # sets _blocked = True
    t = threading.Thread(target=reader)
    t.start()
    t.join()

    assert results[0] is None, "Thread should see _blocked=True"
