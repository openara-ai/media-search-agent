"""
Test cases for export functionality.

Tests cover:
- Export functions (export_images_to_qdrant, export_video_frames_to_qdrant, export_faces_to_qdrant)
- run_export function
- _do_qdrant_export function
- Error handling and failure cases
- Timing statistics
- Success/failure tracking
"""
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import numpy as np
from PIL import Image


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace with test database and FAISS index."""
    tmpdir = tempfile.mkdtemp()
    index_dir = Path(tmpdir) / "index"
    index_dir.mkdir(parents=True)
    
    yield {
        "root": tmpdir,
        "index_dir": index_dir,
        "sqlite_path": index_dir / "media.sqlite",
        "faiss_path": index_dir / "image_vec.faiss",
        "face_faiss_path": index_dir / "face_vec.faiss",
    }
    
    # Cleanup
    shutil.rmtree(tmpdir)


@pytest.fixture
def mock_qdrant_client():
    """Mock QdrantClient for testing."""
    with patch('msa_indexer.db.qdrant_export.QdrantClient') as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        # Mock collection info
        mock_collection_info = MagicMock()
        mock_collection_info.points_count = 0
        mock_collection_info.vectors_config.params.size = 768
        mock_client.get_collection.return_value = mock_collection_info
        
        yield mock_client


@pytest.fixture
def sample_sqlite_db(temp_workspace):
    """Create a sample SQLite database with test data."""
    from msa_indexer.db.sqlite_store import SQLiteStore
    
    db = SQLiteStore(temp_workspace["sqlite_path"])
    db.init_schema(Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql")
    
    # Add some test media items
    test_items = [
        {
            "media_id": "test_image_1",
            "path": "/test/image1.jpg",
            "source_name": "test_source",
            "rel_path": "image1.jpg",
            "size_bytes": 1000,
            "mime": "image/jpeg",
            "ts_utc": "2025-01-01T00:00:00",
        },
        {
            "media_id": "test_image_2",
            "path": "/test/image2.jpg",
            "source_name": "test_source",
            "rel_path": "image2.jpg",
            "size_bytes": 2000,
            "mime": "image/jpeg",
            "ts_utc": "2025-01-02T00:00:00",
        },
    ]
    
    for item in test_items:
        db.upsert_media(item)
    
    db.commit()
    db.close()
    
    return temp_workspace["sqlite_path"]


@pytest.fixture
def sample_image_embeddings(sample_sqlite_db):
    """Seed the image_embedding table with vectors for the two test media rows."""
    from msa_indexer.db.sqlite_store import SQLiteStore

    dim = 768
    db = SQLiteStore(sample_sqlite_db)
    try:
        for media_id in ("test_image_1", "test_image_2"):
            v = np.random.rand(dim).astype(np.float32)
            db.upsert_image_embedding(media_id, v, model="clip-test-v1")
        db.commit()
    finally:
        db.close()
    return sample_sqlite_db


@pytest.fixture
def sample_face_embeddings(temp_workspace):
    """Seed the face_embedding table with one face vector."""
    from msa_indexer.db.sqlite_store import SQLiteStore

    db = SQLiteStore(temp_workspace["sqlite_path"])
    try:
        v = np.random.rand(512).astype(np.float32)
        db.upsert_face_embedding("test_image_1:f0", v, model="facenet-test")
        db.commit()
    finally:
        db.close()
    return temp_workspace["sqlite_path"]


class TestExportImages:
    """Test suite for export_images_to_qdrant function."""
    
    def test_export_images_success(self, temp_workspace, sample_image_embeddings, mock_qdrant_client):
        """Test successful export of images to Qdrant."""
        from msa_indexer.db.qdrant_export import export_images_to_qdrant

        with patch('msa_indexer.db.qdrant_export.S') as mock_config:
            mock_config.collections.image = "test_image_collection"
            mock_config.server.qdrant_url = "http://localhost:6333"
            mock_config.server.qdrant_api_key = None
            mock_config.qdrant_recreate_collections_on_export = False

            stats = export_images_to_qdrant(
                sample_image_embeddings,
                collection="test_image_collection",
                recreate=False,
            )

            assert stats is not None
            assert stats['image_count'] == 2
            assert stats['sent'] == 2
            assert stats['dim'] == 768
            assert stats['skipped'] == 0
            assert stats['errors'] == 0

            assert mock_qdrant_client.upsert.called

    def test_export_images_with_missing_vectors(self, temp_workspace, sample_image_embeddings, mock_qdrant_client):
        """Test export when some images don't have an entry in image_embedding."""
        from msa_indexer.db.qdrant_export import export_images_to_qdrant
        from msa_indexer.db.sqlite_store import SQLiteStore

        # Add a media item without seeding its embedding
        db = SQLiteStore(sample_image_embeddings)
        db.upsert_media({
            "media_id": "test_image_no_vector",
            "path": "/test/image3.jpg",
            "source_name": "test_source",
            "rel_path": "image3.jpg",
            "size_bytes": 3000,
            "mime": "image/jpeg",
        })
        db.commit()
        db.close()

        with patch('msa_indexer.db.qdrant_export.S') as mock_config:
            mock_config.collections.image = "test_image_collection"
            mock_config.server.qdrant_url = "http://localhost:6333"
            mock_config.server.qdrant_api_key = None
            mock_config.qdrant_recreate_collections_on_export = False

            stats = export_images_to_qdrant(
                sample_image_embeddings,
                collection="test_image_collection",
                recreate=False,
            )

            assert stats['image_count'] == 3
            assert stats['sent'] == 2
            assert stats['skipped'] == 1

    def test_export_images_recreate_collection(self, temp_workspace, sample_image_embeddings, mock_qdrant_client):
        """Test export with recreate=True deletes and recreates collection."""
        from msa_indexer.db.qdrant_export import export_images_to_qdrant

        with patch('msa_indexer.db.qdrant_export.S') as mock_config:
            mock_config.collections.image = "test_image_collection"
            mock_config.server.qdrant_url = "http://localhost:6333"
            mock_config.server.qdrant_api_key = None
            mock_config.qdrant_recreate_collections_on_export = False

            mock_qdrant_client.get_collection.return_value = MagicMock(points_count=100)

            stats = export_images_to_qdrant(
                sample_image_embeddings,
                collection="test_image_collection",
                recreate=True,
            )

            assert mock_qdrant_client.delete_collection.called


class TestExportVideoFrames:
    """Test suite for export_video_frames_to_qdrant function."""
    
    def test_export_video_frames_success(self, temp_workspace, mock_qdrant_client):
        """Test successful export of video keyframes to Qdrant."""
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_export import export_video_frames_to_qdrant
        import numpy as np

        db = SQLiteStore(temp_workspace["sqlite_path"])
        db.init_schema(Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql")

        db.upsert_media({
            "media_id": "test_video_1",
            "path": "/test/video1.mp4",
            "source_name": "test_source",
            "rel_path": "video1.mp4",
            "size_bytes": 10000,
            "mime": "video/mp4",
        })

        db.add_keyframes("test_video_1", [
            {
                "shot_index": 0,
                "kf_index": 0,
                "timestamp": 0.0,
                "shot_start": 0.0,
                "shot_end": 10.0,
                "tags": [],
                "gps_lat": 37.1,
                "gps_lon": -121.9,
                "gps_alt": 10.0,
                "gps_datetime_utc": "2026:02:27 01:17:13.200",
                "gps_fix": 3,
                "gps_source": "interpolated",
                "place": "San Jose, California, US",
            },
            {
                "shot_index": 0,
                "kf_index": 1,
                "timestamp": 5.0,
                "shot_start": 0.0,
                "shot_end": 10.0,
                "tags": ["person"],
                "gps_lat": 37.2,
                "gps_lon": -121.8,
                "gps_alt": 20.0,
                "gps_datetime_utc": "2026:02:27 01:17:18.200",
                "gps_fix": 3,
                "gps_source": "nearest",
                "place": "Santa Clara, California, US",
            },
        ])

        # Seed keyframe_embedding for both keyframes
        dim = 768
        for s_idx, k_idx in ((0, 0), (0, 1)):
            kf_id = db.get_keyframe_id("test_video_1", s_idx, k_idx)
            assert kf_id is not None
            db.upsert_keyframe_embedding(
                kf_id, np.random.rand(dim).astype(np.float32), model="clip-test-v1"
            )
        db.commit()
        db.close()

        with patch('msa_indexer.db.qdrant_export.S') as mock_config:
            mock_config.server.qdrant_url = "http://localhost:6333"
            mock_config.server.qdrant_api_key = None
            mock_config.qdrant_recreate_collections_on_export = False

            stats = export_video_frames_to_qdrant(
                temp_workspace["sqlite_path"],
                collection="test_video_collection",
                recreate=False,
            )

            assert stats is not None
            assert stats['video_keyframes_count'] == 2
            assert stats['sent'] == 2
            assert stats['dim'] == 768
            assert stats['skipped'] == 0
            assert stats['errors'] == 0
            upsert_points = mock_qdrant_client.upsert.call_args.kwargs["points"]
            assert upsert_points[0].payload["gps_lat"] == 37.1
            assert upsert_points[0].payload["gps_source"] == "interpolated"
            assert upsert_points[1].payload["place"] == "Santa Clara, California, US"


class TestExportFaces:
    """Test suite for export_faces_to_qdrant function."""
    
    def test_export_faces_success(self, temp_workspace, mock_qdrant_client):
        """Test successful export of faces to Qdrant."""
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_export import export_faces_to_qdrant
        import numpy as np

        db = SQLiteStore(temp_workspace["sqlite_path"])
        db.init_schema(Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql")

        db.upsert_media({
            "media_id": "test_image_1",
            "path": "/test/image1.jpg",
            "source_name": "test_source",
            "rel_path": "image1.jpg",
            "size_bytes": 1000,
            "mime": "image/jpeg",
        })

        db.add_faces("test_image_1", [
            {
                "face_id": "test_image_1:f0",
                "bbox": [100, 100, 200, 200],
                "confidence": 0.95,
            }
        ])
        db.upsert_face_embedding(
            "test_image_1:f0",
            np.random.rand(512).astype(np.float32),
            model="facenet-test",
        )
        db.commit()
        db.close()

        with patch('msa_indexer.db.qdrant_export.S') as mock_config:
            mock_config.server.qdrant_url = "http://localhost:6333"
            mock_config.server.qdrant_api_key = None
            mock_config.qdrant_recreate_collections_on_export = False

            stats = export_faces_to_qdrant(
                temp_workspace["sqlite_path"],
                collection="test_face_collection",
                recreate=False,
            )

            assert stats is not None
            assert stats['faces_count'] == 1
            assert stats['sent'] == 1
            assert stats['dim'] == 512
            assert stats['skipped'] == 0
            assert stats['errors'] == 0


class TestDoQdrantExport:
    """Test suite for _do_qdrant_export function."""

    def _make_config(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, extra=None):
        from types import SimpleNamespace
        ns = SimpleNamespace(
            sqlite_path=str(sample_sqlite_db),
            faiss_path=str(sample_image_embeddings),
            face_faiss_path=str(temp_workspace["face_faiss_path"]),
            export_recreate=False,
            col_video="test_video_collection",
            col_face="test_face_collection",
            reprocess_gps=False,
            reprocess_objects=False,
            reprocess_faces=False,
            reprocess_embeddings=False,
        )
        if extra:
            for k, v in extra.items():
                setattr(ns, k, v)
        return ns

    def test_export_returns_true_when_images_exported(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """_do_qdrant_export returns True when image/video collections actually
        wrote points (sent > 0)."""
        from msa_indexer import pipeline

        config = self._make_config(temp_workspace, sample_sqlite_db, sample_image_embeddings)

        with patch.object(pipeline, 'export_images_to_qdrant', return_value={'sent': 1}), \
             patch.object(pipeline, 'export_video_frames_to_qdrant', return_value={'sent': 0}):
            result = pipeline._do_qdrant_export(config, export_all=True)

        assert result is True

    def test_export_returns_false_when_nothing_exported(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """_do_qdrant_export must NOT signal a successful export when both
        helpers reported zero points sent. This guards against an upgrade-path
        bug where a pre-Stage-3 DB (no SQLite embeddings yet) would otherwise
        cause run_export to record a Qdrant export version that doesn't
        reflect the actual collection state.
        """
        from msa_indexer import pipeline

        config = self._make_config(temp_workspace, sample_sqlite_db, sample_image_embeddings)

        with patch.object(pipeline, 'export_images_to_qdrant', return_value={'sent': 0}), \
             patch.object(pipeline, 'export_video_frames_to_qdrant', return_value={'sent': 0}):
            result = pipeline._do_qdrant_export(config, export_all=True)

        assert result is False

    def test_export_returns_false_in_reprocessing_mode(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """_do_qdrant_export returns False when images are skipped due to a reprocessing flag.

        This is the IDX-001 scenario: a reprocess run must not advance the Qdrant version
        because the image/video collections were not updated.
        """
        from msa_indexer import pipeline

        config = self._make_config(
            temp_workspace, sample_sqlite_db, sample_image_embeddings,
            extra={"reprocess_gps": True},
        )

        with patch.object(pipeline, 'export_images_to_qdrant') as mock_img:
            result = pipeline._do_qdrant_export(config, export_all=False)

        assert result is False
        mock_img.assert_not_called()

    def test_export_raises_on_image_export_failure(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """Image export failures propagate so callers know not to record the version."""
        from msa_indexer import pipeline

        config = self._make_config(temp_workspace, sample_sqlite_db, sample_image_embeddings)

        with patch.object(pipeline, 'export_images_to_qdrant', side_effect=Exception("qdrant down")):
            with pytest.raises(Exception, match="qdrant down"):
                pipeline._do_qdrant_export(config, export_all=True)

    def test_face_export_failure_does_not_affect_return_value(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """Face export failures are swallowed; _do_qdrant_export still returns True
        when image/video export wrote points (version recording is safe)."""
        from msa_indexer import pipeline

        config = self._make_config(
            temp_workspace, sample_sqlite_db, sample_image_embeddings,
            extra={"reprocess_faces": False},
        )

        with patch.object(pipeline, 'export_images_to_qdrant', return_value={'sent': 2}), \
             patch.object(pipeline, 'export_video_frames_to_qdrant', return_value={'sent': 0}), \
             patch('msa_indexer.db.qdrant_export.export_faces_to_qdrant', side_effect=Exception("face fail")):
            result = pipeline._do_qdrant_export(config, export_all=True)

        assert result is True


class TestRunExport:
    """Test suite for run_export function."""

    def test_run_export_calls_record_version_on_success(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """run_export records the Qdrant export version when _do_qdrant_export returns True."""
        from msa_indexer.pipeline import run_export
        from types import SimpleNamespace

        config = SimpleNamespace(
            sqlite_path=str(sample_sqlite_db),
            faiss_path=str(sample_image_embeddings),
            face_faiss_path=str(temp_workspace["face_faiss_path"]),
            export_recreate=False,
            col_video="test_video_collection",
            col_face="test_face_collection",
        )

        with patch('msa_indexer.pipeline._do_qdrant_export', return_value=True) as mock_export, \
             patch('msa_indexer.pipeline.record_qdrant_export_version') as mock_record:
            run_export(config)

        assert mock_export.called
        assert mock_record.called, "record_qdrant_export_version must be called when export succeeded"

    def test_run_export_skips_record_version_when_images_not_exported(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """run_export must NOT record the version when _do_qdrant_export returns False.

        IDX-001: recording a false version would cause the next run to skip the export,
        leaving Qdrant with stale payloads.
        """
        from msa_indexer.pipeline import run_export
        from types import SimpleNamespace

        config = SimpleNamespace(
            sqlite_path=str(sample_sqlite_db),
            faiss_path=str(sample_image_embeddings),
            face_faiss_path=str(temp_workspace["face_faiss_path"]),
            export_recreate=False,
        )

        with patch('msa_indexer.pipeline._do_qdrant_export', return_value=False), \
             patch('msa_indexer.pipeline.record_qdrant_export_version') as mock_record:
            run_export(config)

        mock_record.assert_not_called()

    def test_run_export_success(self, temp_workspace, sample_sqlite_db, sample_image_embeddings, mock_qdrant_client):
        """run_export completes without raising on a successful export."""
        from msa_indexer.pipeline import run_export
        from types import SimpleNamespace

        config = SimpleNamespace(
            sqlite_path=str(sample_sqlite_db),
            faiss_path=str(sample_image_embeddings),
            face_faiss_path=str(temp_workspace["face_faiss_path"]),
            export_recreate=False,
            col_video="test_video_collection",
            col_face="test_face_collection",
        )

        with patch('msa_indexer.pipeline._do_qdrant_export', return_value=True), \
             patch('msa_indexer.pipeline.record_qdrant_export_version'):
            run_export(config)
    
    def test_run_export_missing_files(self, temp_workspace):
        """Test run_export with missing SQLite or FAISS files."""
        from msa_indexer.pipeline import run_export
        from types import SimpleNamespace
        
        config = SimpleNamespace(
            sqlite_path=str(temp_workspace["index_dir"] / "nonexistent.sqlite"),
            faiss_path=str(temp_workspace["index_dir"] / "nonexistent.faiss"),
            face_faiss_path=str(temp_workspace["face_faiss_path"]),
        )
        
        # Should return early without raising exception
        run_export(config)
    
    def test_run_export_missing_files_does_not_raise(self, temp_workspace):
        """run_export returns early without raising when SQLite/FAISS are missing."""
        from msa_indexer.pipeline import run_export
        from types import SimpleNamespace

        config = SimpleNamespace(
            sqlite_path=str(temp_workspace["index_dir"] / "nonexistent.sqlite"),
            faiss_path=str(temp_workspace["index_dir"] / "nonexistent.faiss"),
            face_faiss_path=str(temp_workspace["face_faiss_path"]),
        )

        run_export(config)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
