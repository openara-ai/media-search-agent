from __future__ import annotations
from typing import Iterable, Dict, Any, List, Optional
from pathlib import Path
import math
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

def ensure_collection(
    client: QdrantClient,
    collection: str,
    vector_size: int,
    distance: rest.Distance = rest.Distance.COSINE,
    recreate: bool = False,
) -> None:
    """Create the collection if missing; optionally recreate."""
    import time
    
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
            
            # Attempt to delete (will succeed if exists, fail gracefully if doesn't)
            try:
                client.delete_collection(collection)
                logger.info(f"Delete request sent for collection '{collection}', waiting for completion...")
                # Wait a moment for deletion to complete (Qdrant deletion may be async)
                time.sleep(0.5)
                # Verify deletion by checking if collection still exists
                max_retries = 5
                deleted = False
                for retry in range(max_retries):
                    try:
                        client.get_collection(collection)
                        # Still exists, wait a bit more
                        logger.debug(f"Collection '{collection}' still exists, waiting... (retry {retry + 1}/{max_retries})")
                        time.sleep(0.5)
                    except Exception:
                        # Collection is gone, proceed
                        deleted = True
                        logger.info(f"Collection '{collection}' successfully deleted")
                        break
                if not deleted:
                    # After retries, collection still seems to exist
                    logger.warning(f"Collection '{collection}' may still exist after deletion attempts, but proceeding with creation attempt")
            except Exception as e:
                error_msg = str(e).lower()
                # If collection doesn't exist, that's fine - we'll create it
                if any(phrase in error_msg for phrase in ["not found", "404", "does not exist", "not_found"]):
                    logger.debug(f"Collection '{collection}' does not exist (cannot delete), will create new one")
                else:
                    logger.warning(f"Error deleting collection '{collection}': {e} - will attempt to create anyway")
        except Exception as e:
            logger.error(f"Unexpected error during collection deletion: {e}")
            # Don't raise - continue to try creating the collection
    
    # Check if collection exists now (after potential deletion)
    exists = False
    try:
        info = client.get_collection(collection)
        exists = True
        logger.info(f"Collection '{collection}' exists (points: {getattr(info, 'points_count', 'unknown')})")
        # optional: verify vector size & distance here if you want
        _sz = info.vectors_config.params.size  # type: ignore[attr-defined]
    except Exception as e:
        exists = False
        logger.debug(f"Collection '{collection}' does not exist")
    
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
                    logger.info(f"Collection '{collection}' already exists (skipping creation)")
                except Exception:
                    # If we still can't get collection info, re-raise the original error
                    raise
            else:
                # unknown error — re-raise
                raise
    elif recreate:
        # Collection exists and recreate was requested, but deletion failed or wasn't needed
        logger.warning(f"Collection '{collection}' exists but recreate was requested - collection was not recreated")

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

def _media_id_to_int(media_id: str) -> int:
    """Convert a media_id string to a stable integer ID for Qdrant.
    Uses the same encoding as FaissStore._id_for_media() to ensure compatibility.
    
    Returns a positive integer suitable for Qdrant (non-negative int64).
    """
    # Take first 8 bytes of media_id hex string (same as FaissStore)
    # but mask to ensure we get a non-negative int64
    raw = int.from_bytes(media_id.encode()[:8].ljust(8, b"\0"), "big")
    return raw & ((1 << 63) - 1)  # mask to non-negative int64


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

def export_images_to_qdrant(
    sqlite_path: Path,
    faiss_path: Optional[Path] = None,  # kept for backward-compat; ignored
    collection: Optional[str] = None,
    batch_size: int = 256,
    recreate: bool | None = None,
) -> Dict[str, Any]:
    """
    Read image metadata + embeddings from SQLite and upsert into Qdrant.

    Embeddings live in the ``image_embedding`` table (BLOB float32) joined
    by primary key to ``media``. The legacy ``faiss_path`` parameter is
    accepted but unused — kept so older callers don't break.

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
        ensure_collection(client, collection, vector_size=dim, distance=rest.Distance.COSINE, recreate=recreate)

        sent = skipped = errors = 0
        image_count = 0

        logger.info(f"Starting export to Qdrant collection '{collection}' ({dim}d vectors)")

        # Iterate metadata-bearing rows from the existing iter_items helper
        # (which already filters out videos and resolves paths). For each
        # batch we fetch all needed embedding BLOBs in a single SELECT
        # rather than N per-row queries — at 50K media that's the
        # difference between ~200 queries and 50K.
        for rows in batched(meta.iter_items(), batch_size):
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

def ensure_video_collection(client: QdrantClient, collection: str, vector_size: int, recreate: bool = False):
    ensure_collection(client, collection, vector_size, distance=rest.Distance.COSINE, recreate=recreate)

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
) -> Dict[str, Any]:
    """
    Export video keyframe embeddings from SQLite into Qdrant.

    Embeddings live in ``keyframe_embedding`` (BLOB float32) joined by
    primary key to ``video_keyframes``. Qdrant point IDs use stable
    64-bit ints via ``_compound_point_id`` for backward compatibility
    with downstream consumers.

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
        ensure_video_collection(client, collection, vector_size=dim, recreate=recreate)

        sent = skipped = errors = 0
        video_keyframes_count = 0
        logger.info(f"Starting export of video keyframes to Qdrant collection '{collection}' ({dim}d vectors)")

        for rows in batched(meta.iter_video_keyframes(), batch_size):
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

                payload = {
                    "id": vid,
                    "media_id": vid,
                    "type": "video",
                    "path": row.get("path"),
                    "timestamp": float(row.get("timestamp") or 0.0),
                    "shot_id": s_idx,
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
) -> Dict[str, Any]:
    """
    Export face embeddings from the ``face_embedding`` SQLite table to Qdrant.

    The ``embedding_backend`` value is stored in every point's payload so
    downstream consumers can tell which backend produced the vectors.

    Returns:
        Dictionary with stats: {'faces_count': int, 'sent': int, 'skipped': int, 'errors': int, 'dim': int}
    """
    import numpy as np

    with SQLiteStore(sqlite_path) as meta:
        client = QdrantClient(path=str(S.qdrant_path))

        first_row = meta.conn.execute(
            "SELECT embedding_dim FROM face_embedding LIMIT 1"
        ).fetchone()
        if first_row is None:
            logger.info(
                f"No face embeddings in SQLite — skipping face export to '{collection}'"
            )
            return {'faces_count': 0, 'sent': 0, 'skipped': 0, 'errors': 0, 'dim': 0}
        face_dim = int(first_row[0])

        if recreate is None:
            recreate = bool(getattr(S, 'qdrant_recreate_collections_on_export', False))

        logger.info(f"Ensuring face collection '{collection}' (recreate={recreate})")
        ensure_collection(client, collection, vector_size=face_dim, distance=rest.Distance.COSINE, recreate=recreate)

        sent = skipped = errors = 0
        faces_count = 0
        logger.info(f"Starting export of faces to Qdrant collection '{collection}' ({face_dim}d vectors)")

        for rows in batched(meta.iter_faces(), batch_size):
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

                payload = {
                    "face_id": face_id,
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

                # Use stable hash of face_id as point ID (stable int64 across processes)
                import hashlib
                h = int(hashlib.sha256(face_id.encode()).hexdigest()[:16], 16)
                pid = h & ((1 << 63) - 1)  # non-negative int64
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
