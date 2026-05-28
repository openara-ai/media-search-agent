# ADR-006: Read-Only Source Enforcement

Status: Accepted

## Context

The app indexes user media libraries that may contain years of irreplaceable personal
photos and videos. Sources in `config.yaml` carry a `read_only` flag intended to
prevent accidental modification.

An audit (Phase 3E) found that `read_only` is stored and returned by the API but
**never checked in code**. This is safe today because:

- The indexer is inherently read-only by design — it calls `rglob()`, reads EXIF/video
  metadata, computes hashes, and writes only to `index/` (SQLite, FAISS) and
  `data/thumbnails/`. It never writes, renames, or deletes anything under a source path.
- No API endpoints modify source files. All writes target `index/`, `data/`, or
  `config.yaml`.

Safety is therefore guaranteed by the current absence of write-back features, not by
active enforcement. As the app grows (e.g. EXIF write-back, "delete from source",
file organisation), this must change.

## Decision

### 1. Safe by default

New sources added through any interface (UI file picker, CLI, or direct config edit)
default to `read_only: true`. The previous API default of `read_only: false` is
corrected to `true`. Users who need write-back must explicitly set `read_only: false`.

### 2. Indexer — no enforcement code needed

The indexer pipeline opens source files only for reading (EXIF extraction, hashing,
CLIP embedding). Adding an explicit check would be redundant noise. The constraint is
documented here so future contributors know the invariant is load-bearing.

**Invariant:** Code in `src/msa_indexer/` must never open a source file for writing,
rename it, or delete it, regardless of the `read_only` flag. The flag is irrelevant
to the indexer; sources are always treated as read-only there.

### 3. API — code-level guard for write-back features

Any future API endpoint that would write to, rename, or delete a file under a source
path **must** call a guard before proceeding:

```python
def _require_writable(source: MediaSource) -> None:
    """Raise 403 if the source is marked read-only.
    Call this before any operation that modifies files under a source path.
    """
    if getattr(source, "read_only", True):
        raise HTTPException(
            status_code=403,
            detail=f"Source '{source.name}' is read-only. "
                   "Set read_only: false in config.yaml to enable write operations.",
        )
```

The guard lives in `src/msa_apps/search_api/app.py` alongside the other source
helpers. It is **not** called for search, face labelling, thumbnail generation, or
any other operation that writes only to the index or data directories.

### 4. OS-level enforcement — deferred

Mounting source paths as read-only at the OS level (e.g. `--bind-ro` in Linux namespaces,
or opening with `O_RDONLY` directory descriptors) would be the strongest guarantee.
This is deferred because:

- It requires elevated privileges or distro-level setup at install time.
- The current threat model is accidental bugs, not adversarial code. Code-level guards
  are sufficient for that threat.
- OS-level enforcement can be layered on top later without changing this policy.

## Enforcement checklist for future features

Before merging any feature that writes to source paths:

- [ ] Source is resolved to a `MediaSource` object before any file operation
- [ ] `_require_writable(source)` is called and the 403 case is tested
- [ ] The feature is documented as requiring `read_only: false`
- [ ] The Settings UI exposes the `read_only` toggle for that source

## Consequences

- `_SourceAdd.read_only` default changes from `False` to `True` in `app.py`.
- The Settings UI "Add Source" form defaults the read-only toggle to on.
- The `read_only` field continues to be stored in `config.yaml` and returned by
  `GET /config/sources` — no schema change.
- Future feature authors are responsible for calling `_require_writable()`.
  Code review must verify this for any PR that touches source files.
- The indexer invariant (sources always read-only in the indexer) is now documented
  here; violations are bugs regardless of the flag value.
