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
    qc._inflight = 0
    qc._inflight_writes = 0


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


# ── shared_client_op (M-8/S-2 guarded accessor) ────────────────────────────────

def test_shared_client_op_yields_client_and_tracks_inflight():
    dummy = DummyClient()
    qc._shared = dummy
    qc._blocked = False
    assert qc._inflight == 0
    with qc.shared_client_op() as client:
        assert client is dummy
        assert qc._inflight == 1
    assert qc._inflight == 0


def test_shared_client_op_yields_none_when_blocked_and_skips_refcount():
    qc._blocked = True
    qc._shared = DummyClient()
    with qc.shared_client_op() as client:
        assert client is None
        assert qc._inflight == 0
    assert qc._inflight == 0


def test_shared_client_op_creates_client_lazily(monkeypatch):
    dummy = DummyClient()
    from types import SimpleNamespace
    monkeypatch.setattr(qc, "load_config", lambda: SimpleNamespace(qdrant_path="/tmp/qdrant_test"))
    monkeypatch.setattr(qc, "QdrantClient", lambda *a, **kw: dummy)
    qc._blocked = False
    qc._shared = None
    with qc.shared_client_op() as client:
        assert client is dummy
    assert qc._shared is dummy


def test_shared_client_op_decrements_on_exception():
    qc._shared = DummyClient()
    qc._blocked = False
    with pytest.raises(RuntimeError):
        with qc.shared_client_op() as client:
            assert client is not None
            raise RuntimeError("op failed")
    assert qc._inflight == 0


def test_shared_client_op_nested_ops_count_independently():
    qc._shared = DummyClient()
    qc._blocked = False
    with qc.shared_client_op():
        with qc.shared_client_op():
            assert qc._inflight == 2
        assert qc._inflight == 1
    assert qc._inflight == 0


# ── is_blocked / block_shared_client ───────────────────────────────────────────

def test_is_blocked_tracks_block_and_reopen():
    assert qc.is_blocked() is False
    qc.block_shared_client()
    assert qc.is_blocked() is True
    qc.reopen_shared_client()
    assert qc.is_blocked() is False


def test_block_shared_client_rejects_new_ops_without_closing():
    """block_shared_client is the drain-before-grant first step: new ops get
    None immediately, but the client stays open for in-flight ops to finish."""
    dummy = DummyClient()
    qc._shared = dummy
    qc._blocked = False
    qc.block_shared_client()
    assert dummy.closed is False, "block must not close — close happens after drain"
    assert qc._shared is dummy
    with qc.shared_client_op() as client:
        assert client is None


# ── drain ──────────────────────────────────────────────────────────────────────

def test_drain_returns_immediately_when_idle():
    assert qc.drain(timeout=0.05) is True


def test_drain_waits_for_inflight_op_to_complete():
    """An in-flight shared_client_op must complete before drain() returns —
    the watcher's close-then-grant sequence depends on this."""
    import time

    qc._shared = DummyClient()
    qc._blocked = False
    op_started = threading.Event()
    op_release = threading.Event()
    op_finished = threading.Event()

    def op():
        with qc.shared_client_op() as client:
            assert client is not None
            op_started.set()
            op_release.wait(timeout=5)
        op_finished.set()

    t = threading.Thread(target=op, daemon=True)
    t.start()
    assert op_started.wait(timeout=5)

    qc.block_shared_client()
    drain_result = {}

    def do_drain():
        drain_result["drained"] = qc.drain(timeout=5.0)

    dt = threading.Thread(target=do_drain, daemon=True)
    dt.start()
    time.sleep(0.15)
    assert "drained" not in drain_result, "drain returned while an op was in flight"

    op_release.set()
    dt.join(timeout=5)
    t.join(timeout=5)
    assert drain_result.get("drained") is True
    assert op_finished.is_set(), "in-flight op must have completed before drain returned"


def test_drain_times_out_when_op_stuck():
    qc._shared = DummyClient()
    qc._blocked = False
    op_started = threading.Event()
    op_release = threading.Event()

    def op():
        with qc.shared_client_op():
            op_started.set()
            op_release.wait(timeout=10)

    t = threading.Thread(target=op, daemon=True)
    t.start()
    assert op_started.wait(timeout=5)
    try:
        assert qc.drain(timeout=0.2) is False
    finally:
        op_release.set()
        t.join(timeout=5)


# ── Write holds: drain_writes / close_generation (PR #202 round-2) ─────────────

def test_write_op_tracks_both_refcounts():
    qc._shared = DummyClient()
    qc._blocked = False
    with qc.shared_client_op(write=True) as client:
        assert client is not None
        assert qc._inflight == 1
        assert qc._inflight_writes == 1
    assert qc._inflight == 0
    assert qc._inflight_writes == 0


def test_read_op_does_not_count_as_write():
    qc._shared = DummyClient()
    qc._blocked = False
    with qc.shared_client_op() as client:
        assert client is not None
        assert qc._inflight == 1
        assert qc._inflight_writes == 0


def test_drain_writes_ignores_read_holds():
    """A wedged READER must not delay the write drain — reads keep their own
    bounded drain() and are safely abandonable."""
    qc._shared = DummyClient()
    qc._blocked = False
    op_started = threading.Event()
    op_release = threading.Event()

    def read_op():
        with qc.shared_client_op():
            op_started.set()
            op_release.wait(timeout=10)

    t = threading.Thread(target=read_op, daemon=True)
    t.start()
    assert op_started.wait(timeout=5)
    try:
        assert qc.drain_writes(timeout=0.5) is True, (
            "drain_writes must return immediately when only reads are in flight"
        )
    finally:
        op_release.set()
        t.join(timeout=5)


def test_drain_writes_waits_for_write_hold_then_returns():
    """The grant queues BEHIND an in-flight payload write: drain_writes only
    returns once the write hold is released."""
    import time

    qc._shared = DummyClient()
    qc._blocked = False
    op_started = threading.Event()
    op_release = threading.Event()
    op_finished = threading.Event()

    def write_op():
        with qc.shared_client_op(write=True) as client:
            assert client is not None
            op_started.set()
            op_release.wait(timeout=10)
        op_finished.set()

    t = threading.Thread(target=write_op, daemon=True)
    t.start()
    assert op_started.wait(timeout=5)

    qc.block_shared_client()
    result = {}

    def do_drain():
        result["drained"] = qc.drain_writes(timeout=5.0)

    dt = threading.Thread(target=do_drain, daemon=True)
    dt.start()
    time.sleep(0.15)
    assert "drained" not in result, "drain_writes returned while a write was in flight"

    op_release.set()
    dt.join(timeout=5)
    t.join(timeout=5)
    assert result.get("drained") is True
    assert op_finished.is_set(), "write must have completed before drain_writes returned"


def test_drain_writes_times_out_on_wedged_write():
    """The generous hard ceiling: a WEDGED write cannot block the export
    window forever — drain_writes gives up and the caller closes loudly."""
    qc._shared = DummyClient()
    qc._blocked = False
    op_started = threading.Event()
    op_release = threading.Event()

    def write_op():
        with qc.shared_client_op(write=True):
            op_started.set()
            op_release.wait(timeout=10)

    t = threading.Thread(target=write_op, daemon=True)
    t.start()
    assert op_started.wait(timeout=5)
    try:
        assert qc.drain_writes(timeout=0.2) is False
    finally:
        op_release.set()
        t.join(timeout=5)


def test_close_generation_bumps_only_when_a_live_client_closes():
    """The §4 write guard detects a mid-write close via the generation
    counter — it must move exactly when a live client is actually closed."""
    gen0 = qc.close_generation()
    qc._shared = None
    qc._blocked = False
    qc.close_shared_client()  # nothing to close
    assert qc.close_generation() == gen0

    qc.reopen_shared_client()
    qc._shared = DummyClient()
    qc.close_shared_client()
    assert qc.close_generation() == gen0 + 1

    qc.close_shared_client()  # idempotent second close: no live client
    assert qc.close_generation() == gen0 + 1


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
