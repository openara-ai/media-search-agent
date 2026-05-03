from PIL import Image, ImageOps
from pathlib import Path


def write_thumbnail(src: Path, dst_dir: Path, media_id: str, max_side=512):
    """Generate thumbnail for an image file with EXIF orientation correction.

    Thumbnail is named <media_id>.jpg (SHA256 of file content) to avoid
    filename collisions when multiple files share the same stem (e.g. DSC_1248.JPG
    appearing in several year folders).
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = dst_dir / (media_id + ".jpg")
    try:
        im_raw = Image.open(src).convert("RGB")
        # Apply EXIF orientation correction
        im = ImageOps.exif_transpose(im_raw)
        if im is None:
            im = im_raw
        im.thumbnail((max_side, max_side))
        im.save(out, quality=85)
        return out
    except Exception:
        return None


def write_video_thumbnail(video_path: Path, dst_dir: Path, media_id: str, max_side=512):
    """
    Generate thumbnail for a video file by extracting a representative frame.

    Thumbnail is named <media_id>.jpg to avoid filename collisions.

    Args:
        video_path: Path to the video file
        dst_dir: Directory to save the thumbnail
        media_id: SHA256 of file content — used as thumbnail filename
        max_side: Maximum dimension for the thumbnail

    Returns:
        Path to the generated thumbnail or None if failed
    """
    from .video import get_video_representative_frame

    dst_dir.mkdir(parents=True, exist_ok=True)
    out = dst_dir / (media_id + ".jpg")
    
    try:
        # Extract representative frame from video
        frame = get_video_representative_frame(video_path)
        if frame:
            # Resize and save as thumbnail
            frame.thumbnail((max_side, max_side))
            frame.save(out, quality=85)
            return out
        return None
    except Exception:
        return None


def write_face_thumbnail(img: Image.Image, bbox: tuple, face_id: str, dst_dir: Path, size=128, padding=0.2):
    """
    Generate thumbnail for a detected face by cropping from the source image.
    
    Args:
        img: PIL Image (source image)
        bbox: Face bounding box (x, y, w, h) normalized 0-1
        face_id: Unique face identifier (used for filename)
        dst_dir: Directory to save face thumbnails
        size: Target size for the face thumbnail (square)
        padding: Extra padding around face bbox (fraction of bbox size, e.g., 0.2 = 20%)
    
    Returns:
        Path to the generated face thumbnail or None if failed
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize face_id for filesystem (replace colons with underscores)
    safe_face_id = face_id.replace(":", "_")
    out = dst_dir / f"{safe_face_id}.jpg"
    
    try:
        # Denormalize bbox to pixel coordinates
        img_width, img_height = img.size
        x, y, w, h = bbox
        x_px = int(x * img_width)
        y_px = int(y * img_height)
        w_px = int(w * img_width)
        h_px = int(h * img_height)
        
        # Add padding around face
        pad_w = int(w_px * padding)
        pad_h = int(h_px * padding)
        x1 = max(0, x_px - pad_w)
        y1 = max(0, y_px - pad_h)
        x2 = min(img_width, x_px + w_px + pad_w)
        y2 = min(img_height, y_px + h_px + pad_h)
        
        # Crop face region
        face_crop = img.crop((x1, y1, x2, y2))
        
        # Resize to target size (square)
        face_crop.thumbnail((size, size), Image.Resampling.LANCZOS)
        
        # Save as JPEG
        face_crop.save(out, quality=90)
        return out
    except Exception as e:
        # Log error for debugging
        import sys
        print(f"Error saving face thumbnail {safe_face_id}: {e}", file=sys.stderr)
        return None
