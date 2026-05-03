"""
facenet-pytorch backend: MTCNN detection + InceptionResnetV1 (VGGFace2) embedding.

License: MIT (timesler/facenet-pytorch).
Weights: VGGFace2-pretrained InceptionResnetV1, distributed under MIT by the package author.
Embedding dimension: 512 — matches the legacy InsightFace collection schema.

Platform notes:
  - MTCNN always runs on CPU on macOS MPS: PyTorch MPS does not support adaptive
    pooling (pytorch#96056). InceptionResnetV1 can still use MPS.
  - Model weights are written to models_dir/facenet_pytorch/checkpoints/ via
    TORCH_HOME (facenet-pytorch reads TORCH_HOME directly and appends /checkpoints,
    independent of torch.hub.get_dir()).
"""
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from loguru import logger
from PIL import Image

from msa_indexer.models._face_backends._types import FaceDetection


class FacenetPytorchBackend:
    """MTCNN + InceptionResnetV1 VGGFace2 face detection and embedding."""

    def __init__(
        self,
        model_name: str = "vggface2",
        device: str = "cpu",
        conf_threshold: float = 0.80,
        min_face_size: int = 20,
        model_root: Optional[Path] = None,
    ):
        self.model_name = model_name
        self.conf_threshold = conf_threshold
        self.min_face_size = min_face_size

        try:
            import torch

            # Redirect torch hub cache before any model load.
            if model_root is not None:
                hub_dir = str(Path(model_root) / "facenet_pytorch")
                os.environ.setdefault("TORCH_HOME", hub_dir)
                torch.hub.set_dir(hub_dir)

            # Resolve devices. MTCNN uses adaptive pooling which is unsupported
            # on MPS (pytorch#96056), so detection always falls back to CPU on
            # Apple Silicon even when the caller requests MPS.
            self._embed_device = self._resolve_device(device)
            self._mtcnn_device = (
                torch.device("cpu")
                if str(self._embed_device) == "mps"
                else self._embed_device
            )
            if str(self._embed_device) == "mps" and device == "mps":
                logger.info(
                    "FaceNet: MTCNN running on CPU (MPS adaptive-pooling unsupported, pytorch#96056); "
                    "InceptionResnetV1 running on MPS"
                )

            from facenet_pytorch import MTCNN, InceptionResnetV1

            # Log cache/download status so setup UI and tests can verify feedback.
            if model_root is not None:
                _checkpoints = Path(model_root) / "facenet_pytorch" / "checkpoints"
                # Actual torch hub filename: 20180402-114759-vggface2.pt (model_name as substring)
                _cached = any(_checkpoints.glob(f"*{model_name}*.pt")) if _checkpoints.exists() else False
            else:
                _cached = False
            if _cached:
                logger.info("FaceNet ({}): loading from cache", model_name)
            else:
                logger.info(
                    "FaceNet ({}): not in cache — downloading weights (~108 MB)", model_name
                )

            t0 = time.perf_counter()
            self._mtcnn = MTCNN(
                keep_all=True,
                device=self._mtcnn_device,
                min_face_size=min_face_size,
                thresholds=[0.6, 0.7, conf_threshold],
                post_process=True,
            )
            self._resnet = (
                InceptionResnetV1(pretrained=model_name)
                .eval()
                .to(self._embed_device)
            )
            logger.info(
                "FaceNet ({}) loaded in {:.1f}s — mtcnn={} embed={}",
                model_name,
                time.perf_counter() - t0,
                self._mtcnn_device,
                self._embed_device,
            )

        except ImportError as exc:
            raise ImportError(
                "Backend 'facenet_pytorch' requires facenet-pytorch.\n"
                "Install: uv pip install facenet-pytorch --no-deps"
            ) from exc
        except Exception as exc:
            logger.exception(
                "Failed to initialise FacenetPytorchBackend: {} — {}", type(exc).__name__, exc
            )
            raise

    # ── Public API ────────────────────────────────────────────────────────────

    def detect_and_embed(self, pil_image: Image.Image) -> List[FaceDetection]:
        import torch
        from facenet_pytorch import extract_face, fixed_image_standardization

        try:
            img_rgb = pil_image.convert("RGB")
            w, h = img_rgb.size

            # Single MTCNN pass — boxes and probabilities only.
            boxes, probs = self._mtcnn.detect(img_rgb)
            if boxes is None:
                return []

            # Filter by confidence/size and extract aligned crops from the
            # boxes returned above. This reuses the single detection pass
            # instead of calling self._mtcnn(img) again, which would run a
            # second full MTCNN forward and risk returning a different
            # set/order of detections.
            kept: List[Tuple] = []  # (box, prob, crop_tensor)
            for box, prob in zip(boxes, probs):
                if prob is None or float(prob) < self.conf_threshold:
                    continue
                x1, y1, x2, y2 = box
                face_w, face_h = x2 - x1, y2 - y1
                if face_w < self.min_face_size or face_h < self.min_face_size:
                    logger.debug("Skipping small face: {:.0f}x{:.0f}px", face_w, face_h)
                    continue
                crop = fixed_image_standardization(
                    extract_face(img_rgb, box, image_size=160, margin=0)
                )
                kept.append((box, float(prob), crop))

            logger.debug(
                "FaceNet: {} faces detected ({} passed threshold {:.2f})",
                len(boxes),
                len(kept),
                self.conf_threshold,
            )
            if not kept:
                return []

            # Batch all crops through ResNet in a single forward pass.
            crops_tensor = torch.stack([c for _, _, c in kept]).to(self._embed_device)
            with torch.no_grad():
                embeddings = self._resnet(crops_tensor)

            results: List[FaceDetection] = []
            for (box, prob, _), embedding in zip(kept, embeddings):
                x1, y1, x2, y2 = box
                face_w, face_h = x2 - x1, y2 - y1
                results.append(
                    FaceDetection(
                        bbox=(
                            float(x1) / w,
                            float(y1) / h,
                            float(face_w) / w,
                            float(face_h) / h,
                        ),
                        embedding=embedding.cpu().numpy().astype(np.float32),
                        confidence=prob,
                        metadata={},
                    )
                )
            return results

        except Exception as exc:
            logger.error("FaceNet detect_and_embed failed: {}", exc)
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_device(device: str):
        import torch

        if device == "mps":
            return torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        if device == "cuda":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return torch.device("cpu")
