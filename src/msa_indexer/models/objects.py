"""
Object detection dispatcher.

Supports pluggable backends selected via config:
  rtdetr  — RT-DETR via HuggingFace transformers (Apache-2.0, default)

Backend modules are imported lazily so missing packages only error when
the relevant backend is actually selected.
"""
import importlib
from pathlib import Path
from PIL import Image
from loguru import logger

_BACKENDS: dict[str, str] = {
    "rtdetr": "msa_indexer.models._detection_backends._rtdetr.RtDetrBackend",
}


def _load_backend(name: str) -> type:
    if name not in _BACKENDS:
        raise ValueError(
            f"Unknown object detection backend {name!r}. "
            f"Available: {list(_BACKENDS)}"
        )
    module_path, cls_name = _BACKENDS[name].rsplit(".", 1)
    try:
        module = importlib.import_module(module_path)
        return getattr(module, cls_name)
    except ImportError as exc:
        raise ImportError(
            f"Object detection backend {name!r} requires packages that are not installed.\n"
            f"{exc}"
        ) from exc


class ObjectDetector:
    """Backend-neutral object detector."""

    def __init__(
        self,
        model_name: str = "PekingU/rtdetr_r18vd",
        device: str = "cpu",
        conf_threshold: float = 0.35,
        model_dir: Path | None = None,
        backend: str = "rtdetr",  # only "rtdetr" is supported
    ):
        cls = _load_backend(backend)
        self._impl = cls(
            model_name=model_name,
            device=device,
            conf_threshold=conf_threshold,
            model_dir=model_dir,
        )
        logger.debug("ObjectDetector: backend={} model={}", backend, model_name)

    @property
    def device(self) -> str:
        return self._impl.device

    @property
    def conf_threshold(self) -> float:
        return self._impl.conf_threshold

    @conf_threshold.setter
    def conf_threshold(self, value: float) -> None:
        self._impl.conf_threshold = value

    def detect(self, pil_image: Image.Image, return_boxes: bool = False) -> list[dict]:
        return self._impl.detect(pil_image, return_boxes=return_boxes)

    def get_labels(self, pil_image: Image.Image, min_confidence: float | None = None) -> list[str]:
        return self._impl.get_labels(pil_image, min_confidence=min_confidence)


def detect_objects(
    pil_image: Image.Image,
    model_name: str = "PekingU/rtdetr_r18vd",
    device: str = "cpu",
    conf_threshold: float = 0.35,
    backend: str = "rtdetr",
    model_dir: Path | None = None,
) -> list[str]:
    """Convenience function for one-off detection. Prefer instantiating ObjectDetector for batch use."""
    detector = ObjectDetector(
        model_name=model_name, device=device, conf_threshold=conf_threshold,
        backend=backend, model_dir=model_dir,
    )
    return detector.get_labels(pil_image)
