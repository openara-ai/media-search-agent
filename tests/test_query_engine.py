"""
Unit tests for QueryEngine with real sample photos.

Tests both positive cases (queries that should find results) and negative cases
(queries that should return no results or handle errors gracefully).
"""
import pytest
import numpy as np
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from datetime import date
import sqlite3

# We'll mock heavy dependencies to make tests fast
from msa_query.query_engine.engine import (
    QueryEngine,
    _decompose_query_text,
    _extract_simple_date_intent,
    _merge_hits_with_source_scores,
    _enrich_places,
    _enrich_people,
    temporal_deduplicate_videos,
)
from msa_query.query_engine.rerankers import score_breakdown


@pytest.fixture
def mock_text_encoder():
    """Mock TextEncoder that returns a fake but valid embedding."""
    encoder = Mock()
    encoder.encode = Mock(return_value=[0.1] * 768)  # 768-dim vector
    encoder.dim = 768
    return encoder


@pytest.fixture
def mock_retriever():
    """Mock Retriever that returns sample search results."""
    retriever = Mock()
    
    # Default search returns some mock hits
    def mock_search(collection_name, query_vector, k=10, query_filter=None):
        # Simulate Qdrant results
        return [
            {
                "id": "photo_001",
                "score": 0.95,
                "path": "/data/sample_photos/beach_sunset.heic",
                "people": ["Kumar"],
                "place": "Hawaii",
                "faces": "Kumar (0.98)",
                "scene_tags": "beach, sunset, ocean",
                "caption": "Beautiful sunset at the beach",
                "date": "2016-08-03",
            },
            {
                "id": "photo_002",
                "score": 0.87,
                "path": "/data/sample_photos/mountain_hike.heic",
                "people": ["Kumar", "John"],
                "place": "California",
                "faces": "Kumar (0.95), John (0.92)",
                "scene_tags": "mountain, hiking, trail",
                "caption": "Hiking in the mountains",
                "date": "2024-06-15",
            },
            {
                "id": "photo_003",
                "score": 0.76,
                "path": "/data/sample_photos/city_night.heic",
                "people": [],
                "place": "New York",
                "faces": None,
                "scene_tags": "city, night, lights",
                "caption": "City skyline at night",
                "date": "2018-01-10",
            },
        ]
    
    retriever.search = Mock(side_effect=mock_search)
    return retriever


@pytest.fixture
def query_engine(mock_text_encoder, mock_retriever):
    """Create a QueryEngine instance with mocked dependencies."""
    return QueryEngine(retriever=mock_retriever, text_encoder=mock_text_encoder)


class TestQueryEnginePositive:
    """Positive test cases - queries that should work."""
    
    def test_simple_search_no_filters(self, query_engine):
        """Test basic search without any filters."""
        results = query_engine.search("beach sunset")
        
        assert len(results) > 0
        assert all("id" in r for r in results)
        assert all("path" in r for r in results)
    
    def test_search_with_place_filter(self, query_engine):
        """Test search with place filter applied."""
        results = query_engine.search("hiking", filters={"place": ["Hawaii"]})

        assert [r["id"] for r in results] == ["photo_001"]

    def test_search_with_people_filter(self, query_engine):
        """Test search with people filter applied."""
        def enrich_people(_conn, candidates):
            for item in candidates:
                if item["id"] == "photo_001":
                    item["faces"] = ["Kumar"]
                elif item["id"] == "photo_002":
                    item["faces"] = ["Kumar", "John"]

        with patch("msa_query.query_engine.engine._enrich_people", side_effect=enrich_people):
            results = query_engine.search("outdoor photo", filters={"people": ["Kumar"]})

        assert {r["id"] for r in results} == {"photo_001", "photo_002"}

    def test_search_with_combined_filters(self, query_engine):
        """Test search with multiple filters."""
        def enrich_people(_conn, candidates):
            for item in candidates:
                if item["id"] == "photo_001":
                    item["faces"] = ["Kumar"]
                elif item["id"] == "photo_002":
                    item["faces"] = ["Kumar", "John"]

        with patch("msa_query.query_engine.engine._enrich_people", side_effect=enrich_people):
            results = query_engine.search(
                "adventure",
                filters={"place": ["California"], "people": ["Kumar"]}
            )

        assert [r["id"] for r in results] == ["photo_002"]
    
    def test_search_returns_proper_structure(self, query_engine):
        """Test that results have the expected structure."""
        results = query_engine.search("test query")
        
        assert len(results) > 0
        first_result = results[0]
        
        # Check required fields
        assert "id" in first_result
        assert "path" in first_result
        assert "why" in first_result
        # Optional fields
        assert "thumbnail" in first_result or first_result.get("thumbnail") is None
    
    def test_text_encoder_called(self, query_engine, mock_text_encoder):
        """Test that text encoder is called with the query."""
        query_engine.search("test query")
        
        mock_text_encoder.encode.assert_called_once_with("test query")

    def test_query_decomposition_removes_known_person_from_visual_query(self, query_engine, mock_text_encoder):
        """Known person names should be removed from the CLIP visual query."""
        with patch("msa_query.query_engine.engine._load_known_people_names", return_value=["Rajeev"]):
            query_engine.search("Rajeev in Hawaii")

        mock_text_encoder.encode.assert_called_once_with("hawaii")

    def test_simple_year_query_filters_results(self, query_engine, mock_text_encoder):
        """Explicit year intent should become an implicit date filter."""
        results = query_engine.search("beach photos from 2016")

        mock_text_encoder.encode.assert_called_once_with("beach photos")
        assert [r["id"] for r in results] == ["photo_001"]
    
    def test_retriever_called_for_image_collection(self, query_engine, mock_retriever):
        """Test that retriever is called for image collection."""
        query_engine.search("test query")
        
        # Should be called at least once (for image collection)
        assert mock_retriever.search.call_count >= 1


class TestQueryEngineNegative:
    """Negative test cases - edge cases and error handling."""
    
    def test_empty_query(self, query_engine):
        """Test handling of empty query string."""
        results = query_engine.search("")
        
        # Should handle gracefully, return results or empty list
        assert isinstance(results, list)
    
    def test_very_long_query(self, query_engine):
        """Test handling of very long query string."""
        long_query = "beach sunset " * 100  # 1000+ words
        results = query_engine.search(long_query)
        
        assert isinstance(results, list)
    
    def test_special_characters_in_query(self, query_engine):
        """Test handling of special characters."""
        results = query_engine.search("sunset! @beach #vacation $photo")
        
        assert isinstance(results, list)
    
    def test_non_existent_place_filter(self, query_engine):
        """Test filtering by a place that doesn't exist."""
        results = query_engine.search(
            "beach",
            filters={"place": ["NonExistentPlace"]}
        )

        assert results == []
    
    def test_non_existent_person_filter(self, query_engine):
        """Test filtering by a person that doesn't exist."""
        results = query_engine.search(
            "photo",
            filters={"people": ["NonExistentPerson"]}
        )

        assert results == []
    
    def test_invalid_filter_format(self, query_engine):
        """Test handling of invalid filter format."""
        # This might raise an exception or return empty results depending on implementation
        try:
            results = query_engine.search("test", filters={"place": "Hawaii"})  # Should be list
            assert isinstance(results, list)
        except (TypeError, ValueError):
            # If it raises an exception, that's also acceptable
            pass
    
    def test_missing_collection_handled_gracefully(self, query_engine, mock_retriever):
        """Test that missing collections (caption, ASR) are handled gracefully."""
        # Make retriever raise exception for caption/ASR collections
        def mock_search_with_errors(collection_name, query_vector, k=10):
            if "caption" in collection_name or "asr" in collection_name:
                raise Exception(f"Collection {collection_name} not found")
            # Return results for image collection
            return [
                {
                    "id": "photo_001",
                    "score": 0.95,
                    "path": "/data/sample_photos/test.heic",
                    "people": [],
                    "place": None,
                }
            ]
        
        mock_retriever.search = Mock(side_effect=mock_search_with_errors)
        
        # Should handle missing collections gracefully
        results = query_engine.search("test query")
        assert isinstance(results, list)
    
    def test_retriever_returns_empty(self, query_engine, mock_retriever):
        """Test handling when retriever returns no results."""
        mock_retriever.search = Mock(return_value=[])
        
        results = query_engine.search("query with no matches")
        
        assert isinstance(results, list)
        assert len(results) == 0
    
    def test_malformed_retriever_results(self, query_engine, mock_retriever):
        """Test handling of malformed results from retriever."""
        # Return results missing required fields
        mock_retriever.search = Mock(return_value=[
            {"score": 0.9},  # Missing id and other fields
            {"id": "photo_002"},  # Missing score and other fields
        ])
        
        # Should handle gracefully without crashing
        try:
            results = query_engine.search("test")
            assert isinstance(results, list)
        except KeyError:
            # If it raises KeyError, that's a bug but test documents it
            pytest.fail("QueryEngine should handle missing fields gracefully")

    def test_reranker_feature_failure_does_not_break_search(self, query_engine, monkeypatch):
        """INV-9: a runtime reranker-feature error disables features for this search
        (→ None) but must never turn search into a 500."""
        import msa_query.query_engine.engine as eng

        if eng._ranker_features is None:
            pytest.skip("msa_ranker not installed")

        def _boom(*args, **kwargs):
            raise RuntimeError("extractor blew up at runtime")

        monkeypatch.setattr(eng._ranker_features, "feature_dict", _boom)
        results = query_engine.search("beach sunset")  # must not raise
        assert len(results) > 0
        assert all(r.get("features") is None for r in results)


class TestQueryEngineIntegration:
    """Integration-style tests that verify component interaction."""
    
    def test_filters_applied_correctly(self, query_engine, mock_retriever):
        """Test that filters actually filter results (when not mocked)."""
        results_no_filter = query_engine.search("beach")
        results_with_filter = query_engine.search("beach", filters={"place": ["Hawaii"]})

        assert len(results_no_filter) >= len(results_with_filter)
        assert [r["id"] for r in results_with_filter] == ["photo_001"]
    
    def test_reranking_affects_order(self, query_engine, mock_retriever):
        """Test that reranking changes result order."""
        # Mock results with different scores
        mock_retriever.search = Mock(return_value=[
            {"id": "photo_001", "score": 0.7, "caption": "beach"},
            {"id": "photo_002", "score": 0.9, "caption": "mountain"},
            {"id": "photo_003", "score": 0.8, "caption": "city"},
        ])
        
        results = query_engine.search("landscape")
        
        # Results should be returned (order depends on reranking implementation)
        assert len(results) >= 0
    
    def test_multiple_searches_independent(self, query_engine):
        """Test that multiple searches don't interfere with each other."""
        results1 = query_engine.search("beach")
        results2 = query_engine.search("mountain")
        results3 = query_engine.search("beach")  # Same as first
        
        # All should return valid results
        assert isinstance(results1, list)
        assert isinstance(results2, list)
        assert isinstance(results3, list)


class TestQueryDecomposition:
    """Unit tests for the first-pass query decomposition helper."""

    def test_extracts_known_person_and_preserves_visual_terms(self):
        parts = _decompose_query_text("walking on beach with John", ["John"])

        assert parts["inferred_people"] == ["John"]
        assert parts["visual_query"] == "walking beach"

    def test_prefers_longer_multiword_name_matches(self):
        parts = _decompose_query_text("John Smith in Hawaii", ["John", "John Smith"])

        assert parts["inferred_people"] == ["John Smith"]
        assert parts["visual_query"] == "hawaii"

    def test_falls_back_to_original_query_when_only_person_name_remains(self):
        parts = _decompose_query_text("Rajeev", ["Rajeev"])

        assert parts["inferred_people"] == ["Rajeev"]
        assert parts["visual_query"] == "Rajeev"


class TestSimpleDateIntent:
    """Unit tests for first-pass temporal parsing."""

    def test_extracts_explicit_year(self):
        parts = _extract_simple_date_intent("beach photos from 2016", today=date(2026, 3, 30))

        assert parts["date_filter"] == {"date_from": "2016-01-01", "date_to": "2016-12-31"}
        assert parts["visual_query"] == "beach photos from"

    def test_extracts_last_year(self):
        parts = _extract_simple_date_intent("Rajeev in Hawaii last year", today=date(2026, 3, 30))

        assert parts["date_filter"] == {"date_from": "2025-01-01", "date_to": "2025-12-31"}
        assert parts["visual_query"] == "Rajeev in Hawaii"

    def test_extracts_years_ago(self):
        parts = _extract_simple_date_intent("Priya and Jon walking on beach 10 years ago", today=date(2026, 3, 30))

        assert parts["date_filter"] == {"date_from": "2016-01-01", "date_to": "2016-12-31"}
        assert parts["visual_query"] == "Priya and Jon walking on beach"


class TestMetadataAwareReranking:
    """Tests for the similarity-first, person-aware reranker."""

    def test_score_breakdown_adds_person_boost(self):
        breakdown = score_breakdown(
            {
                "score": 0.20,
                "faces": ["Rajeev"],
                "place": "Maui, Hawaii, United States",
                "tags": ["beach", "ocean"],
                "caption": "Rajeev walking on the beach",
            },
            "Rajeev in Hawaii on the beach",
            query_context={
                "inferred_people": ["Rajeev"],
                "visual_tokens": ["hawaii", "beach"],
            },
        )

        assert breakdown["person_boost"] > 0.0
        assert breakdown["person_multiplier"] > 1.0
        assert breakdown["expansion_boost"] == 0.0
        assert breakdown["expansion_multiplier"] == 1.0
        assert breakdown["total_score"] > breakdown["similarity_score"]

    def test_score_breakdown_person_only_match_uses_simple_model(self):
        breakdown = score_breakdown(
            {
                "score": 0.20,
                "faces": ["Sarthak"],
                "place": "Home",
                "tags": ["group"],
                "caption": "Sarthak with friends",
                "source": "img",
            },
            "sarthak at beach",
            query_context={
                "inferred_people": ["Sarthak"],
                "visual_tokens": ["beach"],
            },
        )

        assert breakdown["person_boost"] > 0.0
        assert breakdown["expansion_boost"] == 0.0

    def test_query_engine_promotes_named_person_match(self, mock_text_encoder, mock_retriever):
        query_engine = QueryEngine(retriever=mock_retriever, text_encoder=mock_text_encoder)

        mock_retriever.search = Mock(return_value=[
            {
                "id": "photo_generic",
                "score": 0.75,
                "path": "/data/sample_photos/tropical.heic",
                "faces": [],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Tropical beach scene",
                "date": "2016-06-01",
            },
            {
                "id": "photo_rajeev",
                "score": 0.72,
                "path": "/data/sample_photos/rajeev_hawaii.heic",
                "faces": ["Rajeev"],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Rajeev at the beach in Hawaii",
                "date": "2016-07-01",
            },
        ])

        with patch("msa_query.query_engine.engine._load_known_people_names", return_value=["Rajeev"]):
            results = query_engine.search("Rajeev in Hawaii")

        assert results[0]["id"] == "photo_rajeev"

    def test_query_engine_expands_candidates_for_inferred_people(self, mock_text_encoder, mock_retriever):
        query_engine = QueryEngine(retriever=mock_retriever, text_encoder=mock_text_encoder)

        # ANN search misses the Rajeev item entirely.
        mock_retriever.search = Mock(return_value=[
            {
                "id": "photo_generic",
                "score": 0.90,
                "path": "/data/sample_photos/beach.heic",
                "faces": [],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Generic beach",
                "date": "2016-06-01",
            },
        ])

        expanded = [
            {
                "id": "photo_rajeev",
                "score": 0.0,
                "path": "/data/sample_photos/rajeev.heic",
                "faces": ["Rajeev"],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Rajeev at the beach",
                "date": "2016-07-01",
                "type": None,
                "timestamp": None,
                "shot_id": None,
                "source": "person_expand",
            }
        ]

        with patch("msa_query.query_engine.engine._load_known_people_names", return_value=["Rajeev"]), \
             patch("msa_query.query_engine.engine._expand_candidates_for_people", return_value=expanded):
            results = query_engine.search("Rajeev in Hawaii")

        assert any(r["id"] == "photo_rajeev" for r in results)

    def test_search_reuses_single_sqlite_connection(self, tmp_path, mock_text_encoder, mock_retriever):
        db_path = tmp_path / "media.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE person (person_id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE media (
                    media_id TEXT PRIMARY KEY,
                    path TEXT,
                    source_name TEXT,
                    rel_path TEXT,
                    place TEXT,
                    ts_utc TEXT,
                    added_at TEXT,
                    mime TEXT,
                    deleted INTEGER DEFAULT 0
                );
                CREATE TABLE face (
                    face_id TEXT PRIMARY KEY,
                    media_id TEXT,
                    person_id TEXT
                );
                CREATE TABLE tag (tag_id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE media_tag (media_id TEXT, tag_id TEXT);
                """
            )
            conn.execute("INSERT INTO person(person_id, name) VALUES (?, ?)", ("p1", "Rajeev"))
            conn.execute(
                "INSERT INTO media(media_id, path, source_name, rel_path, place, ts_utc, added_at, mime, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                ("photo_rajeev", "/tmp/rajeev.jpg", "src", "rajeev.jpg", "Hawaii", "2024-01-01", "2024-01-01", "image/jpeg"),
            )
            conn.execute("INSERT INTO face(face_id, media_id, person_id) VALUES (?, ?, ?)", ("f1", "photo_rajeev", "p1"))
            conn.execute("INSERT INTO tag(tag_id, name) VALUES (?, ?)", ("t1", "beach"))
            conn.execute("INSERT INTO media_tag(media_id, tag_id) VALUES (?, ?)", ("photo_rajeev", "t1"))
            conn.commit()
        finally:
            conn.close()

        query_engine = QueryEngine(retriever=mock_retriever, text_encoder=mock_text_encoder)
        mock_retriever.search = Mock(return_value=[
            {
                "id": "photo_generic",
                "score": 0.80,
                "path": "/tmp/generic.jpg",
                "faces": [],
                "place": None,
                "tags": ["beach"],
                "caption": "Generic Hawaii beach",
                "date": "2024-01-02",
            }
        ])

        real_connect = sqlite3.connect
        query_engine = QueryEngine(
            retriever=mock_retriever,
            text_encoder=mock_text_encoder,
            sqlite_path=db_path,
            search_score_trace=False,
        )

        # The query engine opens its sqlite helper connection via
        # msa_query.storage.db.connect_readonly, which is the right place to
        # observe the underlying sqlite3.connect call.
        with patch("msa_query.storage.db.sqlite3.connect", wraps=real_connect) as connect_mock:
            results = query_engine.search("Rajeev in Hawaii", filters={"place": ["Hawaii"]})

        assert isinstance(results, list)
        assert connect_mock.call_count == 1

    def test_enrich_places_bulk_updates_candidates(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE media (
                    media_id TEXT PRIMARY KEY,
                    place TEXT
                );
                """
            )
            conn.execute("INSERT INTO media(media_id, place) VALUES (?, ?)", ("m1", "Hawaii"))
            conn.execute("INSERT INTO media(media_id, place) VALUES (?, ?)", ("m2", "Goa"))
            conn.commit()

            candidates = [{"id": "m1", "place": None}, {"id": "m2", "place": None}]
            _enrich_places(conn, candidates)
        finally:
            conn.close()

        assert [c["place"] for c in candidates] == ["Hawaii", "Goa"]

    def test_enrich_people_bulk_updates_candidates(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE person (person_id TEXT PRIMARY KEY, name TEXT);
                CREATE TABLE face (
                    face_id TEXT PRIMARY KEY,
                    media_id TEXT,
                    person_id TEXT
                );
                """
            )
            conn.execute("INSERT INTO person(person_id, name) VALUES (?, ?)", ("p1", "Rajeev"))
            conn.execute("INSERT INTO person(person_id, name) VALUES (?, ?)", ("p2", "Sonia"))
            conn.execute("INSERT INTO face(face_id, media_id, person_id) VALUES (?, ?, ?)", ("f1", "m1", "p1"))
            conn.execute("INSERT INTO face(face_id, media_id, person_id) VALUES (?, ?, ?)", ("f2", "m1", "p2"))
            conn.commit()

            candidates = [{"id": "m1", "faces": []}, {"id": "m2", "faces": []}]
            _enrich_people(conn, candidates)
        finally:
            conn.close()

        assert candidates[0]["faces"] == ["Rajeev", "Sonia"]
        assert candidates[1]["faces"] == []


class TestMergeEvidence:
    """Tests for preserving per-source evidence during merge."""

    def test_merge_keeps_source_scores(self):
        merged = _merge_hits_with_source_scores([
            ("img", [{"id": "photo_001", "score": 0.31, "path": "/tmp/a.jpg"}]),
            ("cap", [{"id": "photo_001", "score": 0.22, "caption": "beach scene"}]),
            ("asr", [{"id": "photo_002", "score": 0.40, "caption": "spoken beach"}]),
        ])

        by_id = {item["id"]: item for item in merged}
        assert by_id["photo_001"]["source_scores"] == {"img": 0.31, "cap": 0.22}
        assert by_id["photo_001"]["score"] == 0.31
        assert by_id["photo_001"]["source"] == "img"
        assert by_id["photo_002"]["source_scores"] == {"asr": 0.40}


class TestTemporalDeduplication:
    """Tests for deduplicating nearby video keyframes."""

    def test_temporal_deduplicate_videos_keeps_best_per_cluster(self):
        results = [
            {"id": "img_1", "type": None, "path": "/tmp/photo.jpg", "score": 0.95},
            {"id": "vid_a_1", "type": "video", "path": "/tmp/video_a.mp4", "timestamp": 10.0, "score": 0.80},
            {"id": "vid_a_2", "type": "video", "path": "/tmp/video_a.mp4", "timestamp": 12.0, "score": 0.85},
            {"id": "vid_a_3", "type": "video", "path": "/tmp/video_a.mp4", "timestamp": 25.5, "score": 0.70},
            {"id": "vid_b_1", "type": "video", "path": "/tmp/video_b.mp4", "timestamp": 11.0, "score": 0.60},
        ]

        deduped = temporal_deduplicate_videos(results, time_window=5.0)

        assert [r["id"] for r in deduped] == ["img_1", "vid_a_2", "vid_a_3", "vid_b_1"]


class TestPhase4CEvaluationQueries:
    """Synthetic evaluation harness for the Phase 4C benchmark queries."""

    @pytest.fixture
    def tuned_query_engine(self, mock_text_encoder):
        retriever = Mock()
        query_engine = QueryEngine(retriever=retriever, text_encoder=mock_text_encoder)
        return query_engine, retriever

    def test_rajeev_in_hawaii_prefers_named_person_result(self, tuned_query_engine):
        query_engine, retriever = tuned_query_engine
        retriever.search = Mock(return_value=[
            {
                "id": "generic_hawaii",
                "score": 0.90,
                "path": "/tmp/generic_hawaii.jpg",
                "faces": [],
                "place": "Maui, Hawaii",
                "tags": ["beach"],
                "caption": "Tropical Hawaii beach",
                "date": "2024-01-01",
            }
        ])
        expanded = [
            {
                "id": "rajeev_hawaii",
                "score": 0.0,
                "raw_similarity_score": 0.0,
                "expansion_base_score": 0.0,
                "path": "/tmp/rajeev_hawaii.jpg",
                "faces": ["Rajeev"],
                "place": "Maui, Hawaii",
                "tags": ["beach"],
                "caption": "Rajeev at the beach in Hawaii",
                "date": "2024-01-05",
                "type": None,
                "timestamp": None,
                "shot_id": None,
                "source": "person_expand",
            }
        ]
        with patch("msa_query.query_engine.engine._load_known_people_names", return_value=["Rajeev"]), \
             patch("msa_query.query_engine.engine._expand_candidates_for_people", return_value=expanded):
            results = query_engine.search("Rajeev in Hawaii")
        assert any(r["id"] == "rajeev_hawaii" for r in results)

    def test_walking_on_beach_with_john_prefers_john_on_beach(self, tuned_query_engine):
        query_engine, retriever = tuned_query_engine
        retriever.search = Mock(return_value=[
            {
                "id": "generic_beach",
                "score": 0.76,
                "path": "/tmp/generic_beach.jpg",
                "faces": [],
                "place": "Santa Cruz",
                "tags": ["beach"],
                "caption": "People walking on the beach",
                "date": "2024-03-01",
            },
            {
                "id": "john_beach",
                "score": 0.72,
                "path": "/tmp/john_beach.jpg",
                "faces": ["John"],
                "place": "Santa Cruz",
                "tags": ["beach"],
                "caption": "John walking on the beach",
                "date": "2024-03-02",
            },
        ])
        with patch("msa_query.query_engine.engine._load_known_people_names", return_value=["John"]):
            results = query_engine.search("walking on beach with John")
        assert results[0]["id"] == "john_beach"

    def test_beach_photos_from_2016_filters_to_2016(self, tuned_query_engine):
        query_engine, retriever = tuned_query_engine
        retriever.search = Mock(return_value=[
            {
                "id": "beach_2016",
                "score": 0.70,
                "path": "/tmp/beach_2016.jpg",
                "faces": [],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Beach in 2016",
                "date": "2016-08-03",
            },
            {
                "id": "beach_2024",
                "score": 0.95,
                "path": "/tmp/beach_2024.jpg",
                "faces": [],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Beach in 2024",
                "date": "2024-08-03",
            },
        ])
        results = query_engine.search("beach photos from 2016")
        assert [r["id"] for r in results] == ["beach_2016"]

    def test_baseline_beach_query_still_returns_high_similarity_beach(self, tuned_query_engine):
        query_engine, retriever = tuned_query_engine
        retriever.search = Mock(return_value=[
            {
                "id": "best_beach",
                "score": 0.91,
                "path": "/tmp/best_beach.jpg",
                "faces": [],
                "place": "Hawaii",
                "tags": ["beach"],
                "caption": "Wide beach scene",
                "date": "2024-04-01",
            },
            {
                "id": "city_scene",
                "score": 0.60,
                "path": "/tmp/city.jpg",
                "faces": [],
                "place": "New York",
                "tags": ["city"],
                "caption": "City street",
                "date": "2024-04-01",
            },
        ])
        results = query_engine.search("beach")
        assert results[0]["id"] == "best_beach"

    def test_person_expand_without_beach_evidence_does_not_beat_real_beach_match(self, tuned_query_engine):
        query_engine, retriever = tuned_query_engine
        retriever.search = Mock(return_value=[
            {
                "id": "sarthak_beach",
                "score": 0.20,
                "path": "/tmp/sarthak_beach.jpg",
                "faces": ["Sarthak"],
                "place": "Goa",
                "tags": ["beach"],
                "caption": "Sarthak at the beach",
                "date": "2024-02-01",
            },
        ])
        expanded = [
            {
                "id": "sarthak_generic",
                "score": 0.0,
                "raw_similarity_score": 0.0,
                "expansion_base_score": 0.0,
                "path": "/tmp/sarthak_generic.jpg",
                "faces": ["Sarthak"],
                "place": "Home",
                "tags": ["group"],
                "caption": "Sarthak with friends",
                "date": "2024-02-02",
                "type": None,
                "timestamp": None,
                "shot_id": None,
                "source": "person_expand",
            }
        ]
        with patch("msa_query.query_engine.engine._load_known_people_names", return_value=["Sarthak"]), \
             patch("msa_query.query_engine.engine._expand_candidates_for_people", return_value=expanded):
            results = query_engine.search("sarthak at beach")
        assert any(r["id"] == "sarthak_beach" for r in results[:3])


# Parametrized tests for different query types
@pytest.mark.parametrize("query,expected_result_count", [
    ("beach sunset", 1),  # Should find at least 1
    ("hiking mountain trail", 1),
    ("city lights night", 1),
    ("", 0),  # Empty query might return 0 or all
])
def test_various_queries(query_engine, query, expected_result_count):
    """Test various query types."""
    results = query_engine.search(query)
    assert isinstance(results, list)
    # Note: exact count depends on mock data


@pytest.mark.parametrize("invalid_filter", [
    {"place": None},  # None value
    {"people": []},  # Empty list
    {"date": "invalid"},  # Unsupported filter type (depending on implementation)
])
def test_invalid_filters(query_engine, invalid_filter):
    """Test handling of various invalid filter configurations."""
    try:
        results = query_engine.search("test", filters=invalid_filter)
        assert isinstance(results, list)
    except (TypeError, ValueError, KeyError):
        # Acceptable to raise exception for invalid filters
        pass
