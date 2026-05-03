"""
setup_models.py — First-launch model download manager.

Tracks per-model download state in a thread-safe singleton. FastAPI endpoints
read this state; a background thread does the actual downloading so the async
event loop is never blocked.

Models downloaded:
  - CLIP ViT-L-14/openai              (~850 MB)  via open_clip / huggingface_hub
  - RT-DETR PekingU/rtdetr_r18vd       (~81 MB)  via HuggingFace transformers
  - facenet-pytorch VGGFace2           (~108 MB)  via torch hub (TORCH_HOME redirect)

Integrity: RT-DETR weights are verified by HuggingFace hub's built-in
blob-level SHA-256 during download. We pin a known-good revision for
the default model (see RTDETR_DEFAULT_REVISION) and rely on HF's
content-addressed cache rather than maintaining our own hash table.
CLIP weights are verified by a stored SHA-256. facenet-pytorch weights
are verified by torch hub's own integrity check on download.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

# ── Types ─────────────────────────────────────────────────────────────────────

ModelState = Literal["pending", "downloading", "verifying", "done", "error"]

# Pinned revision for the default RT-DETR model. HuggingFace hub verifies
# per-blob SHA-256 during download, so we don't maintain a separate hash.
# Update this constant deliberately when upgrading the default model weights.
RTDETR_DEFAULT_MODEL    = "PekingU/rtdetr_r18vd"
RTDETR_DEFAULT_REVISION = "ac77a11ff0170a41b771c03264987f8ce2b0d753"

MODEL_META = {
    "clip": {
        "label":   "CLIP ViT-L-14",
        "size_mb": 850,
        # SHA-256 of open_clip_model.safetensors (timm/vit_large_patch14_clip_224.openai)
        "sha256":  "9ce2e8a8ebfff3793d7d375ad6d3c35cb9aebf3de7ace0fc7308accab7cd207e",
    },
    "rtdetr": {
        "label":    "RT-DETR r18vd",
        "size_mb":  81,
        # No sha256 — integrity guaranteed by HuggingFace hub blob-level verification.
        # Revision pinned via RTDETR_DEFAULT_REVISION for reproducible installs.
        "revision": RTDETR_DEFAULT_REVISION,
    },
    "facenet_pytorch": {
        "label":   "facenet-pytorch VGGFace2",
        "size_mb": 108,
        # Weights downloaded by torch hub; no separate hash maintained here.
        # MTCNN weights (~1 MB) are bundled in the facenet-pytorch package itself.
        # InceptionResnetV1 VGGFace2 weights (~107 MB) are fetched from the
        # timesler/facenet-pytorch GitHub release assets by torch hub on first use.
    },
}


# ── Hash verification ─────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    """Return lowercase hex SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_file(path: Path, expected: str) -> bool:
    """Return True if file exists and its SHA-256 matches expected."""
    if not path.exists():
        return False
    actual = _sha256(path)
    if actual != expected:
        logger.warning(
            "setup_models: hash mismatch for {} — expected {} got {}",
            path.name, expected[:12] + "…", actual[:12] + "…",
        )
        return False
    return True


@dataclass
class ModelDownloadState:
    status: ModelState = "pending"
    error: str | None = None

    def to_dict(self) -> dict:
        return {"status": self.status, "error": self.error}


# ── Manager ───────────────────────────────────────────────────────────────────

class SetupManager:
    """Thread-safe singleton managing first-launch model downloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, ModelDownloadState] = {
            k: ModelDownloadState() for k in MODEL_META
        }
        self._download_started = False
        self._complete = False

    # ── Presence checks ───────────────────────────────────────────────────────

    def check_all_present(self, models_dir: Path) -> dict[str, bool]:
        """Check which model weight files are already on disk."""
        clip_cache = models_dir / "clip"

        # Look for the specific weight file that we later verify by hash.
        # On Windows (symlink-less HuggingFace blobs/ layout) the file is stored
        # by content hash rather than name; accept only files >100 MB so small
        # metadata blobs don't falsely satisfy the check.
        clip_present = clip_cache.exists() and (
            any(clip_cache.rglob("open_clip_model.safetensors"))
            or any(
                f.is_file() and f.stat().st_size > 100_000_000
                for bd in clip_cache.rglob("blobs")
                if bd.is_dir()
                for f in bd.iterdir()
            )
        )

        # RT-DETR: look for model.safetensors under any snapshot of the default model.
        # HuggingFace hub layout: <cache>/models--<org>--<name>/snapshots/<commit>/model.safetensors
        rtdetr_cache = models_dir / "rtdetr"
        rtdetr_slug  = "models--" + RTDETR_DEFAULT_MODEL.replace("/", "--")
        rtdetr_snapshots = rtdetr_cache / rtdetr_slug / "snapshots"
        # Require the exact pinned revision snapshot to contain both the model
        # weights and the processor config — a partial copy (weights only) would
        # cause local_files_only=True to fail when the processor tries to load
        # its config at indexer startup.
        rtdetr_snap = rtdetr_snapshots / RTDETR_DEFAULT_REVISION
        rtdetr_present = (
            rtdetr_snap.exists()
            and (rtdetr_snap / "model.safetensors").exists()
            and (rtdetr_snap / "preprocessor_config.json").exists()
        )

        # facenet-pytorch reads TORCH_HOME directly (not torch.hub.get_dir()) and
        # writes to ${TORCH_HOME}/checkpoints — see facenet_pytorch.models.inception_resnet_v1
        # `model_dir = os.path.join(get_torch_home(), 'checkpoints')`. The download worker
        # sets TORCH_HOME=models_dir/facenet_pytorch, so weights land at
        # models_dir/facenet_pytorch/checkpoints (no /hub segment). The actual filename
        # from the release asset is 20180402-114759-vggface2.pt — "vggface2" is a
        # substring, not a prefix.
        facenet_hub = models_dir / "facenet_pytorch" / "checkpoints"
        facenet_present = facenet_hub.is_dir() and any(
            "vggface2" in f.name and f.suffix == ".pt"
            for f in facenet_hub.iterdir()
        )

        return {
            "clip":            clip_present,
            "rtdetr":          rtdetr_present,
            "facenet_pytorch": facenet_present,
        }

    def is_ready(self, models_dir: Path) -> bool:
        return all(self.check_all_present(models_dir).values())

    # ── State access ──────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, dict]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._models.items()}

    def is_complete(self) -> bool:
        with self._lock:
            return self._complete

    # ── Download control ──────────────────────────────────────────────────────

    def start_if_needed(self, models_dir: Path) -> None:
        """Start a background download thread if not already running.

        If a previous attempt left some models in error state, those are reset
        so they will be retried — but only once every in-progress download has
        settled.  Resetting state while another thread is actively writing to
        the same files creates races and inconsistent UI updates.
        """
        with self._lock:
            any_error = any(
                v.status == "error" for v in self._models.values()
            )
            in_progress = any(
                v.status in ("downloading", "verifying")
                for v in self._models.values()
            )
            # Already running cleanly, or a download is still in flight — do nothing.
            if self._download_started and (not any_error or in_progress):
                return
            # All downloads have settled and at least one errored: reset for retry.
            for v in self._models.values():
                if v.status == "error":
                    v.status = "pending"
                    v.error = None
            self._download_started = True
            self._complete = False

        thread = threading.Thread(
            target=self._download_worker,
            args=(models_dir,),
            daemon=True,
            name="msa-model-download",
        )
        thread.start()

    def _set(self, model: str, status: ModelState, error: str | None = None) -> None:
        with self._lock:
            self._models[model].status = status
            self._models[model].error = error

    # ── Download worker (runs in background thread) ───────────────────────────

    def _download_worker(self, models_dir: Path) -> None:
        try:
            models_dir.mkdir(parents=True, exist_ok=True)
            present = self.check_all_present(models_dir)
        except Exception as exc:
            # Bootstrap failure (e.g. unwritable models_dir) — mark every still-pending
            # model as error and set complete so the WS loop terminates and the UI can
            # show the error rather than polling forever.
            logger.error("setup_models: worker bootstrap failed — {}", exc)
            with self._lock:
                for v in self._models.values():
                    if v.status == "pending":
                        v.status = "error"
                        v.error = f"Setup failed to start: {exc}"
                self._complete = True
            return

        # ── CLIP ViT-L-14/openai ──────────────────────────────────────────
        if present["clip"]:
            self._set("clip", "done")
            logger.info("setup_models: CLIP already present, skipping download")
        else:
            self._set("clip", "downloading")
            try:
                clip_cache = models_dir / "clip"
                clip_cache.mkdir(parents=True, exist_ok=True)
                import open_clip  # type: ignore[import]
                open_clip.create_model_and_transforms(
                    "ViT-L-14", pretrained="openai",
                    device="cpu", cache_dir=str(clip_cache),
                )
                # Verify the main weight file (open_clip_model.safetensors).
                # Treat a missing file as a hard error — open_clip may have
                # written only tokeniser metadata without the actual weights.
                self._set("clip", "verifying")
                weight_files = list(clip_cache.rglob("open_clip_model.safetensors"))
                if not weight_files:
                    raise ValueError(
                        "open_clip_model.safetensors not found after download"
                    )
                expected = MODEL_META["clip"]["sha256"]
                if not _verify_file(weight_files[0], expected):
                    weight_files[0].unlink(missing_ok=True)
                    raise ValueError("SHA-256 mismatch for CLIP weights")
                self._set("clip", "done")
                logger.info("setup_models: CLIP ViT-L-14 downloaded and verified")
            except Exception as exc:
                logger.error("setup_models: CLIP download failed — {}", exc)
                self._set("clip", "error", str(exc))

        # ── RT-DETR PekingU/rtdetr_r18vd ─────────────────────────────────
        if present["rtdetr"]:
            self._set("rtdetr", "done")
            logger.info("setup_models: RT-DETR already present, skipping download")
        else:
            self._set("rtdetr", "downloading")
            try:
                from transformers import (  # type: ignore[import]
                    RTDetrForObjectDetection,
                    RTDetrImageProcessor,
                )
                rtdetr_cache = str(models_dir / "rtdetr")
                # Processor (~2 MB config files) downloads first, then model weights.
                RTDetrImageProcessor.from_pretrained(
                    RTDETR_DEFAULT_MODEL,
                    revision=RTDETR_DEFAULT_REVISION,
                    cache_dir=rtdetr_cache,
                )
                RTDetrForObjectDetection.from_pretrained(
                    RTDETR_DEFAULT_MODEL,
                    revision=RTDETR_DEFAULT_REVISION,
                    cache_dir=rtdetr_cache,
                )
                # Integrity guaranteed by HuggingFace hub blob-level SHA-256;
                # no additional file verification needed.
                self._set("rtdetr", "done")
                logger.info(
                    "setup_models: RT-DETR {} downloaded (revision {})",
                    RTDETR_DEFAULT_MODEL, RTDETR_DEFAULT_REVISION[:12],
                )
            except Exception as exc:
                logger.error("setup_models: RT-DETR download failed — {}", exc)
                self._set("rtdetr", "error", str(exc))

        # ── facenet-pytorch VGGFace2 ──────────────────────────────────────
        if present["facenet_pytorch"]:
            self._set("facenet_pytorch", "done")
            logger.info("setup_models: facenet-pytorch VGGFace2 already present, skipping download")
        else:
            self._set("facenet_pytorch", "downloading")
            try:
                import torch  # type: ignore[import]
                # Redirect torch hub to models_dir so weights stay under the
                # application data directory rather than ~/.cache/torch/hub/.
                hub_dir = str(models_dir / "facenet_pytorch")
                os.makedirs(hub_dir, exist_ok=True)
                os.environ["TORCH_HOME"] = hub_dir
                torch.hub.set_dir(hub_dir)

                from facenet_pytorch import InceptionResnetV1, MTCNN  # type: ignore[import]
                # MTCNN weights (~1 MB) are bundled in the package; instantiating
                # it on CPU just registers the model and verifies package integrity.
                MTCNN(keep_all=False, device=torch.device("cpu"))
                # InceptionResnetV1 downloads ~107 MB from GitHub release assets.
                InceptionResnetV1(pretrained="vggface2").eval()

                self._set("facenet_pytorch", "done")
                logger.info("setup_models: facenet-pytorch VGGFace2 downloaded")
            except Exception as exc:
                logger.error("setup_models: facenet-pytorch download failed — {}", exc)
                self._set("facenet_pytorch", "error", str(exc))

        with self._lock:
            self._complete = True
        logger.info("setup_models: all model downloads complete")


# ── Module-level singleton ────────────────────────────────────────────────────

_manager = SetupManager()


def get_manager() -> SetupManager:
    return _manager
