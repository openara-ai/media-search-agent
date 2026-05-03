export interface SearchFilters {
  people?: string[]
  place?: string[]
  tags?: string[]
  media_type?: 'image' | 'video' | null
  date_from?: string
  date_to?: string
  min_duration?: number
  max_duration?: number
}

export interface SearchItem {
  id: string
  path: string | null
  thumbnail: string | null
  score: number | null
  raw_similarity_score?: number | null
  similarity_score?: number | null
  person_boost?: number | null
  person_multiplier?: number | null
  expansion_boost?: number | null
  expansion_multiplier?: number | null
  tags: string[] | null
  type: 'video' | null
  timestamp: number | null
  shot_id: number | null
  date: string | null
  gps_lat: number | null
  gps_lon: number | null
  place: string | null
  why: string | null
}

export interface SearchResponse {
  results: SearchItem[]
}

export interface MediaItem {
  id: string
  path: string
  date: string | null
  type: 'image' | 'video'
  duration: number | null
  gps_lat: number | null
  gps_lon: number | null
  place: string | null
  tags?: string[] | null
}

export interface MediaListResponse {
  items: MediaItem[]
  count: number
  limit: number
  offset: number
}

export interface VideoShotKeyframe {
  kf_index: number
  timestamp: number
  tags: string[]
  gps_lat: number | null
  gps_lon: number | null
  gps_alt: number | null
  gps_datetime_utc: string | null
  gps_fix: number | null
  gps_source: string | null
  place: string | null
}

export interface VideoShot {
  shot_index: number
  t_start: number
  t_end: number
  duration: number
  is_synthetic: boolean
  keyframes: VideoShotKeyframe[]
}

export interface VideoShotsResponse {
  video_id: string
  path: string
  total_duration: number | null
  shots: VideoShot[]
}

export interface FaceOnMedia {
  face_id: string
  bbox: [number, number, number, number]
  confidence: number
  person_id: string | null
  person_name: string | null
  gender: string | null
  age: number | null
  shot_index: number | null
  kf_index: number | null
}

export interface MediaFacesResponse {
  media_id: string
  faces: FaceOnMedia[]
}

export interface MediaFilters {
  media_type?: 'image' | 'video' | null
  date_from?: string
  date_to?: string
  place?: string
  tags?: string[]
  people?: string[]
  people_mode?: 'any' | 'all' | 'only'
  sort_by?: 'date' | 'path'
  sort_order?: 'asc' | 'desc'
}
