# ADR-010: SQLite as Canonical Embedding Store, FAISS Removed

Status: Accepted

## Context

Originally the indexer used three storage layers: SQLite for media
metadata + face/person rows, FAISS for embeddings (`image_vec.faiss` for CLIP
image and video-keyframe vectors, `face_vec.faiss` for facenet face vectors),
and Qdrant as the search-time vector index built from FAISS at the end of each
indexing run.

Three things forced a re-evaluation:

1. **First-time indexing for a 50K+ photo library could take 14+ hours.**
   During the run nothing was browsable or searchable because:
   - SQLite was opened with `autocommit=False` and committed exactly once at
     end of run, so the API saw an empty (or pre-run) snapshot.
   - Embeddings accumulated in Python lists for the entire run; only at the
     end were they appended to FAISS and saved.
   - The API released its embedded-Qdrant lock for the duration of the
     subprocess, even though Qdrant was actually only written during the
     final ~5–15 min export phase.

2. **A crash at hour 13 of 14 lost everything.** SQLite rolled back the open
   transaction; FAISS files were never saved; thumbnails were the only
   artifact that survived (orphaned). 13 hours of GPU time was unrecoverable.

3. **FAISS was being used as a slow non-atomic key-value store, not as a
   search index.** Qdrant handled all actual search; FAISS's only consumers
   were the indexer (writes) and the Qdrant-export phase + a single
   API call site (`fstore.get_vector(face_id)` to look up a face vector by
   ID before sending it to Qdrant for similarity search). The FAISS index's
   strengths — IVF/HNSW/PQ approximate search at 100M+ vectors — were never
   exercised in this project. Plus the storage layout had a known issue:
   `face_vec.faiss.vecs.npy` duplicated `face_vec.faiss` on disk, so a
   typical 50K-image library ran ~270 MB heavier than necessary.

## Decision

**Embeddings live in SQLite as float32 BLOBs in three tables sibling to
`media`/`face`/`video_keyframes`. FAISS is no longer a runtime
dependency on the indexing, query, or API paths.**

### Schema

```sql
CREATE TABLE image_embedding (
    media_id        TEXT PRIMARY KEY REFERENCES media(media_id) ON DELETE CASCADE,
    embedding       BLOB NOT NULL,
    embedding_dim   INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE keyframe_embedding (
    keyframe_id     INTEGER PRIMARY KEY REFERENCES video_keyframes(id) ON DELETE CASCADE,
    embedding       BLOB NOT NULL,
    embedding_dim   INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE face_embedding (
    face_id         TEXT PRIMARY KEY REFERENCES face(face_id) ON DELETE CASCADE,
    embedding       BLOB NOT NULL,
    embedding_dim   INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);
```

PK = parent's PK enforces 1:1 cardinality. `ON DELETE CASCADE` keeps embedding
rows in lockstep with their parents under deletes (e.g. media removal,
face re-detection, video re-shotting).

### Source of truth, derived index

| Layer | Role |
|---|---|
| SQLite (canonical) | Media metadata + embeddings. The single source of truth. |
| Qdrant (derived) | Search index. Built by `msa index export` reading SQLite. Can be rebuilt at any time without data loss. |

## Rationale

### Why not keep FAISS

- **It wasn't doing what it's good at.** No `index.search()` is called from
  the API; only `index.reconstruct(id)`. We were paying FAISS's price (extra
  file format, non-atomic save, separate sidecar files, no FK semantics) for
  zero of its benefit.
- **Storage was duplicated.** `face_vec.faiss.vecs.npy` is a redundant
  serialization of the same vectors as `face_vec.faiss`. ~30% of the index/
  directory was that duplicate.
- **Crash recovery was broken.** Vectors lived in Python lists for the
  entire indexing run. The plan's per-batch SQLite commit (Stage 1) makes
  metadata durable per batch, but only by moving embeddings into SQLite as
  well does that durability cover the embeddings too.
- **Memory bounded.** With FAISS, a 500K-image run would hold ~3 GB of vectors
  in Python heap. With SQLite BLOBs written per file, peak memory is one batch.

### Why separate tables, not BLOB columns on `media`/`face`/`video_keyframes`

Earlier drafts of the plan added `embedding BLOB` columns directly to the
parent rows. Three reasons we rejected that:

1. **Structural protection against `SELECT *`.** A future
   `SELECT * FROM media WHERE deleted=0` would silently 5–10× browse latency
   if BLOBs lived inline. With separate tables, `media` row size is
   unchanged and browse cache density is preserved.
2. **Drop-and-rebuild is clean.** `DELETE FROM image_embedding;` forces a
   re-embed without touching metadata or labels. With inline columns this
   would be `UPDATE media SET embedding = NULL`, rewriting every row.
3. **Independent storage analysis.** `SELECT COUNT(*), AVG(LENGTH(embedding))
   FROM image_embedding` tells you embedding-only footprint cleanly.

The cost — one extra INSERT per file inside the same transaction — is
negligible.

### Why not adopt Qdrant's embedded mode for storage too

Qdrant's `path=`-mode storage is also SQLite-based under the hood, so we
considered consolidating onto Qdrant alone. Two reasons we kept SQLite as
canonical:

1. **Qdrant's storage format is opaque and version-coupled.** SQLite tables
   are inspectable with `sqlite3` CLI, queryable from any tool, and stable
   across decades. Qdrant's storage layout has changed across major versions.
2. **Embedded Qdrant locks the file for a single process.** SQLite-canonical
   means the indexer and the API can both read metadata and embeddings
   concurrently (WAL mode) without coordinating around Qdrant's lock.

## Consequences

### Disk-footprint reduction

Measured on a real 50K image + 100 h video library (per the plan's storage
analysis):

| Layer | Before | After |
|---|---|---|
| `image_vec.faiss` | 263 MB | — |
| `face_vec.faiss` | 273 MB | — |
| `face_vec.faiss.vecs.npy` (duplicate) | 273 MB | — |
| `face_vec.faiss.ids` | 9 MB | — |
| `media.sqlite` | 130 MB | ~660 MB (added embedding BLOBs) |
| **`index/` total** | **~948 MB** | **~660 MB (~30% smaller)** |

The savings come almost entirely from eliminating the `.npy` duplication.

### Memory footprint reduction

Indexer peak resident memory drops from 150–600 MB (vector accumulator) to
one batch's worth (~600 KB at the default N=200 file batch size).

### Migration path

Pre-release product, so no in-app migration UI:

- **Fresh runs** produce SQLite-native embeddings with no extra steps.
- **Existing DBs** auto-backfill `image_embedding` and `keyframe_embedding`
  on the next normal indexer run (idempotent CLIP re-embed).
- **Face embeddings** require explicit user action because re-running
  detection risks overwriting manual labels (`face.person_id`). The
  supported recovery path is `--reprocess-faces` which re-detects from
  images and drops existing labels. An indexer-startup `WARNING` is
  logged when orphan face rows are detected.

### Code surface removed

- `vec.add()` / `vec.save()` / `fstore.add()` / `fstore.save()` end-of-run
  block in `pipeline.py`
- In-memory `vecs` / `ids` / `face_vectors` / `face_vectors_ids` / `kf_vecs`
  accumulators
- `FaissStore` and `FaceFaissStore` references at all runtime call sites
  (the modules themselves remain in the tree solely for the porter script;
  removal once the porter has shipped is a future cleanup)
- `face_vec.faiss` cleanup-on-`reprocess_faces` block (replaced by
  `ON DELETE CASCADE` from `face_embedding` to `face`)
- Three FAISS-dependent fixtures in `test_export.py`

### Code surface added

- `image_embedding` / `keyframe_embedding` / `face_embedding` tables
  (`schema.sql`)
- `SQLiteStore.upsert_*_embedding` / `get_*_embedding` / `delete_faces_for_media` /
  `has_image_embedding` / `media_has_unembedded_keyframes` /
  `media_has_unembedded_faces` / `count_orphan_face_embeddings` methods
- 24 new unit tests + 4 new real-data assertions

### Operational

- `index/face_vec.faiss`, `image_vec.faiss`, and their `.ids` / `.vecs.npy`
  sidecars are no longer written. Existing files left in place by previous
  runs are harmless and can be deleted manually after running the porter
  or re-indexing.
- The `faiss-cpu` Python dependency stays in `requirements.txt` until the
  porter script is retired (a future ADR or cleanup commit). Runtime code
  paths no longer import it.

## Out of Scope

- Re-introducing FAISS for ANN search at very large scale (1B+ vectors). If
  that ever becomes a real product requirement, FAISS would be added as a
  search-side cache derived from the SQLite source of truth, not as primary
  storage.
- Granular Qdrant lock coordination during indexing — tracked separately.
