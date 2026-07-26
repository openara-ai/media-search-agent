"""Helpers to sync Qdrant face_emb payloads on labeling operations."""
from typing import Optional, List
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http import models as rest

try:
    from msa_settings import load_config
    S = load_config()
except Exception:
    S = None


def _get_qdrant_client() -> Optional[QdrantClient]:
    """Return a QdrantClient for the configured embedded Qdrant store.

    Always constructs a local client from config — msa_indexer must not
    import msa_query (that would reverse the documented dependency direction).

    Fallback only: API-process callers pass ``client=`` explicitly (the shared
    client via ``shared_client_op()``). Under the sentinel-file handoff the
    API holds the embedded lock for nearly the entire run, so a per-call
    client constructed here would contend for the lock; construction fails
    fast ("already accessed by another instance") and the caller logs a
    skip rather than deadlocking.
    """
    if S is None:
        return None
    try:
        return QdrantClient(path=str(S.qdrant_path))
    except Exception as e:
        logger.warning("Could not create Qdrant client: {}", e)
        return None


def update_face_payload(
    face_id: str,
    person_id: Optional[str],
    person_name: Optional[str],
    collection: str = "face_emb",
    client: Optional[QdrantClient] = None,
) -> bool:
    """Update Qdrant payload for a single face. Returns True on success, False otherwise."""
    client = client or _get_qdrant_client()
    if client is None:
        logger.warning("Qdrant client unavailable; skipping payload update")
        return False
    
    # Compute stable point ID from face_id using hashlib (stable across processes)
    import hashlib
    h = int(hashlib.sha256(face_id.encode()).hexdigest()[:16], 16)
    pid = h & ((1 << 63) - 1)
    
    try:
        # Overwrite payload fields (set_payload merges by default; we can specify replace=False for merge)
        client.set_payload(
            collection_name=collection,
            payload={
                "person_id": person_id,
                "person_name": person_name,
            },
            points=[pid],
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update Qdrant payload for face_id={face_id}: {e}")
        return False


def update_faces_payload_batch(
    updates: List[tuple[str, Optional[str], Optional[str]]],
    collection: str = "face_emb",
    client: Optional[QdrantClient] = None,
) -> int:
    """
    Batch-update multiple faces' payloads in Qdrant.
    
    Args:
        updates: List of (face_id, person_id, person_name) tuples
        collection: Qdrant collection name
    
    Returns:
        Count of successfully updated faces
    """
    client = client or _get_qdrant_client()
    if client is None:
        logger.warning("Qdrant client unavailable; skipping batch payload update")
        return 0

    updated = 0
    for face_id, person_id, person_name in updates:
        import hashlib
        h = int(hashlib.sha256(face_id.encode()).hexdigest()[:16], 16)
        pid = h & ((1 << 63) - 1)
        try:
            client.set_payload(
                collection_name=collection,
                payload={
                    "person_id": person_id,
                    "person_name": person_name,
                },
                points=[pid],
            )
            updated += 1
        except Exception as e:
            logger.error(f"Failed to update Qdrant payload for face_id={face_id}: {e}")
    
    return updated


def set_face_person_batch(
    face_ids: List[str],
    person_id: str,
    person_name: Optional[str],
    collection: str = "face_emb",
    chunk_size: int = 1000,
    client: Optional[QdrantClient] = None,
) -> int:
    """
    Set person_id/person_name for many faces sharing the same person.
    Sends one set_payload call per chunk — far faster than N individual calls.
    Returns total point count successfully updated.
    """
    import hashlib
    client = client or _get_qdrant_client()
    if client is None:
        logger.warning("Qdrant client unavailable; skipping batch face person update")
        return 0

    def _pid(face_id: str) -> int:
        h = int(hashlib.sha256(face_id.encode()).hexdigest()[:16], 16)
        return h & ((1 << 63) - 1)

    payload = {"person_id": person_id, "person_name": person_name}
    updated = 0
    for i in range(0, len(face_ids), chunk_size):
        chunk = face_ids[i : i + chunk_size]
        pids = [_pid(fid) for fid in chunk]
        try:
            client.set_payload(collection_name=collection, payload=payload, points=pids)
            updated += len(chunk)
        except Exception as e:
            logger.error(f"Qdrant batch set_payload failed (chunk {i}–{i+len(chunk)}): {e}")
    return updated


def sync_person_rename(
    person_id: str,
    new_name: str,
    collection: str = "face_emb",
    client: Optional[QdrantClient] = None,
) -> int:
    """
    Update person_name in Qdrant for all faces assigned to this person.
    Returns count of updated points.
    """
    client = client or _get_qdrant_client()
    if client is None:
        logger.warning("Qdrant client unavailable; skipping person rename sync")
        return 0
    
    try:
        # Scroll to find all faces with this person_id
        offset = None
        updated = 0
        while True:
            result, offset = client.scroll(
                collection_name=collection,
                scroll_filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="person_id",
                            match=rest.MatchValue(value=person_id),
                        )
                    ]
                ),
                limit=100,
                offset=offset,
            )
            if not result:
                break
            # Update payload for all matched points individually (scroll result has ExtendedPointId)
            if result:
                for point in result:
                    try:
                        client.set_payload(
                            collection_name=collection,
                            payload={"person_name": new_name},
                            points=[point.id],
                        )
                        updated += 1
                    except Exception as e:
                        logger.debug(f"Skipped point {point.id}: {e}")
            if offset is None:
                break
        
        logger.info(f"Synced person rename to Qdrant: {updated} faces updated for person_id={person_id}")
        return updated
    
    except Exception as e:
        logger.error(f"Failed to sync person rename in Qdrant: {e}")
        return 0


def sync_person_merge(
    source_id: str,
    target_id: str,
    target_name: str,
    collection: str = "face_emb",
    client: Optional[QdrantClient] = None,
) -> int:
    """
    Update all faces from source_id to target_id in Qdrant.
    Returns count of updated points.
    """
    client = client or _get_qdrant_client()
    if client is None:
        logger.warning("Qdrant client unavailable; skipping person merge sync")
        return 0
    
    try:
        offset = None
        updated = 0
        while True:
            result, offset = client.scroll(
                collection_name=collection,
                scroll_filter=rest.Filter(
                    must=[
                        rest.FieldCondition(
                            key="person_id",
                            match=rest.MatchValue(value=source_id),
                        )
                    ]
                ),
                limit=100,
                offset=offset,
            )
            if not result:
                break
            # Update payload for all matched points individually
            if result:
                for point in result:
                    try:
                        client.set_payload(
                            collection_name=collection,
                            payload={
                                "person_id": target_id,
                                "person_name": target_name,
                            },
                            points=[point.id],
                        )
                        updated += 1
                    except Exception as e:
                        logger.debug(f"Skipped point {point.id}: {e}")
            if offset is None:
                break
        
        logger.info(f"Synced person merge to Qdrant: {updated} faces updated from source={source_id} to target={target_id}")
        return updated
    
    except Exception as e:
        logger.error(f"Failed to sync person merge in Qdrant: {e}")
        return 0
