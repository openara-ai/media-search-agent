from typing import Dict, Any, Iterable
import re


# Face names can arrive in several shapes depending on where the candidate came
# from: a comma-separated string, a list of names, or values that still include
# confidence suffixes such as "Rajeev (0.98)". Normalize all of them into a
# comparable lowercase name list before scoring person matches.
def _normalize_face_names(value: Any) -> list[str]:
    """Normalize faces/people payloads into lowercase person names."""
    if not value:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return [re.sub(r"\s*\([^)]*\)\s*$", "", p).strip().lower() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            out.append(re.sub(r"\s*\([^)]*\)\s*$", "", text).strip().lower())
        return out
    return [str(value).strip().lower()]


def _lower_set(values: Iterable[Any]) -> set[str]:
    return {str(v).strip().lower() for v in values if v is not None and str(v).strip()}


# Keep scoring intentionally simple for Phase 4C:
# - raw semantic similarity stays the anchor
# - named-person matches amplify that similarity
# - person-based candidate expansion only helps when real similarity exists
# This makes the ranking easier to reason about than the earlier additive model.
def score_breakdown(m: Dict[str, Any], q: str, query_context: Dict[str, Any] | None = None) -> Dict[str, float]:
    """Return score components for a similarity-first, person-aware reranker."""
    query_context = query_context or {}
    raw_similarity_score = float(m.get("raw_similarity_score", m.get("score", 0.0)))
    similarity_score = raw_similarity_score
    face_names = _normalize_face_names(m.get("faces"))
    source_scores = m.get("source_scores") or {}

    inferred_people = _lower_set(query_context.get("inferred_people") or [])

    person_hits = len(inferred_people.intersection(set(face_names)))
    person_multiplier = 1.0 + (0.25 * person_hits)
    has_person_expansion_signal = (
        raw_similarity_score > 0.0
        and (
            str(m.get("source") or "") == "person_expand"
            or "person_expand" in source_scores
        )
    )
    expansion_multiplier = 1.5 if has_person_expansion_signal else 1.0
    total_score = similarity_score * person_multiplier * expansion_multiplier
    person_boost = similarity_score * (person_multiplier - 1.0)
    expansion_boost = (similarity_score * person_multiplier) * (expansion_multiplier - 1.0)
    return {
        "raw_similarity_score": raw_similarity_score,
        "expansion_base_score": 0.0,
        "similarity_score": similarity_score,
        "person_boost": person_boost,
        "person_multiplier": person_multiplier,
        "expansion_boost": expansion_boost,
        "expansion_multiplier": expansion_multiplier,
        "total_score": total_score,
    }


# Simple heuristic: combine original ANN score with light boosts.
def simple_score(m: Dict[str, Any], q: str, query_context: Dict[str, Any] | None = None) -> float:
    return score_breakdown(m, q, query_context=query_context)["total_score"]
