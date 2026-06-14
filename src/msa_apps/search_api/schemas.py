from typing import Dict, List, Optional
from pydantic import BaseModel
class SearchFilters(BaseModel):
    people: Optional[List[str]] = None
    place: Optional[List[str]] = None
    tags: Optional[List[str]] = None  # Object/scene detection tags
    media_type: Optional[str] = None  # "image", "video", or None for all
    date_from: Optional[str] = None  # YYYY-MM-DD
    date_to: Optional[str] = None
    # Video-specific filters
    timestamp_range: Optional[List[float]] = None  # [start_seconds, end_seconds] within video
    min_duration: Optional[float] = None  # Minimum video duration in seconds
    max_duration: Optional[float] = None  # Maximum video duration in seconds
class SearchRequest(BaseModel):
    q: str
    filters: Optional[SearchFilters] = None
class SearchItem(BaseModel):
    id: str
    path: str | None
    thumbnail: str | None
    score: Optional[float] = None  # Semantic similarity score from Qdrant (0–1)
    raw_similarity_score: Optional[float] = None
    similarity_score: Optional[float] = None
    person_boost: Optional[float] = None
    person_multiplier: Optional[float] = None
    expansion_boost: Optional[float] = None
    expansion_multiplier: Optional[float] = None
    tags: Optional[List[str]] = None  # Object/scene detection tags
    type: Optional[str] = None  # "video" or None for images
    timestamp: Optional[float] = None  # Video: seek position in seconds
    shot_id: Optional[int] = None  # Video: shot index
    date: Optional[str] = None  # ISO format timestamp
    gps_lat: Optional[float] = None  # EXIF latitude (decimal degrees)
    gps_lon: Optional[float] = None  # EXIF longitude (decimal degrees)
    place: Optional[str] = None  # Human-readable place (e.g., "San Jose, California, US")
    why: str | None
class SearchResponse(BaseModel):
    results: List[SearchItem]
    search_id: Optional[str] = None  # correlates opens back to this search (ADR-009)

class TrackOpenRequest(BaseModel):
    search_id: str
    media_id: str

class KeyframeInfo(BaseModel):
    kf_index: int
    timestamp: float
    tags: List[str]
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    gps_alt: Optional[float] = None
    gps_datetime_utc: Optional[str] = None
    gps_fix: Optional[int] = None
    gps_source: Optional[str] = None
    place: Optional[str] = None

class ShotInfo(BaseModel):
    shot_index: int
    t_start: float  # seconds
    t_end: float    # seconds
    duration: float  # seconds
    is_synthetic: bool
    keyframes: List[KeyframeInfo]

class VideoShotsResponse(BaseModel):
    video_id: str
    path: str
    total_duration: Optional[float] = None
    shots: List[ShotInfo]

# Face search schemas
class FaceSearchRequest(BaseModel):
    face_id: str
    top_k: int = 20

class FaceSearchMatch(BaseModel):
    face_id: str
    score: float
    media_id: Optional[str] = None
    path: Optional[str] = None
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    shot_index: Optional[int] = None
    kf_index: Optional[int] = None

class FaceSearchResponse(BaseModel):
    matches: List[FaceSearchMatch]

# Face suggestions (for labeling assistance)
class PersonSuggestion(BaseModel):
    person_id: str
    person_name: str
    score: float
    face_count: int  # number of similar faces for this person

class FaceSuggestionsResponse(BaseModel):
    face_id: str
    suggestions: List[PersonSuggestion]

# People and labeling schemas
class PersonOut(BaseModel):
    person_id: str
    name: str
    face_count: Optional[int] = None
    thumbnail: Optional[str] = None

class PeopleListResponse(BaseModel):
    people: List[PersonOut]

class PersonCreate(BaseModel):
    name: str

class PersonRename(BaseModel):
    name: str

class MergeRequest(BaseModel):
    source_id: str

class FaceLabelRequest(BaseModel):
    person_id: Optional[str] = None
    name: Optional[str] = None  # If provided without person_id, create person and assign

class BulkLabelRequest(BaseModel):
    face_ids: List[str]
    person_id: Optional[str] = None  # Optional; either person_id or name must be provided
    name: Optional[str] = None       # Create new person if person_id not given

# Batch face suggestions schemas
class BatchFaceSuggestionsRequest(BaseModel):
    face_ids: List[str]
    top_k: int = 5

class BatchFaceSuggestionItem(BaseModel):
    face_id: str
    suggestions: List[PersonSuggestion]

class BatchFaceSuggestionsResponse(BaseModel):
    results: Dict[str, BatchFaceSuggestionItem]
    missing: List[str] = []
