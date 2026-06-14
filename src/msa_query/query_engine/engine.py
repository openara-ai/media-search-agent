from typing import List, Dict, Any
from loguru import logger
from msa_query.embeddings.text_encoder import TextEncoder
from msa_query.query_engine.retrievers import Retriever
from msa_query.query_engine.filters import apply_filters
from msa_query.query_engine.rerankers import score_breakdown
from msa_query.config import settings
from msa_query.storage.db import connect_readonly
from pathlib import Path
import math
import re
import time
from datetime import date

try:  # optional reranker integration (ADR-013) — features only; serving is wired in app.py
    from msa_ranker import features as _ranker_features
except ModuleNotFoundError as _exc:
    _ranker_features = None
    if (_exc.name or "").split(".")[0] != "msa_ranker":
        # msa_ranker is installed but one of ITS deps is missing → broken install, surface it
        logger.warning("msa_ranker present but a dependency is missing; reranker disabled: {}", _exc)
except Exception as _exc:  # present but broken/incompatible → surface it, but stay up
    _ranker_features = None
    logger.warning("msa_ranker present but failed to import; reranker disabled: {}", _exc)


_QUERY_STOPWORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "near",
    "of", "on", "the", "to", "with",
}


def _short_id(value: Any) -> str:
    text = str(value)
    if len(text) <= 6:
        return text
    return f"{text[:2]}..{text[-2:]}"


# First-pass query decomposition keeps CLIP focused on the scene/action terms
# while pulling out known person names for separate scoring logic. This avoids
# baking names like "John" or "Rajeev" into the visual embedding query when we
# already have stronger person metadata available elsewhere in the pipeline.
def _decompose_query_text(q: str, known_people: List[str]) -> Dict[str, Any]:
    """Split a query into a CLIP-friendly visual query and known-person intent."""
    original_query = (q or "").strip()
    if not original_query:
        return {
            "original_query": original_query,
            "visual_query": original_query,
            "inferred_people": [],
            "residual_tokens": [],
        }

    inferred_people: List[str] = []
    residual_query = original_query

    # Longer names first so "John Smith" wins before "John".
    for person_name in sorted({p.strip() for p in known_people if p and p.strip()}, key=len, reverse=True):
        pattern = re.compile(rf"\b{re.escape(person_name)}\b", re.IGNORECASE)
        if pattern.search(residual_query):
            inferred_people.append(person_name)
            residual_query = pattern.sub(" ", residual_query)

    residual_query = re.sub(r"\s+", " ", residual_query).strip()
    residual_tokens = re.findall(r"[A-Za-z0-9']+", residual_query.lower())
    visual_tokens = [tok for tok in residual_tokens if tok not in _QUERY_STOPWORDS]

    visual_query = " ".join(visual_tokens).strip()
    if not visual_query:
        visual_query = original_query

    return {
        "original_query": original_query,
        "visual_query": visual_query,
        "inferred_people": inferred_people,
        "residual_tokens": residual_tokens,
        "visual_tokens": visual_tokens,
    }


# Keep temporal parsing intentionally narrow in Phase 1: extract only explicit
# years and simple relative phrases that can be translated safely into an
# implicit date range without adding a heavyweight natural-language time parser.
# Example: "beach photos from 2016" -> visual_query="beach photos from" plus
# date_from/date_to for the 2016 calendar year.
def _extract_simple_date_intent(q: str, today: date | None = None) -> Dict[str, Any]:
    """Extract simple date intent from a query and return an implicit date filter."""
    original_query = (q or "").strip()
    today = today or date.today()
    if not original_query:
        return {
            "visual_query": original_query,
            "date_filter": {},
            "matched_phrases": [],
        }

    working_query = original_query
    matched_phrases: List[str] = []
    inferred_year: int | None = None

    last_year_match = re.search(r"\blast year\b", working_query, flags=re.IGNORECASE)
    years_ago_match = re.search(r"\b(\d{1,2})\s+years\s+ago\b", working_query, flags=re.IGNORECASE)
    year_matches = list(re.finditer(r"\b(19|20)\d{2}\b", working_query))

    if last_year_match:
        inferred_year = today.year - 1
        matched_phrases.append(last_year_match.group(0))
        working_query = re.sub(r"\blast year\b", " ", working_query, flags=re.IGNORECASE)
    elif years_ago_match:
        years_ago = int(years_ago_match.group(1))
        inferred_year = today.year - years_ago
        matched_phrases.append(years_ago_match.group(0))
        working_query = re.sub(r"\b\d{1,2}\s+years\s+ago\b", " ", working_query, flags=re.IGNORECASE)
    elif len(year_matches) == 1:
        inferred_year = int(year_matches[0].group(0))
        matched_phrases.append(year_matches[0].group(0))
        working_query = re.sub(rf"\b{re.escape(year_matches[0].group(0))}\b", " ", working_query)

    working_query = re.sub(r"\s+", " ", working_query).strip()
    date_filter: Dict[str, str] = {}
    if inferred_year is not None:
        date_filter = {
            "date_from": f"{inferred_year:04d}-01-01",
            "date_to": f"{inferred_year:04d}-12-31",
        }

    return {
        "visual_query": working_query,
        "date_filter": date_filter,
        "matched_phrases": matched_phrases,
    }


def _load_known_people_names(conn: Any | None) -> List[str]:
    """Load labeled people names from SQLite for query decomposition."""
    if conn is None:
        return []
    try:
        cur = conn.execute(
            "SELECT name FROM person WHERE name IS NOT NULL AND TRIM(name) != '' ORDER BY name COLLATE NOCASE ASC"
        )
        return [str(name) for (name,) in cur.fetchall() if name]
    except Exception as exc:
        logger.warning(f"Could not load known people for query decomposition: {exc}")
        return []


def _load_people_name_to_id(conn: Any | None) -> Dict[str, str]:
    """Map labeled person name -> person_id (lowercased key), so the reranker can
    resolve query people to ids alongside names (ADR-008; no extra lookup)."""
    if conn is None:
        return {}
    try:
        cur = conn.execute(
            "SELECT name, person_id FROM person "
            "WHERE name IS NOT NULL AND TRIM(name) != '' AND person_id IS NOT NULL"
        )
        return {str(name).lower(): str(pid) for (name, pid) in cur.fetchall() if name and pid}
    except Exception as exc:
        logger.warning(f"Could not load person name->id map: {exc}")
        return {}


class _DictPersonLookup:
    """A msa_ranker PersonLookup backed by a prebuilt {media_id: {person_id}} map."""

    def __init__(self, mapping: Dict[str, set]) -> None:
        self._m = mapping

    def person_ids_for_media(self, media_id: str) -> set:
        return self._m.get(str(media_id), set())


def _learned_rerank(
    ranked: List[Dict[str, Any]],
    feature_list: List[Dict[str, float] | None],
    ctx: Any,
    now: float | None,
    ranker: Any,
) -> tuple[List[Dict[str, Any]], List[Dict[str, float] | None], bool]:
    """Reorder `ranked` (and the aligned `feature_list`) by a learned model — spec 06.

    **Reorder-only** (INV-6): returns a *permutation* of the inputs — never adds/drops a
    candidate, and touches no SQLite write handle (it operates on in-memory dicts). On a
    closed gate (``ranker is None``), unavailable features, a score-count mismatch, or
    **any** scoring/ordering error, it returns the inputs **untouched** (no dict mutated)
    so the heuristic order stands byte-identically (INV-3 / FR-14 fail-safe). The third
    return is True when the learned model scored and its order was applied — i.e. the model
    *served* (even if ties coincidentally left the order unchanged). The pre-existing
    heuristic ``score`` is preserved on each result as ``heuristic_score`` before being
    overwritten with the served score (NN1, so baseline eval stays reconstructable).
    """
    if ranker is None or not ranked or any(f is None for f in feature_list):
        return ranked, feature_list, False
    try:
        # Score COPIES that carry the seam-computed features, so the original candidate
        # dicts stay untouched until we commit on full success — any error (scoring OR a
        # non-numeric sort key) leaves the heuristic path byte-identical, and the sort
        # itself is inside the guard so it can never turn /search into a 500 (FR-14).
        scoring_inputs = [{**m, "features": f} for m, f in zip(ranked, feature_list)]
        scores = list(ranker.score(scoring_inputs, ctx, now=now))
        if len(scores) != len(ranked):
            raise ValueError(f"ranker returned {len(scores)} scores for {len(ranked)} candidates")
        # NaN/inf do NOT raise on negation or sort (NaN compares False, so Timsort just
        # finishes) — they'd be written into results and blow up the response's JSON
        # serialization (500). Reject them here so the request falls back to the heuristic.
        if not all(math.isfinite(s) for s in scores):
            raise ValueError("ranker returned non-finite score(s)")
        order = sorted(range(len(ranked)), key=lambda i: -scores[i])  # stable; may raise
    except Exception as exc:  # bad model / non-finite / non-numeric / anything → heuristic
        logger.warning("learned reranking failed; keeping heuristic order: {}", exc)
        return ranked, feature_list, False
    new_ranked: List[Dict[str, Any]] = []
    new_feats: List[Dict[str, float] | None] = []
    for i in order:
        m = ranked[i]
        m.setdefault("heuristic_score", m.get("score"))  # preserve NN1 before overwrite
        m["score"] = scores[i]  # the served (learned) score
        new_ranked.append(m)
        new_feats.append(feature_list[i])
    return new_ranked, new_feats, True


class _SearchResults(list):
    """A plain list of results that also carries `.query_context`, so callers that
    just want the list are unaffected while the reranker shim can read the context."""

    query_context: Dict[str, Any]


# Person-aware candidate expansion is a recall helper: it can bring likely
# person matches into the rerank pool even when the ANN search misses them.
# These rows start with zero similarity and must still earn score through real
# similarity support plus the current reranking model.
def _expand_candidates_for_people(
    conn: Any | None,
    inferred_people: List[str],
    date_filter: Dict[str, Any] | None = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Fetch candidate media rows for inferred people directly from SQLite."""
    if conn is None or not inferred_people:
        return []
    try:
        where_clauses = [
            "m.deleted = 0",
            "p.name IN ({})".format(",".join(["?"] * len(inferred_people))),
        ]
        params: List[Any] = list(inferred_people)
        if date_filter and date_filter.get("date_from"):
            where_clauses.append("m.ts_utc >= ?")
            params.append(date_filter["date_from"])
        if date_filter and date_filter.get("date_to"):
            where_clauses.append("m.ts_utc <= ?")
            params.append(date_filter["date_to"])

        sql = f"""
            SELECT DISTINCT
                m.media_id,
                m.path,
                m.source_name,
                m.rel_path,
                m.place,
                m.ts_utc,
                m.mime
            FROM media m
            JOIN face f ON f.media_id = m.media_id
            JOIN person p ON p.person_id = f.person_id
            WHERE {' AND '.join(where_clauses)}
            ORDER BY m.ts_utc DESC, m.added_at DESC
            LIMIT ?
        """
        params.append(limit)

        cur = conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        if not rows:
            return []

        media_ids = [str(row[0]) for row in rows]
        placeholders = ",".join(["?"] * len(media_ids))

        people_cur = conn.execute(
            f"""
            SELECT f.media_id, p.name
            FROM face f
            JOIN person p ON p.person_id = f.person_id
            WHERE f.media_id IN ({placeholders}) AND p.name IS NOT NULL
            ORDER BY p.name
            """,
            tuple(media_ids),
        )
        people_map: Dict[str, set[str]] = {}
        for media_id, person_name in people_cur.fetchall():
            people_map.setdefault(str(media_id), set()).add(str(person_name))

        tags_cur = conn.execute(
            f"""
            SELECT mt.media_id, t.name
            FROM media_tag mt
            JOIN tag t ON t.tag_id = mt.tag_id
            WHERE mt.media_id IN ({placeholders})
            ORDER BY t.name
            """,
            tuple(media_ids),
        )
        tag_map: Dict[str, List[str]] = {}
        for media_id, tag_name in tags_cur.fetchall():
            tag_map.setdefault(str(media_id), []).append(str(tag_name))

        expanded: List[Dict[str, Any]] = []
        for media_id, path, _source_name, _rel_path, place, ts_utc, mime in rows:
            media_id = str(media_id)
            mime_text = str(mime or "").lower()
            expanded.append({
                "id": media_id,
                "score": 0.0,
                "raw_similarity_score": 0.0,
                "expansion_base_score": 0.0,
                "path": path,
                "faces": sorted(people_map.get(media_id, set())),
                "tags": tag_map.get(media_id, []),
                "place": place,
                "date": ts_utc,
                "type": "video" if mime_text.startswith("video/") else None,
                "timestamp": None,
                "shot_id": None,
                "source": "person_expand",
            })
        return expanded
    except Exception as exc:
        logger.warning(f"Could not expand candidates for inferred people: {exc}")
        return []


def _enrich_places(conn: Any | None, candidates: List[Dict[str, Any]]) -> None:
    """Hydrate place metadata for the current candidate set using one shared connection."""
    if conn is None or not candidates:
        return
    ids = [str(m.get("id")) for m in candidates if m.get("id") is not None]
    uniq = sorted(set(ids))
    if not uniq:
        return
    placeholders = ",".join(["?"] * len(uniq))
    sql = f"SELECT media_id, place FROM media WHERE media_id IN ({placeholders})"
    cur = conn.execute(sql, tuple(uniq))
    pmap = {str(mid): place for (mid, place) in cur.fetchall()}
    for m in candidates:
        mid = str(m.get("id"))
        if mid in pmap and pmap[mid] is not None:
            m["place"] = pmap[mid]


def _enrich_people(conn: Any | None, candidates: List[Dict[str, Any]]) -> None:
    """Hydrate named people for the current candidate set using one shared connection."""
    if conn is None or not candidates:
        return
    ids = [str(m.get("id")) for m in candidates if m.get("id") is not None]
    uniq = sorted(set(ids))
    if not uniq:
        return
    placeholders = ",".join(["?"] * len(uniq))
    sql = (
        "SELECT f.media_id, f.person_id, p.name "
        "FROM face f LEFT JOIN person p ON f.person_id = p.person_id "
        f"WHERE f.media_id IN ({placeholders}) AND p.name IS NOT NULL"
    )
    cur = conn.execute(sql, tuple(uniq))
    fmap: Dict[str, set[str]] = {}
    idmap: Dict[str, set[str]] = {}
    for mid, pid, pname in cur.fetchall():
        mid = str(mid)
        if not pname:
            continue
        fmap.setdefault(mid, set()).add(str(pname))
        if pid:
            idmap.setdefault(mid, set()).add(str(pid))  # person_id carried with name
    for m in candidates:
        mid = str(m.get("id"))
        if mid in fmap:
            m["faces"] = sorted(fmap[mid])
        if mid in idmap:
            m["person_ids"] = sorted(idmap[mid])


# Preserve source-specific evidence before reranking so one media item can
# carry both its strongest base score and supporting scores from other
# collections. Example: an image hit at 0.31 plus a caption hit at 0.22 should
# stay one candidate with source_scores={"img": 0.31, "cap": 0.22}.
def _merge_hits_with_source_scores(source_hits: List[tuple[str, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """Merge hits across collections while preserving per-source evidence."""
    merged: Dict[str, Dict[str, Any]] = {}
    for src, hits in source_hits:
        for h in hits:
            if "id" not in h:
                logger.warning(f"Skipping result missing 'id' field: {h}")
                continue
            item_id = str(h["id"])
            score = float(h.get("score", 0.0))
            existing = merged.get(item_id)
            if existing is None:
                merged[item_id] = {
                    **h,
                    "id": item_id,
                    "score": score,
                    "raw_similarity_score": float(h.get("raw_similarity_score", score)),
                    "expansion_base_score": float(h.get("expansion_base_score", 0.0)),
                    "source": src,
                    "source_scores": {src: score},
                }
                continue

            existing.setdefault("source_scores", {})[src] = score

            # Carry forward richer metadata when the newer hit provides it.
            for key, value in h.items():
                if key in {"id", "score"}:
                    continue
                if existing.get(key) in (None, "", [], ()):
                    existing[key] = value

            # Preserve the strongest single-source score as the base score.
            if score > float(existing.get("score", 0.0)):
                existing["score"] = score
                existing["raw_similarity_score"] = float(h.get("raw_similarity_score", score))
                existing["expansion_base_score"] = float(h.get("expansion_base_score", 0.0))
                existing["source"] = src

    return list(merged.values())


def temporal_deduplicate_videos(results: List[Dict[str, Any]], time_window: float = 5.0) -> List[Dict[str, Any]]:
    """
    Deduplicate video keyframes that are close together in time from the same video.
    Keeps the highest-scoring keyframe within each temporal cluster.
    
    Args:
        results: List of search results
        time_window: Group keyframes within this many seconds (default: 5.0)
        
    Returns:
        Deduplicated list with one representative keyframe per temporal cluster
    """
    # Separate videos from images
    videos = [r for r in results if r.get("type") == "video"]
    images = [r for r in results if r.get("type") != "video"]
    
    if not videos:
        return results
    
    # Group by video path (media_id may vary for keyframes)
    from collections import defaultdict
    by_video = defaultdict(list)
    for v in videos:
        path = v.get("path", "")
        by_video[path].append(v)
    
    # Cluster each video's keyframes by timestamp
    deduped_videos = []
    for path, keyframes in by_video.items():
        # Sort by timestamp
        keyframes.sort(key=lambda x: (x.get("timestamp") or 0.0, -(x.get("score") or 0.0)))
        
        clusters = []
        for kf in keyframes:
            ts = kf.get("timestamp") or 0.0
            # Try to add to existing cluster within time_window
            added = False
            for cluster in clusters:
                cluster_ts = cluster["timestamp"]
                if abs(ts - cluster_ts) <= time_window:
                    # Same cluster - keep higher score
                    if kf.get("score", 0) > cluster.get("score", 0):
                        # Replace cluster representative with this better one
                        cluster.update(kf)
                    added = True
                    break
            
            if not added:
                # New cluster
                clusters.append(kf.copy())
        
        deduped_videos.extend(clusters)
    
    # Merge back with images, maintaining relative score order
    all_results = images + deduped_videos
    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    
    logger.debug(f"Temporal deduplication: {len(videos)} video keyframes → {len(deduped_videos)} clusters")
    return all_results

class QueryEngine:
    
    def __init__(
        self,
        retriever: Retriever,
        text_encoder: TextEncoder,
        sqlite_path: str | Path | None = None,
        search_score_trace: bool = False,
    ):
        self.retriever = retriever
        self.text_encoder = text_encoder
        self.sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self.search_score_trace = search_score_trace
    
    def search(
        self, q: str, filters: Dict[str, Any] | None = None, ranker: Any = None
    ) -> List[Dict[str, Any]]:
        """Back-compat: a list of ranked results that also carries `.query_context`
        (a list subclass), so existing callers/mocks are unaffected. `ranker` (the loaded
        learned model, or None) is threaded through — None ⇒ heuristic order (INV-3)."""
        results, query_context = self.search_for_serving(q, filters, ranker=ranker)
        out = _SearchResults(results)
        out.query_context = query_context
        return out

    def search_for_serving(
        self, q: str, filters: Dict[str, Any] | None = None, ranker: Any = None
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Like search() but also returns the query_context — used by the reranker
        shim to log search/shown events. Each result carries `features` +
        `heuristic_score` when msa_ranker is installed."""
        logger.info(f"Search request q='{q}' filters={filters}")
        sqlite_conn = None
        sqlite_path = self.sqlite_path
        if sqlite_path is not None and sqlite_path.exists():
            try:
                sqlite_conn = connect_readonly(sqlite_path)
            except Exception as exc:
                logger.warning(f"Could not open SQLite for search helpers: {exc}")
        try:
            date_parts = _extract_simple_date_intent(q)
            query_parts = _decompose_query_text(
                date_parts["visual_query"],
                _load_known_people_names(sqlite_conn),
            )
            search_text = query_parts["visual_query"] or date_parts["visual_query"] or q
            inferred_filters = dict(filters or {})
            for key, value in date_parts["date_filter"].items():
                inferred_filters.setdefault(key, value)
            _name_to_id = _load_people_name_to_id(sqlite_conn)
            # People the user expressed in TEXT *and* via the UI People filter are both real
            # query intent — merge them, else a filter-only person search would log
            # person_hits=0 and contaminate training data (resolve names -> ids, ADR-008).
            people_names = list(query_parts["inferred_people"]) + list(
                inferred_filters.get("people") or []
            )
            inferred_person_ids: List[str] = []
            for _n in people_names:
                _pid = _name_to_id.get(str(_n).lower())
                if _pid and _pid not in inferred_person_ids:
                    inferred_person_ids.append(_pid)
            # Date intent = whatever is in the merged filters (UI range or text-inferred).
            date_intent = {
                k: inferred_filters[k] for k in ("date_from", "date_to") if inferred_filters.get(k)
            }
            query_context = {
                "original_query": q,
                "visual_query": search_text,
                "inferred_people": query_parts["inferred_people"],
                "inferred_person_ids": inferred_person_ids,  # resolved ids (ADR-008)
                "visual_tokens": query_parts.get("visual_tokens", []),
                "date_filter": date_intent or date_parts["date_filter"],
            }

            if self.search_score_trace and (
                query_parts["inferred_people"] or date_parts["date_filter"]
            ):
                logger.info(
                    "query_decomposition "
                    f"original_query={q!r} "
                    f"visual_query={search_text!r} "
                    f"inferred_people={query_parts['inferred_people']!r} "
                    f"inferred_date_filter={date_parts['date_filter']!r}"
                )
            # 1) embed query
            qvec = self.text_encoder.encode(search_text)

            # 2) Build Qdrant-native filter if tags are specified
            qdrant_filter = None
            if inferred_filters and inferred_filters.get("tags"):
                from msa_query.storage.qdrant_client import QdrantStore
                qdrant_filter = QdrantStore.build_tag_filter(inferred_filters["tags"])

            # 3) retrieve from multiple vector spaces (gracefully handle missing collections)
            hits_img: List[Dict[str, Any]] = []
            hits_vid: List[Dict[str, Any]] = []
            hits_cap: List[Dict[str, Any]] = []
            hits_asr: List[Dict[str, Any]] = []

            # Try image collection (primary)
            try:
                hits_img = self.retriever.search(
                    settings.vector_collection_image, qvec, k=settings.retrieval_top_k, query_filter=qdrant_filter
                )
            except Exception as e:
                logger.warning(f"Could not search image collection '{settings.vector_collection_image}': {e}")

            # Try video keyframes collection
            try:
                hits_vid = self.retriever.search(
                    settings.vector_collection_video, qvec, k=settings.retrieval_top_k, query_filter=qdrant_filter
                )
            except Exception as e:
                logger.info(f"Video collection '{settings.vector_collection_video}' not available: {e}")

            # Try caption collection (optional for now)
            try:
                hits_cap = self.retriever.search(
                    settings.vector_collection_caption, qvec, k=settings.retrieval_top_k, query_filter=qdrant_filter
                )
            except Exception as e:
                logger.info(f"Caption collection '{settings.vector_collection_caption}' not available: {e}")

            # Try ASR collection (optional for now)
            try:
                hits_asr = self.retriever.search(
                    settings.vector_collection_asr, qvec, k=settings.retrieval_top_k, query_filter=qdrant_filter
                )
            except Exception as e:
                logger.info(f"ASR collection '{settings.vector_collection_asr}' not available: {e}")

            logger.debug(
                f"Retriever hits: img={len(hits_img)} vid={len(hits_vid)} cap={len(hits_cap)} asr={len(hits_asr)}"
            )

            # 4) merge while preserving source-level evidence
            candidates = _merge_hits_with_source_scores(
                [("img", hits_img), ("vid", hits_vid), ("cap", hits_cap), ("asr", hits_asr)]
            )

            if query_parts["inferred_people"]:
                expanded_candidates = _expand_candidates_for_people(
                    sqlite_conn,
                    query_parts["inferred_people"],
                    date_filter=date_parts["date_filter"],
                    limit=max(settings.rerank_top_k * 2, 50),
                )
                if expanded_candidates:
                    existing_ids = {str(m.get("id")) for m in candidates if m.get("id") is not None}
                    added = 0
                    for item in expanded_candidates:
                        item_id = str(item.get("id"))
                        if item_id in existing_ids:
                            continue
                        candidates.append(item)
                        existing_ids.add(item_id)
                        added += 1
                    if added:
                        logger.info(
                            "candidate_expansion "
                            f"inferred_people={query_parts['inferred_people']!r} "
                            f"added_candidates={added}"
                        )

            # 5) filters (people, place, date, tags)
            # If place filter is requested, enrich candidates with latest place from SQLite before filtering
            if inferred_filters and inferred_filters.get("place"):
                try:
                    _enrich_places(sqlite_conn, candidates)
                except Exception as e:
                    logger.warning(f"Place enrichment failed: {e}")

            # If people filter is requested, enrich candidates with person names from SQLite
            if (inferred_filters and inferred_filters.get("people")) or query_parts["inferred_people"]:
                try:
                    _enrich_people(sqlite_conn, candidates)
                except Exception as e:
                    logger.warning(f"People enrichment failed: {e}")

            if inferred_filters:
                candidates = apply_filters(candidates, inferred_filters)

            # 6) rerank — write the reranked score back so the score field matches result order
            logger.debug(f"Candidates pre-filter: {len(candidates)}")
            for m in candidates:
                breakdown = score_breakdown(m, q, query_context=query_context)
                m["raw_similarity_score"] = breakdown["raw_similarity_score"]
                m["expansion_base_score"] = breakdown["expansion_base_score"]
                m["similarity_score"] = breakdown["similarity_score"]
                m["person_boost"] = breakdown["person_boost"]
                m["person_multiplier"] = breakdown["person_multiplier"]
                m["expansion_boost"] = breakdown["expansion_boost"]
                m["expansion_multiplier"] = breakdown["expansion_multiplier"]
                m["score"] = breakdown["total_score"]
            ranked = sorted(candidates, key=lambda m: m["score"], reverse=True)[: settings.rerank_top_k]
            
            # 6.5) temporal deduplication for videos (group nearby keyframes)
            ranked = temporal_deduplicate_videos(ranked, time_window=5.0)

            if self.search_score_trace:
                for rank, m in enumerate(ranked, start=1):
                    logger.info(
                        "score "
                        f"rank={rank} "
                        f"query={q!r} "
                        f"media_id={_short_id(m.get('id'))!r} "
                        f"source={m.get('source')!r} "
                        f"raw_similarity_score={m.get('raw_similarity_score', 0.0):.4f} "
                        f"expansion_base_score={m.get('expansion_base_score', 0.0):.4f} "
                        f"similarity_score={m.get('similarity_score', 0.0):.4f} "
                        f"person_boost={m.get('person_boost', 0.0):.4f} "
                        f"person_multiplier={m.get('person_multiplier', 1.0):.4f} "
                        f"expansion_boost={m.get('expansion_boost', 0.0):.4f} "
                        f"expansion_multiplier={m.get('expansion_multiplier', 1.0):.4f} "
                        f"total_score={m.get('score', 0.0):.4f}"
                    )

            # 7) format (+ reranker features, computed once here — the seam — so logging
            #    and serving share one code path; skipped when msa_ranker is absent)
            # Reranker features, computed once here (the seam). All-or-nothing + guarded: a
            # runtime extractor failure disables features for THIS search (→ None) and is
            # logged, but must never turn /search into a 500 (INV-9). None ⇒ shim skips logging.
            feature_list: List[Dict[str, float] | None] = [None] * len(ranked)
            _feat_ctx = None
            _now: float | None = None
            if _ranker_features is not None:
                try:
                    place_filter = inferred_filters.get("place") or []
                    _feat_ctx = _ranker_features.QueryContext(
                        people=query_context["inferred_person_ids"],
                        visual_tokens=query_context["visual_tokens"],
                        date_intent=query_context.get("date_filter") or None,
                        place_intent=place_filter[0] if place_filter else None,
                    )
                    _lookup = _DictPersonLookup(
                        {str(m["id"]): set(m.get("person_ids", [])) for m in ranked}
                    )
                    _now = time.time()
                    feature_list = [
                        _ranker_features.feature_dict(m, _feat_ctx, _now, _lookup) for m in ranked
                    ]
                except Exception as exc:  # extractor blew up at runtime — never fail search
                    logger.warning("reranker features failed; disabled for this search: {}", exc)
                    feature_list = [None] * len(ranked)

            # 7.5) learned reranking (spec 06): reorder the existing candidate set when a
            #      gated model is active on this engine. Permutation-only, fail-safe to the
            #      heuristic order; flag off / model absent ⇒ byte-identical to today (INV-3).
            ranked, feature_list, _ltr_served = _learned_rerank(
                ranked, feature_list, _feat_ctx, _now, ranker
            )
            query_context["ltr_served"] = _ltr_served

            results = [
                {
                    "id": m["id"],
                    "path": m.get("path"),
                    "thumbnail": m.get("thumbnail"),
                    "score": m.get("score"),
                    "raw_similarity_score": m.get("raw_similarity_score"),
                    "similarity_score": m.get("similarity_score"),
                    "person_boost": m.get("person_boost"),
                    "person_multiplier": m.get("person_multiplier"),
                    "expansion_boost": m.get("expansion_boost"),
                    "expansion_multiplier": m.get("expansion_multiplier"),
                    "tags": m.get("tags", []),
                    "type": m.get("type"),
                    "timestamp": m.get("timestamp"),
                    "shot_id": m.get("shot_id"),
                    "date": m.get("date"),
                    "why": f"{m.get('faces')} | {m.get('scene_tags')} | tags={m.get('tags')} | {m.get('caption')} | src={m.get('source')} score={m.get('score'):.3f}",
                    # the score_breakdown total (NN1) — preserved even when a learned model
                    # served and overwrote `score`, so baseline eval stays reconstructable.
                    "heuristic_score": m.get("heuristic_score", m.get("score")),
                    "features": feats,
                }
                for m, feats in zip(ranked, feature_list)
            ]
            logger.info(
                f"Search complete results={len(results)} top_sources={[m.get('source') for m in ranked[:5]]}"
            )
            return results, query_context
        finally:
            if sqlite_conn is not None:
                sqlite_conn.close()
