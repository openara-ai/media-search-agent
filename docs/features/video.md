# Video

Videos are first-class citizens in Media Search Agent. They're broken
into shots, indexed per shot, and search results jump to the matching
moment within a clip rather than just to the file.

## How videos are indexed

For each video the indexer:

1. **Detects shots** with PySceneDetect (content-aware boundary detection).
   A 30-second clip with a few cuts becomes, say, 4 shots; a single
   continuous take stays as one shot.
2. **Picks keyframes per shot.** By default one representative frame per
   shot — configurable up to 3 (start / middle / end) in
   [`config.yaml`](../CONFIGURATION.md#video-shot-detection).
3. **Embeds each keyframe** with CLIP, exactly like a still photo. Each
   keyframe becomes its own entry in the search index.
4. **Tags each keyframe** with RT-DETR objects.
5. **Detects faces in each keyframe** with facenet-pytorch — so people
   labeled on the [People page](people.md) show up in their videos too.
6. **Extracts GoPro GPS** when an action-cam GPMF telemetry track is
   present (see below).

## What this means for search

A search like `kids playing in snow` ranks individual video moments
alongside photos. If a 90-second clip contains 30 seconds of indoor
chatter and 60 seconds of snow play, only the snow-play keyframes will
rank highly — the indoor shots stay in the noise floor.

Click a video result and it opens in the detail drawer with the player
**seeked to the matching keyframe's timestamp**, not the start of the
file. The drawer shows the shot index, timestamp, EXIF/metadata,
detected faces, and any GPS attached to that shot.

## Deduplication

Without help, a single shot's adjacent keyframes (or near-identical
keyframes from neighboring shots) would flood the result list.
Search applies temporal deduplication: keyframes from the same video
within a short window collapse to the highest-scoring representative,
so you see varied results instead of three rows of nearly-the-same
frame.

## GPS for moving cameras (GoPro)

Phones and most cameras tag the whole video with a single GPS coordinate
— wherever the recording started. That's wrong for a 5-minute mountain-
biking clip that covers two miles.

For action-cam videos that embed GPS as a timed metadata track (GoPro's
GPMF format), the indexer reads per-second telemetry via `exiftool` and
attaches a representative GPS coordinate **to each shot's keyframe**, not
to the file as a whole. A 10-shot clip ends up with up to 10 GPS points,
each reverse-geocoded to a place name. Filtering by location surfaces
the right moments instead of every minute of the ride.

For non-GoPro videos with a single file-level GPS coordinate, that one
coordinate is used as before — same behaviour as still photos.

## Supported formats

The indexer accepts: **MP4, MOV, MKV, AVI**. Variable-frame-rate clips
work; HDR videos work; H.264, H.265, and AV1 all decode through ffmpeg.
Format coverage is conservative on purpose — if a video doesn't index,
re-encode to MP4 with `ffmpeg -i in.mov out.mp4` and re-run the indexer.

## Tuning

Two knobs in `config.yaml` are worth knowing about:

- **`shot_detection_threshold`** (default 30.0) — lower it for content
  with subtle cuts (concert footage with similar lighting across cuts);
  raise it if you're getting too many fragmentary shots.
- **`keyframes_per_shot`** (default 1) — bump to 2 or 3 if you have lots
  of long, dynamic shots where one frame doesn't capture the action.
  Costs more index time; usually not needed.

Defaults are tuned for typical consumer video. See
[CONFIGURATION.md](../CONFIGURATION.md#video-shot-detection) for the full
list.

## Limitations

- **Audio is not indexed today.** Speech, music, and ambient sound don't
  contribute to search. Visual-only matching is what CLIP does.
- **Long static shots can over-represent.** A 10-minute fixed-camera
  recording of a sleeping baby still produces only the keyframes the
  shot detector picks, but those keyframes are heavily weighted in the
  search index. If a single video is dominating your results, raising
  the threshold slider usually fixes it.
- **Captions and ASR are not yet indexed.** A future release will add
  automatic transcripts so you can search across what's spoken in
  videos, not just what's visible.

For the indexing pipeline at the architectural level — shot detector,
GPS extractor, embedding flow — see
[ARCHITECTURE.md](../ARCHITECTURE.md#indexing-flow).
