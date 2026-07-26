"""Payload-column derivation map — M-8/S-3 §4.1.

THE CONTRACT (a derivation rule, not a hand-maintained list): any write to a
SQLite cell referenced by any Qdrant payload builder must stamp every
embedding row whose payload embeds that cell, so delta export
(``updated_seq > exported_seq``) can never miss a payload-only change (R7).

This module declares the rule's two halves, derived mechanically from the
three payload builders in ``qdrant_export.py`` (``build_payload``,
``build_video_payload``, ``build_face_payload``) and the iterators feeding
them (``sqlite_store.iter_items`` / ``iter_video_keyframes`` /
``iter_faces``):

- :data:`PAYLOAD_SOURCES` — per collection, every payload key a builder
  emits and the ``(table, column)`` cells that key derives from
  (:data:`CONST` marks keys whose value is a constant or config, not a
  SQLite cell);
- :data:`STAMP_RULES` — for every source cell, the stamp rule that covers
  writes to it (which store-layer write paths stamp which embedding rows).

The coverage test in ``tests/test_delta_export.py`` locks the derivation:

1. each builder's emitted key set must equal its declared key set — a
   builder gaining a payload key without a map entry fails the test;
2. every declared source cell must have a stamp rule — a source column
   without one fails the test;
3. every stamp rule is exercised against a real ``SQLiteStore`` and must be
   observed writing ``updated_seq`` on its declared targets.

A future payload-builder change therefore cannot silently reopen the
stale-payload gap: it forces an update here, which forces a stamp rule,
which the test forces to actually work.

The media-level rule deliberately stamps a SUPERSET (the media's image row,
all its keyframe rows, and its faces' rows) rather than per-column-precise
targets: every derived cell is covered by construction, at the cost of a
few extra row re-upserts — the per-column-precision failure mode produced
five review findings during planning.
"""
from __future__ import annotations

# Marks payload keys whose value does not derive from a SQLite cell
# (builder constants or config values).
CONST: frozenset = frozenset()

_MEDIA_PATH_CELLS = frozenset(
    {("media", "path"), ("media", "source_name"), ("media", "rel_path")}
)
_MEDIA_TAG_CELLS = frozenset(
    {("media_tag", "media_id"), ("media_tag", "tag_id"), ("tag", "name")}
)

#: collection -> payload key -> frozenset of (table, column) source cells
PAYLOAD_SOURCES: dict[str, dict[str, frozenset]] = {
    "image": {
        "media_id": frozenset({("media", "media_id")}),
        "path": _MEDIA_PATH_CELLS,
        "people": CONST,  # iter_items always yields [] today
        "place": frozenset({("media", "place")}),
        "timestamp": frozenset({("media", "ts_utc"), ("media", "added_at")}),
        "tags": _MEDIA_TAG_CELLS,
    },
    "video": {
        "id": frozenset({("media", "media_id")}),
        "media_id": frozenset({("media", "media_id")}),
        "type": CONST,
        "path": _MEDIA_PATH_CELLS,
        "timestamp": frozenset({("video_keyframes", "timestamp")}),
        "shot_id": frozenset({("video_keyframes", "shot_index")}),
        "shot_start": frozenset({("video_keyframes", "shot_start")}),
        "shot_end": frozenset({("video_keyframes", "shot_end")}),
        # keyframe-level tags win, media-level tags are the fallback
        "tags": frozenset({("video_keyframes", "tags")}) | _MEDIA_TAG_CELLS,
        "gps_lat": frozenset({("video_keyframes", "gps_lat")}),
        "gps_lon": frozenset({("video_keyframes", "gps_lon")}),
        "gps_alt": frozenset({("video_keyframes", "gps_alt")}),
        "gps_datetime_utc": frozenset({("video_keyframes", "gps_datetime_utc")}),
        "gps_fix": frozenset({("video_keyframes", "gps_fix")}),
        "gps_source": frozenset({("video_keyframes", "gps_source")}),
        # keyframe place wins, media place is the fallback
        "place": frozenset({("video_keyframes", "place"), ("media", "place")}),
        # distinct person names of the video's faces
        "people": frozenset(
            {("face", "media_id"), ("face", "person_id"), ("person", "name")}
        ),
    },
    "face": {
        "face_id": frozenset({("face", "face_id")}),
        "media_id": frozenset({("face", "media_id")}),
        "path": _MEDIA_PATH_CELLS,
        # derived from the resolved path's extension
        "type": _MEDIA_PATH_CELLS,
        "bbox": frozenset(
            {("face", "x"), ("face", "y"), ("face", "w"), ("face", "h")}
        ),
        "confidence": frozenset({("face", "confidence")}),
        "person_id": frozenset({("face", "person_id")}),
        "person_name": frozenset({("face", "person_id"), ("person", "name")}),
        "gender": frozenset({("face", "gender")}),
        "age": frozenset({("face", "age")}),
        "date": frozenset({("media", "ts_utc")}),
        "shot_index": frozenset({("face", "shot_index")}),
        "kf_index": frozenset({("face", "kf_index")}),
        "embedding_backend": CONST,  # config value
    },
}

# Stamp rules. Each name identifies one store-layer write family and its
# stamp targets; the coverage test exercises every rule:
#
# media_superset  — upsert_media / update_media_fields (payload fields) /
#                   add_tags / resurrect_media: stamp the media's
#                   image_embedding row + all its keyframe_embedding rows +
#                   its faces' face_embedding rows.
# keyframe_rows   — add_keyframes (insert AND conflict-update arms): stamp
#                   the video's keyframe_embedding rows. (Embedding upserts
#                   additionally stamp their own row on both arms.)
# face_rows       — add_faces (insert AND conflict-update arms): stamp the
#                   media's face_embedding rows + its keyframe_embedding
#                   rows (video payloads embed people).
# face_label      — update_face_person / update_faces_person_batch /
#                   clear_face_person: stamp the face's embedding row + the
#                   owning video's keyframe_embedding rows.
# person_rows     — rename_person / merge_people: stamp every face of the
#                   person + every keyframe row of videos containing them.
#
#: (table, column) -> stamp rule name
STAMP_RULES: dict[tuple[str, str], str] = {
    # media-level cells → superset stamp
    ("media", "media_id"): "media_superset",
    ("media", "path"): "media_superset",
    ("media", "source_name"): "media_superset",
    ("media", "rel_path"): "media_superset",
    ("media", "place"): "media_superset",
    ("media", "ts_utc"): "media_superset",
    ("media", "added_at"): "media_superset",
    ("media_tag", "media_id"): "media_superset",
    ("media_tag", "tag_id"): "media_superset",
    ("tag", "name"): "media_superset",
    # keyframe-level cells → that video's keyframe rows
    ("video_keyframes", "timestamp"): "keyframe_rows",
    ("video_keyframes", "shot_index"): "keyframe_rows",
    ("video_keyframes", "shot_start"): "keyframe_rows",
    ("video_keyframes", "shot_end"): "keyframe_rows",
    ("video_keyframes", "tags"): "keyframe_rows",
    ("video_keyframes", "gps_lat"): "keyframe_rows",
    ("video_keyframes", "gps_lon"): "keyframe_rows",
    ("video_keyframes", "gps_alt"): "keyframe_rows",
    ("video_keyframes", "gps_datetime_utc"): "keyframe_rows",
    ("video_keyframes", "gps_fix"): "keyframe_rows",
    ("video_keyframes", "gps_source"): "keyframe_rows",
    ("video_keyframes", "place"): "keyframe_rows",
    # face detection cells → the media's face rows (+ keyframes)
    ("face", "face_id"): "face_rows",
    ("face", "media_id"): "face_rows",
    ("face", "x"): "face_rows",
    ("face", "y"): "face_rows",
    ("face", "w"): "face_rows",
    ("face", "h"): "face_rows",
    ("face", "confidence"): "face_rows",
    ("face", "gender"): "face_rows",
    ("face", "age"): "face_rows",
    ("face", "shot_index"): "face_rows",
    ("face", "kf_index"): "face_rows",
    # labeling cells → face row + owning video's keyframes
    ("face", "person_id"): "face_label",
    # person cells → every face of the person + affected videos' keyframes
    ("person", "name"): "person_rows",
}


def all_source_cells() -> frozenset:
    """Union of every (table, column) cell any payload builder reads."""
    cells: set = set()
    for keys in PAYLOAD_SOURCES.values():
        for sources in keys.values():
            cells |= sources
    return frozenset(cells)
