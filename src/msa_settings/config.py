from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
import os
import sys
import yaml
import logging
from functools import lru_cache

REPO_ROOT = Path(__file__).resolve().parents[2]  # .../media-search-agent
logger = logging.getLogger(__name__)

# Avoid noisy repeat logs when config is loaded multiple times
_MEDIA_SOURCES_PATH_LOGGED = False

@dataclass
class MediaSource:
    """Configuration for a media source (folder/cloud/network share)"""
    name: str                           # Unique identifier (e.g., "sample_photos")
    path: str                           # Absolute path for indexing
    url_base: Optional[str] = None      # Base URL for browsing (file://, smb://, http://)
    read_only: bool = True              # Prevent accidental modifications
    enabled: bool = True                # Allow temporarily disabling sources
    description: Optional[str] = None   # Human-readable description

@dataclass
class IndexerConfig:
    """Indexer-specific configuration"""
    index_dir: Path = Path("index")
    sqlite_path: Path = Path("index/media.sqlite")
    faiss_path: Path = Path("index/image_vec.faiss")
    thumb_dir: Path = Path("data/thumbnails")
    face_thumb_dir: Path = Path("data/face_thumbnails")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    model_name: str = "ViT-L-14"
    pretrained: str = "openai"
    batch_size: int = 16
    device: str = "cpu"
    model_version: str = "clip-2025-10"
    num_workers: int = 4
    # Face recognition
    enable_face_recognition: bool = False
    face_recognizer_backend: str = "facenet_pytorch"
    face_model: str = "vggface2"
    face_confidence_threshold: float = 0.95
    face_min_size: int = 60
    face_store_metadata: bool = True
    face_clustering_enabled: bool = False
    face_cluster_min_size: int = 3
    face_cluster_similarity: float = 0.6
    # Object detection
    # enable_object_detection: "auto" enables on GPU only; True forces on (even CPU); False disables.
    enable_object_detection: str | bool = "auto"
    object_detector_backend: str = "rtdetr"
    object_model: str = "PekingU/rtdetr_r18vd"
    object_confidence_threshold: float = 0.35
    enable_video_object_detection: bool = False
    video_detection_max_frames: int = 10
    # Video shot detection
    enable_video_shot_detection: bool = False
    shot_detection_threshold: float = 30.0
    min_shot_length_frames: int = 15
    keyframes_per_shot: int = 1
    # Advanced
    ann_method: str = "ivfflat"
    ann_lists: int = 100

@dataclass
class ServerConfig:
    """Server configuration"""
    qdrant_recreate_collections_on_export: bool = False

@dataclass
class RetrievalConfig:
    """Query/retrieval configuration"""
    top_k_candidates: int = 200
    top_k_return: int = 50
    search_score_trace: bool = False

@dataclass
class RankerConfig:
    """msa-ranker integration config.

    `event_logging` is the privacy kill-switch (ADR-014, default ON — labels are needed
    to learn; set false to opt out). `ledger_dir` defaults to <data_dir>/ranker-ledger
    (alongside `index/`, NOT logs/ — it holds durable training labels; spec 01) when unset;
    a relative override resolves under the data root.

    Serving (spec 06): `enable_learning_to_rank` is the master flag — **default OFF**, so
    search is byte-identical to today (INV-3) until a model is explicitly enabled.
    `ltr_model_dir` is the deployed model dir (a `manifest.json` + its artifact); the gate
    (`beats_baseline` + feature-version + sha) is applied at load, and any failure falls
    back to the heuristic."""
    event_logging: bool = True
    ledger_dir: Optional[str] = None
    enable_learning_to_rank: bool = False
    ltr_model_dir: Optional[str] = None

@dataclass
class CollectionsConfig:
    """Qdrant collection names"""
    image: str = "image_emb"
    video: str = "video_emb"
    face: str = "face_emb"
    caption: str = "caption_emb"
    asr: str = "asr_emb"

@dataclass
class ApiConfig:
    """API server configuration"""
    host: str = "127.0.0.1"
    port: int = 8000

@dataclass
class UiConfig:
    """UI configuration"""
    api_url: str = "http://localhost:8000"

@dataclass
class Config:
    """Main application configuration with all subsections"""
    # Indexer config (flatten for backward compatibility)
    # Preferred CLI override path for single-run indexing, takes precedence over media_sources
    media_source_override: Optional[Path] = None
    index_dir: Path = Path("index")
    sqlite_path: Path = Path("index/media.sqlite")
    faiss_path: Path = Path("index/image_vec.faiss")
    face_faiss_path: Path = Path("index/face_vec.faiss")
    qdrant_path: Path = Path("qdrant")
    thumb_dir: Path = Path("data/thumbnails")
    face_thumb_dir: Path = Path("data/face_thumbnails")
    models_dir: Path = Path("models")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    model_name: str = "ViT-L-14"
    pretrained: str = "openai"
    batch_size: int = 16
    device: str = "cpu"
    model_version: str = "clip-2025-10"
    num_workers: int = 4
    enable_face_recognition: bool = False
    face_recognizer_backend: str = "facenet_pytorch"
    face_model: str = "vggface2"
    face_confidence_threshold: float = 0.95
    face_min_size: int = 60
    face_store_metadata: bool = True
    face_clustering_enabled: bool = False
    face_cluster_min_size: int = 3
    face_cluster_similarity: float = 0.6
    enable_object_detection: str | bool = "auto"
    object_detector_backend: str = "rtdetr"
    object_model: str = "PekingU/rtdetr_r18vd"
    object_confidence_threshold: float = 0.35
    enable_video_object_detection: bool = False
    video_detection_max_frames: int = 10
    enable_video_shot_detection: bool = False
    shot_detection_threshold: float = 30.0
    min_shot_length_frames: int = 15
    keyframes_per_shot: int = 1
    ann_method: str = "ivfflat"
    ann_lists: int = 100
    # Reprocessing flags (set via CLI, not config file)
    reprocess_gps: bool = False
    reprocess_objects: bool = False
    reprocess_faces: bool = False
    reprocess_embeddings: bool = False
    # Multi-source support
    media_sources: List[MediaSource] = field(default_factory=list)
    # Nested configs
    server: ServerConfig = field(default_factory=ServerConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    ranker: RankerConfig = field(default_factory=RankerConfig)
    collections: CollectionsConfig = field(default_factory=CollectionsConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    # media_sources are authoritative; no legacy root fallback exists.


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------

def detect_device() -> str:
    """Return the best available compute device: 'cuda' > 'mps' > 'cpu'."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


# ---------------------------------------------------------------------------
# Platform directories
# ---------------------------------------------------------------------------

def _platform_data_dir() -> Path:
    """Platform-appropriate user data directory for MSA index and thumbnails.

    XDG_* env vars are intentionally not honored on Linux: the installer
    hardcodes ``$HOME/.local/share/MediaSearchAgent`` and the runtime matches.
    Power users who want a non-default data location should set ``MSA_DATA_DIR``,
    which the launcher exports and which takes precedence over this default.
    See ADR-009.
    """
    if os.name == 'nt':
        # Windows: visible in Explorer under the user's home folder.
        return Path.home() / 'MediaSearchAgent'
    elif sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'MediaSearchAgent'
    else:  # Linux / WSL2
        return Path.home() / '.local' / 'share' / 'MediaSearchAgent'


def _platform_config_dir() -> Path:
    """Platform-appropriate user config directory for config.yaml.

    Windows resolves to %USERPROFILE%\\MediaSearchAgent (matching the installer's
    DataDir default), not %APPDATA%\\MediaSearchAgent. This is intentional per
    ADR-009: the installer surfaces user data and config in the user's home
    folder for discoverability rather than hidden under AppData.

    Linux does not honor XDG_CONFIG_HOME (see _platform_data_dir for rationale).
    """
    if os.name == 'nt':
        return Path.home() / 'MediaSearchAgent'
    elif sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'MediaSearchAgent'
    else:  # Linux / WSL2
        return Path.home() / '.config' / 'MediaSearchAgent'


def _platform_cache_dir() -> Path:
    """Platform-appropriate cache directory for ML model weights.

    The Cache folder holds re-downloadable model files. On Windows it sits inside
    the app-internals root (LOCALAPPDATA) so it is clearly separate from user data.
    Linux does not honor XDG_CACHE_HOME (see _platform_data_dir for rationale).
    """
    if os.name == 'nt':
        local = os.getenv('LOCALAPPDATA')
        base = Path(local) if local else Path.home() / 'AppData' / 'Local'
        return base / 'MediaSearchAgent' / 'Cache'
    elif sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Caches' / 'MediaSearchAgent'
    else:  # Linux / WSL2
        return Path.home() / '.cache' / 'MediaSearchAgent'


def _platform_log_dir() -> Path:
    """Platform-appropriate log directory for msa.log and related logs.

    Linux does not honor XDG_DATA_HOME (see _platform_data_dir for rationale).
    """
    if os.name == 'nt':
        local = os.getenv('LOCALAPPDATA')
        base = Path(local) if local else Path.home() / 'AppData' / 'Local'
        return base / 'MediaSearchAgent' / 'logs'
    elif sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Logs' / 'MediaSearchAgent'
    else:  # Linux / WSL2
        return Path.home() / '.local' / 'share' / 'MediaSearchAgent' / 'logs'


# ---------------------------------------------------------------------------
# Config bootstrap
# ---------------------------------------------------------------------------


def _find_config_path() -> str:
    """
    Determine the config file path using this search order:
      1. MSA_CONFIG_PATH env var  (explicit override)
      2. Platform config dir      (~/.config/MediaSearchAgent/config.yaml on Linux,
                                   ~/Library/Application Support/MediaSearchAgent/config.yaml on macOS,
                                   %USERPROFILE%\\MediaSearchAgent\\config.yaml on Windows)
      3. REPO_ROOT/config.yaml    (dev mode fallback)

    Raises FileNotFoundError if no config is found. On end-user installs the
    installer always places config.yaml before first launch; on dev checkouts
    config.yaml is committed to the repo root.
    """
    # 1. Explicit override via env var
    env_path = os.getenv('MSA_CONFIG_PATH')
    if env_path:
        p = Path(env_path)
        if not p.exists():
            raise FileNotFoundError(
                f"MSA_CONFIG_PATH points to '{env_path}' but the file does not exist"
            )
        logger.info("Config: using MSA_CONFIG_PATH=%s", env_path)
        return str(p)

    # 2. Platform config dir
    platform_config = _platform_config_dir() / 'config.yaml'
    if platform_config.exists():
        logger.info("Config: using platform config at %s", platform_config)
        return str(platform_config)

    # 3. Repo-root fallback (dev mode)
    repo_config = REPO_ROOT / 'config.yaml'
    if repo_config.exists():
        logger.info("Config: using repo-root config at %s (dev mode)", repo_config)
        return str(repo_config)

    # Nothing found — installer or dev checkout must provide config.yaml
    raise FileNotFoundError(
        "config.yaml not found. "
        "On a dev checkout run: git checkout config.yaml\n"
        f"Expected locations checked: {platform_config}, {repo_config}"
    )


# ---------------------------------------------------------------------------
# Data path resolution
# ---------------------------------------------------------------------------

# Fields that hold data-directory paths and should be resolved to absolute paths.
# media_source_override is excluded — it's an explicit CLI argument.
_DATA_PATH_FIELDS = (
    'index_dir', 'sqlite_path', 'faiss_path', 'face_faiss_path',
    'qdrant_path', 'thumb_dir', 'face_thumb_dir',
)

# Fields that hold cache-directory paths (ML model weights).
# Resolved under MSA_CACHE_DIR (or platform cache dir), not MSA_DATA_DIR.
_CACHE_PATH_FIELDS = ('models_dir',)

# Log directory — resolved against MSA_LOG_DIR (or platform log dir), not MSA_DATA_DIR.
# MSA_LOG_DIR is the log directory itself, not a root; cfg.log_dir is set directly.
_LOG_PATH_FIELDS = ('log_dir',)


def _resolve_data_paths(cfg: Config) -> Config:
    """
    Resolve relative data-directory paths in cfg to absolute paths.

    Resolution priority:
      1. MSA_DEV=1         → resolve relative to REPO_ROOT (preserves dev behaviour)
      2. MSA_DATA_DIR env  → resolve relative to that directory
      3. Platform default  → ~/.local/share/MediaSearchAgent (Linux),
                             ~/Library/Application Support/MediaSearchAgent (macOS),
                             %USERPROFILE%\\MediaSearchAgent (Windows)

    Paths that are already absolute are left unchanged, so per-field absolute
    overrides in config.yaml continue to work.
    """
    if os.getenv('MSA_DEV') == '1':
        data_root = REPO_ROOT
        logger.debug("Config: MSA_DEV=1 — data paths relative to repo root %s", data_root)
    else:
        env_data = os.getenv('MSA_DATA_DIR')
        data_root = Path(env_data) if env_data else _platform_data_dir()
        logger.info("Config: data root = %s", data_root)

    for field_name in _DATA_PATH_FIELDS:
        p: Path = getattr(cfg, field_name)
        if not p.is_absolute():
            setattr(cfg, field_name, (data_root / p).resolve())

    # The reranker model dir is nested under `ranker`. Resolve it the same way (relative
    # → data root, `~` expanded) so a relative `ltr_model_dir` works when `msa api start`
    # runs from any cwd — not interpreted against the process working directory.
    _rk = getattr(cfg, "ranker", None)
    _mdir = getattr(_rk, "ltr_model_dir", None) if _rk is not None else None
    if _mdir:
        _mp = Path(_mdir).expanduser()
        _rk.ltr_model_dir = str(_mp if _mp.is_absolute() else (data_root / _mp).resolve())

    # The reranker event ledger (spec 01) lives in MSA's DATA area, alongside `index/` —
    # NOT in logs/. logs/ is treated as disposable (log-rotated, cleaned, often excluded
    # from backups), but the ledger holds the accrued interaction labels the model trains
    # on, so it belongs with the durable, backed-up data set (next to index/media.sqlite).
    # Default it under data_root; resolve a relative override the same way as ltr_model_dir.
    if _rk is not None:
        _ldir = getattr(_rk, "ledger_dir", None)
        if _ldir:
            _lp = Path(_ldir).expanduser()
            _rk.ledger_dir = str(_lp if _lp.is_absolute() else (data_root / _lp).resolve())
        else:
            _rk.ledger_dir = str((data_root / "ranker-ledger").resolve())

    # Create directories that must exist before the app can write to them.
    # Files (sqlite_path, faiss_path, face_faiss_path) share index_dir as parent,
    # so creating the directory fields is sufficient.
    # log_dir is excluded here — it is created by _resolve_log_paths().
    _DIR_FIELDS = ('index_dir', 'thumb_dir', 'face_thumb_dir')
    for field_name in _DIR_FIELDS:
        p = getattr(cfg, field_name, None)
        if p and isinstance(p, Path):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("Could not create directory %s: %s", p, exc)

    return cfg


def _resolve_cache_paths(cfg: Config) -> Config:
    """
    Resolve cache-directory paths (ML models) to absolute paths.

    Resolution priority:
      1. MSA_DEV=1         → resolve relative to REPO_ROOT
      2. MSA_CACHE_DIR env → resolve relative to that directory
      3. Platform default  → ~/.cache/MediaSearchAgent (Linux/WSL2),
                             ~/Library/Caches/MediaSearchAgent (macOS),
                             %LOCALAPPDATA%\\MediaSearchAgent\\Cache (Windows)

    Paths that are already absolute are left unchanged.
    """
    if os.getenv('MSA_DEV') == '1':
        cache_root = REPO_ROOT
    else:
        env_cache = os.getenv('MSA_CACHE_DIR')
        cache_root = Path(env_cache) if env_cache else _platform_cache_dir()

    for field_name in _CACHE_PATH_FIELDS:
        p: Path = getattr(cfg, field_name)
        if not p.is_absolute():
            setattr(cfg, field_name, (cache_root / p).resolve())

    return cfg


def _resolve_log_paths(cfg: Config) -> Config:
    """
    Resolve log_dir to an absolute path.

    Unlike data/cache paths (which are roots under which config-relative subdirs sit),
    MSA_LOG_DIR is the log directory itself — cfg.log_dir is set to it directly.

    Resolution priority:
      1. MSA_DEV=1        → resolve log_dir relative to REPO_ROOT (dev behaviour)
      2. MSA_LOG_DIR env  → use as the absolute log directory
      3. Platform default → %LOCALAPPDATA%\\MediaSearchAgent\\logs (Windows),
                            ~/Library/Logs/MediaSearchAgent (macOS),
                            ~/.local/share/MediaSearchAgent/logs (Linux)

    Note: config.yaml's log_dir field is intentionally ignored in non-dev mode.
    Production log location is controlled exclusively by MSA_LOG_DIR or the
    platform default — not by config.yaml — so that the log directory is always
    predictable regardless of which config file is active.
    """
    if os.getenv('MSA_DEV') == '1':
        p: Path = cfg.log_dir
        if not p.is_absolute():
            cfg.log_dir = (REPO_ROOT / p).resolve()
        logger.debug("Config: log dir = %s (dev mode)", cfg.log_dir)
    else:
        env_log = os.getenv('MSA_LOG_DIR')
        cfg.log_dir = Path(env_log).resolve() if env_log else _platform_log_dir()
        logger.info("Config: log dir = %s", cfg.log_dir)

    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Could not create log directory %s: %s", cfg.log_dir, exc)

    return cfg


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config_impl(path: str | None = None) -> Config:
    """Load config from YAML file with env variable overrides (uncached internal impl)."""
    if not path:
        path = _find_config_path()

    logger.info("Loading configuration from: %s", path)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            d = yaml.safe_load(f)
    except Exception as e:
        logger.error("Failed to load main config file '%s': %s", path, e)
        raise

    from dataclasses import fields, is_dataclass, MISSING
    from typing import get_origin, get_args, get_type_hints

    # Get the actual types (resolves forward references)
    type_hints = get_type_hints(Config)

    # Helper to recursively convert dicts to dataclasses
    def _convert_value(field_type, value):
        """Convert YAML value to appropriate Python type"""
        if field_type == Path and isinstance(value, str):
            return Path(value)
        elif str(field_type).startswith('typing.List') and isinstance(value, list):
            # Convert list of dicts to list of MediaSource objects
            if value and isinstance(value[0], dict) and 'name' in value[0]:
                return [MediaSource(**item) for item in value]
            return value
        elif isinstance(value, dict) and is_dataclass(field_type):
            # Recursively create nested dataclass
            nested_fields = {f.name: f.type for f in fields(field_type)}
            nested_values = {}
            for k, v in value.items():
                if k in nested_fields:
                    nested_values[k] = _convert_value(nested_fields[k], v)
            return field_type(**nested_values)  # type: ignore
        else:
            return value

    # Get all Config fields using type hints (resolves forward references)
    valid_fields = type_hints
    filtered_d = {}

    # Parse all fields from YAML
    for k, v in d.items():
        if k == "media_sources_file" and isinstance(v, str):
            # Load media sources from external file (privacy sensitive)
            private_path = (Path(path).parent / v).resolve() if not Path(v).is_absolute() else Path(v)
            global _MEDIA_SOURCES_PATH_LOGGED
            if not _MEDIA_SOURCES_PATH_LOGGED:
                logger.debug("Loading external media sources from: %s", private_path)
                _MEDIA_SOURCES_PATH_LOGGED = True
            if private_path.exists():
                try:
                    with open(private_path, "r") as pf:
                        private_yaml = yaml.safe_load(pf)
                except Exception as e:
                    logger.error("Failed to load external media sources file '%s': %s", private_path, e)
                    raise
                if not isinstance(private_yaml, dict) or "media_sources" not in private_yaml:
                    logger.error("'media_sources' key missing or invalid in %s", private_path)
                    raise ValueError(f"'media_sources' key missing or invalid in {private_path}")
                filtered_d["media_sources"] = _convert_value(valid_fields["media_sources"], private_yaml["media_sources"])
            else:
                # Don't fail if private file is missing - just log a warning
                # This allows CI/testing to work without requiring private media sources
                logger.warning("External media sources file '%s' does not exist. Will use only inline media_sources if present.", private_path)
            continue
        if k in valid_fields:
            if k == "media_sources" and isinstance(v, list):
                # Merge inline media_sources with any loaded from external file (append)
                existing = filtered_d.get("media_sources", []) or []
                inline_list = _convert_value(valid_fields["media_sources"], v) or []
                # Ensure both are lists
                if not isinstance(existing, list):
                    existing = [existing]
                if not isinstance(inline_list, list):
                    inline_list = [inline_list]
                filtered_d["media_sources"] = [*existing, *inline_list]
            else:
                filtered_d[k] = _convert_value(valid_fields[k], v)

    # Create config - start with defaults, then apply parsed values
    # This preserves default_factory for nested configs that aren't in YAML
    cfg = Config()

    # Apply parsed values
    for k, v in filtered_d.items():
        setattr(cfg, k, v)

    # Migrate legacy object detection config values to RT-DETR defaults
    if cfg.object_detector_backend != "rtdetr":
        logger.warning(
            "Config: object_detector_backend '%s' is no longer supported. "
            "Resetting to 'rtdetr' (RT-DETR, Apache-2.0 default).",
            cfg.object_detector_backend,
        )
        cfg.object_detector_backend = "rtdetr"
    if isinstance(cfg.object_model, str) and cfg.object_model.endswith(".pt"):
        logger.warning(
            "Config: object_model '%s' looks like a legacy weight file. "
            "Resetting to 'PekingU/rtdetr_r18vd' (RT-DETR, Apache-2.0 default).",
            cfg.object_model,
        )
        cfg.object_model = "PekingU/rtdetr_r18vd"

    # Override device from environment, then resolve "auto"
    device_env = os.getenv("MSA_DEVICE") or os.getenv("DEVICE")  # MSA_DEVICE preferred; DEVICE kept for compatibility
    if device_env:
        cfg.device = device_env
    if cfg.device == "auto":
        cfg.device = detect_device()

    logger.info(
        "Device: %s | platform: %s (%s) | os.name: %s",
        cfg.device,
        sys.platform,
        os.uname().machine if hasattr(os, "uname") else "unknown",
        os.name,
    )

    # Resolve relative data paths to absolute platform locations
    _resolve_data_paths(cfg)
    _resolve_cache_paths(cfg)
    _resolve_log_paths(cfg)

    return cfg


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> Config:
    """Cached public loader. Clears repeated disk I/O per request.

    If config.yaml changes, call reload_config() to refresh.
    """
    return _load_config_impl(path)

def reload_config(path: str | None = None) -> Config:
    """Force reloading configuration by clearing cache."""
    try:
        load_config.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    return load_config(path)
