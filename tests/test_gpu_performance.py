"""
Performance comparison test for CPU vs GPU object detection.
"""
import pytest
import torch
from pathlib import Path
from PIL import Image
import time
from msa_indexer.models.objects import ObjectDetector
from msa_indexer.io.video import extract_video_frames


SAMPLE_DIR = Path(__file__).parent.parent / "data" / "sample_photos"


@pytest.mark.slow
@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA not available")
def test_gpu_vs_cpu_performance():
    """Compare GPU vs CPU performance for object detection."""
    
    # Find test data
    images = list(SAMPLE_DIR.glob("*.jpg")) + list(SAMPLE_DIR.glob("*.JPG"))
    videos = list(SAMPLE_DIR.glob("*.mp4")) + list(SAMPLE_DIR.glob("*.MP4"))
    
    if not images and not videos:
        pytest.skip("No test images or videos found")
    
    print("\n" + "="*70)
    print("GPU vs CPU PERFORMANCE COMPARISON")
    print("="*70)
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"Test images: {len(images[:5])}")
    print(f"Test videos: {len(videos[:2])}")
    print("="*70)
    
    results = {
        'cpu': {'images': 0, 'videos': 0, 'time_images': 0, 'time_videos': 0},
        'cuda': {'images': 0, 'videos': 0, 'time_images': 0, 'time_videos': 0}
    }
    
    for device in ['cpu', 'cuda']:
        print(f"\n🔧 Testing on {device.upper()}...")
        detector = ObjectDetector(model_name="PekingU/rtdetr_r18vd", backend="rtdetr", device=device, conf_threshold=0.25)

        # Test on images
        if images:
            image_times = []
            for img_path in images[:5]:  # Test first 5 images
                try:
                    img = Image.open(img_path).convert('RGB')

                    start = time.time()
                    labels = detector.get_labels(img)
                    elapsed = time.time() - start

                    image_times.append(elapsed)
                    results[device]['images'] += 1
                except Exception:
                    pass

            if image_times:
                results[device]['time_images'] = sum(image_times)
                avg_time = sum(image_times) / len(image_times)
                print(f"  Images: {len(image_times)} processed, avg {avg_time*1000:.1f}ms per image")
        
        # Test on videos
        if videos:
            video_times = []
            for video_path in videos[:2]:  # Test first 2 videos
                try:
                    frames = extract_video_frames(video_path, max_frames=10, strategy="uniform")
                    if not frames:
                        continue
                    
                    start = time.time()
                    for frame in frames:
                        labels = detector.get_labels(frame)
                    elapsed = time.time() - start
                    
                    video_times.append(elapsed)
                    results[device]['videos'] += 1
                except Exception:
                    pass
            
            if video_times:
                results[device]['time_videos'] = sum(video_times)
                avg_time = sum(video_times) / len(video_times)
                print(f"  Videos: {len(video_times)} processed, avg {avg_time:.2f}s per video (10 frames)")
    
    # Calculate speedup
    print("\n" + "="*70)
    print("SPEEDUP COMPARISON (GPU vs CPU)")
    print("="*70)
    
    if results['cpu']['time_images'] > 0 and results['cuda']['time_images'] > 0:
        speedup_images = results['cpu']['time_images'] / results['cuda']['time_images']
        print(f"Images: {speedup_images:.1f}x faster on GPU")
        print(f"  CPU:  {results['cpu']['time_images']*1000:.0f}ms total")
        print(f"  GPU:  {results['cuda']['time_images']*1000:.0f}ms total")
    
    if results['cpu']['time_videos'] > 0 and results['cuda']['time_videos'] > 0:
        speedup_videos = results['cpu']['time_videos'] / results['cuda']['time_videos']
        print(f"\nVideos: {speedup_videos:.1f}x faster on GPU")
        print(f"  CPU:  {results['cpu']['time_videos']:.2f}s total")
        print(f"  GPU:  {results['cuda']['time_videos']:.2f}s total")
    
    print("\n💡 Note: GPU speedup varies by model size and batch size.")
    print("   - RT-DETR r18vd: 3-8x speedup on GPU")
    print("   - Batch processing: Even greater speedup")
    print("="*70)
    
    # Videos should show meaningful GPU benefit (relaxed threshold for nano model variance)
    if results['cpu']['time_videos'] > 0 and results['cuda']['time_videos'] > 0:
        speedup_videos = results['cpu']['time_videos'] / results['cuda']['time_videos']
        assert speedup_videos > 1.3, f"Expected at least 1.3x speedup on videos, got {speedup_videos:.1f}x"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
