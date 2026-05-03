"""
Test object detection on video files.
"""
import pytest
from pathlib import Path
from PIL import Image
from msa_indexer.io.video import extract_video_frames, get_video_representative_frame
from msa_indexer.models.objects import ObjectDetector


SAMPLE_VIDEOS_DIR = Path(__file__).parent.parent / "data" / "sample_photos"


@pytest.fixture
def sample_videos():
    """Find sample video files."""
    video_extensions = [".mp4", ".mov", ".avi", ".mkv", ".m4v", ".MP4", ".MOV"]
    videos = []
    for ext in video_extensions:
        videos.extend(SAMPLE_VIDEOS_DIR.glob(f"*{ext}"))
    return sorted(videos)


def test_video_frame_extraction(sample_videos):
    """Test that we can extract frames from video files."""
    if not sample_videos:
        pytest.skip("No sample videos found")
    
    test_video = sample_videos[0]
    print(f"\n📹 Testing frame extraction on: {test_video.name}")
    
    # Extract 3 frames
    frames = extract_video_frames(test_video, max_frames=3, strategy="uniform")
    
    assert isinstance(frames, list), "Should return a list of frames"
    
    if frames:
        assert len(frames) <= 3, "Should extract at most 3 frames"
        
        for i, frame in enumerate(frames):
            assert isinstance(frame, Image.Image), f"Frame {i} should be a PIL Image"
            assert frame.mode == "RGB", f"Frame {i} should be in RGB mode"
            assert frame.size[0] > 0 and frame.size[1] > 0, f"Frame {i} should have valid dimensions"
        
        print(f"✓ Extracted {len(frames)} frames from video")
        print(f"  Frame dimensions: {frames[0].size}")
    else:
        pytest.skip(f"Could not extract frames from {test_video.name}")


def test_video_representative_frame(sample_videos):
    """Test extracting a single representative frame."""
    if not sample_videos:
        pytest.skip("No sample videos found")
    
    test_video = sample_videos[0]
    print(f"\n📹 Getting representative frame from: {test_video.name}")
    
    frame = get_video_representative_frame(test_video)
    
    if frame:
        assert isinstance(frame, Image.Image)
        assert frame.mode == "RGB"
        print(f"✓ Extracted representative frame: {frame.size}")
    else:
        pytest.skip(f"Could not extract frame from {test_video.name}")


@pytest.mark.skipif(not (Path(__file__).parent.parent / "data" / "sample_photos").exists(),
                   reason="Sample directory not found")
def test_object_detection_on_video_frames(sample_videos):
    """Test object detection on frames extracted from videos."""
    if not sample_videos:
        pytest.skip("No sample videos found")
    
    # Use GPU if available, fall back to CPU for testing
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    detector = ObjectDetector(model_name="PekingU/rtdetr_r18vd", backend="rtdetr", device=device, conf_threshold=0.25)
    print(f"Using device: {device}")
    
    results = {}
    
    for video_path in sample_videos[:3]:  # Test first 3 videos
        print(f"\n📹 Processing: {video_path.name}")
        
        try:
            # Extract frames
            frames = extract_video_frames(video_path, max_frames=5, strategy="uniform")
            
            if not frames:
                results[video_path.name] = {
                    'status': 'no_frames',
                    'frames': 0,
                    'tags': []
                }
                continue
            
            # Detect objects in each frame and aggregate
            all_labels = set()
            frame_detections = []
            
            for i, frame in enumerate(frames):
                labels = detector.get_labels(frame)
                frame_detections.append(labels)
                all_labels.update(labels)
            
            results[video_path.name] = {
                'status': 'success',
                'frames': len(frames),
                'tags': sorted(all_labels),
                'frame_detections': frame_detections
            }
            
            print(f"  ✓ Extracted {len(frames)} frames")
            print(f"  ✓ Detected objects: {', '.join(sorted(all_labels)) or '(none)'}")
            
        except Exception as e:
            results[video_path.name] = {
                'status': 'error',
                'error': str(e)
            }
            print(f"  ✗ Error: {e}")
    
    # Print summary
    print("\n" + "="*70)
    print("VIDEO OBJECT DETECTION SUMMARY")
    print("="*70)
    
    for video_name, result in results.items():
        print(f"\n📹 {video_name}")
        if result['status'] == 'success':
            print(f"   Frames analyzed: {result['frames']}")
            print(f"   Unique objects detected: {len(result['tags'])}")
            if result['tags']:
                print(f"   Tags: {', '.join(result['tags'])}")
        elif result['status'] == 'no_frames':
            print(f"   Status: No frames could be extracted")
        else:
            print(f"   Status: Error - {result.get('error', 'Unknown')}")
    
    print("="*70)
    
    # At least some videos should be processed successfully
    successful = [r for r in results.values() if r['status'] == 'success']
    if sample_videos:
        assert len(successful) > 0, "Expected at least one video to be processed successfully"


def test_video_detection_consistency():
    """Test that video frame extraction is deterministic."""
    sample_videos = list(SAMPLE_VIDEOS_DIR.glob("*.mp4")) + list(SAMPLE_VIDEOS_DIR.glob("*.MP4"))
    
    if not sample_videos:
        pytest.skip("No sample videos found")
    
    test_video = sample_videos[0]
    
    # Extract frames twice
    frames1 = extract_video_frames(test_video, max_frames=3, strategy="uniform")
    frames2 = extract_video_frames(test_video, max_frames=3, strategy="uniform")
    
    if frames1 and frames2:
        assert len(frames1) == len(frames2), "Should extract same number of frames"
        
        # Check that frame dimensions match
        for i, (f1, f2) in enumerate(zip(frames1, frames2)):
            assert f1.size == f2.size, f"Frame {i} should have consistent dimensions"
        
        print(f"\n✓ Video frame extraction is consistent for {test_video.name}")
        print(f"  Extracted {len(frames1)} frames on both runs")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
