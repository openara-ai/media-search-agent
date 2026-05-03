PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS media (
  media_id TEXT PRIMARY KEY,       -- sha256 of file (or uuid)
  -- Absolute path on disk (kept for backward compatibility and direct access)
  path TEXT NOT NULL UNIQUE,
  -- Multi-source support: logical source identifier and path relative to source root
  source_name TEXT,                -- e.g., "sample_photos", "onedrive_photos"
  rel_path TEXT,                   -- path relative to source root (e.g., "album1/IMG_001.jpg")
  size_bytes INTEGER,
  mime TEXT,
  ts_utc TEXT,                     -- EXIF or file mtime
  gps_lat REAL, gps_lon REAL,
  place TEXT,
  camera TEXT, lens TEXT,
  width INTEGER, height INTEGER, duration REAL,
  hash_blake3 TEXT,                -- fast content hash for dedup
  added_at TEXT DEFAULT (datetime('now')),
  model_version TEXT DEFAULT 'clip-0.1',
  deleted INTEGER DEFAULT 0,
  face_detection_done INTEGER DEFAULT 0,  -- 1 if face detection has been run, 0 otherwise
  object_detection_done INTEGER DEFAULT 0, -- 1 if object detection has been run, 0 otherwise
  gps_processed INTEGER DEFAULT 0,        -- 1 if GPS metadata has been extracted, 0 otherwise
  gps_data_mode TEXT,
  embeddings_version TEXT                 -- Model version tag for embeddings (null if not embedded)
);
-- Optional index to accelerate lookups by (source_name, rel_path)
CREATE INDEX IF NOT EXISTS idx_media_source_rel ON media(source_name, rel_path);
CREATE TABLE IF NOT EXISTS tag (
  tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS media_tag (
  media_id TEXT,
  tag_id INTEGER,
  PRIMARY KEY (media_id, tag_id),
  FOREIGN KEY (media_id) REFERENCES media(media_id),
  FOREIGN KEY (tag_id) REFERENCES tag(tag_id)
);
-- Person identities (labeled or clustered) - Create BEFORE face table for FK reference
CREATE TABLE IF NOT EXISTS person (
  person_id TEXT PRIMARY KEY,            -- uuid
  name TEXT UNIQUE,                      -- user-provided label
  is_labeled INTEGER DEFAULT 0,          -- 1 if user-labeled, 0 if auto-clustered
  cluster_id INTEGER,                    -- for grouping similar unknown faces
  representative_face_id TEXT,           -- best quality face for this person
  face_count INTEGER DEFAULT 0,          -- number of faces in this identity
  created_at TEXT DEFAULT (datetime('now'))
);

-- Face detections (bounding boxes + metadata)
-- Note: If upgrading from old schema, drop old face table first: DROP TABLE IF EXISTS face;
CREATE TABLE IF NOT EXISTS face (
  face_id TEXT PRIMARY KEY,              -- uuid for this detection
  media_id TEXT NOT NULL,                -- references media.media_id
  x REAL, y REAL, w REAL, h REAL,        -- bbox (normalized 0-1)
  confidence REAL,                       -- detection confidence
  person_id TEXT,                        -- references person.person_id (null if unknown)
  gender TEXT,                           -- optional: 'M', 'F', NULL
  age INTEGER,                           -- optional: estimated age
  shot_index INTEGER,                    -- for videos: which shot
  kf_index INTEGER,                      -- for videos: which keyframe
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (media_id) REFERENCES media(media_id),
  FOREIGN KEY (person_id) REFERENCES person(person_id)
);

-- Index for fast person lookup
CREATE INDEX IF NOT EXISTS idx_face_person ON face(person_id);
CREATE INDEX IF NOT EXISTS idx_face_media ON face(media_id);

-- Per-media-item image embedding (CLIP, 768-dim float32, ~3 KB blob).
-- Separate table from `media` so browse paths (which use explicit-column
-- SELECTs) physically cannot pull in the BLOB pages, and so dropping
-- this table is enough to force a re-embed without losing labels or
-- metadata.
CREATE TABLE IF NOT EXISTS image_embedding (
  media_id        TEXT PRIMARY KEY,
  embedding       BLOB NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (media_id) REFERENCES media(media_id) ON DELETE CASCADE
);

-- Per-face embedding (facenet-pytorch vggface2, 512-dim float32, ~2 KB).
CREATE TABLE IF NOT EXISTS face_embedding (
  face_id         TEXT PRIMARY KEY,
  embedding       BLOB NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (face_id) REFERENCES face(face_id) ON DELETE CASCADE
);

-- Per-keyframe embedding (CLIP, 768-dim float32). Keyed by the parent
-- video_keyframes.id so cascade-delete fires when a keyframe row is
-- dropped (e.g. video re-indexed with new shot boundaries).
CREATE TABLE IF NOT EXISTS keyframe_embedding (
  keyframe_id     INTEGER PRIMARY KEY,
  embedding       BLOB NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (keyframe_id) REFERENCES video_keyframes(id) ON DELETE CASCADE
);

-- Video shot boundaries (for video-semantic search)
CREATE TABLE IF NOT EXISTS shots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,                 -- references media.media_id
  shot_index INTEGER NOT NULL,            -- 0-based index in this video
  t_start REAL NOT NULL,                  -- seconds
  t_end REAL NOT NULL,                    -- seconds
  duration REAL GENERATED ALWAYS AS (t_end - t_start) VIRTUAL,
  keyframe_count INTEGER DEFAULT 1,
  is_synthetic INTEGER DEFAULT 0,         -- 1 if fallback full-video shot, 0 if detected
  FOREIGN KEY (video_id) REFERENCES media(media_id)
);

CREATE INDEX IF NOT EXISTS idx_shots_video ON shots(video_id);

-- Keyframes per shot (non-vector metadata)
CREATE TABLE IF NOT EXISTS video_keyframes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,                 -- references media.media_id
  shot_index INTEGER NOT NULL,            -- shot index within the video
  kf_index INTEGER NOT NULL,              -- keyframe index within the shot
  timestamp REAL NOT NULL,                -- seek position (seconds)
  shot_start REAL NOT NULL,
  shot_end REAL NOT NULL,
  tags TEXT,                              -- JSON array of object detection tags for this keyframe
  gps_lat REAL,
  gps_lon REAL,
  gps_alt REAL,
  gps_datetime_utc TEXT,
  gps_fix INTEGER,
  gps_source TEXT,
  place TEXT,
  UNIQUE (video_id, shot_index, kf_index),
  FOREIGN KEY (video_id) REFERENCES media(media_id)
);

CREATE INDEX IF NOT EXISTS idx_vkf_video ON video_keyframes(video_id);

CREATE TABLE IF NOT EXISTS index_state (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  index_version_seq INTEGER NOT NULL DEFAULT 0,
  index_version_ts TEXT
);
