from PIL import Image, ExifTags
try:
    # Enable HEIC/HEIF support for Pillow if available
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:
    pass
try:
    import exifread  # fallback parser for GPS if Pillow path fails
except Exception:
    exifread = None
from datetime import datetime


def _dms_to_decimal(dms, ref):
    """Convert GPS coordinates in EXIF DMS format to decimal degrees.

    dms is typically a tuple of 3 rational numbers (deg, min, sec).
    ref is a string like 'N', 'S', 'E', 'W'.
    """
    try:
        def _to_float(x):
            try:
                # Pillow may return IFDRational or tuples
                if hasattr(x, "num") and hasattr(x, "den"):
                    return float(x.num) / float(x.den)
                if hasattr(x, "numerator") and hasattr(x, "denominator"):
                    den = float(x.denominator) if float(x.denominator) != 0 else 1.0
                    return float(x.numerator) / den
                # Some images store as (num, den)
                if isinstance(x, (tuple, list)) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x):
                    den = x[1] if x[1] != 0 else 1.0
                    return float(x[0]) / float(den)
                if isinstance(x, (int, float)):
                    return float(x)
                # Some libraries may return strings like '12/1'
                if isinstance(x, str):
                    try:
                        if "/" in x:
                            num, den = x.split("/", 1)
                            den = float(den) if float(den) != 0 else 1.0
                            return float(num) / den
                        return float(x)
                    except Exception:
                        return None
                return None
            except Exception:
                return None

        degrees = _to_float(dms[0])
        minutes = _to_float(dms[1])
        seconds = _to_float(dms[2])
        if degrees is None or minutes is None or seconds is None:
            return None
        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except Exception:
        return None


def _extract_gps_from_exif(exif):
    """Return decimal GPS coordinates from a Pillow EXIF object when available."""
    try:
        gps_info = exif.get_ifd(0x8825)
    except Exception:
        gps_info = None

    if not gps_info:
        return None, None

    gps_parsed = {}
    try:
        for key, value in gps_info.items():
            tag_name = ExifTags.GPSTAGS.get(key, key)
            gps_parsed[tag_name] = value
    except Exception:
        return None, None

    lat = gps_parsed.get("GPSLatitude")
    lat_ref = gps_parsed.get("GPSLatitudeRef")
    lon = gps_parsed.get("GPSLongitude")
    lon_ref = gps_parsed.get("GPSLongitudeRef")
    if not (lat and lat_ref and lon and lon_ref):
        return None, None

    return _dms_to_decimal(lat, lat_ref), _dms_to_decimal(lon, lon_ref)


def get_exif_basic(path):
    """Extract a minimal set of EXIF metadata including timestamp, camera/lens, size, and GPS.

    Returns dict keys: ts_utc, camera, lens, width, height, gps_lat, gps_lon (lat/lon may be None).
    """
    im = None
    exif_dict = {}
    gps_lat = None
    gps_lon = None
    try:
        im = Image.open(path)
        exif = im.getexif()
        exif_dict = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
        gps_lat, gps_lon = _extract_gps_from_exif(exif)
    except Exception:
        # Fall back to empty exif if anything fails
        exif_dict = {}

    # Fallback: if Pillow did not yield GPS, try exifread (works well for some HEIC/JPEG variants)
    if (gps_lat is None or gps_lon is None) and exifread is not None:
        try:
            with open(path, 'rb') as f:
                tags = exifread.process_file(f, details=False)
            lat_vals = tags.get('GPS GPSLatitude')
            lat_ref = tags.get('GPS GPSLatitudeRef')
            lon_vals = tags.get('GPS GPSLongitude')
            lon_ref = tags.get('GPS GPSLongitudeRef')
            def _vals_to_tuple(vals):
                # exifread returns Ratio objects; convert to (num,den)-like floats
                try:
                    seq = list(vals.values) if hasattr(vals, 'values') else list(vals)
                except Exception:
                    seq = []
                out = []
                for x in seq[:3]:
                    try:
                        num = getattr(x, 'num', None)
                        den = getattr(x, 'den', None)
                        if num is not None and den is not None and den != 0:
                            out.append((float(num), float(den)))
                        else:
                            # Try string like '12/1'
                            xs = str(x)
                            if '/' in xs:
                                n, d = xs.split('/', 1)
                                d = float(d) if float(d) != 0 else 1.0
                                out.append((float(n), d))
                            else:
                                out.append(float(xs))
                    except Exception:
                        pass
                return tuple(out) if len(out) == 3 else None

            if lat_vals and lat_ref and lon_vals and lon_ref:
                lat_tuple = _vals_to_tuple(lat_vals)
                lon_tuple = _vals_to_tuple(lon_vals)
                lat_ref_s = str(lat_ref.values) if hasattr(lat_ref, 'values') else str(lat_ref)
                lon_ref_s = str(lon_ref.values) if hasattr(lon_ref, 'values') else str(lon_ref)
                if lat_tuple and lon_tuple:
                    lat_dec = _dms_to_decimal(lat_tuple, lat_ref_s)
                    lon_dec = _dms_to_decimal(lon_tuple, lon_ref_s)
                    gps_lat = gps_lat if gps_lat is not None else lat_dec
                    gps_lon = gps_lon if gps_lon is not None else lon_dec
        except Exception:
            pass

    ts = exif_dict.get("DateTimeOriginal") or exif_dict.get("DateTime")
    ts_iso = None
    if ts:
        try:
            ts_iso = datetime.strptime(str(ts), "%Y:%m:%d %H:%M:%S").isoformat()
        except Exception:
            # Some cameras include timezone or different separators; ignore if unparseable for now
            pass

    width, height = (getattr(im, "width", None), getattr(im, "height", None)) if im else (None, None)
    return {
        "ts_utc": ts_iso,
        "camera": exif_dict.get("Model"),
        "lens": exif_dict.get("LensModel"),
        "width": width,
        "height": height,
        "gps_lat": gps_lat,
        "gps_lon": gps_lon,
    }
