from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Dict, Any, List, Optional
from pathlib import Path
import gc
import math
import os
import time
from shutil import rmtree as _rmtree  # module-level so tests can patch OUR
# removal in isolation from the vendored client's own shutil.rmtree
from loguru import logger  # for structured logging

# Qdrant client
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest

# Load structured config
from msa_settings import load_config
S = load_config()

# ---- Interfaces we expect from your existing DB/FAISS helpers ----
# You likely have similar helpers already; adapt these calls if names differ.
#
# sqlite_store: must expose an iterator over items with stable ids & metadata
# faiss_store:  must expose a get_vector(item_id) -> List[float] or np.ndarray
#
# Example expected fields from sqlite: id, path, people (list), place (str), ts (str)
# Adjust the field names to match your schema.
from msa_indexer.db.sqlite_store import SQLiteStore

QDRANT_EXPORT_STATE_COLLECTION = "_msa_export_state"
QDRANT_EXPORT_STATE_POINT_ID = 1

# Phrases that identify a VERIFIED collection-not-found error across both
# backends: embedded local mode raises ValueError("Collection X not found");
# the server raises UnexpectedResponse with a 404 body. Anything else (a
# transient embedded-store IO error, a closed client's RuntimeError, ...)
# must NOT match — swallowing it where "absent" means "skip" would convert
# a transient failure into a recorded success (§4.2 gate).
_COLLECTION_NOT_FOUND_PHRASES = (
    "not found",
    "404",
    "does not exist",
    "doesn't exist",
    "not_found",
)


def _is_collection_not_found(exc: Exception) -> bool:
    """True only for a verified collection-not-found error."""
    msg = str(exc).lower()
    return any(p in msg for p in _COLLECTION_NOT_FOUND_PHRASES)


@dataclass
class ExportOutcome:
    """Result of one _do_qdrant_export pass (M-8/S-3 §4.2 widened gate).

    record_qdrant_export_version may fire ONLY when record_ok — every
    exporter that was supposed to run succeeded AND the unconditional
    deletion pass succeeded. A partial failure leaves the unexported rows
    stamped above the watermark for the next run (R8); under delta export
    the pre-S-3 face-swallow gate would instead have advanced the watermark
    past dirty face rows and face tombstones, permanently skipping them.
    """
    images_attempted: bool = False  # image/video export ran (not reprocess-gated off)
    images_ok: bool = False
    faces_ok: bool = False
    deletions_ok: bool = False
    image_sent: int = 0
    video_sent: int = 0
    face_sent: int = 0
    deleted_points: int = 0
    empty_tables: bool = False  # pre-Stage-3 shape blocked the record

    @property
    def record_ok(self) -> bool:
        return (
            self.images_attempted
            and self.images_ok
            and self.faces_ok
            and self.deletions_ok
        )

    def __bool__(self) -> bool:
        # Callers (and legacy test fakes returning a plain bool) gate the
        # version record on truthiness.
        return self.record_ok

# The embedded backend's class name. We detect embedded (local) mode by CLASS
# IDENTITY rather than by duck-typed attribute presence so that a future
# qdrant-client that renames the internals the WIN-008 recreate purge depends
# on (persistent/location/collections/LocalCollection.close) fails LOUD (via
# _persistent_local_backend below) instead of silently reverting the purge to a
# no-op and letting the Windows stale-collection regression back in.
_QDRANT_LOCAL_CLASSNAME = "QdrantLocal"

# The QdrantLocal internals the close-handle + purge seam reaches into. Pinned
# to qdrant-client 1.11.x (see BUGS_AND_GOTCHAS WIN-008). If a bump renames any
# of these, _persistent_local_backend RAISES naming this assumption.
_EMBEDDED_BACKEND_INTERNALS = ("persistent", "location", "collections")


def _persistent_local_backend(client: QdrantClient) -> Optional[Any]:
    """Return the embedded ``QdrantLocal`` backend of ``client`` IFF it is a
    persistent (on-disk) local client; else None for server/remote/``:memory:``.

    Embedded mode is detected by the backend's CLASS NAME (stable across the
    attribute churn we actually worry about), NOT by ``getattr(...)`` probes.
    Once we know we ARE on the embedded backend, the internals the WIN-008
    recreate purge depends on MUST be present: if a future qdrant-client
    renames them we RAISE a clear error naming the pinned-version assumption
    rather than silently skipping the purge (which would reintroduce the
    Windows stale-collection regression this fix closes). ``:memory:`` mode
    has no on-disk dir, so it returns None WITHOUT raising — matching the
    server/remote "no local dir" contract.
    """
    local = getattr(client, "_client", None)
    if local is None:
        return None
    if type(local).__name__ != _QDRANT_LOCAL_CLASSNAME:
        # Server/remote (QdrantRemote) — no on-disk collection dir.
        return None
    missing = [n for n in _EMBEDDED_BACKEND_INTERNALS if not hasattr(local, n)]
    if missing:
        raise RuntimeError(
            f"qdrant-client embedded backend ({type(local).__name__}) is "
            f"missing expected internal(s) {missing}: the WIN-008 recreate "
            f"purge (release the sqlite handle + remove the on-disk collection "
            f"dir before create_collection) pins qdrant-client 1.11.x "
            f"internals. A version bump renamed them — refusing to silently "
            f"no-op the purge (that reintroduces the Windows stale-collection "
            f"regression); update _embedded_collection_dir / "
            f"_close_local_collection_handle for the new client shape."
        )
    if not getattr(local, "persistent"):
        # :memory: — no on-disk directory; the purge is a legitimate no-op.
        return None
    return local


def _embedded_collection_dir(client: QdrantClient, collection: str) -> Optional[str]:
    """On-disk directory of ``collection`` in EMBEDDED (local) mode, else None.

    Mirrors QdrantLocal._collection_path: ``<location>/collection/<name>``.
    Returns None for server/remote mode and for the ``:memory:`` backend —
    neither has an on-disk collection directory, so the Windows open-handle
    recreate fix (below) is a harmless no-op there. RAISES if we ARE on the
    embedded backend but its internals were renamed by a qdrant-client bump
    (see _persistent_local_backend) — a dependency bump must break loud, not
    silently revert the fix.
    """
    local = _persistent_local_backend(client)
    if local is None:
        return None
    location = getattr(local, "location", None)
    if not location or location == ":memory:":
        return None
    return os.path.join(str(location), "collection", collection)


def _close_local_collection_handle(client: QdrantClient, collection: str) -> None:
    """Deterministically release the embedded sqlite handle for ``collection``.

    QdrantLocal.delete_collection pops the LocalCollection and only ``del``s
    it — it never calls ``.close()``. On CPython the object *usually* frees
    at once, but a reference cycle defers the close to the cyclic GC, and on
    Windows a still-open sqlite handle makes the subsequent
    ``rmtree(ignore_errors=True)`` fail (WinError 32) *silently*, leaving
    ``storage.sqlite`` on disk for create_collection to RELOAD.

    Closing the handle HERE — through the LocalCollection's own supported
    ``close()`` — while it is still registered (before delete_collection pops
    it) is correct-by-construction: it does not depend on GC timing. No-op in
    server/remote/in-memory mode. RAISES loudly (rather than skipping the
    close) if we ARE on the embedded backend but its internals were renamed by
    a qdrant-client bump — the WIN-008 regression must never re-enter silently.
    """
    local = _persistent_local_backend(client)
    if local is None:
        return
    collections = getattr(local, "collections", None)
    if not isinstance(collections, dict):
        raise RuntimeError(
            f"qdrant-client embedded backend '.collections' is "
            f"{type(collections).__name__}, expected dict — the WIN-008 "
            f"close-handle seam pins the 1.11.x internal; a version bump "
            f"changed it. Update _close_local_collection_handle."
        )
    coll = collections.get(collection)
    if coll is None:
        # Not loaded in memory (already popped, or never opened) — no handle
        # to release; not a shape change, so no raise.
        return
    close = getattr(coll, "close", None)
    if not callable(close):
        raise RuntimeError(
            f"qdrant-client LocalCollection for '{collection}' has no callable "
            f"close(): WIN-008 needs it to release the sqlite handle before "
            f"delete_collection so the on-disk dir can be removed on Windows. "
            f"A qdrant-client bump removed/renamed it — update "
            f"_close_local_collection_handle."
        )
    try:
        close()
    except Exception as e:  # never let a close error mask the recreate
        logger.debug(
            f"LocalCollection.close() for '{collection}' raised (ignored): {e}"
        )


def _purge_local_collection_dir(client: QdrantClient, collection: str) -> None:
    """Guarantee the embedded on-disk collection dir is gone before a recreate.

    LOG-001 sibling (embedded-Qdrant recreate open-handle on Windows): the
    backend's delete_collection uses ``shutil.rmtree(path, ignore_errors=True)``
    while the popped LocalCollection may still hold an open sqlite handle. On
    Windows that rmtree fails with WinError 32, ``ignore_errors=True`` swallows
    it, ``storage.sqlite`` survives, and create_collection reloads the stale
    points — a recreate that silently keeps old data while the caller records
    the §4.2 watermark.

    This forces the removal on OUR seam and **RAISES** on persistent failure
    (no ``ignore_errors``): a recreate that cannot guarantee removal MUST
    propagate so the widened watermark gate never records a version over
    surviving points. No-op in server/remote/in-memory mode, or when the dir
    is already gone (POSIX, where the backend's own rmtree already succeeded).
    """
    coll_dir = _embedded_collection_dir(client, collection)
    if coll_dir is None or not os.path.isdir(coll_dir):
        return

    # Belt-and-suspenders: if delete_collection already popped+del'd the
    # LocalCollection, force any lingering cyclic garbage to be collected so
    # the sqlite Connection's deallocator releases the OS file handle. The
    # deterministic release is _close_local_collection_handle above; this
    # only matters when the handle escaped that path.
    gc.collect()

    last_exc: Optional[Exception] = None
    for _attempt in range(5):
        try:
            _rmtree(coll_dir)
        except FileNotFoundError:
            return
        except Exception as e:  # WinError 32 / lingering handle / AV scan
            last_exc = e
            gc.collect()
            time.sleep(0.2)
            continue
        if not os.path.isdir(coll_dir):
            return
    raise RuntimeError(
        f"Could not remove on-disk collection dir '{coll_dir}' while "
        f"recreating '{collection}' — the old points would survive the "
        f"recreate; refusing to proceed so the export version is NOT "
        f"recorded over them (§4.2 gate)"
    ) from last_exc


def _recreate_delete_collection(client: QdrantClient, collection: str) -> None:
    """Delete ``collection`` for a recreate, GUARANTEEING the embedded on-disk
    directory is gone (Windows open-handle safe).

    THE single close-handle + delete + purge-with-raise path (WIN-008), shared
    by ``ensure_collection``'s recreate branch and ``export_faces_to_qdrant``'s
    empty-``face_embedding``-table recreate branch. EVERY recreate/direct-delete
    that must reset on-disk state MUST route through here: a second call site
    that deletes directly would let the Windows stale-collection survival
    (``storage.sqlite`` left on disk for ``create_collection`` to RELOAD) slip
    back in — exactly the empty-face-table branch the round-1 review flagged.

    Steps:
      1. Release THIS collection's embedded sqlite handle via
         ``LocalCollection.close()`` BEFORE ``delete_collection`` pops it, so
         the backend's own rmtree can remove the dir on Windows instead of
         hitting an open handle (no-op in server/remote/``:memory:`` mode).
      2. ``delete_collection``, tolerating an already-absent collection; any
         OTHER delete error propagates (a transient failure must not be read as
         "deleted" — that would upsert into a stale collection and record the
         §4.2 watermark over it).
      3. Force-remove the on-disk dir and RAISE if it cannot be guaranteed gone,
         so the §4.2 gate never records a version over surviving points.
    """
    # 1. Deterministic handle release before delete_collection pops it.
    _close_local_collection_handle(client, collection)

    # 2. Delete (tolerate an already-absent collection).
    try:
        client.delete_collection(collection)
        logger.info(
            f"Delete request sent for collection '{collection}', waiting for completion..."
        )
        # Deletion may be async (server) — verify it is actually gone.
        time.sleep(0.5)
        deleted = False
        for retry in range(5):
            try:
                client.get_collection(collection)
                logger.debug(
                    f"Collection '{collection}' still exists, waiting... (retry {retry + 1}/5)"
                )
                time.sleep(0.5)
            except Exception:
                deleted = True
                logger.info(f"Collection '{collection}' successfully deleted")
                break
        if not deleted:
            logger.warning(
                f"Collection '{collection}' may still exist after deletion "
                "attempts, but proceeding with the on-disk purge"
            )
    except Exception as e:
        if _is_collection_not_found(e):
            logger.debug(
                f"Collection '{collection}' does not exist (cannot delete), "
                "will create new one"
            )
        else:
            # §4.2 gate: a transient deletion failure must propagate. Proceeding
            # would upsert into the STALE collection, the export would
            # "succeed", and the caller would record the watermark / clear
            # face_recreate_required with the orphaned points still present.
            raise

    # 3. Windows open-handle guarantee (embedded Qdrant, WIN-008): force-remove
    #    the on-disk dir and RAISE if it cannot be removed, so the §4.2 gate
    #    never records a watermark over surviving points. No-op in
    #    server/remote mode / when already gone.
    _purge_local_collection_dir(client, collection)


def ensure_collection(
    client: QdrantClient,
    collection: str,
    vector_size: int,
    distance: rest.Distance = rest.Distance.COSINE,
    recreate: bool = False,
) -> bool:
    """Create the collection if missing; optionally recreate.

    Returns True when the collection was actually CREATED (or recreated)
    by this call — i.e. it starts EMPTY — and False when a pre-existing
    collection was reused. Callers running a windowed (delta) export MUST
    degrade to a full export for a collection this reports as created
    (M-8/S-3 §4.2, round-3 review finding, P2): uploading only rows above
    the watermark into a fresh empty collection and then recording the
    version would leave every unchanged point permanently absent and
    unsearchable.
    """
    # If recreate is requested, always try to delete the collection first
    # This is safer than checking existence first, as the check might fail
    if recreate:
        logger.info(f"Recreate requested for collection '{collection}' - attempting to delete...")
        try:
            # Try to get collection info first to see if it exists (for logging)
            try:
                info = client.get_collection(collection)
                points_count = getattr(info, 'points_count', 'unknown')
                logger.info(f"Collection '{collection}' exists (points: {points_count}) - deleting...")
            except Exception:
                logger.debug(f"Collection '{collection}' does not exist (or error checking), will attempt deletion anyway")

            # THE shared close-handle + delete + purge-with-raise path (WIN-008):
            # releases the embedded sqlite handle, deletes (tolerating absent),
            # and force-removes the on-disk dir — RAISING if it cannot — so a
            # blocked Windows rmtree can never leave storage.sqlite for
            # create_collection to RELOAD. No-op in server/remote/in-memory mode.
            _recreate_delete_collection(client, collection)
        except Exception as e:
            logger.error(f"Recreate deletion of collection '{collection}' failed: {e}")
            raise

    # Check if collection exists now (after potential deletion)
    exists = False
    try:
        info = client.get_collection(collection)
        exists = True
        logger.info(f"Collection '{collection}' exists (points: {getattr(info, 'points_count', 'unknown')})")
    except Exception as e:
        exists = False
        logger.debug(f"Collection '{collection}' does not exist")
    if exists:
        # Optional vector-size probe, isolated from the existence verdict:
        # embedded local mode exposes config.params.vectors instead of
        # vectors_config, and letting this raise inside the block above
        # silently flipped `exists` to False — routing every pre-existing
        # collection through the 409 "already exists" create path, which
        # the round-3 created-report would misread as "created".
        try:
            _sz = info.vectors_config.params.size  # type: ignore[attr-defined]
        except Exception:
            pass
    
    # Create collection if it doesn't exist
    if not exists:
        logger.info(f"Creating collection '{collection}' (vector_size={vector_size}, distance={distance})")
        try:
            client.create_collection(
                collection_name=collection,
                vectors_config=rest.VectorParams(size=vector_size, distance=distance),
            )
            logger.info(f"Successfully created collection '{collection}'")
        except Exception as e:
            # If the collection already exists (409), verify it exists and has correct config
            msg = str(e).lower()
            if "already exists" in msg or ("collection" in msg and "already exists" in msg):
                try:
                    # confirm it exists now
                    info = client.get_collection(collection)
                except Exception:
                    # If we still can't get collection info, re-raise the original error
                    raise
                if recreate:
                    # The collection survived the recreate-deletion above
                    # (e.g. the deletion verify loop was fooled by a
                    # transient lookup error). Silently reusing it would
                    # keep the stale points and record success (§4.2 gate).
                    raise RuntimeError(
                        f"Collection '{collection}' still exists after "
                        "recreate-deletion — recreate was not honored"
                    )
                logger.info(f"Collection '{collection}' already exists (skipping creation)")
                # The collection was ABSENT at the existence check above, so
                # whatever raced us into creating it cannot be assumed to
                # hold the historical points — report created so a delta
                # caller degrades to full (safe either way).
                return True
            else:
                # unknown error — re-raise
                raise
        # Created fresh (starts empty).
        return True
    elif recreate:
        # Collection exists and recreate was requested, but the deletion did
        # not actually remove it. Proceeding would keep the stale points and
        # let the caller record success (§4.2 gate) — propagate instead.
        raise RuntimeError(
            f"Collection '{collection}' still exists after recreate-deletion "
            "— recreate was not honored"
        )
    # Pre-existing collection reused as-is.
    return False

def batched(iterable: Iterable, batch_size: int):
    """Yield lists of length <= batch_size from iterable."""
    batch = []
    for x in iterable:
        batch.append(x)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch

# The three payload builders below are THE derivation source for the
# write→stamp mapping in payload_columns.py (M-8/S-3 §4.1): any SQLite cell
# a builder reads must have a stamp rule so delta export can never miss a
# payload-only change. The coverage test in tests/test_delta_export.py
# locks each builder's emitted key set against PAYLOAD_SOURCES — update
# payload_columns.py (and its stamp rules) whenever a builder changes.

def build_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map your SQLite row to a Qdrant payload. Adjust fields to your schema."""
    # Use ts (from ts_utc) if available, otherwise fall back to added_at
    timestamp = row.get("ts") or row.get("added_at")

    return {
        "media_id": row.get("id"),  # preserve original ID for joins
        "path": row.get("path"),
        "people": row.get("people") or [],
        "place": row.get("place"),
        "timestamp": timestamp,
        "tags": row.get("tags") or [],  # Object/scene detection tags
        # add more: camera_model, duration, fps, faces, etc.
    }


def build_video_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Qdrant payload for one video keyframe (rows from iter_video_keyframes)."""
    vid = row["video_id"]
    return {
        "id": vid,
        "media_id": vid,
        "type": "video",
        "path": row.get("path"),
        "timestamp": float(row.get("timestamp") or 0.0),
        "shot_id": int(row["shot_index"]),
        "shot_start": float(row.get("shot_start") or 0.0),
        "shot_end": float(row.get("shot_end") or 0.0),
        "tags": row.get("tags") or [],
        "gps_lat": row.get("gps_lat"),
        "gps_lon": row.get("gps_lon"),
        "gps_alt": row.get("gps_alt"),
        "gps_datetime_utc": row.get("gps_datetime_utc"),
        "gps_fix": row.get("gps_fix"),
        "gps_source": row.get("gps_source"),
        "place": row.get("place"),
        "people": row.get("people") or [],
    }


def build_face_payload(row: Dict[str, Any], embedding_backend: str = "facenet_pytorch") -> Dict[str, Any]:
    """Qdrant payload for one face detection (rows from iter_faces)."""
    return {
        "face_id": row["face_id"],
        "media_id": row["media_id"],
        "path": row.get("path"),
        "type": row.get("type", "image"),
        "bbox": row.get("bbox"),
        "confidence": float(row.get("confidence", 0.0)),
        "person_id": row.get("person_id"),
        "person_name": row.get("person_name"),
        "gender": row.get("gender"),
        "age": row.get("age"),
        "date": row.get("date"),
        "shot_index": row.get("shot_index"),
        "kf_index": row.get("kf_index"),
        "embedding_backend": embedding_backend,
    }

def _media_id_to_int(media_id: str) -> int:
    """Convert a media_id string to a stable integer ID for Qdrant.
    Uses the same encoding as FaissStore._id_for_media() to ensure compatibility.
    
    Returns a positive integer suitable for Qdrant (non-negative int64).
    """
    # Take first 8 bytes of media_id hex string (same as FaissStore)
    # but mask to ensure we get a non-negative int64
    raw = int.from_bytes(media_id.encode()[:8].ljust(8, b"\0"), "big")
    return raw & ((1 << 63) - 1)  # mask to non-negative int64


def _face_point_id(face_id: str) -> int:
    """Stable non-negative int64 Qdrant point id for a face_id.

    THE single derivation shared by the face exporter and the §4.2 tombstone
    deletion pass — the two must never diverge or deletions would miss the
    exported points.
    """
    import hashlib
    h = int(hashlib.sha256(face_id.encode()).hexdigest()[:16], 16)
    return h & ((1 << 63) - 1)


def get_qdrant_export_version() -> dict[str, Any] | None:
    """Return the last successfully exported local index version recorded in Qdrant."""
    client = QdrantClient(path=str(S.qdrant_path))
    try:
        client.get_collection(QDRANT_EXPORT_STATE_COLLECTION)
    except Exception:
        return None

    try:
        points = client.retrieve(
            collection_name=QDRANT_EXPORT_STATE_COLLECTION,
            ids=[QDRANT_EXPORT_STATE_POINT_ID],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as e:
        logger.warning(f"Failed to read Qdrant export state: {e}")
        return None

    if not points:
        return None

    payload = getattr(points[0], "payload", None) or {}
    seq = payload.get("index_version_seq")
    ts = payload.get("index_version_ts")
    if seq is None:
        return None
    return {
        "index_version_seq": int(seq),
        "index_version_ts": ts,
    }


def record_qdrant_export_version(index_version_seq: int, index_version_ts: str | None) -> None:
    """Persist the currently exported local index version inside Qdrant."""
    client = QdrantClient(path=str(S.qdrant_path))
    ensure_collection(
        client,
        QDRANT_EXPORT_STATE_COLLECTION,
        vector_size=1,
        distance=rest.Distance.COSINE,
    )
    client.upsert(
        collection_name=QDRANT_EXPORT_STATE_COLLECTION,
        points=[
            rest.PointStruct(
                id=QDRANT_EXPORT_STATE_POINT_ID,
                vector=[0.0],
                payload={
                    "kind": "export_state",
                    "index_version_seq": int(index_version_seq),
                    "index_version_ts": index_version_ts,
                },
            )
        ],
    )


def delete_tombstoned_points_from_qdrant(
    sqlite_path: Path,
    since_seq: Optional[int] = None,
    image_collection: Optional[str] = None,
    video_collection: str = "video_emb",
    face_collection: str = "face_emb",
) -> Dict[str, Any]:
    """§4.2 deletion pass (M-8/S-3): remove the Qdrant points of stamped
    tombstones (``media.deleted = 1 AND deleted_seq > since_seq``;
    since_seq=None selects EVERY stamped tombstone — the full-export case).

    Runs unconditionally on every export, full included: a full export that
    skipped deletions would orphan any tombstone created in that same run
    forever — its deleted_seq equals the watermark that run records, so no
    later delta pass ever selects it.

    Soft-deleted rows survive in SQLite, so every point id is computable:
    the image point via _media_id_to_int, keyframe compound points via
    _compound_point_id, face points via _face_point_id. All three id kinds
    are requested per tombstone; ids that were never exported are filtered
    out below.

    PLATFORM TRAP (verified against qdrant-client 1.11 local mode): in
    embedded (``path=``) Qdrant, ``delete()`` of an ABSENT point id raises
    KeyError instead of no-oping like the server. Ids are therefore
    filtered through ``retrieve()`` first, which tolerates absent ids on
    both backends — re-deleting an already-deleted tombstone is then a true
    no-op everywhere.

    One client for the whole pass, closed explicitly in ``finally`` (the
    _read_qdrant_collection_counts idiom): a GC-released reference could
    outlive the handoff window and collide with the reopened API client.
    Callers must invoke this INSIDE the handoff window.
    """
    from msa_indexer.db.sqlite_store import SQLiteStore as _Store

    if image_collection is None:
        image_collection = S.collections.image

    with _Store(sqlite_path) as meta:
        tombstones = meta.iter_stamped_tombstones(since_seq)

    stats: Dict[str, Any] = {
        "tombstones": len(tombstones),
        "image_points": 0,
        "video_points": 0,
        "face_points": 0,
        "deleted_points": 0,
    }
    if not tombstones:
        return stats

    image_ids: List[int] = []
    video_ids: List[int] = []
    face_ids: List[int] = []
    for t in tombstones:
        image_ids.append(_media_id_to_int(t["media_id"]))
        for s_idx, k_idx in t["keyframes"]:
            video_ids.append(_compound_point_id(t["media_id"], int(s_idx), int(k_idx)))
        for fid in t["face_ids"]:
            face_ids.append(_face_point_id(fid))

    client = QdrantClient(path=str(S.qdrant_path))
    try:
        for stat_key, collection, ids in (
            ("image_points", image_collection, image_ids),
            ("video_points", video_collection, video_ids),
            ("face_points", face_collection, face_ids),
        ):
            if not ids:
                continue
            try:
                client.get_collection(collection)
            except Exception as e:
                if _is_collection_not_found(e):
                    # Collection never created — nothing to delete there.
                    logger.debug(
                        f"Collection '{collection}' absent — no tombstone "
                        "points to delete"
                    )
                    continue
                # Any OTHER lookup failure (transient embedded-store error,
                # closed client, ...) must propagate: treating it as "absent"
                # would return a successful pass, deletions_ok would let the
                # watermark record, and the selected tombstones would fall
                # below the next since_seq with their points never deleted —
                # permanent dangling points. Raising keeps deletions_ok
                # False, blocks the record, and the rows stay stamped (R8).
                raise
            deleted_here = 0
            for chunk in batched(ids, 1024):
                existing = [
                    p.id
                    for p in client.retrieve(
                        collection_name=collection,
                        ids=chunk,
                        with_payload=False,
                        with_vectors=False,
                    )
                ]
                if not existing:
                    continue
                client.delete(
                    collection_name=collection,
                    points_selector=rest.PointIdsList(points=existing),
                    wait=True,
                )
                deleted_here += len(existing)
            stats[stat_key] = deleted_here
            stats["deleted_points"] += deleted_here
        logger.info(
            "Tombstone deletion pass: {} point(s) deleted for {} tombstoned media "
            "(image={} video={} face={})",
            stats["deleted_points"],
            stats["tombstones"],
            stats["image_points"],
            stats["video_points"],
            stats["face_points"],
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    return stats


def export_images_to_qdrant(
    sqlite_path: Path,
    faiss_path: Optional[Path] = None,  # kept for backward-compat; ignored
    collection: Optional[str] = None,
    batch_size: int = 256,
    recreate: bool | None = None,
    since_seq: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Read image metadata + embeddings from SQLite and upsert into Qdrant.

    Embeddings live in the ``image_embedding`` table (BLOB float32) joined
    by primary key to ``media``. The legacy ``faiss_path`` parameter is
    accepted but unused — kept so older callers don't break.

    since_seq (M-8/S-3 §4.2): when set, delta mode — only rows with
    ``updated_seq > since_seq`` are exported. None = full export
    (``msa index export``, ``--recreate``, repair), unchanged. Delta
    degrades to full automatically when the target collection had to be
    created/recreated (it starts EMPTY — a delta-only upload would leave
    every unchanged point permanently absent once the version records).

    Returns:
        Dictionary with stats: {'image_count': int, 'sent': int, 'skipped': int, 'errors': int, 'dim': int}
    """
    if collection is None:
        collection = S.collections.image
    if not isinstance(collection, str):
        raise ValueError("collection must be a string")

    import numpy as np

    with SQLiteStore(sqlite_path) as meta:
        client = QdrantClient(path=str(S.qdrant_path))

        if recreate is None:
            recreate = bool(getattr(S, 'qdrant_recreate_collections_on_export', False))

        # Detect vector size from the first available row. If no embeddings
        # exist there is nothing to export — bail out cleanly.
        first_row = meta.conn.execute(
            "SELECT embedding_dim FROM image_embedding LIMIT 1"
        ).fetchone()
        if first_row is None:
            logger.info(
                f"No image embeddings in SQLite — skipping image export to '{collection}'"
            )
            return {'image_count': 0, 'sent': 0, 'skipped': 0, 'errors': 0, 'dim': 0}
        dim = int(first_row[0])

        logger.info(f"Ensuring collection '{collection}' (recreate={recreate})")
        created = ensure_collection(client, collection, vector_size=dim, distance=rest.Distance.COSINE, recreate=recreate)
        if created and since_seq is not None:
            # §4.2 (round-3 review finding, P2): the target collection was
            # missing (or recreated) — it starts EMPTY, so a delta pass
            # would upload only rows above the watermark, record the
            # version, and leave every unchanged point permanently absent.
            logger.warning(
                f"Collection '{collection}' was missing — running full "
                f"export for it (delta since_seq={since_seq} discarded)"
            )
            since_seq = None

        sent = skipped = errors = 0
        image_count = 0

        mode = f"delta since seq {since_seq}" if since_seq is not None else "full"
        logger.info(
            f"Starting export to Qdrant collection '{collection}' ({dim}d vectors, {mode})"
        )

        # Iterate metadata-bearing rows from the existing iter_items helper
        # (which already filters out videos and resolves paths; in delta
        # mode it selects only rows stamped above since_seq). For each
        # batch we fetch all needed embedding BLOBs in a single SELECT
        # rather than N per-row queries — at 50K media that's the
        # difference between ~200 queries and 50K.
        for rows in batched(meta.iter_items(since_seq=since_seq), batch_size):
            ids_in_batch = [row["id"] for row in rows]
            placeholders = ",".join(["?"] * len(ids_in_batch))
            blob_by_id: Dict[str, bytes] = dict(
                meta.conn.execute(
                    f"SELECT media_id, embedding FROM image_embedding "
                    f"WHERE media_id IN ({placeholders})",
                    ids_in_batch,
                ).fetchall()
            )

            points: List[rest.PointStruct] = []
            for row in rows:
                item_id = row["id"]
                image_count += 1
                blob = blob_by_id.get(item_id)
                if blob is None:
                    logger.warning(f"Skipping item {item_id}: no embedding in image_embedding")
                    skipped += 1
                    continue
                try:
                    v = np.frombuffer(blob, dtype=np.float32).tolist()
                except Exception as e:
                    logger.error(f"Error decoding embedding for {item_id}: {e}")
                    errors += 1
                    continue

                payload = build_payload(row)
                qdrant_id = _media_id_to_int(item_id)
                points.append(rest.PointStruct(id=int(qdrant_id), vector=v, payload=payload))

            if points:
                client.upsert(collection_name=collection, points=points)
            sent += len(points)
            print(f"Upserted {sent} items")

        logger.info("Export complete:")
        logger.info(f"  - Sent: {sent} points")
        if skipped: logger.warning(f"  - Skipped: {skipped} items (no embedding row)")
        if errors: logger.error(f"  - Errors: {errors} items")
        logger.info(f"  - Collection: {collection} ({dim}d vectors)")
    return {
        'image_count': image_count,
        'sent': sent,
        'skipped': skipped,
        'errors': errors,
        'dim': dim
    }

# ---------------------- Video frames export helpers ----------------------

def _compound_point_id(media_id: str, shot_index: int, kf_index: int) -> int:
    """Stable int64 id from (media_id, shot_index, keyframe_index)."""
    base = int.from_bytes(media_id.encode()[:6].ljust(6, b"\0"), "big")
    val = (base << 16) | ((shot_index & 0xFF) << 8) | (kf_index & 0xFF)
    return val & ((1 << 63) - 1)

def ensure_video_collection(client: QdrantClient, collection: str, vector_size: int, recreate: bool = False) -> bool:
    """See ensure_collection — returns True when the collection was created."""
    return ensure_collection(client, collection, vector_size, distance=rest.Distance.COSINE, recreate=recreate)

def upsert_video_keyframes(
    client: QdrantClient,
    collection: str,
    vectors: List[List[float]],
    payloads: List[Dict[str, Any]],
    ids: List[int],
):
    points = [
        rest.PointStruct(id=int(pid), vector=v if isinstance(v, list) else list(v), payload=p)
        for pid, v, p in zip(ids, vectors, payloads)
    ]
    client.upsert(collection_name=collection, points=points)

def export_video_frames_to_qdrant(
    sqlite_path: Path,
    faiss_path: Optional[Path] = None,  # kept for backward-compat; ignored
    collection: str = "video_emb",
    batch_size: int = 256,
    recreate: bool | None = None,
    since_seq: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Export video keyframe embeddings from SQLite into Qdrant.

    Embeddings live in ``keyframe_embedding`` (BLOB float32) joined by
    primary key to ``video_keyframes``. Qdrant point IDs use stable
    64-bit ints via ``_compound_point_id`` for backward compatibility
    with downstream consumers.

    since_seq (M-8/S-3 §4.2): delta mode when set — see
    export_images_to_qdrant (incl. the degrade-to-full rule when the
    collection had to be created).

    Returns:
        Dictionary with stats: {'video_keyframes_count': int, 'sent': int, 'skipped': int, 'errors': int, 'dim': int}
    """
    import numpy as np

    with SQLiteStore(sqlite_path) as meta:
        client = QdrantClient(path=str(S.qdrant_path))
        if recreate is None:
            recreate = bool(getattr(S, 'qdrant_recreate_collections_on_export', False))

        first_row = meta.conn.execute(
            "SELECT embedding_dim FROM keyframe_embedding LIMIT 1"
        ).fetchone()
        if first_row is None:
            logger.info(
                f"No keyframe embeddings in SQLite — skipping video export to '{collection}'"
            )
            return {'video_keyframes_count': 0, 'sent': 0, 'skipped': 0, 'errors': 0, 'dim': 0}
        dim = int(first_row[0])

        logger.info(f"Ensuring video collection '{collection}' (recreate={recreate})")
        created = ensure_video_collection(client, collection, vector_size=dim, recreate=recreate)
        if created and since_seq is not None:
            # §4.2 degrade-to-full — see export_images_to_qdrant.
            logger.warning(
                f"Collection '{collection}' was missing — running full "
                f"export for it (delta since_seq={since_seq} discarded)"
            )
            since_seq = None

        sent = skipped = errors = 0
        video_keyframes_count = 0
        mode = f"delta since seq {since_seq}" if since_seq is not None else "full"
        logger.info(
            f"Starting export of video keyframes to Qdrant collection '{collection}' ({dim}d vectors, {mode})"
        )

        for rows in batched(meta.iter_video_keyframes(since_seq=since_seq), batch_size):
            # Fetch keyframe ids and embedding blobs for the entire batch
            # in two SQL queries instead of two per row. The natural-key
            # → id lookup uses one IN clause via a tuple of triples; the
            # embedding fetch then uses a single IN over the resolved
            # keyframe_id values.
            triples = [
                (r["video_id"], int(r["shot_index"]), int(r["kf_index"]))
                for r in rows
            ]
            kf_id_by_triple: Dict[tuple, int] = {}
            if triples:
                # Build a dynamic OR-of-tuples filter. SQLite supports
                # ``WHERE (a,b,c) IN (VALUES (?,?,?), ...)`` but
                # cross-version compatibility is friendlier with simple
                # OR-of-equalities for batch sizes typical here (≤256).
                placeholders = " OR ".join(
                    ["(video_id=? AND shot_index=? AND kf_index=?)"] * len(triples)
                )
                params: list = []
                for v_id, s_i, k_i in triples:
                    params.extend([v_id, s_i, k_i])
                cur = meta.conn.execute(
                    f"SELECT id, video_id, shot_index, kf_index FROM video_keyframes "
                    f"WHERE {placeholders}",
                    params,
                )
                for kf_id, v_id, s_i, k_i in cur:
                    kf_id_by_triple[(v_id, int(s_i), int(k_i))] = int(kf_id)

            blob_by_kf_id: Dict[int, bytes] = {}
            kf_id_list = list(kf_id_by_triple.values())
            if kf_id_list:
                ph = ",".join(["?"] * len(kf_id_list))
                blob_by_kf_id = dict(
                    meta.conn.execute(
                        f"SELECT keyframe_id, embedding FROM keyframe_embedding "
                        f"WHERE keyframe_id IN ({ph})",
                        kf_id_list,
                    ).fetchall()
                )

            points: List[rest.PointStruct] = []
            for row in rows:
                vid = row["video_id"]
                s_idx = int(row["shot_index"])
                k_idx = int(row["kf_index"])
                video_keyframes_count += 1

                kf_id = kf_id_by_triple.get((vid, s_idx, k_idx))
                if kf_id is None:
                    logger.warning(f"Skip video frame {vid} s{s_idx} k{k_idx}: no video_keyframes row")
                    skipped += 1
                    continue
                blob = blob_by_kf_id.get(kf_id)
                if blob is None:
                    logger.warning(f"Skip video frame {vid} s{s_idx} k{k_idx}: no embedding row")
                    skipped += 1
                    continue
                try:
                    v = np.frombuffer(blob, dtype=np.float32).tolist()
                except Exception as e:
                    logger.error(f"Error decoding keyframe embedding for {vid} s{s_idx} k{k_idx}: {e}")
                    errors += 1
                    continue

                payload = build_video_payload(row)
                pid = _compound_point_id(vid, s_idx, k_idx)
                points.append(rest.PointStruct(id=int(pid), vector=v, payload=payload))

            if points:
                client.upsert(collection_name=collection, points=points)
                sent += len(points)
                logger.debug(f"Upserted batch of {len(points)} video keyframes")

        logger.info("Video keyframes export complete:")
        logger.info(f"  - Sent: {sent} points")
        if skipped: logger.warning(f"  - Skipped: {skipped} items (no embedding row)")
        if errors: logger.error(f"  - Errors: {errors} items")
    return {
        'video_keyframes_count': video_keyframes_count,
        'sent': sent,
        'skipped': skipped,
        'errors': errors,
        'dim': dim
    }

def export_faces_to_qdrant(
    sqlite_path: Path,
    faiss_path: Optional[Path] = None,  # kept for backward-compat; ignored
    collection: str = "face_emb",
    recreate: Optional[bool] = None,
    batch_size: int = 256,
    face_vec_path: Optional[Path] = None,  # kept for backward-compat; ignored
    embedding_backend: str = "facenet_pytorch",
    since_seq: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Export face embeddings from the ``face_embedding`` SQLite table to Qdrant.

    The ``embedding_backend`` value is stored in every point's payload so
    downstream consumers can tell which backend produced the vectors.

    since_seq (M-8/S-3 §4.2): delta mode when set. Callers running in
    recreate mode must pass None — a freshly recreated (empty) collection
    needs the full row set. Enforced here too: delta degrades to full
    whenever the collection had to be created/recreated (see
    export_images_to_qdrant).

    Returns:
        Dictionary with stats: {'faces_count': int, 'sent': int, 'skipped': int, 'errors': int, 'dim': int}
    """
    import numpy as np

    with SQLiteStore(sqlite_path) as meta:
        client = QdrantClient(path=str(S.qdrant_path))

        if recreate is None:
            recreate = bool(getattr(S, 'qdrant_recreate_collections_on_export', False))

        first_row = meta.conn.execute(
            "SELECT embedding_dim FROM face_embedding LIMIT 1"
        ).fetchone()
        if first_row is None:
            if recreate:
                # §4.1: an empty face_embedding table must still clear the
                # old points — the durable face_recreate_required path hits
                # exactly this shape when reprocessing deleted the LAST face
                # rows. Route through the SAME close-handle + delete +
                # purge-with-raise path as ensure_collection (WIN-008): a
                # direct delete_collection here would let a blocked Windows
                # rmtree leave the stale face collection on disk for the next
                # non-empty export to RELOAD, after face_recreate_required was
                # already cleared. A real removal failure propagates so the
                # caller blocks the record and keeps the marker set. (The next
                # non-empty export recreates the collection with the right dim.)
                logger.info(
                    f"No face embeddings in SQLite but recreate requested — "
                    f"clearing stale face collection '{collection}'"
                )
                _recreate_delete_collection(client, collection)
                logger.info(f"Cleared stale face collection '{collection}'")
            else:
                logger.info(
                    f"No face embeddings in SQLite — skipping face export to '{collection}'"
                )
            return {'faces_count': 0, 'sent': 0, 'skipped': 0, 'errors': 0, 'dim': 0}
        face_dim = int(first_row[0])

        logger.info(f"Ensuring face collection '{collection}' (recreate={recreate})")
        created = ensure_collection(client, collection, vector_size=face_dim, distance=rest.Distance.COSINE, recreate=recreate)
        if created and since_seq is not None:
            # §4.2 degrade-to-full — see export_images_to_qdrant. In the
            # recreate paths (reprocess-faces / face_recreate_required /
            # --recreate) the caller already passes since_seq=None, so this
            # only fires for a silently-missing collection under delta —
            # the single safety net shared with that recreate logic.
            logger.warning(
                f"Collection '{collection}' was missing — running full "
                f"export for it (delta since_seq={since_seq} discarded)"
            )
            since_seq = None

        sent = skipped = errors = 0
        faces_count = 0
        mode = f"delta since seq {since_seq}" if since_seq is not None else "full"
        logger.info(
            f"Starting export of faces to Qdrant collection '{collection}' ({face_dim}d vectors, {mode})"
        )

        for rows in batched(meta.iter_faces(since_seq=since_seq), batch_size):
            face_ids_in_batch = [row["face_id"] for row in rows]
            placeholders = ",".join(["?"] * len(face_ids_in_batch))
            blob_by_face_id: Dict[str, bytes] = dict(
                meta.conn.execute(
                    f"SELECT face_id, embedding FROM face_embedding "
                    f"WHERE face_id IN ({placeholders})",
                    face_ids_in_batch,
                ).fetchall()
            )

            points: List[rest.PointStruct] = []
            for row in rows:
                face_id = row["face_id"]
                faces_count += 1
                blob = blob_by_face_id.get(face_id)
                if blob is None:
                    logger.warning(f"Skipping face_id={face_id}: no embedding in face_embedding")
                    skipped += 1
                    continue
                try:
                    v = np.frombuffer(blob, dtype=np.float32).tolist()
                except Exception as e:
                    logger.error(f"Error decoding face embedding for {face_id}: {e}")
                    errors += 1
                    continue

                payload = build_face_payload(row, embedding_backend)

                # Stable hash of face_id as point ID (stable int64 across processes)
                pid = _face_point_id(face_id)
                points.append(rest.PointStruct(id=int(pid), vector=v, payload=payload))

            if points:
                try:
                    client.upsert(collection_name=collection, points=points)
                    sent += len(points)
                    logger.debug(f"Upserted batch of {len(points)} faces")
                except Exception as e:
                    logger.error(f"Error upserting face batch: {e}")
                    errors += len(points)

        logger.info("Face export complete:")
        logger.info(f"  - Sent: {sent} points")
        if skipped: logger.warning(f"  - Skipped: {skipped} items")
        if errors: logger.error(f"  - Errors: {errors} items")
    return {
        'faces_count': faces_count,
        'sent': sent,
        'skipped': skipped,
        'errors': errors,
        'dim': face_dim
    }

# Optional helpers if you also store caption/ASR embeddings to separate collections:
def export_text_embeddings_to_qdrant(
    sqlite_path: Path,
    text_vec_source,   # your adapter with .get_caption_vec(item_id) -> vector
    collection: str,
    dim: int,
    batch_ids: Iterable[Any],
    recreate: bool = False,
    batch_size: int = 256,
) -> None:
    client = QdrantClient(path=str(S.qdrant_path))
    ensure_collection(client, collection, vector_size=dim, distance=rest.Distance.COSINE, recreate=recreate)
    for ids in batched(batch_ids, batch_size):
        points = []
        for item_id in ids:
            v = text_vec_source.get_caption_vec(item_id)
            points.append(rest.PointStruct(id=item_id, vector=v, payload={"id": item_id}))
        client.upsert(collection_name=collection, points=points)
