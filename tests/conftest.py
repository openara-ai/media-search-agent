"""Shared pytest fixtures for all tests.

This file provides isolated, clean fixtures for testing to ensure:
1. No production database contamination
2. Automatic cleanup via tmp_path
3. Reusable test data patterns
4. Fast test execution where possible

Usage:
    def test_something(isolated_db, sample_media_dir):
        # isolated_db is fresh SQLite in tmp_path
        # sample_media_dir points to data/sample_photos/ (read-only)
"""
import pytest
from pathlib import Path
import shutil
from msa_indexer.db.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# UI dist stub — must run at module level, before app.py is first imported.
# app.py checks `if _UI_DIST.is_dir():` at import time to decide whether to
# register the SPA catch-all route.  In CI there is no npm build, so we
# create a minimal index.html stub here so the route is always registered
# and the SPA-serving tests pass.
# ---------------------------------------------------------------------------
_UI_DIST = Path(__file__).parent.parent / "src" / "msa_apps" / "ui" / "dist"
_UI_INDEX = _UI_DIST / "index.html"
_CREATED_DIST_STUB = False

if not _UI_DIST.is_dir():
    _UI_DIST.mkdir(parents=True, exist_ok=True)
    _UI_INDEX.write_text(
        '<!doctype html><html><body><div id="root"></div></body></html>'
    )
    _CREATED_DIST_STUB = True


@pytest.fixture(scope="session", autouse=True)
def _cleanup_ui_dist_stub():
    """Remove the dist stub created above after the test session ends."""
    yield
    if _CREATED_DIST_STUB:
        _UI_INDEX.unlink(missing_ok=True)
        try:
            _UI_DIST.rmdir()  # only removes if empty
        except OSError:
            pass


# ============================================================================
# Test Data Management
# ============================================================================

@pytest.fixture(scope="session")
def sample_media_dir():
    """Path to sample_photos directory with real test content.
    
    Contains:
    - ~34 HEIC files (iOS photos with EXIF/GPS)
    - ~8 JPG files (high-res DSLR photos)
    - ~5 MP4 files (GoPro and test videos)
    - Total: ~1.8GB
    
    This is the authoritative test dataset. DO NOT modify these files in tests.
    Use read-only operations or copy to tmp_path for destructive tests.
    
    Example:
        def test_exif(sample_media_dir):
            photo = sample_media_dir / "20251005_211659690_iOS.heic"
            exif = get_exif_basic(photo)  # Read-only, safe
    """
    media_dir = Path(__file__).parent.parent / "data" / "sample_photos"
    if not media_dir.exists():
        pytest.skip(f"Sample media directory not found: {media_dir}")
    return media_dir


@pytest.fixture
def test_media_subset(sample_media_dir, tmp_path):
    """Copy a small subset of sample media to temp directory for fast tests.
    
    Copies:
    - 2 JPG files (~37MB total)
    - 1 HEIC file (~3MB)
    - 1 MP4 file (~4MB - "Test video.mp4")
    
    Total: ~44MB (vs 1.8GB full dataset)
    
    Use this for tests that need real media but want fast execution.
    Tests can modify these files safely since they're copies in tmp_path.
    
    Example:
        def test_indexer(test_media_subset, isolated_db):
            config.media_sources = [MediaSource(path=test_media_subset)]
            run_index(config)  # Can modify subset safely
    """
    subset_dir = tmp_path / "test_media"
    subset_dir.mkdir()
    
    # Copy small, representative samples
    files_to_copy = [
        "_DSC2085.JPG",      # ~18MB DSLR photo
        "_DSC2086.JPG",      # ~19MB DSLR photo
        "20251005_211659690_iOS.heic",  # ~3MB iPhone photo with GPS
        "Test video.mp4",    # ~4MB test video
    ]
    
    for filename in files_to_copy:
        src = sample_media_dir / filename
        if src.exists():
            shutil.copy2(src, subset_dir / filename)
        else:
            # If file missing, skip gracefully (don't fail fixture)
            pass
    
    return subset_dir


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture
def schema_path():
    """Path to SQLite schema file.
    
    Example:
        def test_schema(schema_path):
            assert schema_path.exists()
            content = schema_path.read_text()
            assert "CREATE TABLE media" in content
    """
    return Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"


@pytest.fixture
def isolated_db(tmp_path, schema_path):
    """Create isolated test SQLite database in temp directory.
    
    This database is:
    - Completely isolated from production (index/media.sqlite)
    - Recreated fresh for each test function
    - Automatically cleaned up after test (tmp_path)
    - Pre-initialized with schema
    - Contains minimal test data (1 media record)
    
    Use this for ALL tests that interact with SQLite.
    
    Example:
        def test_create_person(isolated_db):
            person = isolated_db.create_person("Alice")
            assert person["name"] == "Alice"
            # No production DB contamination!
    """
    db_path = tmp_path / "test.db"
    db = SQLiteStore(db_path)
    db.init_schema(schema_path)
    
    # Insert minimal test data for common scenarios
    db.conn.execute(
        "INSERT INTO media(media_id, path, mime, source_name, rel_path) VALUES(?, ?, ?, ?, ?)",
        ("test_media_001", "/test/photo1.jpg", "image/jpeg", "test_source", "photo1.jpg")
    )
    db.commit()
    
    yield db
    
    # Cleanup
    db.close()


# ============================================================================
# Configuration Fixtures
# ============================================================================

@pytest.fixture
def mock_config(tmp_path):
    """Mock config with test paths pointing to isolated temp directories.
    
    All paths use tmp_path to ensure complete isolation from production:
    - sqlite_path: tmp/test.db (not index/media.sqlite)
    - faiss_path: tmp/test.faiss (not index/image_vec.faiss)
    - face_faiss_path: tmp/face_test.faiss (not index/face_vec.faiss)
    - thumb_dir: tmp/thumbnails (not data/thumbnails)
    - face_thumb_dir: tmp/face_thumbnails (not data/face_thumbnails)
    - log_dir: tmp/logs (not logs/)
    
    Use this for any test that instantiates components requiring config.
    
    Example:
        def test_pipeline(mock_config, test_media_subset):
            mock_config.media_sources = [MediaSource(path=test_media_subset)]
            run_index(mock_config)  # All outputs go to tmp_path
    """
    from types import SimpleNamespace
    
    # Create necessary directories
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "face_thumbnails").mkdir()
    (tmp_path / "logs").mkdir()
    
    return SimpleNamespace(
        # Database paths (isolated)
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "face_test.faiss",
        
        # Output directories (isolated)
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir=tmp_path / "logs",
        
        # Runtime settings
        log_level="DEBUG",
        device="cpu",  # Force CPU for test consistency
        model_version="test-v1",
        
        # Feature flags (disabled by default for speed)
        enable_object_detection=False,
        enable_face_recognition=False,
        
        # Media sources (empty by default, tests set explicitly)
        media_sources=[],
        
        # Model settings
        keyframes_per_shot=1,
        object_confidence_threshold=0.25,
        face_confidence_threshold=0.7,
        face_min_size=20,
    )


# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "requires_qdrant: marks tests that require Qdrant service running"
    )
    config.addinivalue_line(
        "markers", "requires_gpu: marks tests that require GPU (CUDA)"
    )
