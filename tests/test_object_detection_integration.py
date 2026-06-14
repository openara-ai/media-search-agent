"""
Integration test for object detection in indexing and search pipeline.
"""
import sys
import pytest
from pathlib import Path
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from PIL import Image
import numpy as np


def _fake_transformers_module(mock_model, mock_processor):
    """Build a transformers stand-in for sys.modules.

    Patching transformers.RTDetrForObjectDetection.from_pretrained directly
    triggers the package's lazy-import machinery, which fails on CI runners
    that don't have transformers' vision extras installed (mock surfaces it
    as KeyError: 'from_pretrained'). Replacing the whole transformers module
    via sys.modules sidesteps the lazy importer entirely.
    """
    fake = MagicMock()
    fake.RTDetrForObjectDetection.from_pretrained = MagicMock(return_value=mock_model)
    fake.RTDetrImageProcessor.from_pretrained = MagicMock(return_value=mock_processor)
    return fake


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace with test images."""
    tmpdir = tempfile.mkdtemp()
    data_dir = Path(tmpdir) / "data" / "sample_photos"
    data_dir.mkdir(parents=True)
    index_dir = Path(tmpdir) / "index"
    index_dir.mkdir(parents=True)
    
    # Create a few test images with different colors
    test_images = {
        "red.jpg": (255, 0, 0),
        "green.jpg": (0, 255, 0),
        "blue.jpg": (0, 0, 255),
    }
    
    for filename, color in test_images.items():
        img = Image.new('RGB', (640, 480), color=color)
        img.save(data_dir / filename)
    
    yield {
        "root": tmpdir,
        "data_dir": data_dir,
        "index_dir": index_dir,
        "sqlite_path": index_dir / "media.sqlite",
        "faiss_path": index_dir / "image_vec.faiss",
    }
    
    # Cleanup
    shutil.rmtree(tmpdir)


def test_tags_stored_in_sqlite(temp_workspace):
    """Test that object detection tags are stored in SQLite database."""
    from msa_indexer.db.sqlite_store import SQLiteStore
    from msa_indexer.models.objects import ObjectDetector
    from msa_indexer.utils.hashes import sha256_of_file
    
    # Initialize database
    db = SQLiteStore(temp_workspace["sqlite_path"])
    db.init_schema(Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql")
    
    # Initialize object detector — stub transformers so no network/cache needed.
    mock_model = MagicMock()
    mock_model.config.id2label = {}
    mock_model.to.return_value = mock_model
    mock_model.eval.return_value = mock_model
    with patch.dict(sys.modules, {"transformers": _fake_transformers_module(mock_model, MagicMock())}):
        detector = ObjectDetector(
            model_name="PekingU/rtdetr_r18vd", device="cpu",
            conf_threshold=0.3, backend="rtdetr",
        )

    # Process one test image
    test_image_path = list(temp_workspace["data_dir"].glob("*.jpg"))[0]
    img = Image.open(test_image_path).convert('RGB')
    media_id = sha256_of_file(test_image_path)
    
    # Insert media record
    row = {
        "media_id": media_id,
        "path": str(test_image_path),
        "size_bytes": test_image_path.stat().st_size,
        "mime": "image/jpeg",
    }
    db.upsert_media(row)
    
    # Detect and add tags
    labels = detector.get_labels(img)
    if labels:
        db.add_tags(media_id, labels)
    
    db.commit()
    
    # Verify tags were stored
    items = list(db.iter_items())
    assert len(items) == 1
    assert "tags" in items[0]
    assert isinstance(items[0]["tags"], list)
    
    db.close()
    print(f"✓ Tags stored in SQLite: {items[0]['tags']}")


def test_tags_included_in_qdrant_payload(temp_workspace):
    """Test that build_payload includes tags field."""
    from msa_indexer.db.qdrant_export import build_payload
    
    test_row = {
        "id": "test123",
        "path": "/test/image.jpg",
        "people": ["Alice"],
        "place": "Beach",
        "ts": "2025-01-01",
        "tags": ["dog", "beach", "person"],
    }
    
    payload = build_payload(test_row)
    
    assert "tags" in payload
    assert payload["tags"] == ["dog", "beach", "person"]
    print(f"✓ Payload includes tags: {payload['tags']}")


def test_tag_filtering_logic():
    """Test that filter logic correctly filters by tags."""
    from msa_query.query_engine.filters import apply_filters
    
    test_items = [
        {"id": "1", "tags": ["dog", "park"], "path": "/img1.jpg"},
        {"id": "2", "tags": ["cat", "house"], "path": "/img2.jpg"},
        {"id": "3", "tags": ["dog", "beach"], "path": "/img3.jpg"},
        {"id": "4", "tags": [], "path": "/img4.jpg"},
    ]
    
    # Filter for images with "dog" tag
    filters = {"tags": ["dog"]}
    filtered = apply_filters(test_items, filters)
    
    assert len(filtered) == 2
    assert all("dog" in item["tags"] for item in filtered)
    print(f"✓ Tag filtering works: {len(filtered)} items with 'dog' tag")
    
    # Filter for images with "cat" tag
    filters = {"tags": ["cat"]}
    filtered = apply_filters(test_items, filters)
    
    assert len(filtered) == 1
    assert filtered[0]["id"] == "2"
    print(f"✓ Tag filtering works: {len(filtered)} items with 'cat' tag")
    
    # Filter for images with "beach" OR "park" tags
    filters = {"tags": ["beach", "park"]}
    filtered = apply_filters(test_items, filters)
    
    assert len(filtered) == 2
    print(f"✓ Multi-tag filtering works: {len(filtered)} items")


def test_qdrant_filter_builder():
    """Test that Qdrant filter builder creates correct filter objects."""
    from msa_query.storage.qdrant_client import QdrantStore
    
    # Test with tags
    tags = ["dog", "cat"]
    filter_obj = QdrantStore.build_tag_filter(tags)
    
    assert filter_obj is not None
    assert hasattr(filter_obj, 'must')
    print(f"✓ Qdrant filter built for tags: {tags}")
    
    # Test with empty tags
    filter_obj = QdrantStore.build_tag_filter([])
    assert filter_obj is None
    print(f"✓ Qdrant filter returns None for empty tags")


@pytest.mark.slow
def test_full_pipeline_with_object_detection(temp_workspace):
    """Test the full indexing pipeline with object detection enabled.

    Marked `slow` (kept out of the quick `-m "not slow"` gate): although the object
    detector is stubbed, `run_index` still builds the real `ViT-L-14` CLIP encoder, which
    downloads weights from HuggingFace on a cold runner → flaky HTTP 429. The fast gate
    must stay hermetic; the full lane (which runs slow tests) keeps this coverage.
    """
    from msa_indexer.pipeline import run_index
    from msa_indexer.db.sqlite_store import SQLiteStore
    from types import SimpleNamespace
    from msa_settings.config import MediaSource
    
    # Create config using media_sources instead of deprecated root
    config = SimpleNamespace(
        media_sources=[MediaSource(
            name="test_source",
            path=str(temp_workspace["data_dir"]),
            enabled=True
        )],
        sqlite_path=temp_workspace["sqlite_path"],
        faiss_path=temp_workspace["faiss_path"],
        thumb_dir=Path(temp_workspace["root"]) / "thumbnails",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="ViT-L-14/openai",
        device="cpu",
        enable_object_detection=True,
        object_detector_backend="rtdetr",
        object_model="PekingU/rtdetr_r18vd",
        object_confidence_threshold=0.35,
    )

    # Run the indexing pipeline — stub transformers so no network/cache needed.
    mock_model = MagicMock()
    mock_model.config.id2label = {}
    mock_model.to.return_value = mock_model
    mock_model.eval.return_value = mock_model
    try:
        with patch.dict(sys.modules, {"transformers": _fake_transformers_module(mock_model, MagicMock())}):
            run_index(config)

        db = SQLiteStore(temp_workspace["sqlite_path"])
        items = list(db.iter_items())
        assert len(items) == 3  # We created 3 test images

        items_with_tags = [item for item in items if item.get("tags")]
        print(f"✓ Pipeline processed {len(items)} images")
        print(f"✓ {len(items_with_tags)} images have detected tags")
        for item in items_with_tags[:2]:
            print(f"  - {Path(item['path']).name}: {item['tags']}")
        db.close()

    except Exception as e:
        pytest.fail(f"Pipeline failed: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
