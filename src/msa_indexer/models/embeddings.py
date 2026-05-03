import time
import torch, open_clip, numpy as np
from pathlib import Path
from loguru import logger
from PIL import Image
from pillow_heif import register_heif_opener
register_heif_opener()

class ClipEmbedder:
    def __init__(self, model_name="ViT-L-14", pretrained="openai", device="cpu",
                 cache_dir: Path | None = None):
        kwargs = {}
        clip_cache: Path | None = None
        if cache_dir is not None:
            clip_cache = cache_dir / "clip"
            clip_cache.mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = str(clip_cache)

        # On Linux/Mac, open_clip writes .pt or .safetensors files directly.
        # On Windows without Developer Mode (no symlinks), huggingface_hub stores
        # weights as hash-named files with no extension inside a blobs/ subdirectory.
        # Check both layouts so the "not in cache" message isn't a false negative.
        cached = clip_cache is not None and (
            any(clip_cache.rglob("*.pt"))
            or any(clip_cache.rglob("*.safetensors"))
            or any(
                f.is_file()
                for blobs_dir in clip_cache.rglob("blobs")
                if blobs_dir.is_dir()
                for f in blobs_dir.iterdir()
            )
        )
        if cached:
            logger.info("Loading CLIP model {} ({}) from cache...", model_name, pretrained)
        else:
            logger.info(
                "CLIP model {} ({}) not in cache — downloading (this may take several minutes)...",
                model_name, pretrained,
            )

        t0 = time.perf_counter()
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, device=device, **kwargs)
        self.tokenizer = open_clip.get_tokenizer(model_name)
        self.device = device
        logger.info("CLIP model {} loaded in {:.1f}s", model_name, time.perf_counter() - t0)
    
    @property
    def dim(self): return self.model.text_projection.shape[1]

    def image_embed(self, pil_images: list[Image.Image]) -> np.ndarray:
        batch = torch.stack([self.preprocess(img) for img in pil_images]).to(self.device)
        with torch.no_grad(), torch.autocast(device_type=self.device if self.device!="cpu" else "cpu", enabled=False):
            feats = self.model.encode_image(batch)
        return feats.float().cpu().numpy()

    def text_embed(self, texts) -> np.ndarray:
        """Encode one or more texts into CLIP embeddings.

        Args:
            texts: a single string or an iterable of strings

        Returns:
            np.ndarray of shape (N, dim) where N is number of input texts.
        """
        # Accept a single string or a list/iterable of strings
        if isinstance(texts, str):
            texts = [texts]

        # Tokenize and move tokens to device. open_clip's tokenizer may return a
        # torch.Tensor or a dict-like object; handle both cases.
        tokens = self.tokenizer(texts)
        # If tokenizer returned a dict (e.g., with 'input_ids'), extract a tensor
        if isinstance(tokens, dict):
            if 'input_ids' in tokens:
                tokens = tokens['input_ids']
            else:
                # fallback: take first tensor-like value
                for v in tokens.values():
                    if isinstance(v, torch.Tensor):
                        tokens = v
                        break
        # Move tensor to the configured device
        if isinstance(tokens, torch.Tensor):
            tokens = tokens.to(self.device)

        with torch.no_grad(), torch.autocast(device_type=self.device if self.device!="cpu" else "cpu", enabled=False):
            feats = self.model.encode_text(tokens)

        return feats.float().cpu().numpy()
