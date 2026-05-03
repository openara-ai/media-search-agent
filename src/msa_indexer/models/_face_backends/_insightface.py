"""
InsightFace backend (opt-in only).

The InsightFace library (MIT) is permissively licensed, but the bundled
pretrained weights (buffalo_l, buffalo_s, antelopev2) are NON-COMMERCIAL
per the InsightFace model zoo. Do not use this backend in a commercial
product without a separate commercial license from DeepInsight.

To opt in, set in config.yaml:
    face_recognizer_backend: insightface
    face_model: buffalo_l
and install: pip install insightface onnxruntime  (macOS)
             pip install insightface onnxruntime-gpu  (Linux/Windows CUDA)
"""
import time
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from loguru import logger
from PIL import Image

from msa_indexer.models._face_backends._types import FaceDetection


class InsightFaceBackend:
    """InsightFace ArcFace detection and embedding (opt-in, non-commercial weights)."""

    def __init__(
        self,
        model_name: str = "buffalo_l",
        device: str = "cuda",
        conf_threshold: float = 0.70,
        min_face_size: int = 20,
        model_root: Optional[Path] = None,
    ):
        self.model_name = model_name
        self.device = device
        self.conf_threshold = conf_threshold
        self.min_face_size = min_face_size
        self.app = None

        try:
            import insightface
            from insightface.app import FaceAnalysis

            if model_root is not None:
                insightface_root = Path(model_root) / "insightface"
                insightface_root.mkdir(parents=True, exist_ok=True)
            else:
                insightface_root = None

            _model_dir = (
                (insightface_root / "models" / model_name)
                if insightface_root
                else (Path.home() / ".insightface" / "models" / model_name)
            )

            # Windows extraction bug: nested directory self-heal.
            _nested = _model_dir / model_name
            if _nested.is_dir():
                logger.info("Fixing double-nested InsightFace model directory...")
                for _child in list(_nested.iterdir()):
                    _dst = _model_dir / _child.name
                    if not _dst.exists():
                        shutil.move(str(_child), str(_model_dir))
                if not any(_nested.iterdir()):
                    _nested.rmdir()
                logger.info("InsightFace model directory fixed.")

            cached = (
                _model_dir.exists() and any(_model_dir.iterdir())
                if _model_dir.exists()
                else False
            )
            if cached:
                logger.info("Loading InsightFace model {} from cache...", model_name)
            else:
                logger.info(
                    "InsightFace model {} not in cache — downloading (may take several minutes)...",
                    model_name,
                )

            if device == "cuda":
                _providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            elif device == "mps":
                _providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
            else:
                _providers = ["CPUExecutionProvider"]

            fa_kwargs: dict = {"name": model_name, "providers": _providers}
            if insightface_root is not None:
                fa_kwargs["root"] = str(insightface_root)

            t0 = time.perf_counter()
            self.app = FaceAnalysis(**fa_kwargs)
            self.app.prepare(ctx_id=0 if device in ("cuda", "mps") else -1, det_size=(640, 640))
            logger.info(
                "InsightFace model {} loaded in {:.1f}s on {}",
                model_name,
                time.perf_counter() - t0,
                device,
            )

        except ImportError as exc:
            raise ImportError(
                "Backend 'insightface' requires insightface and onnxruntime.\n"
                "macOS:         pip install insightface onnxruntime\n"
                "Linux/Windows: pip install insightface onnxruntime-gpu"
            ) from exc
        except Exception as exc:
            logger.exception(
                "Failed to initialise InsightFaceBackend: {} — {}", type(exc).__name__, exc
            )
            raise

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_and_embed(self, pil_image: Image.Image) -> List[FaceDetection]:
        if self.app is None:
            return []
        try:
            img_array = np.array(pil_image.convert("RGB"))
            img_height, img_width = img_array.shape[:2]
            faces = self.app.get(img_array)

            results: List[FaceDetection] = []
            for face in faces:
                bbox_abs = face.bbox.astype(int)
                x1, y1, x2, y2 = bbox_abs
                fw = x2 - x1
                fh = y2 - y1

                if fw < self.min_face_size or fh < self.min_face_size:
                    continue

                confidence = float(face.det_score)
                if confidence < self.conf_threshold:
                    continue

                bbox_norm: Tuple[float, float, float, float] = (
                    x1 / img_width,
                    y1 / img_height,
                    fw / img_width,
                    fh / img_height,
                )

                metadata: dict = {}
                if hasattr(face, "gender"):
                    metadata["gender"] = "M" if face.gender == 1 else "F"
                if hasattr(face, "age"):
                    metadata["age"] = int(face.age)
                if hasattr(face, "landmark_2d_106"):
                    metadata["landmarks"] = face.landmark_2d_106.tolist()

                results.append(
                    FaceDetection(
                        bbox=bbox_norm,
                        embedding=face.embedding,
                        confidence=confidence,
                        metadata=metadata,
                    )
                )
            return results

        except Exception as exc:
            logger.error("InsightFace detect_and_embed failed: {}", exc)
            return []
