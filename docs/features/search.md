# Search

Natural-language search over your photos and video keyframes. Type what
you're looking for in plain English; the engine ranks images and video
moments by how well they match the meaning of your query.

## How it works

Your query and every indexed image (or video keyframe) are encoded into the
same vector space by CLIP — a vision-language model that learned to map
images and text into nearby points when their meaning matches. Search is
nearest-neighbour lookup in that space.

This means two things in practice:

- **Meaning beats keywords.** Searching `kids playing in snow` finds
  matching photos even if the filename is `IMG_4821.jpg` and there are no
  tags. Conversely, queries that rely on text-on-the-image (a license
  plate, a sign, a date stamp) usually don't help — CLIP doesn't read.
- **Concepts work better than specific instances.** `dog on a beach` is
  great; `my golden retriever Max` is not — until you've labeled Max on
  the People page, after which his name becomes a usable concept.

## Scoring and the threshold slider

Each result has a similarity score, displayed as a percentage. Don't read
this as "confidence" or "probability of being right" — it's a cosine
similarity converted to a percentage, and good matches in a CLIP model
typically land in the 20–35% range, not 80–90%. A 25% match can be the
exact photo you wanted; a 40% match sometimes isn't.

The **threshold slider** filters out results below a chosen score. Drag it
up to surface only the strongest matches; drag it down to see broader
candidates. There's no universally "right" threshold — it depends on the
query and your library.

## Filters

The filter bar narrows the candidate set before ranking. Available filters:

- **Date range** — based on EXIF capture time when available, falling back
  to file modified time.
- **Faces / People** — restrict to photos containing a labeled person (see
  the [People guide](people.md)).
- **GPS / Place** — geographic filtering when EXIF or video GPS-track data
  is present (see the [Video guide](video.md) for the GoPro path).
- **Source** — limit to one of your indexed media folders.
- **Tags** — object/scene tags from RT-DETR (`dog`, `car`, `beach`, etc.).

Filters compose: `kids playing` + person `Lily` + date range `2023` returns
only what matches all three.

## Query tips

- **Be a little specific.** `birthday cake with candles` works better than
  `birthday`. CLIP responds well to a sentence-fragment level of detail.
- **Try a few phrasings.** `red car at night`, `red car in the dark`, and
  `red sportscar at night` rank slightly differently — try the one that
  matches how you'd describe the scene to a friend.
- **Combine with filters for narrow asks.** Looking for "the photo from
  Lisa's wedding"? Don't try to encode "Lisa's wedding" semantically —
  filter to person `Lisa`, date range, then search `wedding`.
- **Don't expect text reading.** Queries like "the screenshot with the
  error message about port 8000" won't work; CLIP doesn't OCR.
- **Negation is unreliable.** `dog without a leash` may surface dogs *on*
  leashes — CLIP sees the words and pulls in matching imagery either way.

## What's searched

Both images and video keyframes are searched in the same query. A video
result links directly to the matching moment within the clip — see the
[Video guide](video.md). Multiple keyframes from the same video are
deduplicated so you don't get a result list flooded with near-identical
adjacent frames.

## When search returns nothing

- The indexer's first run hasn't finished — semantic search comes online
  when the run completes (see the [FAQ](../FAQ.md)).
- The threshold slider is too high — drag it down.
- Your filters exclude every candidate — clear filters and try again.
- The query is asking for text-on-image (license plates, signs, dates).
  CLIP can't help; try a tag or date filter instead.

For the technical details — model, vector dimensions, retrieval pipeline,
reranking — see [ARCHITECTURE.md](../ARCHITECTURE.md#search-request-flow).
