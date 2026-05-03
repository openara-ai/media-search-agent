from pathlib import Path
# On macOS Apple Silicon, faiss-cpu uses the Accelerate BLAS which conflicts
# with PyTorch's MPS backend if faiss is imported before torch — causing a
# SIGSEGV when the CLIP model is later moved to MPS. Importing torch first
# ensures Metal/MPS is initialised before FAISS touches the Accelerate framework.
import torch  # noqa: F401 — must come before faiss on macOS/MPS
import faiss, numpy as np
from typing import Optional
import hashlib


class FaissStore:
    def __init__(self, dim: Optional[int], path: Path):
        """If dim is provided, create a new index; if dim is None and path exists,
        load the index from disk and infer dim. If neither dim nor an existing
        index is available, raise ValueError.
        """
        self.dim = dim
        self.path = path
        self.index = None

        # If an index file exists, load it and infer the dimension
        if self.path.exists():
            try:
                self.index = faiss.read_index(str(self.path))
                # many faiss index types expose 'd' as the vector dimension
                try:
                    self.dim = int(self.index.d)  # type: ignore[attr-defined]
                except Exception:
                    # fallback: try reconstruct of 0 if available
                    try:
                        v = self.index.reconstruct(0)
                        self.dim = len(v)
                    except Exception:
                        pass
            except Exception:
                # Could not read index; we'll create a new one if dim provided
                self.index = None

        if self.index is None:
            if self.dim is None:
                raise ValueError("dim must be provided when initializing a new FaissStore")
            base = faiss.IndexFlatIP(self.dim)           # cosine via normalized vectors
            self.index = faiss.IndexIDMap2(base)    # <-- key change

    def add(self, ids: list[str], vecs: np.ndarray):
        assert vecs.shape[1] == self.dim
        faiss.normalize_L2(vecs)
        # Stable int64 IDs via 64-bit hash (avoids collisions for compound ids)
        to_ids = np.array([self._hash64(i) for i in ids], dtype='int64')
        self.index.add_with_ids(vecs.astype("float32"), to_ids)

    def save(self):
        # TODO: This is intentionally the simple direct-write path for now. If
        # commit/rollback drift between SQLite and FAISS becomes a recurring issue,
        # move to a temp-write-then-rename save here.
        faiss.write_index(self.index, str(self.path))
    def load(self):
        if self.path.exists():
            self.index = faiss.read_index(str(self.path))
            try:
                self.dim = int(self.index.d)  # type: ignore[attr-defined]
            except Exception:
                pass

    def vector_size(self) -> int:
        """Return the embedding dimensionality. Raises if unknown."""
        if self.dim is None:
            raise RuntimeError("FaissStore: vector dimension unknown. Ensure index file exists or initialize with dim.")
        return int(self.dim)

    def _hash64(self, key: str) -> int:
        """Stable 64-bit integer from arbitrary string using blake2b(64-bit).
        Returns a non-negative int64 suitable for FAISS (masked to [0, 2^63-1]).
        """
        h = hashlib.blake2b(key.encode(), digest_size=8).digest()
        val = int.from_bytes(h, 'big', signed=False)
        # Mask to non-negative int64 range for FAISS compatibility
        return val & ((1 << 63) - 1)

    def _id_for_media(self, media_id: str) -> int:
        # Use same 64-bit hash as add() for consistent id mapping
        return self._hash64(media_id)

    def get_vector(self, media_id: str):
        """Reconstruct the vector for the given media id. Returns list[float] or raises KeyError."""
        iid = self._id_for_media(media_id)
        try:
            # faiss reconstruct takes int64 id
            v = self.index.reconstruct(iid)
            return v.tolist()
        except Exception as e:
            # ID not present or index not support reconstruct
            raise KeyError(f"Vector for id {media_id} not found: {e}")
