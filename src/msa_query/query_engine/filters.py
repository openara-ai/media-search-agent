from typing import List, Dict, Any

def _match(term_list, values):
    if not term_list:
        return True
    values = values or []
    svals = {str(v).lower() for v in values}
    return any(str(t).lower() in svals for t in term_list)

def _match_contains(term_list, values):
    """Substring match: any term appears within any value (case-insensitive)."""
    if not term_list:
        return True
    values = [str(v).lower() for v in (values or []) if v is not None]
    if not values:
        return False
    terms = [str(t).lower() for t in term_list]
    return any(any(term in val for val in values) for term in terms)

# filters expects e.g. {"people":["Kumar"], "place":["Hawaii"], "tags":["dog", "beach"], "media_type":"video", "date_from":"2022-01-01", "date_to":"2022-12-31"}
def apply_filters(items: List[Dict[str, Any]], f: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for m in items:
        ok = True
        if not _match(f.get("people"), m.get("faces")):
            ok = False
        # Place filter supports substring matching (e.g., 'California' in 'San Jose, California, US')
        if not _match_contains(f.get("place"), [m.get("place"), m.get("country"), m.get("state")]):
            ok = False
        # Object/scene tags filter
        if not _match(f.get("tags"), m.get("tags")):
            ok = False
        # Media type filter (image/video)
        media_type_filter = f.get("media_type")
        if media_type_filter:
            path = m.get("path", "").lower()
            if media_type_filter.lower() == "video":
                # Check if path has video extension
                if not any(path.endswith(ext) for ext in ['.mp4', '.mov', '.m4v', '.avi', '.mkv']):
                    ok = False
            elif media_type_filter.lower() == "image":
                # Check if path has image extension
                if not any(path.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp']):
                    ok = False
        # date range (YYYY-MM-DD)
        if f.get("date_from") and (m.get("date") or "") < f["date_from"]:
            ok = False
        if f.get("date_to") and (m.get("date") or "") > f["date_to"]:
            ok = False
        
        # Video-specific filters
        if m.get("type") == "video":
            # Timestamp range filter (for video keyframes)
            timestamp_range = f.get("timestamp_range")
            if timestamp_range and len(timestamp_range) == 2:
                ts = m.get("timestamp")
                if ts is not None:
                    start_ts, end_ts = timestamp_range
                    if not (start_ts <= ts <= end_ts):
                        ok = False
            
            # Duration filters (requires shot_start/shot_end or similar metadata)
            # For now, filter based on shot duration if available
            shot_start = m.get("shot_start")
            shot_end = m.get("shot_end")
            if shot_start is not None and shot_end is not None:
                duration = shot_end - shot_start
                if f.get("min_duration") and duration < f["min_duration"]:
                    ok = False
                if f.get("max_duration") and duration > f["max_duration"]:
                    ok = False
        
        if ok:
            out.append(m)
    return out
