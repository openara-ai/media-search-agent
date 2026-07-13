import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from msa_settings.paths import resolve_for_access, display_path, is_wsl2
from msa_apps.search_api.schemas import (
    SearchRequest, SearchResponse, SearchItem, VideoShotsResponse, ShotInfo,
    FaceSearchRequest, FaceSearchResponse, FaceSearchMatch,
    PersonOut, PeopleListResponse, PersonCreate, PersonRename, MergeRequest,
    FaceLabelRequest, BulkLabelRequest,
    PersonSuggestion, FaceSuggestionsResponse,
    BatchFaceSuggestionsRequest, BatchFaceSuggestionsResponse, BatchFaceSuggestionItem,
    TrackOpenRequest,
)
from msa_apps.search_api.deps import get_query_engine
from msa_query.storage.db import connect_readonly
import os
from loguru import logger
from typing import Optional, List
try:
    from msa_settings import load_config
    from msa_indexer.utils.logging import setup_logging
    S = load_config()
    setup_logging(S.log_dir, S.log_level)
except Exception:
    # Fallback to default console logging if settings aren't available
    pass

# Expose QdrantClient symbol for easier testing monkeypatch; handle environments without qdrant
try:
    from qdrant_client import QdrantClient as _QdrantClient  # type: ignore
    QdrantClient = _QdrantClient  # alias for monkeypatching in tests
except Exception:  # pragma: no cover
    QdrantClient = None  # type: ignore


def _resolve_app_version() -> str:
    """OpenAPI schema version — derived from installed package metadata (stamped
    from the git tag at release build), never a hardcoded literal. Mirrors
    `msa status`. Falls back to a dev marker for an uninstalled source checkout."""
    from importlib.metadata import version, PackageNotFoundError
    for dist in ("media-search-agent", "msa"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0.0.0.dev0"


_APP_VERSION = _resolve_app_version()


def _effective_api_url(cfg) -> str:
    """The URL the API is actually reachable at, for the diagnostics endpoint (spec §1.3
    "runtime port authority"). In the Tauri desktop shell the backend runs on the supervisor-
    assigned ephemeral port (``SIDECAR_PORT``), not ``config.yaml`` ``api.port`` — so a
    diagnostics ``api_url`` derived from config would be wrong (the config port is unused in
    shell mode). Prefer the actually-bound sidecar port when present; else fall back to the
    configured port (browser / ``msa api start`` mode, byte-identical to before)."""
    sidecar_raw = os.environ.get("SIDECAR_PORT")
    if sidecar_raw:
        try:
            sidecar_port = int(sidecar_raw)
            if 1 <= sidecar_port <= 65535:
                return f"http://127.0.0.1:{sidecar_port}"
        except ValueError:
            pass
    return f"http://localhost:{getattr(getattr(cfg, 'api', None), 'port', 8000)}"

# --- Helpers for resolving absolute paths from (source_name, rel_path) ---
from pathlib import Path
from typing import Dict
import uuid
from fastapi import Request, BackgroundTasks, Response

# Optional reranker integration (ADR-013). The ledger (logging) and serving modules are
# imported SEPARATELY so an older msa_ranker that ships `ledger` but not yet `serving`
# keeps event logging working — serving simply stays unavailable (learning-to-rank off).
try:
    from msa_ranker.ledger import LedgerWriter as _LedgerWriter
except ModuleNotFoundError as _exc:
    _LedgerWriter = None
    if (_exc.name or "").split(".")[0] != "msa_ranker":
        logger.warning("msa_ranker present but a dependency is missing; ranker logging disabled: {}", _exc)
except Exception as _exc:  # present but broken/incompatible → surface it, but stay up
    _LedgerWriter = None
    logger.warning("msa_ranker present but failed to import; ranker logging disabled: {}", _exc)

try:
    from msa_ranker import serving as _ranker_serving
except ModuleNotFoundError as _exc:
    _ranker_serving = None
    if (_exc.name or "").split(".")[0] != "msa_ranker":
        logger.warning("msa_ranker present but a dependency is missing; learned ranking disabled: {}", _exc)
except Exception as _exc:
    _ranker_serving = None
    logger.warning("msa_ranker present but failed to import; learned ranking disabled: {}", _exc)


def _append_search_events(writer, search_id, q, query_context, results, model_version=None) -> None:
    """Best-effort: append the search + shown events for a completed search
    (runs as a post-response BackgroundTask; never affects the response)."""
    try:
        # If feature extraction was unavailable (msa_ranker present but broken), results
        # carry features=None. Logging shown rows with empty features would corrupt the
        # training dataset — skip logging entirely instead (#B).
        if results and results[0].get("features") is None:
            logger.warning("ranker features unavailable; skipping event logging to avoid empty rows")
            return
        ctx = {
            "people": query_context.get("inferred_person_ids", []),
            "date_intent": query_context.get("date_filter"),
            "visual_tokens": query_context.get("visual_tokens", []),
        }
        _served = bool(query_context.get("ltr_served"))
        writer.append_search(
            search_id=search_id, user_id="default", query=q, ctx=ctx,
            flag_on=_served, model_version=(model_version if _served else None), k=len(results),
        )
        rows = [
            {
                "media_id": str(r.get("id")),
                "position": i,
                "score": r.get("score") or 0.0,
                "heuristic_score": (
                    r.get("heuristic_score")
                    if r.get("heuristic_score") is not None
                    else (r.get("score") or 0.0)
                ),
                "features": r.get("features") or {},
            }
            for i, r in enumerate(results)
        ]
        writer.append_shown(search_id=search_id, rows=rows)
    except Exception as exc:  # never let logging surface to the user
        logger.warning("ranker event append failed (best-effort): {}", exc)

def _get_config(request: Request):
    """Return current config from app.state if set, else load from settings.

    Tests using create_app(config_override=...) can inject a config object that will
    be returned here, ensuring DB isolation and predictable behavior.
    """
    cfg = getattr(request.app.state, "S", None) or getattr(request.app.state, "config", None)
    if cfg is not None:
        return cfg
    # Fallback to module-global S if available
    try:
        if 'S' in globals() and globals().get('S') is not None:
            return globals()['S']
    except Exception:
        pass
    # As a last resort, load config once and attach to app.state to avoid repeated loads
    try:
        from msa_settings import load_config as _lc
        loaded = _lc()
        try:
            setattr(request.app.state, 'S', loaded)
        except Exception:
            pass
        return loaded
    except Exception:
        raise HTTPException(status_code=500, detail="Server configuration not available")

def _source_map() -> Dict[str, Path]:
    """Cache and return mapping of source_name -> base path for enabled sources."""
    global S
    try:
        cfg = S if 'S' in globals() else None
    except Exception:
        cfg = None
    mapping: Dict[str, Path] = {}
    try:
        if cfg and getattr(cfg, 'media_sources', None):
            for s in cfg.media_sources:
                try:
                    if getattr(s, 'enabled', True) and getattr(s, 'path', None):
                        mapping[s.name] = Path(resolve_for_access(str(s.path))).resolve()
                except Exception:
                    continue
    except Exception:
        pass
    return mapping

def _resolve_path(db_path: str | None, source_name: str | None, rel_path: str | None) -> str | None:
    """Reconstruct abs path when possible, else return db_path."""
    if source_name and rel_path:
        base = _source_map().get(source_name)
        if base is not None:
            try:
                return str((base / rel_path).resolve())
            except Exception:
                pass
    return db_path

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Set app.state._ready after startup completes; clear on shutdown."""
    app.state._ready = False
    lock_path = None
    try:
        # Acquire single-instance lock (skipped in test/override contexts)
        if not getattr(app.state, "_skip_instance_lock", False):
            try:
                from msa_settings import load_config as _lc, acquire_instance_lock
                _cfg = getattr(app.state, "S", None) or _lc()
                lock_path = Path(_cfg.log_dir) / "msa-api.lock"
                acquire_instance_lock(lock_path, "Media Search Agent API")
                app.state._lock_path = lock_path
            except SystemExit:
                raise
            except Exception as _exc:
                logger.warning("Instance lock unavailable: {}", _exc)
        # Kick off model setup in the background immediately at startup.
        # This runs regardless of whether any client connects, so headless
        # deployments get the same first-launch behaviour as the browser UI.
        _startup_cfg = getattr(app.state, "S", None)
        if _startup_cfg is None:
            try:
                from msa_settings import load_config as _lc
                _startup_cfg = _lc()
            except Exception as _exc:
                logger.warning("Could not resolve config for model setup: {}", _exc)
        _models_dir = getattr(_startup_cfg, 'models_dir', None) if _startup_cfg is not None else None
        if _models_dir is not None:
            _setup_models.get_manager().start_if_needed(_models_dir)
        # Reranker event ledger (ADR-012/013): build a best-effort LedgerWriter onto
        # app.state when msa_ranker is installed and event_logging is on. Absent package
        # or any failure → no writer → MSA behaves exactly as today.
        app.state.ledger_writer = None
        try:
            if _LedgerWriter is not None and _startup_cfg is not None:
                _rk = getattr(_startup_cfg, "ranker", None)
                _logging_on = getattr(_rk, "event_logging", True) if _rk else True
                if _logging_on:
                    # Config resolution (_resolve_data_paths) normally sets ranker.ledger_dir
                    # to <data_dir>/ranker-ledger. This fallback only fires for configs built
                    # without that resolution; keep it in the DATA area (alongside index/),
                    # never logs/ (spec 01 — the ledger holds durable training labels).
                    _ldir = Path(
                        (getattr(_rk, "ledger_dir", None) if _rk else None)
                        or (_startup_cfg.index_dir.parent / "ranker-ledger")
                    )
                    app.state.ledger_writer = _LedgerWriter(_ldir, event_logging=True)
                    logger.info("Ranker event ledger active at {}", _ldir)
        except Exception as _exc:  # pragma: no cover
            logger.warning("Ranker ledger init failed (ranking telemetry off): {}", _exc)
            app.state.ledger_writer = None
        # Learned reranker (spec 06): resolve the model ONCE at startup, GUARDED. The gate
        # (gate_ok) can pass yet load() race a half-written rename, so the whole thing is
        # wrapped — a failure sets ranker=None and MSA still starts on the heuristic
        # (AC-06.8, INV-3). Master flag default OFF ⇒ no model loaded unless asked.
        app.state.ranker = None
        app.state.ranker_model_id = None
        try:
            _rk = getattr(_startup_cfg, "ranker", None) if _startup_cfg is not None else None
            if (
                _ranker_serving is not None
                and _rk is not None
                and getattr(_rk, "enable_learning_to_rank", False)
                and getattr(_rk, "ltr_model_dir", None)
            ):
                _mdir = Path(_rk.ltr_model_dir).expanduser()
                # `Ranker.load` RE-VALIDATES the full gate (beats_baseline + feature-version
                # + sha) on the exact bytes it reads, so even if the dir is swapped between
                # gate_ok() and load() a gate-failing model is never served — load raises and
                # we fall back. Deployment is a manual, atomic write-temp-rename step
                # (ADR-011), not racing startup.
                if _ranker_serving.gate_ok(_mdir):
                    app.state.ranker = _ranker_serving.Ranker.load(_mdir)
                    # Record the served model's id (from the manifest) so learned-search
                    # events carry it — impressions/opens stay attributable per model. The id
                    # is best-effort telemetry; serving correctness does not depend on it.
                    try:
                        _mf = json.loads((_mdir / "manifest.json").read_text())
                        app.state.ranker_model_id = _mf.get("model_id")
                    except Exception:
                        app.state.ranker_model_id = None
                    logger.info(
                        "Learned ranker active from {} (model_id={})",
                        _mdir,
                        app.state.ranker_model_id,
                    )
                else:
                    logger.info("Learned ranker gate closed at {} — using heuristic", _mdir)
        except Exception as _exc:
            logger.warning("Ranker load failed at startup — using heuristic: {}", _exc)
            app.state.ranker = None
            app.state.ranker_model_id = None
        # Startup complete — mark the app ready for health checks
        app.state._ready = True
        logger.info("App startup complete - status: ready")
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover
        logger.error("App startup error: {}", exc)
        # Leave _ready=False so /health correctly reports not-ready
    yield
    app.state._ready = False
    try:
        _lp = getattr(app.state, "_lock_path", None)
        if _lp:
            from msa_settings import release_instance_lock
            release_instance_lock(Path(_lp))
    except Exception as _exc:
        logger.warning("Failed to release instance lock: {}", _exc)


def create_app(config_override=None, query_engine_override=None, reset_dependencies: bool = False) -> FastAPI:
    """Application factory for tests & production.

    Why: Existing tests previously relied on monkeypatch + module re-import to
    override configuration, which was brittle and caused production DB
    contamination. This factory enables passing an in-memory config object and
    optional query_engine for clean, isolated tests.

    Parameters:
      config_override: Object returned instead of load_config() when endpoints call it.
      query_engine_override: Instance to return from get_query_engine dependency.
      reset_dependencies: If True, clear any previous dependency overrides to avoid leakage between tests.

    Notes:
      - Endpoints still call load_config on-demand; we monkeypatch the symbol in this module's globals.
      - For performance we reuse the same route functions; we just create a new FastAPI app and register them.
      - Static directories use override paths if present on config (thumb_dir, face_thumb_dir) else defaults.
    """
    # Patch load_config if override supplied (for any legacy code paths)
    if config_override is not None:
        def _mock_load_config(*_args, **_kwargs):  # mimic signature
            return config_override
        globals()['load_config'] = _mock_load_config  # type: ignore
    # Use existing global app if present (keeps route registrations), else create a new one
    app_instance = globals().get('app') if isinstance(globals().get('app'), FastAPI) else None
    if app_instance is None:
        app_instance = FastAPI(title="Media Search API", version=_APP_VERSION, lifespan=_lifespan)
        # Save into globals so subsequent decorators bind to this instance
        globals()['app'] = app_instance
        app_instance.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        # Mount static directories — use platform-resolved paths from loaded config when available.
        # Falls back to repo-relative dev paths so tests and dev mode work without a full install.
        _dev_thumbs     = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../data/thumbnails'))
        _dev_face_thumbs = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../data/face_thumbnails'))
        _loaded_cfg = globals().get('S')
        THUMBNAILS_DIR_LOCAL  = str(getattr(_loaded_cfg, 'thumb_dir',      _dev_thumbs))      or _dev_thumbs
        FACE_THUMBS_DIR_LOCAL = str(getattr(_loaded_cfg, 'face_thumb_dir', _dev_face_thumbs)) or _dev_face_thumbs
        for d in (THUMBNAILS_DIR_LOCAL, FACE_THUMBS_DIR_LOCAL):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        app_instance.mount("/thumbnails", StaticFiles(directory=THUMBNAILS_DIR_LOCAL), name="thumbnails")
        app_instance.mount("/face_thumbnails", StaticFiles(directory=FACE_THUMBS_DIR_LOCAL), name="face_thumbnails")

        # Note: React SPA serving is done via a catch-all route registered at module level
        # (after all API routes) so that API routes always take priority. See end of file.

    # If override config provides custom dirs and mounts already exist, leave as-is to avoid remount conflicts
    # Also attach the override config onto the app state so request handlers can fetch it via _get_config
    if config_override is not None:
        try:
            setattr(app_instance.state, "S", config_override)
            setattr(app_instance.state, "config", config_override)  # alt key used by some code paths
            setattr(app_instance.state, "_skip_instance_lock", True)  # no lock in test contexts
        except Exception:
            pass
    else:
        # Attach module-global S if available so endpoints don't reload config per request
        try:
            if 'S' in globals() and globals().get('S') is not None:
                setattr(app_instance.state, 'S', globals()['S'])
        except Exception:
            pass
    if reset_dependencies:
        app_instance.dependency_overrides.clear()
    if query_engine_override is not None:
        # Override dependency for get_query_engine
        from msa_apps.search_api import deps as _deps
        def _override_qe():
            return query_engine_override
        app_instance.dependency_overrides[_deps.get_query_engine] = _override_qe
    return app_instance

# Backwards-compatible global app instance for production usage
app = create_app()
@app.get("/health")
def health(request: Request):
    ready = getattr(request.app.state, "_ready", False)
    return {"status": "ready" if ready else "starting"}
@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest, request: Request, tasks: BackgroundTasks, qe = Depends(get_query_engine)):
    filters = req.filters.dict() if req.filters else None
    logger.info(f"API /search q='{req.q}' filters={filters}")
    # Pass the startup-loaded, gated ranker (or None) through to the engine for this request
    # — None ⇒ heuristic order, byte-identical to today (INV-3, spec 06). Threaded as a
    # parameter (not shared engine state) so concurrent requests never race.
    results = qe.search(req.q, filters=filters, ranker=getattr(request.app.state, "ranker", None))
    query_context = getattr(results, "query_context", {})
    # Enrich results with GPS from SQLite
    try:
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if db_path.exists() and results:
            ids = [str(r.get("id")) for r in results if r.get("id") is not None]
            # Deduplicate ids and guard against empty list
            uniq = sorted(set(ids))
            if uniq:
                # Build parameterized IN clause safely
                placeholders = ",".join(["?"] * len(uniq))
                sql = f"SELECT media_id, gps_lat, gps_lon, place, ts_utc FROM media WHERE media_id IN ({placeholders})"
                conn = connect_readonly(db_path)
                try:
                    cur = conn.execute(sql, tuple(uniq))
                    meta_map = {}
                    for row in cur.fetchall():
                        mid, lat, lon, place, ts_utc = row
                        meta_map[str(mid)] = (lat, lon, place, ts_utc)
                finally:
                    conn.close()
                for r in results:
                    mid = str(r.get("id"))
                    if mid in meta_map:
                        lat, lon, place, ts_utc = meta_map[mid]
                        if r.get("gps_lat") is None:
                            r["gps_lat"] = lat
                        if r.get("gps_lon") is None:
                            r["gps_lon"] = lon
                        if place is not None and not r.get("place"):
                            r["place"] = place
                        if not r.get("date") and ts_utc:
                            r["date"] = ts_utc
    except Exception as e:
        logger.warning(f"GPS enrichment failed: {e}")
    logger.info(f"API /search results={len(results)}")
    # Reranker telemetry (ADR-009/012): correlate opens to this search, append events
    # AFTER the response (BackgroundTask) so logging never adds latency or failure.
    search_id = str(uuid.uuid4())
    writer = getattr(request.app.state, "ledger_writer", None)
    if writer is not None:
        _model_id = getattr(request.app.state, "ranker_model_id", None)
        tasks.add_task(
            _append_search_events, writer, search_id, req.q, query_context, results, _model_id
        )
    # Ensure id is always a string for Pydantic validation
    return SearchResponse(
        results=[SearchItem(**{**r, "id": str(r["id"])}) for r in results],
        search_id=search_id,
    )


@app.post("/track/open", status_code=204)
def track_open(req: TrackOpenRequest, request: Request):
    """Record that a search result was opened (the label). Fire-and-forget: returns
    204 unconditionally, and never depends on the ranker being present (INV-9/ADR-009)."""
    writer = getattr(request.app.state, "ledger_writer", None)
    if writer is not None and req.search_id and req.media_id:
        try:
            writer.append_open(search_id=req.search_id, media_id=req.media_id, user_id="default")
        except Exception as exc:  # telemetry must never fail the request (INV-9)
            logger.warning("track_open append failed (best-effort): {}", exc)
    return Response(status_code=204)

# --- Media library listing ---
@app.get("/media")
def list_media(
    request: Request,
    limit: int = 100,
    offset: int = 0,
    media_type: Optional[str] = None,
    people: Optional[str] = None,
    people_mode: Optional[str] = "any",
    tags: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    place: Optional[str] = None,
    sort_by: Optional[str] = "date",
    sort_order: Optional[str] = "desc",
):
    """Browse media library with filters & pagination (isolated via request config)."""
    try:
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            return {"items": [], "count": 0, "limit": limit, "offset": offset}

        # Build SQL dynamically with safe parameters
        where = ["m.deleted=0"]
        params: List[object] = []

        # Media type filter using mime ('image/*' or 'video/*')
        if media_type in ("image", "video"):
            where.append("m.mime LIKE ?")
            params.append(f"{media_type}/%")

        # Date range filters (ts_utc stored as ISO string)
        if date_from:
            where.append("m.ts_utc >= ?")
            params.append(date_from)
        if date_to:
            where.append("m.ts_utc <= ?")
            params.append(date_to)

        # Place filter (simple LIKE)
        if place:
            where.append("m.place LIKE ?")
            params.append(f"%{place}%")

        # People filter using labeled face/person associations.
        people_list: List[str] = []
        if people:
            seen_people: set[str] = set()
            for raw_name in people.split(","):
                name = raw_name.strip()
                if not name or name in seen_people:
                    continue
                seen_people.add(name)
                people_list.append(name)

        people_mode = (people_mode or "any").lower()
        if people_mode not in {"any", "all", "only"}:
            people_mode = "any"

        if people_list:
            placeholders = ",".join(["?"] * len(people_list))
            if people_mode == "all":
                where.append(
                    f"""
                    m.media_id IN (
                        SELECT f.media_id
                        FROM face f
                        JOIN person p ON p.person_id = f.person_id
                        WHERE p.name IN ({placeholders})
                        GROUP BY f.media_id
                        HAVING COUNT(DISTINCT p.name) = ?
                    )
                    """
                )
                params.extend(people_list)
                params.append(len(people_list))
            elif people_mode == "only":
                where.append(
                    f"""
                    m.media_id IN (
                        SELECT f.media_id
                        FROM face f
                        JOIN person p ON p.person_id = f.person_id
                        WHERE p.name IS NOT NULL
                        GROUP BY f.media_id
                        HAVING COUNT(DISTINCT CASE WHEN p.name IN ({placeholders}) THEN p.name END) = ?
                           AND COUNT(DISTINCT p.name) = ?
                    )
                    """
                )
                params.extend(people_list)
                params.append(len(people_list))
                params.append(len(people_list))
            else:
                # "any" mode — match media that has at least one face for any
                # of the named people. Use IN (SELECT ...) rather than a
                # correlated EXISTS so SQLite resolves the inner set once
                # (driven by idx_face_person and the implicit UNIQUE index on
                # person.name) instead of re-evaluating per outer media row.
                where.append(
                    f"""
                    m.media_id IN (
                        SELECT f.media_id
                        FROM face f
                        JOIN person p ON p.person_id = f.person_id
                        WHERE p.name IN ({placeholders})
                    )
                    """
                )
                params.extend(people_list)

        # Tags filter (match ANY of provided tags)
        join_tags = False
        tag_list: List[str] = []
        if tags:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            join_tags = True

        # Base FROM and optional JOINs
        from_sql = "FROM media m"
        if join_tags:
            from_sql += " JOIN media_tag mt ON mt.media_id = m.media_id JOIN tag t ON t.tag_id = mt.tag_id"

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        # Count query
        count_sql = f"SELECT COUNT(DISTINCT m.media_id) {from_sql}{where_sql}"
        # Apply tag condition if present (ANY)
        if tag_list:
            # Use t.name IN (?,...)
            placeholders = ",".join(["?"] * len(tag_list))
            if where_sql:
                count_sql += f" AND t.name IN ({placeholders})"
            else:
                count_sql += f" WHERE t.name IN ({placeholders})"

        # Data query
        data_sql = f"""
            SELECT DISTINCT m.media_id, m.path, m.source_name, m.rel_path, m.ts_utc, m.mime, m.duration, m.gps_lat, m.gps_lon, m.place
            {from_sql}
            {where_sql}
        """
        if tag_list:
            placeholders = ",".join(["?"] * len(tag_list))
            if where_sql:
                data_sql += f" AND t.name IN ({placeholders})"
            else:
                data_sql += f" WHERE t.name IN ({placeholders})"
        
        # Apply sorting
        sort_column = "m.ts_utc" if sort_by == "date" else "m.path"
        sort_direction = "ASC" if sort_order and sort_order.lower() == "asc" else "DESC"
        data_sql += f" ORDER BY {sort_column} {sort_direction}, m.media_id LIMIT ? OFFSET ?"

        # Execute queries
        conn = connect_readonly(db_path)
        try:
            cur = conn.execute(count_sql, tuple(params + tag_list))
            total = int(cur.fetchone()[0])

            cur = conn.execute(data_sql, tuple(params + tag_list + [int(limit), int(offset)]))
            rows = cur.fetchall()
        finally:
            conn.close()

        tag_map: dict[str, list[str]] = {}
        if rows:
            media_ids = [str(media_id) for media_id, *_ in rows]
            placeholders = ",".join(["?"] * len(media_ids))
            conn = connect_readonly(db_path)
            try:
                cur = conn.execute(
                    f"""
                    SELECT mt.media_id, t.name
                    FROM media_tag mt
                    JOIN tag t ON t.tag_id = mt.tag_id
                    WHERE mt.media_id IN ({placeholders})
                    ORDER BY t.name COLLATE NOCASE ASC
                    """,
                    tuple(media_ids),
                )
                for media_id, tag_name in cur.fetchall():
                    if not tag_name:
                        continue
                    tag_map.setdefault(str(media_id), []).append(str(tag_name))
                # Supplement with keyframe tags for videos that have none in media_tag
                untagged = [mid for mid in media_ids if mid not in tag_map]
                if untagged:
                    import json as _json
                    kf_placeholders = ",".join(["?"] * len(untagged))
                    kf_cur = conn.execute(
                        f"SELECT video_id, tags FROM video_keyframes WHERE video_id IN ({kf_placeholders}) AND tags IS NOT NULL",
                        tuple(untagged),
                    )
                    for video_id, tags_json in kf_cur.fetchall():
                        try:
                            for t in _json.loads(tags_json):
                                tag_map.setdefault(str(video_id), [])
                                if t not in tag_map[str(video_id)]:
                                    tag_map[str(video_id)].append(t)
                        except Exception:
                            pass
                    for mid in tag_map:
                        tag_map[mid] = sorted(tag_map[mid], key=str.lower)
            finally:
                conn.close()

        # Format items
        items = []
        for media_id, path, source_name, rel_path, ts_utc, mime, duration, gps_lat, gps_lon, place in rows:
            path = _resolve_path(path, source_name, rel_path)
            typ = None
            if isinstance(mime, str):
                if mime.startswith("image/"): typ = "image"
                elif mime.startswith("video/"): typ = "video"
            items.append({
                "id": str(media_id),
                "path": display_path(path),
                "date": ts_utc,
                "type": typ,
                "duration": duration,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
                "place": place,
                "tags": tag_map.get(str(media_id), []),
            })

        return {"items": items, "count": total, "limit": limit, "offset": offset}
    except HTTPException:
        raise
    except Exception as e:
        import sqlite3 as _sqlite3
        if isinstance(e, _sqlite3.OperationalError) and "no such table" in str(e):
            return {"items": [], "count": 0, "limit": limit, "offset": offset}
        logger.error(f"/media listing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

# --- Single media item metadata ---
@app.get("/media/{media_id}/info")
def get_media_info(media_id: str, request: Request):
    """Return metadata for a single media item (date, GPS, place, type, path)."""
    try:
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        conn = connect_readonly(db_path)
        try:
            cur = conn.execute(
                """SELECT media_id, path, source_name, rel_path, ts_utc, mime,
                          duration, gps_lat, gps_lon, place
                   FROM media WHERE media_id=? AND deleted=0""",
                (media_id,)
            )
            row = cur.fetchone()
            tag_cur = conn.execute(
                """
                SELECT t.name
                FROM media_tag mt
                JOIN tag t ON t.tag_id = mt.tag_id
                WHERE mt.media_id=?
                ORDER BY t.name COLLATE NOCASE ASC
                """,
                (media_id,),
            )
            tags = [str(name) for (name,) in tag_cur.fetchall() if name]
            if not tags:
                # Videos store tags per keyframe rather than in media_tag
                import json as _json
                kf_cur = conn.execute(
                    "SELECT tags FROM video_keyframes WHERE video_id=? AND tags IS NOT NULL",
                    (media_id,),
                )
                kf_tags: set[str] = set()
                for (tags_json,) in kf_cur.fetchall():
                    try:
                        kf_tags.update(_json.loads(tags_json))
                    except Exception:
                        pass
                tags = sorted(kf_tags, key=str.lower)
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Media not found")
        mid, path, source_name, rel_path, ts_utc, mime, duration, gps_lat, gps_lon, place = row
        path = _resolve_path(path, source_name, rel_path)
        typ = None
        if isinstance(mime, str):
            if mime.startswith("image/"): typ = "image"
            elif mime.startswith("video/"): typ = "video"
        return {"id": str(mid), "path": display_path(path), "type": typ, "date": ts_utc,
                "gps_lat": gps_lat, "gps_lon": gps_lon, "place": place, "duration": duration, "tags": tags}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/media/{media_id}/info failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

# --- Video file serving ---
@app.get("/videos/{media_id}")
def get_video(media_id: str, request: Request):
    """
    Serve a video file by media_id, resolving the absolute path from SQLite.
    Trust boundary: serves any non-deleted indexed file present on disk.
    Source-root restriction was intentionally removed so files indexed under
    renamed/removed media sources remain accessible. The SQLite DB is the
    authorisation boundary for this local-first application.
    """
    try:
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        conn = connect_readonly(db_path)
        try:
            cur = conn.execute("SELECT path, source_name, rel_path FROM media WHERE media_id=? AND deleted=0", (media_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Media not found")
        db_path_str, source_name, rel_path = row
        # Try to resolve using source_name first (if config name matches)
        resolved_path = _resolve_path(db_path_str, source_name, rel_path)
        file_path = Path(resolve_for_access(resolved_path or db_path_str)).resolve()
        # Security: file must be present in the index (deleted=0, confirmed above).
        # Source paths can change over time; blocking by current config would break
        # access to files indexed under old sources. Serving any indexed file is safe.
        if not file_path.exists() and db_path_str and str(file_path) != str(Path(resolve_for_access(db_path_str)).resolve()):
            # Reconstruction from source_name+rel_path produced a path that doesn't exist;
            # fall back to the absolute path stored at index time.
            file_path = Path(resolve_for_access(db_path_str)).resolve()
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")
        # Let Starlette serve the file; Range requests are handled at client level (full file otherwise)
        return FileResponse(str(file_path), media_type="video/mp4")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/videos/{media_id} failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- Image file serving ---
@app.get("/images/{media_id}")
def get_image(media_id: str, request: Request):
    """
    Serve an image file by media_id, resolving the absolute path from SQLite.
    Trust boundary: serves any non-deleted indexed file present on disk.
    Source-root restriction was intentionally removed so files indexed under
    renamed/removed media sources remain accessible. The SQLite DB is the
    authorisation boundary for this local-first application.
    Converts HEIC to JPEG on-the-fly for browser compatibility.
    """
    try:
        import mimetypes
        import tempfile
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        conn = connect_readonly(db_path)
        try:
            cur = conn.execute("SELECT path, source_name, rel_path FROM media WHERE media_id=? AND deleted=0", (media_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Media not found")
        db_path_str, source_name, rel_path = row
        # Try to resolve using source_name first (if config name matches)
        resolved_path = _resolve_path(db_path_str, source_name, rel_path)
        file_path = Path(resolve_for_access(resolved_path or db_path_str)).resolve()
        # Security: file must be present in the index (deleted=0, confirmed above).
        # Source paths can change over time; blocking by current config would break
        # access to files indexed under old sources. Serving any indexed file is safe.
        if not file_path.exists() and db_path_str and str(file_path) != str(Path(resolve_for_access(db_path_str)).resolve()):
            # Reconstruction from source_name+rel_path produced a path that doesn't exist;
            # fall back to the absolute path stored at index time.
            file_path = Path(resolve_for_access(db_path_str)).resolve()
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")

        # Convert HEIC to JPEG on-the-fly for browser compatibility
        if file_path.suffix.lower() == '.heic':
            try:
                from PIL import Image, ImageOps  # type: ignore
                from fastapi.responses import StreamingResponse
                import pillow_heif  # type: ignore
                import io
                pillow_heif.register_heif_opener()
                with Image.open(file_path) as pil_img:
                    transposed = ImageOps.exif_transpose(pil_img)
                    buf = io.BytesIO()
                    # Ensure RGB for JPEG
                    to_save = transposed.convert('RGB') if hasattr(transposed, 'convert') else transposed  # type: ignore
                    to_save.save(buf, 'JPEG', quality=90)  # type: ignore
                    buf.seek(0)
                    return StreamingResponse(buf, media_type="image/jpeg", headers={
                        "Content-Disposition": "inline",
                        "Cache-Control": "no-cache"
                    })
            except Exception as e:
                logger.error(f"HEIC conversion failed for {file_path}: {e}")
                # Fallback attempt to PNG
                try:
                    from PIL import Image, ImageOps  # type: ignore
                    from fastapi.responses import StreamingResponse
                    import io
                    with Image.open(file_path) as pil_img:
                        transposed = ImageOps.exif_transpose(pil_img)
                        buf = io.BytesIO()
                        to_save = transposed.convert('RGB') if hasattr(transposed, 'convert') else transposed  # type: ignore
                        to_save.save(buf, 'PNG')  # type: ignore
                        buf.seek(0)
                        return StreamingResponse(buf, media_type="image/png", headers={
                            "Content-Disposition": "inline",
                            "Cache-Control": "no-cache"
                        })
                except Exception as e2:
                    logger.error(f"PNG fallback also failed for {file_path}: {e2}")
                    raise HTTPException(status_code=500, detail="Failed to convert HEIC image")
        
        # NOTE: EXIF orientation is now handled during indexing (pipeline.py) and thumbnail generation.
        # Face bboxes are calculated on the EXIF-corrected orientation, so we must NOT apply
        # exif_transpose here or face crops will appear incorrectly rotated.
        # The browser should respect the EXIF orientation tag when displaying the original image.
        
        # Guess content type; default to image/jpeg if unknown
        mime, _ = mimetypes.guess_type(str(file_path))
        media_type = mime or "image/jpeg"
        
        # Always set Content-Disposition to inline so browser displays instead of downloads
        return FileResponse(str(file_path), media_type=media_type, headers={
            "Content-Disposition": "inline",
            "Cache-Control": "no-cache"
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/images/{media_id} failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/videos/{media_id}/shots", response_model=VideoShotsResponse)
def get_video_shots(media_id: str, request: Request):
    """
    Get shot boundaries and keyframe metadata for a specific video.
    Returns shot timings, duration, and associated keyframes with their timestamps and tags.
    """
    try:
        import json
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        
        conn = connect_readonly(db_path)
        try:
            # Get video metadata
            media_cur = conn.execute(
                "SELECT path, source_name, rel_path, duration FROM media WHERE media_id=? AND deleted=0",
                (media_id,)
            )
            media_row = media_cur.fetchone()
            if not media_row:
                raise HTTPException(status_code=404, detail="Video not found")
            
            db_path_str, source_name, rel_path, total_duration = media_row
            video_path = _resolve_path(db_path_str, source_name, rel_path) or db_path_str
            
            # Get shots
            shots_cur = conn.execute(
                """
                SELECT shot_index, t_start, t_end, is_synthetic
                FROM shots
                WHERE video_id = ?
                ORDER BY shot_index
                """,
                (media_id,)
            )
            shots_data = shots_cur.fetchall()
            
            if not shots_data:
                raise HTTPException(status_code=404, detail="No shots found for this video")
            
            # Get keyframes grouped by shot
            kf_cur = conn.execute(
                """
                SELECT shot_index, kf_index, timestamp, tags,
                       gps_lat, gps_lon, gps_alt, gps_datetime_utc, gps_fix, gps_source, place
                FROM video_keyframes
                WHERE video_id = ?
                ORDER BY shot_index, kf_index
                """,
                (media_id,)
            )
            
            # Group keyframes by shot
            from collections import defaultdict
            keyframes_by_shot = defaultdict(list)
            for shot_idx, kf_idx, timestamp, tags_json, gps_lat, gps_lon, gps_alt, gps_datetime_utc, gps_fix, gps_source, place in kf_cur.fetchall():
                tags = json.loads(tags_json) if tags_json else []
                keyframes_by_shot[shot_idx].append({
                    "kf_index": kf_idx,
                    "timestamp": timestamp,
                    "tags": tags,
                    "gps_lat": gps_lat,
                    "gps_lon": gps_lon,
                    "gps_alt": gps_alt,
                    "gps_datetime_utc": gps_datetime_utc,
                    "gps_fix": gps_fix,
                    "gps_source": gps_source,
                    "place": place,
                })
            
            # Build shot info
            shots = []
            for shot_idx, t_start, t_end, is_synthetic in shots_data:
                shots.append(ShotInfo(
                    shot_index=shot_idx,
                    t_start=t_start,
                    t_end=t_end,
                    duration=t_end - t_start,
                    is_synthetic=bool(is_synthetic),
                    keyframes=keyframes_by_shot.get(shot_idx, [])
                ))
            
            return VideoShotsResponse(
                video_id=media_id,
                path=video_path,
                total_duration=total_duration,
                shots=shots
            )
        
        finally:
            conn.close()
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/videos/{media_id}/shots failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/media/{media_id}/faces")
def get_media_faces(media_id: str, request: Request):
    """
    Get all detected faces in a specific photo or video.
    Returns face bounding boxes, confidence, person identities, and metadata.
    
    Example response:
    {
      "media_id": "abc123...",
      "faces": [
        {
          "face_id": "abc123:f0",
          "bbox": [0.3, 0.2, 0.15, 0.2],  // normalized x, y, w, h
          "confidence": 0.95,
          "person_id": "p456",
          "person_name": "Alice",
          "gender": "F",
          "age": 28,
          "shot_index": null,  // null for images, int for videos
          "kf_index": null
        }
      ]
    }
    """
    try:
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        
        db = SQLiteStore(db_path)
        faces = db.get_media_faces(media_id)
        db.close()
        
        return {
            "media_id": media_id,
            "faces": faces
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/media/{media_id}/faces failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/faces")
def list_faces(request: Request, limit: int = 200, offset: int = 0, labeled: Optional[str] = None):
    """
    List detected faces with minimal metadata and thumbnail URL.
    Returns one face per labeled person (highest confidence) and all unknown faces.
    Labeled faces are returned first, then unknown faces.
    Query param `labeled`: "known" | "unknown" | omit/null for all.
    """
    try:
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")

        if labeled is not None and labeled not in ("known", "unknown", "all"):
            raise HTTPException(status_code=400, detail="labeled must be 'known', 'unknown', or 'all'")

        db = SQLiteStore(db_path)
        # Get unique faces per person with pagination
        faces_list = db.get_unique_faces_per_person(limit=limit, offset=offset, labeled=labeled)
        total_count = db.count_unique_faces_per_person(labeled=labeled)
        
        # Format for API response
        faces = []
        for row in faces_list:
            face_id = row.get("face_id")
            # Face thumbnail file name sanitizes ':' -> '_' and uses .jpg
            safe = face_id.replace(":", "_") if face_id else None
            thumb = f"/face_thumbnails/{safe}.jpg" if safe else None
            faces.append({
                "face_id": face_id,
                "media_id": row.get("media_id"),
                "path": row.get("path"),
                "gender": row.get("gender"),
                "age": row.get("age"),
                "person_id": row.get("person_id"),
                "person_name": row.get("person_name"),
                "thumbnail": thumb,
            })
        db.close()
        return {"faces": faces, "count": total_count, "offset": offset}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/faces failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

# --- Face search ---
@app.post("/faces/search", response_model=FaceSearchResponse)
def face_search(req: FaceSearchRequest, request: Request):
    """
    Search for similar faces given a known face_id.
    Reads the face embedding from the SQLite face_embedding table, then
    queries Qdrant for matches.
    """
    try:
        import numpy as np
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)

        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            # Don't let SQLiteStore silently create an empty DB on a
            # misconfigured path — surface the misconfiguration instead.
            raise HTTPException(status_code=500, detail="SQLite database not found")
        with SQLiteStore(db_path) as db:
            blob = db.get_face_embedding(req.face_id)
        if blob is None:
            raise HTTPException(status_code=404, detail="Face vector not found (reindex required)")
        v = np.frombuffer(blob, dtype=np.float32)

        client = get_shared_client()
        if client is None:
            raise HTTPException(status_code=503, detail="Qdrant unavailable (indexer running)")
        hits = client.search(collection_name=S.collections.face, query_vector=v.tolist(), limit=req.top_k)
        matches: list[FaceSearchMatch] = []
        for h in hits:
            payload = h.payload or {}
            matches.append(FaceSearchMatch(
                face_id=str(payload.get("face_id") or h.id),
                score=float(h.score),
                media_id=payload.get("media_id"),
                path=payload.get("path"),
                person_id=payload.get("person_id"),
                person_name=payload.get("person_name"),
                shot_index=payload.get("shot_index"),
                kf_index=payload.get("kf_index"),
            ))
        return FaceSearchResponse(matches=matches)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/faces/search failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

# ---------------- People & labeling endpoints ----------------
@app.post("/people", response_model=PersonOut)
def create_person(req: PersonCreate, request: Request):
    try:
        from pathlib import Path
        import sqlite3
        from msa_indexer.db.sqlite_store import SQLiteStore
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        db = SQLiteStore(db_path)
        try:
            p = db.create_person(req.name)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Person name already exists")
        finally:
            db.close()
        return PersonOut(person_id=p["person_id"], name=p["name"], face_count=0)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /people failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/people", response_model=PeopleListResponse)
def list_people(request: Request):
    try:
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            return PeopleListResponse(people=[])
        db = SQLiteStore(db_path)
        people = db.list_people()
        db.close()
        result = []
        for p in people:
            face_id = p.get("thumbnail_face_id")
            safe = face_id.replace(":", "_") if face_id else None
            thumb = f"/face_thumbnails/{safe}.jpg" if safe else None
            result.append(PersonOut(
                person_id=p["person_id"],
                name=p["name"],
                face_count=p.get("face_count", 0),
                thumbnail=thumb,
            ))
        return PeopleListResponse(people=result)
    except HTTPException:
        raise
    except Exception as e:
        import sqlite3 as _sqlite3
        if isinstance(e, _sqlite3.OperationalError) and "no such table" in str(e):
            return PeopleListResponse(people=[])
        logger.error(f"GET /people failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/tags", response_model=list[str])
def list_tags(request: Request):
    try:
        import sqlite3
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            return []

        conn = connect_readonly(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM tag WHERE name IS NOT NULL AND TRIM(name) != '' ORDER BY name COLLATE NOCASE ASC"
            )
            return [str(name) for (name,) in cur.fetchall() if name]
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        if isinstance(e, sqlite3.OperationalError) and "no such table" in str(e):
            return []
        logger.error(f"GET /tags failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.patch("/people/{person_id}", response_model=PersonOut)
def rename_person(person_id: str, req: PersonRename, request: Request):
    try:
        from pathlib import Path
        import sqlite3
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_sync import sync_person_rename
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        db = SQLiteStore(db_path)
        # Ensure person exists
        person = db.get_person(person_id)
        if not person:
            db.close()
            raise HTTPException(status_code=404, detail="Person not found")
        try:
            db.rename_person(person_id, req.name)
        except sqlite3.IntegrityError:
            db.close()
            raise HTTPException(status_code=409, detail="Person name already exists")
        person = db.get_person(person_id) or {"person_id": person_id, "name": req.name}
        # Face count after rename
        people = db.list_people()
        db.close()
        # Sync to Qdrant
        sync_person_rename(person_id, req.name, client=get_shared_client())
        cnt = next((p.get("face_count", 0) for p in people if p["person_id"] == person_id), 0)
        return PersonOut(person_id=person["person_id"], name=person["name"], face_count=cnt)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PATCH /people/{person_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/people/{target_id}/merge")
def merge_people(target_id: str, req: MergeRequest, request: Request):
    try:
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_sync import sync_person_merge
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        db = SQLiteStore(db_path)
        # Validate both exist
        src = db.get_person(req.source_id)
        tgt = db.get_person(target_id)
        if not src or not tgt:
            db.close()
            raise HTTPException(status_code=404, detail="Source or target person not found")
        target_name = tgt.get("name", "")
        try:
            reassigned = db.merge_people(req.source_id, target_id)
        except ValueError as ve:
            db.close()
            raise HTTPException(status_code=400, detail=str(ve))
        db.close()
        # Sync to Qdrant
        sync_person_merge(req.source_id, target_id, target_name, client=get_shared_client())
        return {"reassigned": reassigned, "target_id": target_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /people/{target_id}/merge failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/faces/{face_id}/label")
def label_face(face_id: str, req: FaceLabelRequest, request: Request):
    try:
        from pathlib import Path
        import sqlite3
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_sync import update_face_payload
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        db = SQLiteStore(db_path)
        person_id: Optional[str] = req.person_id
        person_name: Optional[str] = None
        # Create person if only name provided
        if not person_id and req.name:
            try:
                p = db.create_person(req.name)
                person_id = p["person_id"]
                person_name = p["name"]
            except sqlite3.IntegrityError:
                # Name exists: fetch existing
                existing = db.find_person_by_name(req.name)
                if not existing:
                    db.close()
                    raise HTTPException(status_code=409, detail="Person name already exists")
                person_id = existing["person_id"]
                person_name = existing["name"]
        if not person_id:
            db.close()
            raise HTTPException(status_code=400, detail="person_id or name required")
        # Assign in SQLite
        db.update_face_person(face_id, person_id)
        # Return info
        person = db.get_person(person_id)
        db.close()
        # Sync to Qdrant using the shared client (avoids second embedded client conflict)
        final_name = person_name or (person and person.get("name")) if person else None
        if not isinstance(final_name, str):
            final_name = None
        update_face_payload(face_id, person_id, final_name, client=get_shared_client())
        return {"face_id": face_id, "person_id": person_id, "person_name": final_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /faces/{face_id}/label failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/faces/label-batch")
def label_faces_batch(req: BulkLabelRequest, request: Request):
    """
    Label many faces with the same person in one request.
    Uses a single SQLite executemany + chunked Qdrant set_payload — handles 10k+ faces.
    Body: { face_ids: [...], person_id?: str, name?: str }
    """
    try:
        import sqlite3
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_sync import set_face_person_batch
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        if not req.face_ids:
            raise HTTPException(status_code=400, detail="face_ids must not be empty")

        db = SQLiteStore(db_path)
        person_id: Optional[str] = req.person_id
        person_name: Optional[str] = None

        # Resolve or create person
        if not person_id and req.name:
            try:
                p = db.create_person(req.name)
                person_id = p["person_id"]
                person_name = p["name"]
            except sqlite3.IntegrityError:
                existing = db.find_person_by_name(req.name)
                if not existing:
                    db.close()
                    raise HTTPException(status_code=409, detail="Person name conflict")
                person_id = existing["person_id"]
                person_name = existing["name"]
        if not person_id:
            db.close()
            raise HTTPException(status_code=400, detail="person_id or name required")

        # Fetch person name if not already resolved; validate person exists
        if not person_name:
            p = db.get_person(person_id)
            if not p:
                db.close()
                raise HTTPException(status_code=404, detail="Person not found")
            person_name = p.get("name")
        if not isinstance(person_name, str):
            person_name = None

        # Single-transaction SQLite update
        labeled = db.update_faces_person_batch(req.face_ids, person_id)
        db.close()

        # Single chunked Qdrant update (1000 points per call)
        qdrant_collection = getattr(getattr(S, 'collections', None), 'face', 'face_emb')
        set_face_person_batch(req.face_ids, person_id, person_name, collection=qdrant_collection, client=get_shared_client())

        return {"labeled": labeled, "person_id": person_id, "person_name": person_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /faces/label-batch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.delete("/faces/{face_id}/label")
def unlabel_face(face_id: str, request: Request):
    try:
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_sync import update_face_payload
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        db = SQLiteStore(db_path)
        db.clear_face_person(face_id)
        db.close()
        # Sync to Qdrant using the shared client
        update_face_payload(face_id, None, None, client=get_shared_client())
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DELETE /faces/{face_id}/label failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/faces/bulk_label")
def bulk_label(req: BulkLabelRequest, request: Request):
    try:
        from pathlib import Path
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_indexer.db.qdrant_sync import update_faces_payload_batch
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)
        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        db = SQLiteStore(db_path)
        updated = 0
        person_name: Optional[str] = None
        if req.person_id:
            # Get person name
            person = db.get_person(req.person_id)
            person_name = person.get("name") if person else None
            for fid in req.face_ids:
                db.update_face_person(fid, req.person_id)
                updated += 1
        else:
            for fid in req.face_ids:
                db.clear_face_person(fid)
                updated += 1
        db.close()
        # Batch sync to Qdrant
        qdrant_updates = [(fid, req.person_id, person_name) for fid in req.face_ids]
        update_faces_payload_batch(qdrant_updates, client=get_shared_client())
        return {"updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /faces/bulk_label failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/faces/{face_id}/suggestions", response_model=FaceSuggestionsResponse)
def get_face_suggestions(request: Request, face_id: str, top_k: int = 5):
    """
    Get person suggestions for a face based on similar faces in Qdrant.
    Returns top-N people ranked by number of similar faces and average score.
    """
    try:
        from collections import defaultdict
        import numpy as np
        from msa_indexer.db.sqlite_store import SQLiteStore
        from msa_query.storage.qdrant_client import get_shared_client
        S = _get_config(request)

        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        with SQLiteStore(db_path) as db:
            blob = db.get_face_embedding(face_id)
        if blob is None:
            raise HTTPException(status_code=404, detail="Face vector not found")
        v = np.frombuffer(blob, dtype=np.float32)

        # Query Qdrant for similar faces
        client = get_shared_client()
        if client is None:
            raise HTTPException(status_code=503, detail="Qdrant unavailable (indexer running)")
        hits = client.search(
            collection_name=S.collections.face,
            query_vector=v.tolist(),
            limit=50,  # Get more results to aggregate by person
        )

        # Aggregate by person_id
        from typing import cast
        person_scores: dict[str, dict] = defaultdict(lambda: {"scores": [], "name": None})
        for h in hits:
            payload = h.payload or {}
            pid = payload.get("person_id")
            if pid:  # Only consider labeled faces
                cast(list, person_scores[pid]["scores"]).append(float(h.score))
                if not person_scores[pid]["name"]:
                    person_scores[pid]["name"] = payload.get("person_name")

        # Rank by count and average score
        suggestions: List[PersonSuggestion] = []
        for pid, data in person_scores.items():
            scores = cast(list, data["scores"])
            pname = data["name"]
            if not scores:
                continue
            suggestions.append(PersonSuggestion(
                person_id=str(pid),
                person_name=str(pname) if pname else "Unknown",
                score=float(sum(scores) / len(scores)),
                face_count=len(scores)
            ))

        # Sort by face count (primary) and average score (secondary)
        suggestions.sort(key=lambda x: (-x.face_count, -x.score))
        suggestions = suggestions[:top_k]

        return FaceSuggestionsResponse(face_id=face_id, suggestions=suggestions)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GET /faces/{face_id}/suggestions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/faces/suggestions/batch", response_model=BatchFaceSuggestionsResponse)
def batch_face_suggestions(req: BatchFaceSuggestionsRequest, request: Request):
    """
    Batch suggestions for multiple faces in a single request.
    Loads vectors from the SQLite face_embedding table and queries Qdrant
    using a batch search when available. Returns a mapping of
    face_id -> suggestions and a list of missing face_ids.
    """
    try:
        from collections import defaultdict
        from typing import cast
        import numpy as np
        from msa_indexer.db.sqlite_store import SQLiteStore
        S = _get_config(request)

        db_path = Path(S.sqlite_path)
        if not db_path.exists():
            raise HTTPException(status_code=500, detail="SQLite database not found")
        # Load vectors for all requested face_ids from SQLite
        vectors: dict[str, object] = {}
        missing: list[str] = []
        with SQLiteStore(db_path) as db:
            for fid in req.face_ids:
                blob = db.get_face_embedding(fid)
                if blob is None:
                    missing.append(fid)
                else:
                    vectors[str(fid)] = np.frombuffer(blob, dtype=np.float32)

        if not vectors:
            return BatchFaceSuggestionsResponse(results={}, missing=missing)

        from msa_query.storage.qdrant_client import get_shared_client
        client = get_shared_client()
        if client is None:
            raise HTTPException(status_code=503, detail="Qdrant unavailable (indexer running)")

        # Prepare batch searches (request a bit more to aggregate per person)
        batch_limit = max(10, req.top_k * 10)
        searches = []
        face_order: list[str] = []
        for fid, vec in vectors.items():
            vec_list = np.asarray(vec, dtype="float32").tolist()
            searches.append({
                "collection_name": S.collections.face,
                "query_vector": cast("list[float]", vec_list),
                "limit": batch_limit,
            })
            face_order.append(fid)

        results_map: dict[str, BatchFaceSuggestionItem] = {}
        try:
            batch_results = client.search_batch(searches)  # type: ignore[attr-defined]
            iterable = zip(face_order, batch_results)
        except Exception:
            # Fallback: sequential search if batch API unavailable
            seq_hits = []
            face_order_seq: list[str] = []
            for fid, vec in vectors.items():
                vec_list = np.asarray(vec, dtype="float32").tolist()
                hits = client.search(collection_name=S.collections.face, query_vector=cast("list[float]", vec_list), limit=batch_limit)
                seq_hits.append(hits)
                face_order_seq.append(fid)
            iterable = zip(face_order_seq, seq_hits)

        for fid, hits in iterable:
            person_scores: dict[str, dict] = defaultdict(lambda: {"scores": [], "name": None})
            for h in hits:
                payload = h.payload or {}
                pid = payload.get("person_id")
                if pid:
                    cast(list, person_scores[pid]["scores"]).append(float(h.score))
                    if not person_scores[pid]["name"]:
                        person_scores[pid]["name"] = payload.get("person_name")
            suggestions: list[PersonSuggestion] = []
            for pid, data in person_scores.items():
                scores = cast(list, data["scores"])
                pname = data["name"]
                if not scores:
                    continue
                suggestions.append(PersonSuggestion(
                    person_id=str(pid),
                    person_name=str(pname) if pname else "Unknown",
                    score=float(sum(scores) / len(scores)),
                    face_count=len(scores)
                ))
            suggestions.sort(key=lambda x: (-x.face_count, -x.score))
            results_map[fid] = BatchFaceSuggestionItem(face_id=fid, suggestions=suggestions[: req.top_k])

        return BatchFaceSuggestionsResponse(results=results_map, missing=missing)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /faces/suggestions/batch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Phase 1B: Indexer control + config sources ────────────────────────────────

from msa_apps.search_api.indexer_manager import indexer_manager, get_index_stats
import sys
import yaml as _yaml
from ruamel.yaml import YAML as _RYAML


def _ryaml() -> _RYAML:
    """Return a ruamel.yaml instance configured for comment-preserving round-trips."""
    y = _RYAML()
    y.preserve_quotes = True
    return y


def _rescue_seq_trailing_comments(parent_map, seq_key: str) -> None:
    """
    Ruamel attaches inter-key comments (blank lines + section headers between a
    block sequence and the next mapping key) to the LAST item of the sequence.
    If that item is then deleted the comment is lost.

    Call this before removing items from a block sequence.  It moves any trailing
    comments from items in the sequence back onto the next sibling key in the
    parent map, so they survive even when the sequence is emptied.
    """
    seq = parent_map.get(seq_key)
    if not seq or not hasattr(seq, "ca"):
        return

    keys = list(parent_map.keys())
    try:
        next_key = keys[keys.index(seq_key) + 1]
    except IndexError:
        return  # seq_key is last — nothing to rescue

    # Only the last item can hold inter-key comments (blank lines / section headers
    # between this sequence and the next mapping key). Leave all other items untouched.
    last = seq[-1]
    rescued = []
    if hasattr(last, "ca") and last.ca.items:
        for field_ca in last.ca.items.values():
            if field_ca and len(field_ca) > 2 and field_ca[2]:
                tokens = field_ca[2] if isinstance(field_ca[2], list) else [field_ca[2]]
                rescued.extend(tokens)
                field_ca[2] = None

    if not rescued:
        return

    existing = parent_map.ca.items.get(next_key) or [None, None, None, None]
    if existing[1] is None:
        existing[1] = []
    existing[1] = rescued + existing[1]
    parent_map.ca.items[next_key] = existing
from pydantic import BaseModel as _BaseModel


def _require_writable(source) -> None:
    """Raise 403 if the source is marked read-only (ADR-006).
    Call before any operation that modifies files under a source path."""
    if getattr(source, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail=f"Source '{source.name}' is read-only. "
                   "Set read_only: false in config.yaml to enable write operations.",
        )


class _SourceAdd(_BaseModel):
    name: str
    path: str
    read_only: bool = True  # safe by default (ADR-006)
    description: str = ""


def _config_path(request: Request) -> str:
    # Prefer the explicit launcher-provided config path so installer builds do
    # not depend on uvicorn's working directory. Fall back to cwd/config.yaml in
    # dev/test mode, and normalize Windows paths for WSL access when needed.
    config_env = os.environ.get("MSA_CONFIG_PATH") or os.environ.get("MSA_CONFIG_FILE")
    config_file = config_env if config_env else str(Path(os.getcwd()) / "config.yaml")
    return resolve_for_access(config_file)


def _venv_bin() -> str:
    return str(Path(sys.executable).parent)


@app.post("/indexer/start")
def indexer_start(request: Request):
    """Spawn msa-index run as a background subprocess."""
    try:
        result = indexer_manager.start(
            config_path=_config_path(request),
            venv_bin=_venv_bin(),
        )
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"POST /indexer/start failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/indexer/stop")
def indexer_stop():
    """Request a cooperative stop of the running indexer subprocess.

    Writes a sentinel file the indexer polls between files; on POSIX also
    sends SIGTERM for fast shutdown. On Windows the sentinel is the only
    path (CTRL_BREAK is hijacked by Intel Fortran runtime — see WIN-006).
    Returns 500 if the sentinel cannot be written — on Windows that means
    the indexer was not asked to stop.
    """
    result = indexer_manager.stop()
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("detail", "stop failed"))
    return result


@app.get("/indexer/status")
def indexer_status():
    """Return current indexer state: idle | running | complete | stopped | error."""
    return indexer_manager.get_status()


@app.get("/indexer/log")
def indexer_log(lines: int = 100):
    """Return the last N lines of the current (or most recent) indexer run log."""
    return {"lines": indexer_manager.get_log_lines(tail=lines)}


@app.get("/indexer/stats")
def indexer_stats(request: Request):
    """Return current index totals from SQLite."""
    cfg = _get_config(request)
    return get_index_stats(str(cfg.sqlite_path))


@app.get("/config/sources")
def config_sources_get(request: Request):
    """Return the list of media sources from config.yaml."""
    cfg = _get_config(request)
    sources = []
    for s in getattr(cfg, "media_sources", []):
        stored = str(s.path)
        sources.append({
            "name": s.name,
            "path": stored,
            "display_path": display_path(stored),
            "read_only": getattr(s, "read_only", True),
            "enabled": getattr(s, "enabled", True),
            "description": getattr(s, "description", ""),
        })
    return {"sources": sources}


@app.post("/config/sources", status_code=201)
def config_sources_add(body: _SourceAdd, request: Request):
    """Add a media source to config.yaml."""
    config_file = _config_path(request)
    try:
        ry = _ryaml()
        with open(config_file, "r", encoding="utf-8-sig") as f:
            data = ry.load(f)
        # Mutate the existing CommentedSeq in-place to preserve attached comments.
        if not isinstance(data.get("media_sources"), list):
            data["media_sources"] = []
        sources = data["media_sources"]
        if any(s.get("name") == body.name for s in sources):
            raise HTTPException(status_code=409, detail=f"Source '{body.name}' already exists")
        sources.append({
            "name": body.name,
            "path": body.path,
            "read_only": body.read_only,
            "description": body.description,
        })
        with open(config_file, "w", encoding="utf-8") as f:
            ry.dump(data, f)
        # Invalidate config cache and refresh app.state.S so in-flight requests
        # see the updated sources without needing an API restart.
        try:
            from msa_settings import load_config as _lc
            _lc.cache_clear()
            setattr(request.app.state, "S", _lc())
        except Exception:
            pass
        return {"status": "added", "name": body.name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /config/sources failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/config/sources/{source_name}")
def config_sources_delete(source_name: str, request: Request):
    """Remove a media source from config.yaml by name."""
    config_file = _config_path(request)
    try:
        ry = _ryaml()
        with open(config_file, "r", encoding="utf-8-sig") as f:
            data = ry.load(f)
        sources = data.get("media_sources") or []
        indices = [i for i, s in enumerate(sources) if s.get("name") == source_name]
        if not indices:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")
        # Rescue inter-key comments from the last sequence item before deletion
        # so section-header comments between media_sources and the next key survive.
        _rescue_seq_trailing_comments(data, "media_sources")
        for i in reversed(indices):
            del sources[i]
        with open(config_file, "w", encoding="utf-8") as f:
            ry.dump(data, f)
        try:
            from msa_settings import load_config as _lc
            _lc.cache_clear()
            setattr(request.app.state, "S", _lc())
        except Exception:
            pass
        return {"status": "removed", "name": source_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DELETE /config/sources/{source_name} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Platform + directory browser ──────────────────────────────────────────────

@app.get("/platform")
def platform_info():
    """Return the server's runtime platform (ADR-007).
    Used by the UI to enable platform-specific behaviour (e.g. directory picker)."""
    import sys as _sys
    if is_wsl2():
        name = "wsl2"
    elif _sys.platform == "darwin":
        name = "macos"
    elif _sys.platform == "win32":
        name = "windows"
    else:
        name = "linux"
    return {"platform": name}


@app.get("/browse/pick")
def browse_pick(request: Request = None):
    """Open the native OS folder-picker dialog and return the selected path.

    macOS: osascript 'choose folder' (standard Finder sheet) — reliable.
    Other platforms (Windows, Linux, WSL2): 405. The UI falls back to the
    in-app server-side directory browser at /browse, which is rendered as a
    web modal and immune to OS-level focus quirks. The path field next to
    the Browse button also accepts free-text entry, so any path the in-app
    browser doesn't surface (network shares, OneDrive cloud-only folders,
    deep paths) can be typed directly.

    Restricted to localhost — same policy as /browse.
    """
    import subprocess as _sp
    import sys as _sys

    if request is not None:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ("localhost", "127.0.0.1", "::1", ""):
            raise HTTPException(status_code=403, detail="Only available from localhost")

    if _sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "Select a media folder")'
        try:
            result = _sp.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=120,
            )
            selected = result.stdout.strip().rstrip("/")
            if not selected:
                return {"path": None, "cancelled": True}
            return {"path": selected, "cancelled": False}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Picker failed: {exc}")

    raise HTTPException(status_code=405, detail="Native picker not available on this platform")


@app.get("/browse")
def browse_directory(path: str = "", request: Request = None):
    """Server-side directory browser (ADR-007).

    Accepts a path in user-native format (Windows or POSIX). Returns the
    immediate child directories with both the stored display_path and the
    wsl_path (OS-accessible form). Skips inaccessible entries silently.

    Restricted to localhost — filesystem browsing must not be reachable from
    remote origins even when CORS is permissive.
    """
    import sys as _sys
    import string as _string
    if request is not None:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host not in ("localhost", "127.0.0.1", "::1", ""):
            raise HTTPException(status_code=403, detail="Directory browsing is only available from localhost")

    # On Windows, empty path → enumerate available drive letters ("This PC" view).
    # Use GetLogicalDrives() bitmask — Path.exists() hangs on floppy/network drives.
    if _sys.platform == "win32" and not path:
        import ctypes as _ctypes
        try:
            bitmask = _ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        except Exception:
            bitmask = 0
        drives = []
        for i, letter in enumerate(_string.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append({
                    "name": f"{letter}:",
                    "display_path": f"{letter}:\\",
                    "wsl_path": f"{letter}:\\",
                    "is_dir": True,
                })
        return {
            "current": {"display_path": "This PC", "wsl_path": ""},
            "parent": None,
            "entries": drives,
        }

    if not path:
        if is_wsl2():
            path = "/mnt"
        else:
            path = "/"
    access_path = resolve_for_access(path)
    target = Path(access_path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Directory not found: {path}")
    _MEDIA_EXTS = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
        ".tiff", ".tif", ".bmp", ".avif",
        ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".webm",
        ".mts", ".m2ts", ".3gp",
    }
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        for child in sorted(target.iterdir()):
            try:
                if child.is_dir():
                    dirs.append({
                        "name": child.name,
                        "display_path": display_path(str(child)),
                        "wsl_path": str(child),
                        "is_dir": True,
                    })
                elif child.suffix.lower() in _MEDIA_EXTS and len(files) < 50:
                    files.append({
                        "name": child.name,
                        "display_path": display_path(str(child)),
                        "wsl_path": str(child),
                        "is_dir": False,
                    })
            except PermissionError:
                continue
    except PermissionError:
        pass
    entries = dirs + files
    parent = target.parent
    # On Windows, at a drive root (parent == target), link back to the drives list
    if _sys.platform == "win32" and parent == target:
        parent_entry: dict | None = {"display_path": "This PC", "wsl_path": ""}
    elif parent != target:
        parent_entry = {
            "display_path": display_path(str(parent)),
            "wsl_path": str(parent),
        }
    else:
        parent_entry = None
    return {
        "current": {
            "display_path": display_path(str(target)),
            "wsl_path": str(target),
        },
        "parent": parent_entry,
        "entries": entries,
    }


# ── Diagnostics ───────────────────────────────────────────────────────────────

def _latest_timestamped_log(log_dir: Path, prefix: str) -> Optional[Path]:
    """Return the most-recently-created log matching <prefix>-*.log, or None."""
    matches = sorted(log_dir.glob(f"{prefix}-*.log"))
    return matches[-1] if matches else None


@app.get("/diagnostics")
def diagnostics(request: Request):
    """Return paths to key files and service URLs for diagnostics."""
    cfg = _get_config(request)

    # Config file: prefer the resolved MSA_CONFIG_PATH env var over cwd fallback
    config_env = os.environ.get("MSA_CONFIG_PATH") or os.environ.get("MSA_CONFIG_FILE")
    config_file = Path(config_env) if config_env else Path(os.getcwd()) / "config.yaml"

    # log_dir: cfg.log_dir is absolute after _resolve_data_paths.
    # On Windows native, start.ps1 passes MSA_LOG_DIR for the AppDir\logs location
    # (separate from the DataDir data root).  Fall back to cfg.log_dir, then cwd/logs.
    log_dir_env = os.environ.get("MSA_LOG_DIR")
    log_dir_raw = log_dir_env or getattr(cfg, "log_dir", None)
    log_dir = Path(log_dir_raw) if log_dir_raw else config_file.parent / "logs"

    latest_launch  = _latest_timestamped_log(log_dir, "launch")
    latest_install = _latest_timestamped_log(log_dir, "install")
    latest_stop    = _latest_timestamped_log(log_dir, "stop")

    def dp(p) -> str:
        return display_path(str(p))

    logs: dict = {}
    # msa.log is written by the Python/Loguru app to cfg.log_dir.
    # On Windows native, cfg.log_dir (UserProfile\...\logs) differs from
    # MSA_LOG_DIR (LocalAppData\...\logs) where the launcher writes uvicorn.log.
    cfg_log_dir = Path(getattr(cfg, "log_dir", log_dir))
    app_log = cfg_log_dir / "msa.log"
    if app_log.exists():
        logs["app"] = dp(app_log)
    # uvicorn and qdrant logs live in the launcher log dir (MSA_LOG_DIR on Windows native)
    for name, filename in [("uvicorn", "uvicorn.log"), ("qdrant", "qdrant.log")]:
        p = log_dir / filename
        if p.exists():
            logs[name] = dp(p)
    # msa-desktop.log: the unified rotating shell log (Tauri desktop mode) — the primary
    # troubleshooting artifact spanning the provisioning shim + uvicorn. Written to the
    # launcher log dir (MSA_LOG_DIR); absent in browser / `msa api start` mode.
    desktop_log = log_dir / "msa-desktop.log"
    if desktop_log.exists():
        logs["desktop"] = dp(desktop_log)
    if latest_launch:
        logs["launch"] = dp(latest_launch)
    if latest_install:
        logs["install"] = dp(latest_install)
    if latest_stop:
        logs["stop"] = dp(latest_stop)

    # Qdrant: show remote URL only when an explicit remote is configured.
    # Embedded Qdrant (path= mode) has no HTTP endpoint to show.
    server_cfg = getattr(cfg, 'server', None)
    qdrant_url = getattr(server_cfg, 'qdrant_url', None)

    result: dict = {
        "msa_root": dp(config_file.parent),
        "config_file": dp(config_file),
        "sqlite_path": dp(getattr(cfg, "sqlite_path", config_file.parent / "index" / "media.sqlite")),
        "models_dir": dp(getattr(cfg, "models_dir", config_file.parent / "models")),
        "log_dir": dp(log_dir),
        "logs": logs,
        "api_url": _effective_api_url(cfg),
        "app_version": _APP_VERSION,
    }
    if qdrant_url:
        result["qdrant_url"] = qdrant_url
    return result


# Log names that can be served via GET /logs/{name}
_LOG_NAME_MAP = {
    "app":     ("msa.log", None),
    "uvicorn": ("uvicorn.log", None),
    "qdrant":  ("qdrant.log", None),
    "launch":  (None, "launch"),
    "install": (None, "install"),
    "stop":    (None, "stop"),
}

@app.get("/logs/{name}")
def serve_log(name: str, request: Request, tail: int = 2000):
    """Serve the last N lines of a named log file as plain text for browser viewing."""
    from fastapi.responses import PlainTextResponse
    from collections import deque
    if tail < 1 or tail > 2000:
        raise HTTPException(status_code=400, detail="tail must be between 1 and 2000")
    if name not in _LOG_NAME_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown log '{name}'")
    cfg = _get_config(request)
    log_dir = Path(os.getcwd()) / getattr(cfg, "log_dir", "logs")
    fixed_name, ts_prefix = _LOG_NAME_MAP[name]
    if fixed_name:
        log_file = log_dir / fixed_name
    else:
        log_file = _latest_timestamped_log(log_dir, ts_prefix)  # type: ignore[arg-type]
    if not log_file or not log_file.exists():
        raise HTTPException(status_code=404, detail=f"Log file not found for '{name}'")
    try:
        with log_file.open(errors="replace") as fh:
            content = "\n".join(deque(fh, maxlen=tail))
        filename = log_file.name
        return PlainTextResponse(
            content,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Model config ──────────────────────────────────────────────────────────────

# Canonical defaults — used to show reset targets in the UI
_MODEL_CONFIG_DEFAULTS: dict = {
    "batch_size": 32,
    "enable_object_detection": "auto",
    "object_detector_backend": "rtdetr",
    "object_model": "PekingU/rtdetr_r18vd",
    "object_confidence_threshold": 0.35,
    "enable_face_recognition": True,
    "face_recognizer_backend": "facenet_pytorch",
    "face_model": "vggface2",
    "face_confidence_threshold": 0.95,
    "face_min_size": 60,
    "face_store_metadata": True,
}

# Keys that are read-only — never written by PATCH
_MODEL_CONFIG_READONLY = {"device", "model_name", "pretrained"}


class _ModelConfigPatch(_BaseModel):
    batch_size: Optional[int] = None
    enable_object_detection: Optional[str | bool] = None
    object_detector_backend: Optional[str] = None
    object_model: Optional[str] = None
    object_confidence_threshold: Optional[float] = None
    enable_face_recognition: Optional[bool] = None
    face_recognizer_backend: Optional[str] = None
    face_model: Optional[str] = None
    face_confidence_threshold: Optional[float] = None
    face_min_size: Optional[int] = None
    face_store_metadata: Optional[bool] = None


@app.get("/config/model")
def config_model_get(request: Request):
    """Return model configuration (read-only fields + editable fields + defaults)."""
    cfg = _get_config(request)
    editable = {k: getattr(cfg, k, v) for k, v in _MODEL_CONFIG_DEFAULTS.items()}
    readonly = {
        "device":     getattr(cfg, "device", "cuda"),
        "model_name": getattr(cfg, "model_name", "ViT-L-14"),
        "pretrained": getattr(cfg, "pretrained", "openai"),
    }
    return {"readonly": readonly, "editable": editable, "defaults": _MODEL_CONFIG_DEFAULTS}


@app.patch("/config/model")
def config_model_patch(body: _ModelConfigPatch, request: Request):
    """Persist editable model settings to config.yaml."""
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")
    config_file = _config_path(request)
    try:
        ry = _ryaml()
        with open(config_file, encoding="utf-8-sig") as f:
            data = ry.load(f) or {}
        for key, value in updates.items():
            if key not in _MODEL_CONFIG_READONLY:
                data[key] = value
        with open(config_file, "w", encoding="utf-8") as f:
            ry.dump(data, f)
        # Refresh app.state.S so subsequent requests see the new values
        from msa_settings import load_config as _lc
        try:
            _lc.cache_clear()  # type: ignore[attr-defined]
        except AttributeError:
            pass
        try:
            new_cfg = _lc()
            setattr(request.app.state, "S", new_cfg)
            setattr(request.app.state, "config", new_cfg)
        except Exception:
            pass
        return {"updated": list(updates.keys())}
    except Exception as e:
        logger.error(f"PATCH /config/model failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Setup: first-launch model download ───────────────────────────────────────

from msa_apps.search_api import setup_models as _setup_models  # noqa: E402


@app.get("/api/setup/status")
def get_setup_status(request: Request):
    """Return whether all required model weights are present on disk."""
    S = _get_config(request)
    models_dir: Path = S.models_dir
    manager = _setup_models.get_manager()
    present = manager.check_all_present(models_dir)
    def _integrity_hint(meta: dict) -> str:
        """Return a short integrity label for display.

        SHA-256-verified models: 'sha256:<first 12 chars>'
        Revision-pinned models:  'rev:<first 12 chars>'
        Torch-hub-verified models: 'torch-hub'
        Otherwise: '' (UI must skip rendering — see SetupPage.tsx)
        """
        if "sha256" in meta:
            h = meta["sha256"]
            # InsightFace has a dict of per-file hashes; use det_10g.onnx as representative
            if isinstance(h, dict):
                h = h.get("det_10g.onnx", next(iter(h.values())))
            return "sha256:" + h[:12]
        if "revision" in meta:
            return "rev:" + meta["revision"][:12]
        # Models verified by torch.hub's own integrity check (no separate
        # sha256/revision maintained here) — currently facenet-pytorch.
        # Surface the provenance so the UI doesn't render a lone ellipsis
        # next to the "Verified" status line.
        if meta.get("source", "").startswith("github.com/"):
            return "torch-hub"
        return ""

    models_out = [
        {
            "id":             mid,
            "label":          meta["label"],
            "size_mb":        meta["size_mb"],
            "present":        present[mid],
            "integrity_hint": _integrity_hint(meta),
            "source":         meta.get("source", ""),
        }
        for mid, meta in _setup_models.MODEL_META.items()
    ]
    return {"ready": all(present.values()), "models": models_out}


@app.post("/api/setup/retry", status_code=202)
def retry_setup(request: Request):
    """Re-trigger the first-launch model downloads: reset any errored models to pending and restart
    the background worker. This is what the setup screen's Retry button needs — a plain page reload
    only re-subscribes to the manager's held complete/error state (``/ws/setup`` is a pure subscriber
    that must never call ``start_if_needed``). Idempotent and safe: ``start_if_needed`` is a no-op
    when a download is already cleanly in flight or fully done, and only resets+restarts once every
    in-progress model has settled with at least one error."""
    S = _get_config(request)
    _setup_models.get_manager().start_if_needed(S.models_dir)
    return {"status": "retrying"}


@app.websocket("/ws/setup")
async def ws_setup(websocket: WebSocket):
    """Stream first-launch model download progress.

    Connects → polls SetupManager state every second → sends {type:"complete"}
    when all models are done or errored. The download itself is initiated at
    API startup (lifespan); this handler is a pure progress subscriber and must
    not call start_if_needed() — a reconnecting client must not race with an
    in-progress download.
    """
    await websocket.accept()

    # WebSocket handlers cannot use Depends(), so resolve config the same way
    # _get_config() does: prefer app.state.S so that test config_override and
    # runtime hot-reload are both respected.
    _cfg = (
        getattr(websocket.app.state, "S", None)
        or getattr(websocket.app.state, "config", None)
    )
    if _cfg is None:
        try:
            from msa_settings import load_config as _lc
            _cfg = _lc()
        except Exception:
            await websocket.close(code=1011)
            return

    models_dir: Path = _cfg.models_dir
    manager = _setup_models.get_manager()

    try:
        while True:
            status = manager.get_status()
            complete = manager.is_complete()
            await websocket.send_json({
                "type":   "complete" if complete else "update",
                "models": status,
            })
            if complete:
                break
            await asyncio.sleep(1)
    except Exception:
        pass


# ── WebSocket — real-time indexer feed ───────────────────────────────────────

@app.websocket("/ws/indexer")
async def ws_indexer(websocket: WebSocket):
    """Stream indexer status + log tail every 2 seconds until client disconnects."""
    await websocket.accept()
    try:
        while True:
            status = indexer_manager.get_status()
            log_lines = indexer_manager.get_log_lines(tail=50)
            await websocket.send_json({
                "type": "update",
                "status": status,
                "log": log_lines,
            })
            await asyncio.sleep(2)
    except Exception:
        pass


# ── React SPA catch-all (must be last) ────────────────────────────────────────
# Registered here, after all API routes, so API routes always take priority.
# Serves actual dist/ files (JS/CSS bundles) if they exist, otherwise returns
# index.html so React Router handles client-side navigation.

_UI_DIST = Path(os.path.dirname(__file__)).parent / "ui" / "dist"
if not _UI_DIST.is_dir():
    # Non-editable (system) install: __file__ is inside site-packages.
    # Prefer MSA_ROOT env var (set by start.sh export), fall back to cwd
    # (start.sh does `cd "$MSA_ROOT"` before launching uvicorn).
    _msa_root = os.environ.get("MSA_ROOT") or str(Path.cwd())
    _UI_DIST = Path(_msa_root) / "src" / "msa_apps" / "ui" / "dist"

if _UI_DIST.is_dir():
    # Cache policy: Vite emits content-hashed files under assets/ (e.g.
    # index-C2xlEnZ1.js) — their content can never change under a fixed name, so
    # cache them forever. EVERYTHING else (the index.html shell, favicon) must
    # revalidate on each load: the shell is the only thing that points at the
    # current hashed bundles, so a stale-cached shell silently keeps users on the
    # OLD UI after an app update (observed: /track/open went un-fired until a
    # manual hard-refresh). `no-cache` = "cache but revalidate" → a ~0.5 KB
    # conditional 304, not a re-download.
    _IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
    _NO_CACHE = {"Cache-Control": "no-cache"}

    def _index() -> FileResponse:
        return FileResponse(_UI_DIST / "index.html", headers=_NO_CACHE)

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        # Resolve to an absolute path and verify it stays inside _UI_DIST to
        # prevent directory traversal (e.g. /../../etc/passwd).
        try:
            asset = (_UI_DIST / full_path).resolve()
            asset.relative_to(_UI_DIST.resolve())  # raises ValueError if outside
        except ValueError:
            return _index()
        if asset.is_file():
            headers = _IMMUTABLE if full_path.startswith("assets/") else _NO_CACHE
            return FileResponse(asset, headers=headers)
        return _index()
