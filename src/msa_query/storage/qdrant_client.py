import threading
from typing import List, Dict, Any, Optional
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
# _blocked is set True while the indexer subprocess is running so that the
# API gracefully declines new Qdrant operations rather than racing for the lock.
# ---------------------------------------------------------------------------
_client_lock = threading.Lock()
_shared: Optional[QdrantClient] = None
_blocked: bool = False  # True while indexer subprocess holds the Qdrant lock


def get_shared_client() -> Optional[QdrantClient]:
    """Return the process-wide QdrantClient, creating it if needed.

    Returns None if Qdrant is currently unavailable (indexer running).
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


def close_shared_client() -> None:
    """Close the shared client and block new ones (call before launching indexer)."""
    global _shared, _blocked
    _blocked = True
    with _client_lock:
        if _shared is not None:
            try:
                _shared.close()
            except Exception:
                pass
            _shared = None


def reopen_shared_client() -> None:
    """Unblock Qdrant access (call after indexer subprocess exits)."""
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
        client = get_shared_client()
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
