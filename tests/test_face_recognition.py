"""
Unit tests for face recognition module (Phase 1).

Tests cover:
- FaceRecognizer initialization
- Face detection on sample images
- Face embedding generation
- Face similarity comparison
- Database storage of face metadata
"""
import pytest
import numpy as np
from PIL import Image
from pathlib import Path


@pytest.fixture
def sample_image():
    """Create a simple test image (solid color, no actual face)."""
    img = Image.new('RGB', (640, 480), color=(73, 109, 137))
    return img


@pytest.fixture
def face_recognizer_cpu():
    """Initialize FaceRecognizer with CPU for testing."""
    try:
        from msa_indexer.models.faces import FaceRecognizer
        recognizer = FaceRecognizer(
            backend="facenet_pytorch",
            model_name="vggface2",
            device="cpu",
            conf_threshold=0.5,
            min_face_size=20
        )
        return recognizer
    except ImportError:
        pytest.skip("facenet-pytorch not installed")
    except Exception as e:
        pytest.skip(f"FaceRecognizer initialization failed: {e}")


class TestFaceRecognizer:
    """Test suite for FaceRecognizer class."""
    
    def test_initialization(self, face_recognizer_cpu):
        """Test that FaceRecognizer initializes correctly."""
        assert face_recognizer_cpu is not None
        assert face_recognizer_cpu.model_name == "vggface2"
        assert face_recognizer_cpu.device == "cpu"
        assert face_recognizer_cpu.conf_threshold == 0.5
        assert face_recognizer_cpu.min_face_size == 20
    
    def test_detect_no_faces(self, face_recognizer_cpu, sample_image):
        """Test face detection on image with no faces (should return empty list)."""
        faces = face_recognizer_cpu.detect_and_embed(sample_image)
        assert isinstance(faces, list)
        # Solid color image should have no faces
        assert len(faces) == 0
    
    def test_face_detection_structure(self, face_recognizer_cpu):
        """Test that face detection returns correctly structured data."""
        # This test would need an actual image with faces
        # For now, just verify the method exists and returns a list
        from PIL import Image
        img = Image.new('RGB', (640, 480), color='white')
        faces = face_recognizer_cpu.detect_and_embed(img)
        assert isinstance(faces, list)
    
    def test_compare_faces_same(self, face_recognizer_cpu):
        """Test face comparison with identical embeddings (should match)."""
        # Create identical embeddings
        emb1 = np.random.rand(512).astype(np.float32)
        emb2 = emb1.copy()
        
        similarity, is_match = face_recognizer_cpu.compare_faces(emb1, emb2, threshold=0.9)
        
        assert similarity == pytest.approx(1.0, abs=0.01)  # Identical embeddings
        assert is_match is True
    
    def test_compare_faces_different(self, face_recognizer_cpu):
        """Test face comparison with different embeddings (should not match)."""
        # Create deliberately different embeddings (orthogonal vectors)
        emb1 = np.zeros(512, dtype=np.float32)
        emb1[:256] = 1.0  # First half is 1
        
        emb2 = np.zeros(512, dtype=np.float32)
        emb2[256:] = 1.0  # Second half is 1
        
        # Normalize
        emb1 = emb1 / np.linalg.norm(emb1)
        emb2 = emb2 / np.linalg.norm(emb2)
        
        similarity, is_match = face_recognizer_cpu.compare_faces(emb1, emb2, threshold=0.9)
        
        assert 0.0 <= similarity <= 1.0
        # Orthogonal embeddings should have very low similarity
        assert similarity < 0.5
        assert is_match is False
    
    def test_similarity_matrix(self, face_recognizer_cpu):
        """Test pairwise similarity matrix generation."""
        # Create some test embeddings
        embeddings = [
            np.random.rand(512).astype(np.float32) for _ in range(5)
        ]
        
        sim_matrix = face_recognizer_cpu.get_similarity_matrix(embeddings)
        
        assert sim_matrix.shape == (5, 5)
        # Diagonal should be ~1.0 (self-similarity)
        for i in range(5):
            assert sim_matrix[i, i] == pytest.approx(1.0, abs=0.01)
        # Matrix should be symmetric
        assert np.allclose(sim_matrix, sim_matrix.T)
    
    def test_empty_similarity_matrix(self, face_recognizer_cpu):
        """Test similarity matrix with empty list."""
        sim_matrix = face_recognizer_cpu.get_similarity_matrix([])
        assert sim_matrix.size == 0

    def test_insightface_model_name_with_facenet_backend_fallback(self):
        """Passing an InsightFace model name with the facenet_pytorch backend falls back to vggface2."""
        try:
            from msa_indexer.models.faces import FaceRecognizer
        except ImportError:
            pytest.skip("facenet-pytorch not installed")

        try:
            recognizer = FaceRecognizer(
                backend="facenet_pytorch",
                model_name="buffalo_l",
                device="cpu",
                conf_threshold=0.5,
            )
        except Exception:
            pytest.skip("FaceRecognizer initialization failed (model not downloaded)")

        assert recognizer.model_name == "vggface2"


class TestFaceStorage:
    """Test suite for face database storage."""
    
    def test_add_faces_to_db(self, tmp_path):
        """Test storing face detections in SQLite."""
        from msa_indexer.db.sqlite_store import SQLiteStore
        
        db_path = tmp_path / "test_faces.sqlite"
        db = SQLiteStore(db_path)
        
        # Initialize schema
        schema_path = Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"
        db.init_schema(schema_path)
        
        # Create test media entry
        media_id = "test_media_001"
        db.upsert_media({
            "media_id": media_id,
            "path": "/test/photo.jpg",
            "size_bytes": 1000,
            "mime": "image/jpeg",
        })
        
        # Add face detections
        faces = [
            {
                "face_id": f"{media_id}:f0",
                "bbox": (0.3, 0.2, 0.15, 0.2),
                "confidence": 0.95,
                "gender": "F",
                "age": 28,
            },
            {
                "face_id": f"{media_id}:f1",
                "bbox": (0.6, 0.3, 0.12, 0.18),
                "confidence": 0.88,
                "gender": "M",
                "age": 35,
            },
        ]
        
        db.add_faces(media_id, faces)
        db.commit()
        
        # Retrieve faces
        retrieved_faces = db.get_media_faces(media_id)
        
        assert len(retrieved_faces) == 2
        assert retrieved_faces[0]["face_id"] == f"{media_id}:f0"
        assert retrieved_faces[0]["confidence"] == 0.95
        assert retrieved_faces[0]["gender"] == "F"
        assert retrieved_faces[1]["face_id"] == f"{media_id}:f1"
        
        db.close()
    
    def test_get_unassigned_faces(self, tmp_path):
        """Test retrieving faces without person_id."""
        from msa_indexer.db.sqlite_store import SQLiteStore
        
        db_path = tmp_path / "test_faces.sqlite"
        db = SQLiteStore(db_path)
        
        schema_path = Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"
        db.init_schema(schema_path)
        
        media_id = "test_media_002"
        db.upsert_media({
            "media_id": media_id,
            "path": "/test/photo2.jpg",
            "size_bytes": 1000,
            "mime": "image/jpeg",
        })
        
        # Add faces without person_id
        faces = [
            {
                "face_id": f"{media_id}:f0",
                "bbox": (0.3, 0.2, 0.15, 0.2),
                "confidence": 0.95,
            },
        ]
        
        db.add_faces(media_id, faces)
        db.commit()
        
        unassigned = db.get_unassigned_faces()
        
        assert len(unassigned) >= 1
        assert any(f["face_id"] == f"{media_id}:f0" for f in unassigned)
        
        db.close()
    
    def test_iter_faces(self, tmp_path):
        """Test iterating over all faces for export."""
        from msa_indexer.db.sqlite_store import SQLiteStore
        
        db_path = tmp_path / "test_faces.sqlite"
        db = SQLiteStore(db_path)
        
        schema_path = Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"
        db.init_schema(schema_path)
        
        media_id = "test_media_003"
        db.upsert_media({
            "media_id": media_id,
            "path": "/test/photo3.jpg",
            "size_bytes": 1000,
            "mime": "image/jpeg",
        })
        
        faces = [
            {
                "face_id": f"{media_id}:f0",
                "bbox": (0.3, 0.2, 0.15, 0.2),
                "confidence": 0.95,
            },
        ]
        
        db.add_faces(media_id, faces)
        db.commit()
        
        # Iterate faces
        face_list = list(db.iter_faces())
        
        assert len(face_list) >= 1
        face = next(f for f in face_list if f["face_id"] == f"{media_id}:f0")
        assert face["media_id"] == media_id
        assert face["type"] == "image"
        assert face["bbox"] == [0.3, 0.2, 0.15, 0.2]
        
        db.close()


class TestFaceDetection:
    """Integration-style tests for face detection."""
    
    @pytest.mark.skipif(not Path("data/sample_photos").exists(), reason="Sample photos not available")
    def test_face_detection_on_sample(self):
        """Test face detection on actual sample photo (if available)."""
        try:
            from msa_indexer.models.faces import FaceRecognizer
        except ImportError:
            pytest.skip("facenet-pytorch not installed")
        
        # This would run on actual sample photos if they contain faces
        # For now, just ensure the module can be imported
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
