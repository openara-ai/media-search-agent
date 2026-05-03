"""
Tests for Phase 2F Item 9 — model download feedback.

Each loader must:
  - Log "not in cache — downloading" on first run (model absent)
  - Log "from cache" when model files are already present
  - Log elapsed load time
"""
import sys
import pytest
from unittest.mock import patch, MagicMock
from loguru import logger


@pytest.fixture
def log_capture():
    """Capture loguru messages into a list for the duration of the test."""
    messages = []
    handler_id = logger.add(lambda msg: messages.append(msg), level="INFO", format="{message}")
    yield messages
    logger.remove(handler_id)


# ---------------------------------------------------------------------------
# ClipEmbedder
# ---------------------------------------------------------------------------

class TestClipEmbedderFeedback:
    def _make_embedder(self, cache_dir, model_files, log_capture):


        clip_cache = cache_dir / "clip"
        clip_cache.mkdir(parents=True, exist_ok=True)
        for fname in model_files:
            (clip_cache / fname).write_text("fake")

        mock_model = MagicMock()
        mock_model.text_projection.shape = (None, 768)

        with patch("open_clip.create_model_and_transforms",
                   return_value=(mock_model, None, MagicMock())), \
             patch("open_clip.get_tokenizer", return_value=MagicMock()):
            from msa_indexer.models.embeddings import ClipEmbedder
            return ClipEmbedder(cache_dir=cache_dir)

    def test_cache_miss_logs_downloading(self, tmp_path, log_capture):
        self._make_embedder(tmp_path, model_files=[], log_capture=log_capture)
        assert any("downloading" in m.lower() for m in log_capture), \
            f"Expected 'downloading'; got: {log_capture}"

    def test_cache_hit_logs_from_cache(self, tmp_path, log_capture):
        self._make_embedder(tmp_path, model_files=["model.pt"], log_capture=log_capture)
        assert any("cache" in m.lower() for m in log_capture), \
            f"Expected 'cache'; got: {log_capture}"

    def test_elapsed_time_logged(self, tmp_path, log_capture):
        self._make_embedder(tmp_path, model_files=[], log_capture=log_capture)
        assert any("loaded in" in m.lower() for m in log_capture), \
            f"Expected 'loaded in' timing; got: {log_capture}"


# ---------------------------------------------------------------------------
# ObjectDetector
# ---------------------------------------------------------------------------

class TestObjectDetectorFeedback:
    _REVISION = "ac77a11ff0170a41b771c03264987f8ce2b0d753"
    _MODEL_NAME = "PekingU/rtdetr_r18vd"

    def _make_fake_snapshot(self, model_dir) -> None:
        """Create a minimal HF snapshot so _is_cached() returns True."""
        from pathlib import Path
        snap = (
            Path(model_dir) / "rtdetr"
            / "models--PekingU--rtdetr_r18vd"
            / "snapshots" / self._REVISION
        )
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "model.safetensors").write_text("fake")
        (snap / "config.json").write_text("{}")
        (snap / "preprocessor_config.json").write_text("{}")

    def _make_detector(self, model_dir, cached, log_capture):
        from pathlib import Path
        Path(model_dir).mkdir(parents=True, exist_ok=True)
        if cached:
            self._make_fake_snapshot(model_dir)

        mock_model = MagicMock()
        mock_model.config.id2label = {}
        mock_model.to.return_value = mock_model
        mock_model.eval.return_value = mock_model
        mock_processor = MagicMock()

        # Patch the transformers module wholesale via sys.modules. Reaching
        # into transformers.RTDetrForObjectDetection.from_pretrained directly
        # triggers the package's lazy-import machinery, which fails on CI
        # runners that don't have transformers' vision extras (torchvision,
        # etc.) installed — the lazy importer raises ImportError and mock
        # surfaces it as KeyError: 'from_pretrained'. Same pattern as the
        # facenet_pytorch stub below.
        fake_transformers = MagicMock()
        fake_transformers.RTDetrForObjectDetection.from_pretrained = MagicMock(return_value=mock_model)
        fake_transformers.RTDetrImageProcessor.from_pretrained = MagicMock(return_value=mock_processor)

        with patch.dict(sys.modules, {"transformers": fake_transformers}):
            from msa_indexer.models.objects import ObjectDetector
            return ObjectDetector(
                model_name=self._MODEL_NAME,
                model_dir=model_dir,
                backend="rtdetr",
                device="cpu",
            )

    def test_cache_miss_logs_downloading(self, tmp_path, log_capture):
        self._make_detector(tmp_path / "models", cached=False, log_capture=log_capture)
        assert any("downloading" in m.lower() for m in log_capture), \
            f"Expected 'downloading'; got: {log_capture}"

    def test_cache_hit_logs_from_cache(self, tmp_path, log_capture):
        self._make_detector(tmp_path / "models", cached=True, log_capture=log_capture)
        assert any("cache" in m.lower() for m in log_capture), \
            f"Expected 'cache'; got: {log_capture}"

    def test_elapsed_time_logged(self, tmp_path, log_capture):
        self._make_detector(tmp_path / "models", cached=False, log_capture=log_capture)
        assert any("loaded in" in m.lower() for m in log_capture), \
            f"Expected 'loaded in' timing; got: {log_capture}"


# ---------------------------------------------------------------------------
# FaceRecognizer
# ---------------------------------------------------------------------------

class TestFaceRecognizerFeedback:
    """Verify FacenetPytorchBackend emits the expected log messages."""

    def _make_recognizer(self, model_root, model_exists, log_capture):
        from pathlib import Path
        model_root = Path(model_root)
        if model_exists:
            checkpoints = model_root / "facenet_pytorch" / "checkpoints"
            checkpoints.mkdir(parents=True, exist_ok=True)
            (checkpoints / "vggface2-fake.pt").write_text("fake")

        # Patch facenet_pytorch so no real network access or model load occurs.
        fake_resnet = MagicMock()
        fake_resnet.eval.return_value = fake_resnet
        fake_resnet.to.return_value = fake_resnet

        fake_fp = MagicMock()
        fake_fp.MTCNN = MagicMock(return_value=MagicMock())
        fake_fp.InceptionResnetV1 = MagicMock(return_value=fake_resnet)

        with patch.dict(sys.modules, {"facenet_pytorch": fake_fp}):
            sys.modules.pop("msa_indexer.models.faces", None)
            sys.modules.pop("msa_indexer.models._face_backends._facenet_pytorch", None)
            from msa_indexer.models.faces import FaceRecognizer
            result = FaceRecognizer(
                backend="facenet_pytorch",
                model_name="vggface2",
                device="cpu",
                model_root=model_root,
            )
        return result

    def test_cache_miss_logs_downloading(self, tmp_path, log_capture):
        self._make_recognizer(tmp_path / "models", model_exists=False, log_capture=log_capture)
        assert any("downloading" in m.lower() for m in log_capture), \
            f"Expected 'downloading'; got: {log_capture}"

    def test_cache_hit_logs_from_cache(self, tmp_path, log_capture):
        self._make_recognizer(tmp_path / "models", model_exists=True, log_capture=log_capture)
        assert any("cache" in m.lower() for m in log_capture), \
            f"Expected 'cache'; got: {log_capture}"

    def test_elapsed_time_logged(self, tmp_path, log_capture):
        self._make_recognizer(tmp_path / "models", model_exists=False, log_capture=log_capture)
        assert any("loaded in" in m.lower() for m in log_capture), \
            f"Expected 'loaded in' timing; got: {log_capture}"
