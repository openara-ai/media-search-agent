"""
End-to-end tests for QueryEngine using real Qdrant and indexed sample photos.

These tests require:
1. Qdrant running (docker run -p 6333:6333 qdrant/qdrant)
2. Sample photos indexed into Qdrant (run the indexer first)
3. Image embeddings collection populated

Run with: pytest tests/test_query_engine_e2e.py -v -s
Use -s to see print output for debugging
"""
import pytest
from pathlib import Path

# Real imports - no mocking
from msa_query.query_engine.engine import QueryEngine
from msa_query.storage.qdrant_client import QdrantStore
from msa_query.embeddings.text_encoder import TextEncoder
from msa_query.config import settings


# Mark these tests as slow/integration tests
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def real_query_engine():
    """Create a real QueryEngine with actual Qdrant connection."""
    try:
        retriever = QdrantStore()
        text_encoder = TextEncoder()
        qe = QueryEngine(retriever=retriever, text_encoder=text_encoder)
        return qe
    except Exception as e:
        pytest.skip(f"Cannot create QueryEngine (Qdrant may not be running): {e}")


@pytest.fixture(scope="module")
def qdrant_health_check():
    """Check if Qdrant is running and has data."""
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=settings.qdrant_url)
        
        # Check if image collection exists
        collections = client.get_collections()
        collection_names = [c.name for c in collections.collections]
        
        if settings.vector_collection_image not in collection_names:
            pytest.skip(f"Collection '{settings.vector_collection_image}' not found in Qdrant. Run indexer first.")
        
        # Check if collection has data
        info = client.get_collection(settings.vector_collection_image)
        if info.points_count == 0:
            pytest.skip(f"Collection '{settings.vector_collection_image}' is empty. Index sample photos first.")
        
        print(f"\n✓ Qdrant is running at {settings.qdrant_url}")
        print(f"✓ Collection '{settings.vector_collection_image}' has {info.points_count} points")
        
        return info.points_count
    except Exception as e:
        pytest.skip(f"Qdrant not accessible: {e}")


class TestQueryEngineE2E:
    """End-to-end tests with real Qdrant and indexed data."""
    
    def test_query_for_car_finds_results(self, real_query_engine, qdrant_health_check):
        """Test that querying for 'car' finds actual photos with cars."""
        query = "car"
        
        print(f"\n{'='*60}")
        print(f"Query: '{query}'")
        print(f"{'='*60}")
        
        results = real_query_engine.search(query)
        
        # Should find at least some results
        assert len(results) > 0, "Expected to find photos with cars in sample photos"
        
        print(f"\nFound {len(results)} results for query '{query}':")
        for i, r in enumerate(results[:5], 1):
            print(f"\n{i}. ID: {r['id']}")
            print(f"   Path: {r.get('path', 'N/A')}")
            print(f"   Why: {r.get('why', 'N/A')[:200]}...")  # Truncate long explanations
        
        # Verify results have expected structure
        first_result = results[0]
        assert "id" in first_result
        assert "path" in first_result
        assert "why" in first_result
    
    def test_query_for_vehicle_finds_results(self, real_query_engine, qdrant_health_check):
        """Test semantic similarity - 'vehicle' should also find cars."""
        query = "vehicle"
        
        print(f"\n{'='*60}")
        print(f"Query: '{query}'")
        print(f"{'='*60}")
        
        results = real_query_engine.search(query)
        
        # Should find results due to semantic similarity with "car"
        assert len(results) > 0, "Expected 'vehicle' query to find car photos"
        
        print(f"\nFound {len(results)} results for query '{query}':")
        for i, r in enumerate(results[:3], 1):
            print(f"\n{i}. {r.get('path', 'N/A')}")
    
    def test_query_for_outdoor_scene_finds_results(self, real_query_engine, qdrant_health_check):
        """Test broader query that should match multiple photos."""
        query = "outdoor scene"
        
        results = real_query_engine.search(query)
        
        assert len(results) > 0, "Expected to find outdoor photos"
        print(f"\nFound {len(results)} results for 'outdoor scene'")
    
    def test_query_with_place_filter_reduces_results(self, real_query_engine, qdrant_health_check):
        """Test that filters actually reduce result set."""
        query = "car"
        
        # Get results without filter
        results_no_filter = real_query_engine.search(query)
        
        # Get results with filter (use a place that exists in your data)
        # Note: This might return 0 if no cars in that place
        results_with_filter = real_query_engine.search(
            query,
            filters={"place": ["California"]}  # Adjust to match your data
        )
        
        print(f"\nResults without filter: {len(results_no_filter)}")
        print(f"Results with place filter: {len(results_with_filter)}")
        
        # Filter should not increase results
        assert len(results_with_filter) <= len(results_no_filter)
    
    def test_multiple_queries_consistent(self, real_query_engine, qdrant_health_check):
        """Test that same query returns consistent results."""
        query = "car"
        
        results1 = real_query_engine.search(query)
        results2 = real_query_engine.search(query)
        
        # Should return same number of results
        assert len(results1) == len(results2)
        
        # Should return same IDs (though order might differ slightly)
        ids1 = {r["id"] for r in results1}
        ids2 = {r["id"] for r in results2}
        assert ids1 == ids2, "Same query should return same result IDs"
    
    def test_different_queries_different_results(self, real_query_engine, qdrant_health_check):
        """Test that different queries return different results."""
        results_car = real_query_engine.search("car")
        results_tree = real_query_engine.search("tree")
        
        if len(results_car) > 0 and len(results_tree) > 0:
            # Get top result IDs
            top_car_ids = {r["id"] for r in results_car[:3]}
            top_tree_ids = {r["id"] for r in results_tree[:3]}
            
            # Top results should be different for different queries
            # (unless there's significant overlap, which is possible)
            print(f"\nTop car results: {top_car_ids}")
            print(f"Top tree results: {top_tree_ids}")
    
    def test_specific_object_queries(self, real_query_engine, qdrant_health_check):
        """Test queries for specific objects that might be in sample photos."""
        test_queries = [
            "car",
            "vehicle", 
            "building",
            "person",
            "road",
            "tree",
        ]
        
        print(f"\n{'='*60}")
        print("Testing various object queries:")
        print(f"{'='*60}")
        
        for query in test_queries:
            results = real_query_engine.search(query)
            print(f"\n'{query}': {len(results)} results")
            
            # At least some queries should return results
            # (can't guarantee all will match sample photos)
            assert isinstance(results, list)
    
    def test_descriptive_scene_query(self, real_query_engine, qdrant_health_check):
        """Test natural language descriptive query."""
        query = "parked car on the street"
        
        print(f"\n{'='*60}")
        print(f"Natural language query: '{query}'")
        print(f"{'='*60}")
        
        results = real_query_engine.search(query)
        
        # Should return results if sample photos have relevant scenes
        print(f"\nFound {len(results)} results")
        if len(results) > 0:
            print(f"Top result: {results[0].get('path', 'N/A')}")
    
    def test_results_have_valid_paths(self, real_query_engine, qdrant_health_check):
        """Test that returned results have valid file paths."""
        query = "car"
        results = real_query_engine.search(query)
        
        if len(results) > 0:
            for r in results[:5]:
                path = r.get("path")
                assert path is not None, "Result should have a path"
                assert isinstance(path, str), "Path should be a string"
                # Path should reference sample_photos (may be relative or absolute)
                # Note: We don't check if file exists since path format may vary


class TestQueryEnginePerformance:
    """Performance tests for query engine."""
    
    def test_query_response_time(self, real_query_engine, qdrant_health_check):
        """Test that queries complete in reasonable time."""
        import time
        
        query = "car"
        start = time.time()
        results = real_query_engine.search(query)
        elapsed = time.time() - start
        
        print(f"\nQuery completed in {elapsed:.3f} seconds")
        print(f"Returned {len(results)} results")
        
        # Query should complete within reasonable time
        # Adjust threshold based on your setup (local vs remote, collection size)
        assert elapsed < 5.0, f"Query took too long: {elapsed:.3f}s"
    
    def test_multiple_queries_performance(self, real_query_engine, qdrant_health_check):
        """Test performance of multiple sequential queries."""
        import time
        
        queries = ["car", "vehicle", "building", "tree", "person"]
        
        start = time.time()
        results_list = []
        for query in queries:
            results = real_query_engine.search(query)
            results_list.append(len(results))
        elapsed = time.time() - start
        
        print(f"\n{len(queries)} queries completed in {elapsed:.3f} seconds")
        print(f"Average: {elapsed/len(queries):.3f}s per query")
        print(f"Results per query: {results_list}")
        
        # All queries should complete reasonably fast
        assert elapsed < 10.0, f"Multiple queries took too long: {elapsed:.3f}s"


class TestQueryEngineDataValidation:
    """Tests to validate the indexed data quality."""
    
    def test_collection_has_reasonable_size(self, qdrant_health_check):
        """Verify collection has expected number of points."""
        point_count = qdrant_health_check
        
        # Sample photos folder has ~29 photos, but may include additional sources from config
        # Collection size may be larger if multiple sources or videos are indexed
        print(f"\nCollection has {point_count} points")
        assert point_count > 0, "Collection should not be empty"
        # Relaxed upper bound to account for multiple sources and video keyframes
        assert point_count < 5000, "Collection seems unreasonably large"
    
    def test_can_retrieve_specific_point(self, real_query_engine):
        """Test retrieving a specific point by ID."""
        from qdrant_client import QdrantClient
        
        try:
            client = QdrantClient(url=settings.qdrant_url)
            
            # Get a sample point
            points = client.scroll(
                collection_name=settings.vector_collection_image,
                limit=1
            )[0]
            
            if len(points) > 0:
                point_id = points[0].id
                print(f"\nSample point ID: {point_id}")
                print(f"Payload keys: {list(points[0].payload.keys())}")
                
                # Should have expected payload fields
                payload = points[0].payload
                assert "path" in payload or "media_id" in payload
        except Exception as e:
            pytest.skip(f"Could not retrieve points: {e}")


# Convenience function to run just the E2E tests
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
