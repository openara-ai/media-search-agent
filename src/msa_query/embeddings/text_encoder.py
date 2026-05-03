from __future__ import annotations
from typing import List
import numpy as np

# import your existing embedder
from msa_indexer.models.embeddings import ClipEmbedder
from msa_settings import load_config

S = load_config()

class TextEncoder:
    """
    Thin adapter so QueryEngine can call .encode(text)->List[float]
    backed by your real CLIP/SigLIP text model from Steps 1–3.
    """
    def __init__(self):
        # reuse the same model config you used in run_index.py
        self.model = ClipEmbedder(
            S.model_name,
            S.pretrained,
            S.device,
            cache_dir=getattr(S, "models_dir", None),
        )

        # If your class exposes .dim, keep it for sanity checks
        self.dim = getattr(self.model, "dim", None)

    def encode(self, text: str) -> List[float]:
        vec = self.model.text_embed(text)         # shape: (1, D) for single text
        # Flatten to (D,) if batched output
        if isinstance(vec, np.ndarray) and vec.ndim == 2:
            vec = vec.flatten()
        vec = vec.astype("float32")
        n = np.linalg.norm(vec)
        if n > 0:
            vec = vec / n
        return vec.tolist()
