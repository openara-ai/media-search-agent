import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Dict, Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.http.models import SearchRequest, Filter, FieldCondition, MatchAny
from msa_settings import load_config

# ---------------------------------------------------------------------------
# Process-wide shared Qdrant client
#
# Embedded Qdrant uses a file lock that only ONE process can hold at a time.
# Within the API process we must also share a single client — the portalocker
# used by qdrant-client is per-open-file-description, so two QdrantClient()
# instances in the same process will deadlock each other.
#
# _blocked is set True while the indexer subprocess needs the embedded lock —
# under the M-8/S-2 sentinel-file handoff that is only the export window at
# the end of a run (with MSA_QDRANT_HANDOFF=off it spans the whole run, the
# pre-S-2 behavior). While blocked, the API gracefully declines new Qdrant
# operations rather than racing for the lock.
#
# EVERY shared-client operation outside this module must go through the
# shared_client_op() context manager. It maintains the in-flight refcount
# that drain() waits on before the client is closed for the indexer's export
# window — a bare get_shared_client() fetch would let an in-flight operation
# keep using the embedded lock after the handoff grant is written. The
# grep-gate in tests/test_qdrant_handoff.py enforces this.
#
# READ holds vs WRITE holds: reads (search/suggestions) are safely
# abandonable — they error harmlessly if the client closes under them — so
# the handoff watcher drains them with a short bounded timeout. WRITE holds
# (shared_client_op(write=True), the §4 payload-write guard) are NOT: a
# write abandoned mid-flight commits SQLite while its Qdrant payload sync
# silently fails against the closed client. The watcher therefore waits for
# write holds via drain_writes() without the reader cap (writes are bounded
# operations), up to a generous hard ceiling; past the ceiling the close is
# loud and the write surfaces a retryable 503 via the close-generation
# check instead of a 200 over silently-stale Qdrant state.
# ---------------------------------------------------------------------------
_client_lock = threading.Lock()
_inflight_cv = threading.Condition(_client_lock)
_inflight: int = 0  # shared-client operations currently executing (reads + writes)
_inflight_writes: int = 0  # subset of _inflight holding write=True (§4 payload writes)
_close_gen: int = 0  # bumped every time close_shared_client() closes a live client
_shared: Optional[QdrantClient] = None
_blocked: bool = False  # True while the indexer subprocess needs the Qdrant lock


def get_shared_client() -> Optional[QdrantClient]:
    """Return the process-wide QdrantClient, creating it if needed.

    Returns None if Qdrant is currently unavailable (indexer holds the lock).

    Internal building block — callers outside this module must use
    shared_client_op() instead, so the in-flight refcount protects the
    operation from a concurrent close (see module header).
    """
    global _shared
    # Fast path: avoid lock acquisition when clearly blocked
    if _blocked:
        return None
    with _client_lock:
        # Re-check under the lock — close_shared_client() may have fired
        # between the check above and acquiring the lock (TOCTOU).
        if _blocked:
            return None
        if _shared is None:
            try:
                cfg = load_config()
                _shared = QdrantClient(path=str(cfg.qdrant_path))
            except Exception:
                return None
        return _shared


@contextmanager
def shared_client_op(write: bool = False) -> Iterator[Optional[QdrantClient]]:
    """Guarded access to the shared client for ONE Qdrant operation.

    Yields the shared client, or None when Qdrant is blocked (indexer holds
    the lock) or unavailable. While the with-block runs, the in-flight
    refcount is held so drain() — called by the handoff watcher before it
    closes the client for the indexer — waits for the operation to finish.

    ``write=True`` marks a Qdrant-payload WRITE hold (the §4 payload-write
    guard): the watcher waits for write holds via drain_writes() without
    the bounded reader cap, because abandoning a write mid-flight would
    commit SQLite while its payload sync silently fails on a closed client.
    Reads keep the short bounded drain — they error harmlessly if closed
    under.

    Keep the Qdrant calls themselves inside the with-block; post-processing
    of already-fetched results can happen outside.
    """
    global _shared, _inflight, _inflight_writes
    with _inflight_cv:
        if _blocked:
            client = None
        else:
            if _shared is None:
                try:
                    cfg = load_config()
                    _shared = QdrantClient(path=str(cfg.qdrant_path))
                except Exception:
                    _shared = None
            client = _shared
        if client is not None:
            _inflight += 1
            if write:
                _inflight_writes += 1
    try:
        yield client
    finally:
        if client is not None:
            with _inflight_cv:
                _inflight -= 1
                if write:
                    _inflight_writes -= 1
                _inflight_cv.notify_all()


def is_blocked() -> bool:
    """True while the indexer subprocess needs the embedded Qdrant lock.

    Under the sentinel-file handoff (MSA_QDRANT_HANDOFF enabled) this is
    True only during the export window — the API-side write-rejection
    predicate for payload-mutating endpoints keys off it.
    """
    return _blocked


def block_shared_client() -> None:
    """Reject new shared-client operations without closing the client yet.

    First step of the handoff watcher's drain-before-grant sequence: new
    operations immediately get None while in-flight ones finish (drain()),
    after which close_shared_client() actually releases the embedded lock.
    """
    global _blocked
    _blocked = True


def drain(timeout: float = 10.0) -> bool:
    """Wait for in-flight shared-client operations (reads AND writes) to finish.

    Returns True when the in-flight count reached zero, False on timeout.
    Call block_shared_client() first so no new operations start while
    draining. On timeout the caller closes anyway (logged) — abandoned
    READS error harmlessly on the closed client, and the indexer's
    lock-retry ladder absorbs the residual race. Writes must be waited on
    via drain_writes() BEFORE this bounded reader drain.
    """
    deadline = time.monotonic() + timeout
    with _inflight_cv:
        while _inflight > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _inflight_cv.wait(remaining)
        return True


def drain_writes(timeout: float) -> bool:
    """Wait for in-flight WRITE holds (shared_client_op(write=True)) to finish.

    The handoff watcher calls this before the bounded reader drain(): a
    payload write abandoned mid-flight commits SQLite while its Qdrant sync
    silently fails against the closed client — the exact silent staleness
    §4 of the lock-window plan exists to prevent. Writes are bounded
    operations, so the grant waits behind them without the reader cap; the
    caller passes a generous hard ceiling purely so a WEDGED write cannot
    block the export window forever. Past the ceiling the caller closes
    anyway with a loud log, and the write fails with a retryable 503 via
    the close-generation check instead of returning 200.
    """
    deadline = time.monotonic() + timeout
    with _inflight_cv:
        while _inflight_writes > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _inflight_cv.wait(remaining)
        return True


def close_generation() -> int:
    """Monotonic count of closes that actually shut a live shared client.

    The §4 payload-write guard records this at entry and re-checks after
    its sync: a change means the client the write was using got closed
    mid-flight (the drain_writes hard ceiling fired), so the qdrant_sync
    helpers may have swallowed the failure — the request must surface a
    retryable 503 rather than a 200 over silently-stale Qdrant payloads.
    """
    return _close_gen


def close_shared_client() -> None:
    """Close the shared client and block new ones (call before the indexer needs the lock)."""
    global _shared, _blocked, _close_gen
    _blocked = True
    with _client_lock:
        if _shared is not None:
            try:
                _shared.close()
            except Exception:
                pass
            _shared = None
            _close_gen += 1


def reopen_shared_client() -> None:
    """Unblock Qdrant access (call once the indexer has released the lock)."""
    global _blocked
    _blocked = False
    # Client will be lazily re-created on next get_shared_client() call


class QdrantStore:
    def __init__(self):
        # Don't hold a client — use the shared singleton dynamically so that
        # the indexer subprocess can take exclusive Qdrant access when needed.
        pass

    def search(self, collection: str, vector, k: int, query_filter: Optional[Filter] = None) -> List[Dict[str, Any]]:
        """
        Search in Qdrant collection with optional filtering.
        
        Args:
            collection: Collection name
            vector: Query vector
            k: Number of results to return
            query_filter: Qdrant Filter object for filtering results
        """
        with shared_client_op() as client:
            if client is None:
                return []
            res = client.search(
                collection_name=collection,
                query_vector=vector,
                limit=k,
                with_payload=True,
                query_filter=query_filter,
            )
        out: List[Dict[str, Any]] = []
        for p in res:
            payload = p.payload or {}

            # Prefer original media_id from payload when available
            media_or_point_id = payload.get("media_id") or payload.get("id") or p.id

            # Handle video vs image payloads differently
            is_video = payload.get("type") == "video"
            # Normalize people/faces field naming across pipeline:
            faces_or_people = payload.get("faces") or payload.get("people")
            result = {
                "id": media_or_point_id,
                "score": float(p.score or 0.0),
                "path": payload.get("path"),
                "thumbnail": payload.get("thumbnail"),
                "faces": faces_or_people,
                "tags": payload.get("tags", []),  # Object/scene detection tags
                "scene_tags": payload.get("scene_tags"),
                "caption": payload.get("caption"),
                "place": payload.get("place"),
                "country": payload.get("country"),
                "state": payload.get("state"),
            }
            
            # For videos: timestamp is seek position (float), date is None
            # For images: timestamp is date string, no separate timestamp field
            if is_video:
                result["type"] = "video"
                result["timestamp"] = payload.get("timestamp")  # float: seek position
                result["shot_id"] = payload.get("shot_id")
                result["shot_start"] = payload.get("shot_start")
                result["shot_end"] = payload.get("shot_end")
                result["gps_lat"] = payload.get("gps_lat")
                result["gps_lon"] = payload.get("gps_lon")
                if payload.get("place") is not None:
                    result["place"] = payload.get("place")
                result["date"] = None  # Videos don't have date metadata yet
            else:
                result["date"] = payload.get("timestamp")  # ISO date string for images
                result["type"] = None
                result["timestamp"] = None
                result["shot_id"] = None
            
            out.append(result)
        return out
    
    @staticmethod
    def build_tag_filter(tags: List[str]) -> Optional[Filter]:
        """
        Build a Qdrant filter to match any of the specified tags.
        
        Args:
            tags: List of tag names to filter by
            
        Returns:
            Qdrant Filter object or None if no tags provided
        """
        if not tags:
            return None
        
        # Match any of the tags in the tags array field
        return Filter(
            must=[
                FieldCondition(
                    key="tags",
                    match=MatchAny(any=tags)
                )
            ]
        )
