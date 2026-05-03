import time
from pathlib import Path
from PIL import Image
from loguru import logger

# Commit pinned for known-tested model variants. None → resolves to main branch.
KNOWN_REVISIONS: dict[str, str | None] = {
    "PekingU/rtdetr_r18vd": "ac77a11ff0170a41b771c03264987f8ce2b0d753",
    "PekingU/rtdetr_r34vd": None,
    "PekingU/rtdetr_r50vd": None,
}

# tvmonitor → tv keeps labels consistent with the historical YOLO vocabulary.
_LABEL_NORM: dict[str, str] = {
    "tvmonitor": "tv",
    "pottedplant": "potted plant",
}


class RtDetrBackend:
    """RT-DETR object detection via HuggingFace transformers (Apache-2.0)."""

    def __init__(self, model_name: str, device: str, conf_threshold: float,
                 model_dir: Path | None = None):
        try:
            import torch
            from transformers import RTDetrForObjectDetection, RTDetrImageProcessor
        except ImportError as exc:
            raise ImportError(
                "Backend 'rtdetr' requires the transformers package.\n"
                "Install it: pip install transformers accelerate"
            ) from exc

        cache_dir = str(Path(model_dir) / "rtdetr") if model_dir is not None else None
        revision = KNOWN_REVISIONS.get(model_name)  # None for unknown variants → main

        cached = self._is_cached(model_name, cache_dir, revision)
        if cached:
            logger.info("Loading RT-DETR model {} from cache...", model_name)
        else:
            logger.info(
                "RT-DETR model {} not in cache — downloading (~81 MB, this may take a minute)...",
                model_name,
            )

        t0 = time.perf_counter()
        hf_kwargs: dict = {"cache_dir": cache_dir, "revision": revision}
        if cached:
            hf_kwargs["local_files_only"] = True
        self._processor = RTDetrImageProcessor.from_pretrained(model_name, **hf_kwargs)
        self._model = RTDetrForObjectDetection.from_pretrained(model_name, **hf_kwargs)

        torch_device = self._resolve_device(device)
        self._model = self._model.to(torch_device)
        self._model.eval()
        self._torch_device = torch_device
        self.conf_threshold = conf_threshold
        logger.info(
            "RT-DETR model {} loaded in {:.1f}s on {}", model_name, time.perf_counter() - t0, torch_device
        )

    @property
    def device(self) -> str:
        return str(self._torch_device)

    @staticmethod
    def _resolve_device(device: str):
        import torch
        if device == "mps":
            return torch.device("mps" if torch.backends.mps.is_available() else "cpu")
        if device == "cuda":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return torch.device("cpu")

    @staticmethod
    def _is_cached(model_name: str, cache_dir: str | None, revision: str | None = None) -> bool:
        """Return True if model.safetensors is in the HF snapshot cache for the requested revision.

        When revision is given we check the exact snapshot directory so that
        local_files_only=True is only set when the pinned commit is actually present —
        a snapshot from a different commit would cause from_pretrained() to fail.
        """
        if cache_dir is None:
            return False
        # HF layout: <cache_dir>/models--<org>--<name>/snapshots/<commit>/model.safetensors
        slug = "models--" + model_name.replace("/", "--")
        snapshots = Path(cache_dir) / slug / "snapshots"
        if not snapshots.exists():
            return False
        required = {"model.safetensors", "config.json", "preprocessor_config.json"}
        if revision is not None:
            snap = snapshots / revision
            return all((snap / f).exists() for f in required)
        return any(
            all((s / f).exists() for f in required)
            for s in snapshots.iterdir()
        )

    def detect(self, pil_image: Image.Image, return_boxes: bool = False) -> list[dict]:
        import torch
        try:
            inputs = self._processor(images=pil_image, return_tensors="pt")
            inputs = {k: v.to(self._torch_device) for k, v in inputs.items()}
            with torch.no_grad():
                outputs = self._model(**inputs)

            target_sizes = torch.tensor(
                [[pil_image.height, pil_image.width]], device=self._torch_device
            )
            results = self._processor.post_process_object_detection(
                outputs, threshold=self.conf_threshold, target_sizes=target_sizes
            )[0]

            id2label = self._model.config.id2label
            detections = []
            for score, label_id, box in zip(results["scores"], results["labels"], results["boxes"]):
                raw_label = id2label.get(label_id.item(), f"class_{label_id.item()}")
                label = _LABEL_NORM.get(raw_label, raw_label)
                det = {"label": label, "confidence": round(score.item(), 4)}
                if return_boxes:
                    det["bbox"] = [round(v, 1) for v in box.cpu().tolist()]
                detections.append(det)
            return detections
        except Exception as e:
            logger.warning("RT-DETR detection failed: {}", e)
            return []

    def get_labels(self, pil_image: Image.Image, min_confidence: float | None = None) -> list[str]:
        original_conf = None
        if min_confidence is not None:
            original_conf = self.conf_threshold
            self.conf_threshold = min_confidence

        detections = self.detect(pil_image, return_boxes=False)
        labels = sorted({d["label"] for d in detections})

        if original_conf is not None:
            self.conf_threshold = original_conf
        return labels
