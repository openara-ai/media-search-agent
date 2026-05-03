from msa_query.storage.qdrant_client import QdrantStore
from msa_query.embeddings.text_encoder import TextEncoder
from msa_query.query_engine.engine import QueryEngine
from msa_settings import load_config
_qe: QueryEngine | None = None


def get_query_engine() -> QueryEngine:
    global _qe
    if _qe is None:
        store = QdrantStore()
        encoder = TextEncoder()
        cfg = load_config()
        _qe = QueryEngine(
            retriever=store,
            text_encoder=encoder,
            sqlite_path=cfg.sqlite_path,
            search_score_trace=bool(getattr(getattr(cfg, "retrieval", None), "search_score_trace", False)),
        )
    return _qe


def reset_query_engine() -> None:
    """Discard the cached QueryEngine so the next search creates a fresh one.

    Call this after the indexer export completes — the embedded Qdrant client
    opened before the export won't see collections written by the indexer's
    own embedded client unless we reinitialise it.
    """
    global _qe
    _qe = None
