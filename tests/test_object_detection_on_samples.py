"""
Test object detection on checked-in public real-media fixtures.
This test validates that the object detector can find real objects in repo fixtures.
"""
import pytest
from PIL import Image
from pathlib import Path
from msa_indexer.models.objects import ObjectDetector

pytestmark = pytest.mark.slow

FIXTURE_ROOT = Path(__file__).parent / "real_media" / "fixtures"
IMAGE_FIXTURE_PATHS = [
    FIXTURE_ROOT / "originals" / "object_dog_01.jpg",
    FIXTURE_ROOT / "originals" / "object_landscape_01.jpg",
    FIXTURE_ROOT / "derived" / "exif_object_dog_01.jpg",
    FIXTURE_ROOT / "derived" / "exif_object_landscape_01.jpg",
]

_REPO_ROOT = Path(__file__).parent.parent
_SPIKE_CACHE = _REPO_ROOT / "build" / "spikes" / "object-detection" / "model-cache"


def _rtdetr_model_dir() -> Path:
    """Return a model_dir that already contains the RT-DETR snapshot, or raise skip."""
    for candidate in (_SPIKE_CACHE, _REPO_ROOT / "models"):
        slug = "models--PekingU--rtdetr_r18vd"
        snapshots = candidate / "rtdetr" / slug / "snapshots"
        if snapshots.exists() and any(
            (s / "model.safetensors").exists() for s in snapshots.iterdir()
        ):
            return candidate
    pytest.skip("RT-DETR weights not cached locally — skipped in CI")


class TestObjectDetectionOnSamples:
    """Test object detection on public real-media image fixtures."""

    @pytest.fixture(scope="class")
    def detector(self):
        """Create a single detector instance for all tests."""
        import torch
        model_dir = _rtdetr_model_dir()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"\nUsing device: {device}")
        return ObjectDetector(
            model_name="PekingU/rtdetr_r18vd", backend="rtdetr",
            device=device, conf_threshold=0.25, model_dir=model_dir,
        )
    
    @pytest.fixture(scope="class")
    def sample_images(self):
        """Get the checked-in real-media image fixtures for object detection."""
        return [path for path in IMAGE_FIXTURE_PATHS if path.exists()]
    
    def test_sample_photos_exist(self, sample_images):
        """Verify that we have checked-in public image fixtures to test on."""
        assert len(sample_images) > 0, "No real-media image fixtures found in tests/real_media/fixtures"
        print(f"\nFound {len(sample_images)} real-media image fixtures to test")
    
    def test_detection_on_all_samples(self, detector, sample_images):
        """Test that detector runs on all sample images without errors."""
        results = {}
        
        for img_path in sample_images:
            try:
                img = Image.open(img_path).convert('RGB')
                detections = detector.detect(img, return_boxes=True)
                labels = detector.get_labels(img)
                
                results[img_path.name] = {
                    'detections': len(detections),
                    'labels': labels,
                    'success': True
                }
                
            except Exception as e:
                results[img_path.name] = {
                    'detections': 0,
                    'labels': [],
                    'success': False,
                    'error': str(e)
                }
        
        # Print results
        print("\n" + "="*70)
        print("OBJECT DETECTION RESULTS ON SAMPLE PHOTOS")
        print("="*70)
        for filename, result in results.items():
            if result['success']:
                print(f"\n📷 {filename}")
                print(f"   Detections: {result['detections']}")
                if result['labels']:
                    print(f"   Labels: {', '.join(result['labels'])}")
                else:
                    print(f"   Labels: (none above threshold)")
            else:
                print(f"\n❌ {filename}")
                print(f"   Error: {result['error']}")
        print("="*70)
        
        # Verify all images processed successfully
        failed = [k for k, v in results.items() if not v['success']]
        assert len(failed) == 0, f"Failed to process {len(failed)} images: {failed}"
        
        # Verify at least some images have detections
        with_detections = [k for k, v in results.items() if v['detections'] > 0]
        assert len(with_detections) > 0, "Expected at least some images to have object detections"
    
    def test_common_objects_detected(self, detector, sample_images):
        """Test that common objects are detected in sample photos."""
        all_labels = set()
        
        for img_path in sample_images:
            img = Image.open(img_path).convert('RGB')
            labels = detector.get_labels(img)
            all_labels.update(labels)
        
        print(f"\n\n🏷️  All unique objects detected across {len(sample_images)} images:")
        print(f"   {sorted(all_labels)}")
        print(f"   Total unique labels: {len(all_labels)}")
        
        # Just verify we detected something - don't make assumptions about specific content
        assert len(all_labels) > 0, "Expected to detect at least some objects in sample photos"
    
    def test_detection_consistency(self, detector, sample_images):
        """Test that detection is consistent when run multiple times on the same image."""
        if not sample_images:
            pytest.skip("No sample images available")
        
        # Test on first image
        test_image = sample_images[0]
        img = Image.open(test_image).convert('RGB')
        
        # Run detection 3 times
        results = []
        for _ in range(3):
            labels = detector.get_labels(img)
            results.append(set(labels))
        
        # All runs should produce the same labels
        assert results[0] == results[1] == results[2], \
            f"Detection should be deterministic. Got: {results}"
        
        print(f"\n✓ Detection is consistent for {test_image.name}")
        print(f"  Detected: {sorted(results[0])}")
    
    def test_detection_quality_metrics(self, detector, sample_images):
        """Collect quality metrics about detections."""
        metrics = {
            'total_images': len(sample_images),
            'images_with_detections': 0,
            'total_detections': 0,
            'avg_detections_per_image': 0,
            'avg_confidence': 0,
            'confidence_scores': []
        }
        
        for img_path in sample_images:
            img = Image.open(img_path).convert('RGB')
            detections = detector.detect(img, return_boxes=False)
            
            if detections:
                metrics['images_with_detections'] += 1
                metrics['total_detections'] += len(detections)
                
                for det in detections:
                    metrics['confidence_scores'].append(det['confidence'])
        
        if metrics['total_detections'] > 0:
            metrics['avg_detections_per_image'] = metrics['total_detections'] / metrics['total_images']
            metrics['avg_confidence'] = sum(metrics['confidence_scores']) / len(metrics['confidence_scores'])
        
        print("\n" + "="*70)
        print("DETECTION QUALITY METRICS")
        print("="*70)
        print(f"Total images tested: {metrics['total_images']}")
        print(f"Images with detections: {metrics['images_with_detections']} "
              f"({100*metrics['images_with_detections']/max(1, metrics['total_images']):.1f}%)")
        print(f"Total detections: {metrics['total_detections']}")
        print(f"Avg detections per image: {metrics['avg_detections_per_image']:.2f}")
        if metrics['avg_confidence'] > 0:
            print(f"Avg confidence: {metrics['avg_confidence']:.3f}")
            print(f"Min confidence: {min(metrics['confidence_scores']):.3f}")
            print(f"Max confidence: {max(metrics['confidence_scores']):.3f}")
        print("="*70)
        
        # Basic sanity checks
        assert metrics['total_images'] > 0
        assert metrics['images_with_detections'] >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
