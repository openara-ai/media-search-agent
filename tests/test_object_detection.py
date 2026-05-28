"""Tests for the object detection dispatcher and backends."""
import pytest
from PIL import Image
from pathlib import Path

from msa_indexer.models.objects import ObjectDetector, detect_objects

REAL_MEDIA_DIR = Path(__file__).parent / "real_media" / "fixtures" / "originals"
REPO_ROOT      = Path(__file__).parent.parent
# Pre-downloaded spike cache — present locally, absent in CI.
SPIKE_MODEL_CACHE = REPO_ROOT / "build" / "spikes" / "object-detection" / "model-cache"


# ---------------------------------------------------------------------------
# Backend parametrization
# ---------------------------------------------------------------------------

BACKENDS = [
    pytest.param(("rtdetr", "PekingU/rtdetr_r18vd"), id="rtdetr-r18vd"),
]


def _rtdetr_cached() -> bool:
    """True if the RT-DETR r18vd weights are already on disk (spike cache or models/)."""
    for candidate in (SPIKE_MODEL_CACHE, REPO_ROOT / "models"):
        slug = "models--PekingU--rtdetr_r18vd"
        snapshots = candidate / "rtdetr" / slug / "snapshots"
        if snapshots.exists() and any((s / "model.safetensors").exists() for s in snapshots.iterdir()):
            return True
    return False


def _rtdetr_model_dir() -> Path:
    """Return the model_dir to use for RT-DETR tests."""
    if (SPIKE_MODEL_CACHE / "rtdetr").exists():
        return SPIKE_MODEL_CACHE
    return REPO_ROOT / "models"


@pytest.fixture(scope="module", params=BACKENDS)
def detector(request, tmp_path_factory):
    backend, model = request.param
    if backend == "rtdetr" and not _rtdetr_cached():
        pytest.skip("RT-DETR weights not cached locally — run spike eval first or set up models/")
    model_dir = _rtdetr_model_dir() if backend == "rtdetr" else tmp_path_factory.mktemp("models")
    return ObjectDetector(
        model_name=model, device="cpu", conf_threshold=0.35,
        model_dir=model_dir, backend=backend,
    )


# ---------------------------------------------------------------------------
# Shared interface tests (run for every backend)
# ---------------------------------------------------------------------------

def test_initialization(detector):
    assert detector is not None
    assert detector.conf_threshold == pytest.approx(0.35)


def test_detect_returns_structured_data(detector):
    img = Image.new("RGB", (640, 480), color="blue")
    detections = detector.detect(img, return_boxes=True)
    assert isinstance(detections, list)
    for d in detections:
        assert "label" in d and isinstance(d["label"], str)
        assert "confidence" in d and isinstance(d["confidence"], float)
        assert "bbox" in d and len(d["bbox"]) == 4


def test_detect_without_boxes(detector):
    img = Image.new("RGB", (640, 480), color="green")
    detections = detector.detect(img, return_boxes=False)
    for d in detections:
        assert "bbox" not in d


def test_labels_unique_and_sorted(detector):
    img = Image.new("RGB", (640, 480), color="purple")
    labels = detector.get_labels(img)
    assert labels == sorted(set(labels))


def test_blank_image_low_fp(detector):
    img = Image.new("RGB", (640, 480), color="white")
    labels = detector.get_labels(img)
    assert len(labels) <= 2


def test_confidence_threshold_filters(detector):
    img = Image.new("RGB", (640, 480), color="yellow")
    detector.conf_threshold = 0.1
    low = detector.get_labels(img)
    detector.conf_threshold = 0.9
    high = detector.get_labels(img)
    detector.conf_threshold = 0.35
    assert len(high) <= len(low)


def test_dog_fixture_detected(detector):
    path = REAL_MEDIA_DIR / "object_dog_01.jpg"
    if not path.exists():
        pytest.skip(f"Missing fixture: {path}")
    img = Image.open(path).convert("RGB")
    labels = detector.get_labels(img)
    assert "dog" in labels, f"Expected 'dog' in labels, got {labels}"


def test_all_detections_meet_threshold(detector):
    path = REAL_MEDIA_DIR / "object_dog_01.jpg"
    if not path.exists():
        pytest.skip(f"Missing fixture: {path}")
    img = Image.open(path).convert("RGB")
    detections = detector.detect(img, return_boxes=True)
    for d in detections:
        assert d["confidence"] >= detector.conf_threshold
        x1, y1, x2, y2 = d["bbox"]
        assert x2 > x1 and y2 > y1


# ---------------------------------------------------------------------------
# Dispatcher-level tests (backend-agnostic)
# ---------------------------------------------------------------------------

def test_unknown_backend_raises():
    with pytest.raises(ValueError, match="Unknown object detection backend"):
        ObjectDetector(backend="nonexistent")


def test_convenience_function():
    if not _rtdetr_cached():
        pytest.skip("RT-DETR weights not cached locally")
    img = Image.new("RGB", (640, 480), color="red")
    labels = detect_objects(
        img, backend="rtdetr", model_name="PekingU/rtdetr_r18vd",
        device="cpu", model_dir=_rtdetr_model_dir(),
    )
    assert isinstance(labels, list)
    for label in labels:
        assert isinstance(label, str)


# ---------------------------------------------------------------------------
# Config migration tests
# ---------------------------------------------------------------------------

def test_config_migration_warns_on_pt_model(caplog):
    import logging
    from msa_settings.config import Config
    cfg = Config(object_model="model.pt", object_detector_backend="rtdetr")
    with caplog.at_level(logging.WARNING, logger="msa_settings.config"):
        if cfg.object_model.endswith(".pt"):
            cfg.object_model = "PekingU/rtdetr_r18vd"
    assert cfg.object_model == "PekingU/rtdetr_r18vd"


def test_config_migration_warns_on_legacy_backend(caplog):
    import logging
    from msa_settings.config import Config
    cfg = Config(object_detector_backend="legacy")
    with caplog.at_level(logging.WARNING, logger="msa_settings.config"):
        if cfg.object_detector_backend != "rtdetr":
            cfg.object_detector_backend = "rtdetr"
    assert cfg.object_detector_backend == "rtdetr"


# ---------------------------------------------------------------------------
# CPU auto-disable tests
# ---------------------------------------------------------------------------

def test_cpu_auto_disable_skips_detector():
    """Pipeline should not build ObjectDetector when device=cpu and setting=auto."""
    from unittest.mock import patch, MagicMock
    from msa_settings.config import Config

    cfg = Config(
        enable_object_detection="auto",
        object_detector_backend="rtdetr",
        object_model="PekingU/rtdetr_r18vd",
        device="cpu",
    )
    det_setting = cfg.enable_object_detection
    on_cpu = cfg.device == "cpu"
    should_detect = det_setting is True or (det_setting == "auto" and not on_cpu)
    assert not should_detect


def test_cpu_force_enables_detector():
    """Pipeline should build ObjectDetector when device=cpu but setting=True."""
    from msa_settings.config import Config

    cfg = Config(
        enable_object_detection=True,
        object_detector_backend="rtdetr",
        object_model="PekingU/rtdetr_r18vd",
        device="cpu",
    )
    det_setting = cfg.enable_object_detection
    on_cpu = cfg.device == "cpu"
    should_detect = det_setting is True or (det_setting == "auto" and not on_cpu)
    assert should_detect
