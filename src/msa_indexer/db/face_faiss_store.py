import json
import numpy as np
from pathlib import Path
# On macOS Apple Silicon, faiss-cpu uses the Accelerate BLAS which conflicts
# with PyTorch's MPS backend if faiss is imported before torch — causing a
# SIGSEGV when the CLIP model is later moved to MPS. See faiss_store.py.
import torch  # noqa: F401 — must come before faiss on macOS/MPS
import faiss


class FaceFaissStore:
    """FAISS index for face embeddings (512-dim, IP with normalized vectors).

    Persists a sidecar ids file and vectors numpy file for retrieval by id.
    """

    def __init__(self, dim: int = 512, path: str | Path = "index/face_vec.faiss"):
        self.dim = dim
        self.path = str(path)
        self.ids_path = self.path + ".ids"
        self.vecs_path = self.path + ".vecs.npy"
        self.index = None
        self.ids: list[str] = []
        self._vecs: np.ndarray | None = None
        if Path(self.path).exists():
            self.load()
        else:
            self.index = faiss.IndexFlatIP(dim)
            self.ids = []
            self._vecs = np.zeros((0, dim), dtype="float32")

    def add(self, ids, vecs):
        vecs = np.asarray(vecs, dtype="float32")
        if vecs.ndim == 1:
            vecs = vecs[None, :]
        # Normalize for cosine via inner product
        faiss.normalize_L2(vecs)
        # Append to FAISS and sidecar arrays
        if self.index is None:
            self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(vecs)
        self.ids.extend([str(x) for x in ids])
        if self._vecs is None or self._vecs.size == 0:
            self._vecs = vecs.copy()
        else:
            self._vecs = np.vstack([self._vecs, vecs])

    def save(self):
        faiss.write_index(self.index, self.path)
        # Save ids and vectors sidecars
        with open(self.ids_path, "w", encoding="utf-8") as f:
            for _id in self.ids:
                f.write(_id + "\n")
        if self._vecs is not None:
            np.save(self.vecs_path, self._vecs)

    def load(self):
        self.index = faiss.read_index(self.path)
        # Load ids
        try:
            with open(self.ids_path, "r", encoding="utf-8") as f:
                self.ids = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            self.ids = []
        # Load vectors
        try:
            self._vecs = np.load(self.vecs_path)
        except Exception:
            self._vecs = None

    def get_vector(self, face_id: str) -> np.ndarray | None:
        """Return the stored vector for a given face_id, if available."""
        if not self.ids or self._vecs is None:
            return None
        try:
            idx = self.ids.index(str(face_id))
        except ValueError:
            return None
        return self._vecs[idx]

    def search(self, query_vec, k=5):
        query_vec = np.asarray(query_vec, dtype="float32").reshape(1, -1)
        faiss.normalize_L2(query_vec)
        if self.index is None:
            raise RuntimeError("FAISS index not initialized")
        D, I = self.index.search(query_vec, k)
        return D[0], [self.ids[i] for i in I[0]]
