from typing import List, Dict, Any, Optional
from msa_query.storage.qdrant_client import QdrantStore
from qdrant_client.http.models import Filter

class Retriever:
    def __init__(self, store: QdrantStore):
        self.store = store

    def search(self, collection: str, vector, k: int, query_filter: Optional[Filter] = None) -> List[Dict[str, Any]]:
        return self.store.search(collection, vector, k, query_filter=query_filter)
