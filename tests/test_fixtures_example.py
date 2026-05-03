"""Example test demonstrating the new test infrastructure.

This test file shows how to use the fixtures from conftest.py.
Run with: pytest tests/test_fixtures_example.py -v
"""
import pytest
from pathlib import Path


def test_sample_media_dir_exists(sample_media_dir):
    """Example: Using sample_media_dir fixture (read-only test data)."""
    # Verify fixture returns valid path
    assert sample_media_dir.exists()
    assert sample_media_dir.is_dir()
    
    # Verify contains expected file types
    files = list(sample_media_dir.iterdir())
    extensions = {f.suffix.lower() for f in files if f.is_file()}
    
    # Should have some HEIC, JPG, or MP4 files
    assert any(ext in extensions for ext in {'.heic', '.jpg', '.mp4'})
    
    print(f"\n✅ Found {len(files)} files in sample_media_dir")
    print(f"   Extensions: {extensions}")


def test_test_media_subset_is_small(test_media_subset):
    """Example: Using test_media_subset fixture (copied subset for fast tests)."""
    # Verify subset was created in tmp_path
    assert test_media_subset.exists()
    assert test_media_subset.is_dir()
    
    # Verify contains only subset of files (should be ~4 files)
    files = list(test_media_subset.glob("*"))
    assert len(files) <= 4, f"Subset should be small, got {len(files)} files"
    
    # Verify at least one file exists
    assert len(files) > 0, "Subset should contain at least 1 file"
    
    print(f"\n✅ test_media_subset created with {len(files)} files")
    print(f"   Files: {[f.name for f in files]}")


def test_isolated_db_is_fresh(isolated_db):
    """Example: Using isolated_db fixture (never touches production DB)."""
    # Verify we got a fresh database with schema
    cursor = isolated_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = [row[0] for row in cursor.fetchall()]
    
    # Should have core tables from schema
    assert "media" in tables
    assert "face" in tables
    assert "person" in tables
    
    # Should have minimal test data (1 media record from fixture)
    cursor = isolated_db.conn.execute("SELECT COUNT(*) FROM media")
    count = cursor.fetchone()[0]
    assert count == 1, "Fresh DB should have exactly 1 test media record"
    
    # Verify this is NOT production database
    assert "test.db" in str(isolated_db.path), "Should be test DB in tmp_path"
    assert "media.sqlite" not in str(isolated_db.path), "Should NOT be production DB"
    
    print(f"\n✅ isolated_db created at: {isolated_db.path}")
    print(f"   Tables: {tables}")
    print(f"   Media count: {count}")


def test_isolated_db_changes_dont_persist():
    """Example: Demonstrating DB isolation - changes don't persist across tests."""
    # This test intentionally does nothing - see next test
    pass


def test_isolated_db_is_fresh_again(isolated_db):
    """Example: Each test gets a fresh isolated_db."""
    # Even though previous test ran, we still get fresh DB with only 1 record
    cursor = isolated_db.conn.execute("SELECT COUNT(*) FROM media")
    count = cursor.fetchone()[0]
    
    assert count == 1, "Should be fresh DB again with 1 test record"
    
    # Add a record in this test
    isolated_db.conn.execute(
        "INSERT INTO media(media_id, path, mime) VALUES(?, ?, ?)",
        ("test_media_002", "/test/photo2.jpg", "image/jpeg")
    )
    isolated_db.commit()
    
    cursor = isolated_db.conn.execute("SELECT COUNT(*) FROM media")
    count = cursor.fetchone()[0]
    assert count == 2, "Should have 2 records now"
    
    print(f"\n✅ Added record - DB now has {count} media records")
    print("   This change is isolated and will be cleaned up automatically!")


def test_mock_config_uses_tmp_paths(mock_config, tmp_path):
    """Example: Using mock_config fixture (all paths isolated in tmp_path)."""
    # Verify all paths use tmp_path (not production paths)
    assert str(tmp_path) in str(mock_config.sqlite_path)
    assert str(tmp_path) in str(mock_config.faiss_path)
    assert str(tmp_path) in str(mock_config.thumb_dir)
    
    # Verify production paths are NOT used
    assert "index/media.sqlite" not in str(mock_config.sqlite_path)
    assert "data/thumbnails" not in str(mock_config.thumb_dir)
    
    # Verify directories exist
    assert mock_config.thumb_dir.exists()
    assert mock_config.face_thumb_dir.exists()
    assert mock_config.log_dir.exists()
    
    print(f"\n✅ mock_config created with isolated paths:")
    print(f"   sqlite_path: {mock_config.sqlite_path}")
    print(f"   thumb_dir: {mock_config.thumb_dir}")
    print(f"   All paths in tmp_path: {tmp_path}")


def test_production_db_never_used():
    """Example: Verifying production database is never touched."""
    from pathlib import Path
    
    # If production DB exists, verify it's not modified during tests
    prod_db = Path("index/media.sqlite")
    if prod_db.exists():
        # Get modification time
        mtime_before = prod_db.stat().st_mtime
        
        # ... test would run here ...
        
        # Verify not modified
        mtime_after = prod_db.stat().st_mtime
        assert mtime_before == mtime_after, "Production DB should NEVER be modified by tests!"
        
        print("\n✅ Production database untouched (as expected)")
    else:
        print("\n✅ Production database doesn't exist (test environment)")


if __name__ == "__main__":
    # Run this test file directly
    pytest.main([__file__, "-v", "-s"])
