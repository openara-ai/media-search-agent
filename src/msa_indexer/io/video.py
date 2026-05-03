from pathlib import Path
from datetime import datetime
import bisect
from PIL import Image
import cv2
import numpy as np
from loguru import logger
from typing import List, Optional, Tuple, Dict, Any
import json
import re
import shutil
import subprocess

try:
    from pymediainfo import MediaInfo
except ImportError:
    MediaInfo = None
    logger.warning("pymediainfo is not installed. Video metadata parsing will be skipped.")


ISO6709_RE = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?/)?")
DOC_KEY_RE = re.compile(r"^(Doc\d+(?:-\d+)?):(.*)$")


def should_extract_video_gps_track(path: str | Path) -> bool:
    """Return True for MP4 names likely to carry GoPro GPMF GPS telemetry."""
    p = Path(path)
    if p.suffix.lower() != ".mp4":
        return False
    stem = p.stem.lower()
    return stem.startswith(("gx", "gh", "gopr")) or "gopro" in stem


def _doc_sort_key(doc_id: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", doc_id)]


def _parse_gps_datetime(text: str | None) -> Optional[datetime]:
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S.%f", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(str(text), fmt)
        except ValueError:
            continue
    return None


def _format_gps_datetime_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat(timespec="milliseconds") + "Z"


def _probe_embedded_gps_via_exiftool(path: str | Path) -> bool:
    exe = shutil.which("exiftool")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [
                exe,
                "-j",
                "-MetaFormat",
                "-HandlerDescription",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        payload = json.loads(proc.stdout)
        if not isinstance(payload, list) or not payload:
            return False
        record = payload[0]
        meta_format = str(record.get("MetaFormat") or "").lower()
        handler_description = str(record.get("HandlerDescription") or "").lower()
        return meta_format == "gpmd" or "gopro met" in handler_description
    except Exception:
        return False


def likely_has_embedded_gps_track(path: str | Path) -> bool:
    """Compatibility probe for videos likely to carry timed telemetry."""
    return _probe_embedded_gps_via_exiftool(path)


def _extract_embedded_gps_records(path: str | Path) -> list[dict[str, Any]]:
    exe = shutil.which("exiftool")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [
                exe,
                "-ee3",
                "-api", "RequestAll=3",
                "-n",
                "-G3",
                "-j",
                "-GPSDateTime",
                "-GPSLatitude",
                "-GPSLongitude",
                "-GPSAltitude",
                "-GPSMeasureMode",
                "-GPSStatus",
                "-SampleTime",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def extract_video_gps_track(path: str | Path) -> list[dict[str, Any]]:
    """Extract a timed GPS track and normalize it into ordered GPS samples."""
    payload = _extract_embedded_gps_records(path)
    if not payload:
        return []

    flat = payload[0]
    docs: dict[str, dict[str, Any]] = {}
    for key, value in flat.items():
        match = DOC_KEY_RE.match(str(key))
        if not match:
            continue
        doc_id, field = match.groups()
        docs.setdefault(doc_id, {})[field] = value

    if not docs:
        return []

    samples: list[dict[str, Any]] = []
    first_dt: Optional[datetime] = None
    for doc_id in sorted(docs.keys(), key=_doc_sort_key):
        rec = docs[doc_id]
        lat = rec.get("GPSLatitude")
        lon = rec.get("GPSLongitude")
        if lat is None or lon is None:
            continue

        dt_text = rec.get("GPSDateTime")
        dt = _parse_gps_datetime(str(dt_text)) if dt_text is not None else None
        if dt is not None and first_dt is None:
            first_dt = dt

        if dt is not None and first_dt is not None:
            t_sec = (dt - first_dt).total_seconds()
        else:
            try:
                t_sec = float(rec.get("SampleTime") or 0.0)
            except Exception:
                t_sec = 0.0

        alt = rec.get("GPSAltitude")
        fix = rec.get("GPSMeasureMode")
        try:
            fix = int(fix) if fix is not None else None
        except Exception:
            fix = None
        samples.append({
            "sample_id": doc_id,
            "t_offset_sec": float(t_sec),
            "gps_datetime_utc": _format_gps_datetime_utc(dt),
            "gps_lat": float(lat),
            "gps_lon": float(lon),
            "gps_alt": float(alt) if alt is not None else None,
            "gps_fix": fix,
            "gps_source": "exiftool-ee3",
        })

    samples.sort(key=lambda s: float(s["t_offset_sec"]))
    return samples


def sample_video_gps_at_timestamp(
    samples: list[dict[str, Any]],
    target_t: float,
    *,
    nearest_max_gap_sec: float = 1.0,
    interpolate_max_gap_sec: float = 2.0,
) -> Optional[dict[str, Any]]:
    """Return representative GPS for a keyframe timestamp using interpolation."""
    if not samples:
        return None
    times = [float(sample["t_offset_sec"]) for sample in samples]
    idx = bisect.bisect_left(times, target_t)
    if idx == 0:
        first = samples[0]
        if abs(float(first["t_offset_sec"]) - target_t) > nearest_max_gap_sec:
            return None
        return {
            "gps_lat": first["gps_lat"],
            "gps_lon": first["gps_lon"],
            "gps_alt": first.get("gps_alt"),
            "gps_datetime_utc": first.get("gps_datetime_utc"),
            "gps_fix": first.get("gps_fix"),
            "gps_source": "nearest",
        }
    if idx >= len(samples):
        last = samples[-1]
        if abs(target_t - float(last["t_offset_sec"])) > nearest_max_gap_sec:
            return None
        return {
            "gps_lat": last["gps_lat"],
            "gps_lon": last["gps_lon"],
            "gps_alt": last.get("gps_alt"),
            "gps_datetime_utc": last.get("gps_datetime_utc"),
            "gps_fix": last.get("gps_fix"),
            "gps_source": "nearest",
        }

    prev_sample = samples[idx - 1]
    next_sample = samples[idx]
    t0 = float(prev_sample["t_offset_sec"])
    t1 = float(next_sample["t_offset_sec"])
    if t1 <= t0:
        if abs(target_t - t0) > nearest_max_gap_sec:
            return None
        return {
            "gps_lat": prev_sample["gps_lat"],
            "gps_lon": prev_sample["gps_lon"],
            "gps_alt": prev_sample.get("gps_alt"),
            "gps_datetime_utc": prev_sample.get("gps_datetime_utc"),
            "gps_fix": prev_sample.get("gps_fix"),
            "gps_source": "nearest",
        }

    if (t1 - t0) > interpolate_max_gap_sec:
        nearest_sample = prev_sample if abs(target_t - t0) <= abs(t1 - target_t) else next_sample
        nearest_t = float(nearest_sample["t_offset_sec"])
        if abs(nearest_t - target_t) > nearest_max_gap_sec:
            return None
        return {
            "gps_lat": nearest_sample["gps_lat"],
            "gps_lon": nearest_sample["gps_lon"],
            "gps_alt": nearest_sample.get("gps_alt"),
            "gps_datetime_utc": nearest_sample.get("gps_datetime_utc"),
            "gps_fix": nearest_sample.get("gps_fix"),
            "gps_source": "nearest",
        }

    frac = (target_t - t0) / (t1 - t0)
    alt = None
    if prev_sample.get("gps_alt") is not None and next_sample.get("gps_alt") is not None:
        alt = float(prev_sample["gps_alt"]) + frac * (float(next_sample["gps_alt"]) - float(prev_sample["gps_alt"]))
    return {
        "gps_lat": float(prev_sample["gps_lat"]) + frac * (float(next_sample["gps_lat"]) - float(prev_sample["gps_lat"])),
        "gps_lon": float(prev_sample["gps_lon"]) + frac * (float(next_sample["gps_lon"]) - float(prev_sample["gps_lon"])),
        "gps_alt": alt,
        "gps_datetime_utc": prev_sample.get("gps_datetime_utc") if frac < 0.5 else next_sample.get("gps_datetime_utc"),
        "gps_fix": prev_sample.get("gps_fix") if prev_sample.get("gps_fix") == next_sample.get("gps_fix") else None,
        "gps_source": "interpolated",
    }

def _parse_iso6709(s: str) -> Optional[Tuple[float, float]]:
    """Parse QuickTime ISO6709 location string into (lat, lon).
    Examples: "+37.3317-122.0307+000.00/" -> (37.3317, -122.0307)
    """
    if not s:
        return None
    m = ISO6709_RE.search(s.strip())
    if not m:
        return None
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
        return (lat, lon)
    except Exception:
        return None

def _scan_track_for_location(track: Any) -> Optional[Tuple[float, float]]:
    """Scan a MediaInfo track dict for any field containing ISO6709/QuickTime location."""
    try:
        data: Dict[str, Any] = track.to_data() if hasattr(track, 'to_data') else {}
    except Exception:
        data = {}
    # Search all string values for the ISO6709 pattern or 'com.apple.quicktime.location' content
    def _iter_vals(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from _iter_vals(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _iter_vals(v)
        else:
            yield obj
    for v in _iter_vals(data):
        if isinstance(v, str):
            if 'ISO6709' in v or 'com.apple.quicktime.location' in v or '©xyz' in v or ('+' in v and '/' in v):
                parsed = _parse_iso6709(v)
                if parsed:
                    return parsed
    # Some builds expose a dedicated attribute: "com_apple_quicktime_location_iso6709"
    for attr in dir(track):
        if 'iso6709' in attr.lower() or 'location' in attr.lower() or 'xyz' in attr.lower():
            try:
                val = getattr(track, attr)
                if isinstance(val, str):
                    parsed = _parse_iso6709(val)
                    if parsed:
                        return parsed
            except Exception:
                pass
    return None

def get_video_meta(path, *, allow_exiftool_gps: bool = True):
    duration = None
    width = None
    height = None
    gps_lat = None
    gps_lon = None

    if MediaInfo is not None:
        try:
            mi = MediaInfo.parse(path)
        except Exception as e:
            logger.warning(f"MediaInfo parse failed for {path}: {e}")
        else:
            for t in mi.tracks:
                try:
                    if t.track_type == "Video":
                        duration = t.duration/1000 if getattr(t, 'duration', None) else duration
                        width = getattr(t, 'width', width)
                        height = getattr(t, 'height', height)
                except Exception:
                    pass
                # Look for location data on any track (often on the General track for iPhone MOV)
                if gps_lat is None or gps_lon is None:
                    loc = _scan_track_for_location(t)
                    if loc:
                        gps_lat, gps_lon = loc

    # OpenCV is a useful fallback for lightweight fixtures where MediaInfo
    # may omit duration/dimensions but frame decoding still works.
    if duration is None or width is None or height is None:
        try:
            cap = cv2.VideoCapture(str(path))
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if duration is None and fps and fps > 0 and frame_count and frame_count > 0:
                    duration = float(frame_count / fps)
                if width is None:
                    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    width = frame_width or width
                if height is None:
                    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    height = frame_height or height
            cap.release()
        except Exception as e:
            logger.debug(f"OpenCV metadata fallback failed for {path}: {e}")
    # Fallback: try exiftool for cheap static/file-level GPS. Timed track
    # extraction is gated separately to avoid scanning every video with -ee3.
    if allow_exiftool_gps and (gps_lat is None or gps_lon is None):
        try:
            latlon = _extract_gps_with_exiftool(path)
            if latlon:
                gps_lat, gps_lon = latlon
        except Exception:
            pass

    meta = {"duration": duration, "width": width, "height": height}
    if gps_lat is not None and gps_lon is not None:
        meta["gps_lat"] = gps_lat
        meta["gps_lon"] = gps_lon
    return meta


def _extract_gps_with_exiftool(path: str | Path) -> Optional[Tuple[float, float]]:
    """Use exiftool (if available) to extract GPS from videos (e.g., GoPro GPMF).

    Returns (lat, lon) or None if not available.
    """
    exe = shutil.which("exiftool")
    if not exe:
        return None
    try:
        # -n returns numeric values; use -S -s to simplify output (no descriptions)
        proc = subprocess.run(
            [exe, "-n", "-S", "-s", "-GPSLatitude", "-GPSLongitude", str(path)],
            capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            return None
        lat = None
        lon = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("GPSLatitude:"):
                try:
                    lat = float(line.split(":",1)[1].strip())
                except Exception:
                    pass
            elif line.startswith("GPSLongitude:"):
                try:
                    lon = float(line.split(":",1)[1].strip())
                except Exception:
                    pass
        if lat is not None and lon is not None:
            return (lat, lon)
    except Exception:
        return None
    return None


def extract_video_frames(video_path: Path, 
                        max_frames: int = 5,
                        strategy: str = "uniform") -> List[Image.Image]:
    """
    Extract frames from a video file for analysis.
    
    Args:
        video_path: Path to the video file
        max_frames: Maximum number of frames to extract
        strategy: Frame extraction strategy:
            - "uniform": Evenly spaced frames throughout the video
            - "keyframes": Extract actual keyframes (not implemented yet)
            - "first": Just the first frame
    
    Returns:
        List of PIL Image objects
    """
    frames = []
    
    try:
        cap = cv2.VideoCapture(str(video_path))
        
        if not cap.isOpened():
            logger.warning(f"Could not open video file: {video_path}")
            return frames
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        duration_sec = total_frames / fps if fps > 0 else 0
        
        if total_frames <= 0:
            logger.warning(f"Video has no frames: {video_path}")
            return frames
        
        logger.debug(f"Video {video_path.name}: {total_frames} frames, {fps:.1f} fps, {duration_sec:.1f}s duration")
        
        # Determine which frames to extract
        if strategy == "first":
            frame_indices = [0]
        elif strategy == "uniform":
            # Extract frames uniformly distributed throughout the video
            if total_frames <= max_frames:
                frame_indices = list(range(total_frames))
            else:
                step = total_frames / max_frames
                frame_indices = [int(i * step) for i in range(max_frames)]
        else:
            logger.warning(f"Unknown frame extraction strategy: {strategy}, using 'uniform'")
            step = total_frames / max_frames
            frame_indices = [int(i * step) for i in range(max_frames)]
        
        # Extract frames
        for frame_idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            
            if ret:
                # Convert BGR (OpenCV) to RGB (PIL)
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_image = Image.fromarray(frame_rgb)
                frames.append(pil_image)
            else:
                logger.debug(f"Failed to read frame {frame_idx} from {video_path}")
        
        cap.release()
        logger.debug(f"Extracted {len(frames)} frames from {video_path.name}")
        
    except Exception as e:
        logger.warning(f"Error extracting frames from {video_path}: {e}")
    
    return frames


def get_video_representative_frame(video_path: Path) -> Optional[Image.Image]:
    """
    Extract a single representative frame from a video (middle frame).
    
    Args:
        video_path: Path to the video file
    
    Returns:
        PIL Image object or None if extraction fails
    """
    frames = extract_video_frames(video_path, max_frames=1, strategy="uniform")
    return frames[0] if frames else None


def extract_frame_at_timestamp(video_path: Path, t_sec: float) -> Optional[Image.Image]:
    """
    Extract a single frame at the given timestamp (in seconds).
    Uses fps and frame count to compute target frame index.
    """
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            logger.warning(f"Could not open video file: {video_path}")
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            fps = 30.0
        frame_idx = int(max(0, t_sec * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame_rgb)
    except Exception as e:
        logger.warning(f"Error extracting frame at {t_sec:.3f}s from {video_path}: {e}")
        return None


def extract_keyframes_from_shot(
    video_path: Path,
    shot: Tuple[float, float],
    keyframes_per_shot: int = 1,
) -> List[Tuple[float, Optional[Image.Image]]]:
    """
    Extract 1-3 keyframes for a shot: middle only, start+end, or start+mid+end.

    Returns list of (timestamp_sec, image) tuples. Image may be None if decoding fails.
    """
    t0, t1 = shot
    try:
        if keyframes_per_shot <= 1:
            t_mid = (t0 + t1) / 2.0
            img = extract_frame_at_timestamp(video_path, t_mid)
            if img is None:
                img = get_video_representative_frame(video_path)
            return [(t_mid, img)]
        elif keyframes_per_shot == 2:
            img0 = extract_frame_at_timestamp(video_path, t0)
            img1 = extract_frame_at_timestamp(video_path, t1)
            if img0 is None:
                img0 = get_video_representative_frame(video_path)
            if img1 is None:
                img1 = get_video_representative_frame(video_path)
            return [
                (t0, img0),
                (t1, img1),
            ]
        else:
            t_mid = (t0 + t1) / 2.0
            img0 = extract_frame_at_timestamp(video_path, t0)
            img_mid = extract_frame_at_timestamp(video_path, t_mid)
            img1 = extract_frame_at_timestamp(video_path, t1)
            if img0 is None:
                img0 = get_video_representative_frame(video_path)
            if img_mid is None:
                img_mid = get_video_representative_frame(video_path)
            if img1 is None:
                img1 = get_video_representative_frame(video_path)
            return [
                (t0, img0),
                (t_mid, img_mid),
                (t1, img1),
            ]
    except Exception as e:
        logger.error(f"Failed to extract keyframes from shot [{t0:.2f}, {t1:.2f}] in {video_path}: {e}")
        return []
