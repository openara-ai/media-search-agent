"""
Face detection and recognition dispatcher.

Supports pluggable backends selected via config:
  facenet_pytorch  — MTCNN + InceptionResnetV1 VGGFace2 (MIT, default)
  insightface      — ArcFace buffalo_l/s (NON-COMMERCIAL weights, opt-in only)

Backend modules are imported lazily so missing packages only error when
the relevant backend is actually selected.
"""
import importlib
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from loguru import logger
from PIL import Image

from msa_indexer.models._face_backends._types import FaceDetection  # re-exported for callers

# Legacy InsightFace model names — used only to detect misconfigured backends
# and emit a migration warning.
_INSIGHTFACE_MODEL_NAMES = frozenset({"buffalo_l", "buffalo_s", "antelopev2"})

_BACKENDS: dict[str, str] = {
    "facenet_pytorch": "msa_indexer.models._face_backends._facenet_pytorch.FacenetPytorchBackend",
    "insightface": "msa_indexer.models._face_backends._insightface.InsightFaceBackend",
}


def _load_backend(name: str) -> type:
    if name not in _BACKENDS:
        raise ValueError(
            f"Unknown face backend {name!r}. Available: {list(_BACKENDS)}"
        )
    module_path, cls_name = _BACKENDS[name].rsplit(".", 1)
    try:
        module = importlib.import_module(module_path)
        return getattr(module, cls_name)
    except ImportError as exc:
        raise ImportError(
            f"Face backend {name!r} requires packages that are not installed.\n{exc}"
        ) from exc


class FaceRecognizer:
    """
    Backend-neutral face detector and embedder. Public API is unchanged from
    the InsightFace era.

    Default backend is facenet_pytorch (MIT). To opt in to InsightFace (which
    carries non-commercial weight restrictions), set:
        backend="insightface", model_name="buffalo_l"
    and install insightface + onnxruntime separately.
    """

    def __init__(
        self,
        model_name: str = "vggface2",
        device: str = "cuda",
        conf_threshold: float = 0.80,
        min_face_size: int = 20,
        model_root: Optional[Path] = None,
        backend: str = "facenet_pytorch",
    ):
        self._impl = None

        # Detect misconfigured legacy model names and warn.
        if model_name in _INSIGHTFACE_MODEL_NAMES and backend != "insightface":
            logger.warning(
                "face_model '{}' is an InsightFace model name but backend is '{}'. "
                "The default backend has changed to 'facenet_pytorch'. "
                "Using face_model='vggface2' instead. "
                "To keep InsightFace, set face_recognizer_backend: insightface in config.yaml "
                "(non-commercial weights — see NOTICE).",
                model_name,
                backend,
            )
            model_name = "vggface2"

        self.model_name = model_name
        self.device = device
        self.conf_threshold = conf_threshold
        self.min_face_size = min_face_size

        cls = _load_backend(backend)
        self._impl = cls(
            model_name=model_name,
            device=device,
            conf_threshold=conf_threshold,
            min_face_size=min_face_size,
            model_root=model_root,
        )
        logger.debug("FaceRecognizer: backend={} model={}", backend, model_name)

    # ── Detection ─────────────────────────────────────────────────────────────

    def detect_and_embed(self, pil_image: Image.Image) -> List[FaceDetection]:
        return self._impl.detect_and_embed(pil_image)

    # ── Comparison (pure NumPy — no backend dependency) ───────────────────────

    def compare_faces(
        self,
        emb1: np.ndarray,
        emb2: np.ndarray,
        threshold: float = 0.6,
    ) -> Tuple[float, bool]:
        """Cosine similarity between two face embeddings."""
        try:
            similarity = float(
                np.dot(emb1, emb2) / (np.linalg.norm(emb1) * np.linalg.norm(emb2))
            )
            similarity = max(0.0, min(1.0, similarity))
            return similarity, similarity >= threshold
        except Exception as exc:
            logger.error("compare_faces failed: {}", exc)
            return 0.0, False

    def get_similarity_matrix(self, embeddings: List[np.ndarray]) -> np.ndarray:
        """Pairwise cosine similarity matrix for a list of face embeddings."""
        n = len(embeddings)
        if n == 0:
            return np.array([])
        emb_matrix = np.stack(embeddings)
        emb_matrix_norm = emb_matrix / np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        return np.dot(emb_matrix_norm, emb_matrix_norm.T)


def detect_faces_and_embeddings(pil_image):
    """Legacy stub retained for import compatibility. Do not use."""
    return []
