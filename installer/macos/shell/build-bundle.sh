#!/usr/bin/env bash
set -euo pipefail

# Build the Linux shell installer bundle.
# Produces a self-contained tarball that install.sh downloads and extracts —
# no git, no Node.js, no package managers needed on the target machine.
#
# Linux-only since M-7/S-5.5: macOS + Windows moved to the Tauri desktop app;
# their shell bundles were retired. (This file still lives under installer/macos/
# for git history; it now builds only the Linux bundle. release.yml's
# build-linux-shell-bundle calls it with --platform linux.)
#
# Usage (from repo root):
#   bash installer/macos/shell/build-bundle.sh --version 0.2.0 [--platform linux]
#   bash installer/macos/shell/build-bundle.sh --version 0.2.0 --dirty
#     --dirty  Copy app source from the working tree instead of git archive HEAD.
#              Picks up staged and unstaged changes. Use for local dev testing only.
#
# Output (in dist/shell/):
#   MediaSearchAgent-<version>-linux-x86_64.tar.gz
#
# Bundle layout:
#   msa-<version>/
#     src/                       ← app source
#     scripts/                   ← runtime scripts (start.sh, stop.sh, lib/)
#     pyproject.toml
#     requirements.txt
#     src/msa_apps/ui/dist/      ← pre-built React UI (must exist before running)
#     bin/uv                     ← uv binary (linux musl)
#     bin/exiftool               ← exiftool (pure Perl)
#     bin/lib/                   ← exiftool Perl libs
#     bin/mediainfo              ← static mediainfo
#     config.yaml.template       ← Linux user-facing config

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist/shell"

# ── Parse args ────────────────────────────────────────────────────────────────

VERSION=""
# Linux-only since M-7/S-5.5: the macOS + Windows shell bundles were retired in
# favour of the Tauri desktop app. This builder still produces the Linux
# tar.gz shell bundle (release.yml build-linux-shell-bundle). --platform is
# accepted for backward compatibility but must be linux.
PLATFORM="linux"
DIRTY=""      # non-empty = copy from working tree instead of git archive

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)    VERSION="${2:?'--version requires a value'}"; shift 2 ;;
    --version=*)  VERSION="${1#*=}"; shift ;;
    --platform)   PLATFORM="${2:?'--platform requires a value'}"; shift 2 ;;
    --platform=*) PLATFORM="${1#*=}"; shift ;;
    --dirty)      DIRTY=1; shift ;;
    --msa-ranker-wheel)   RANKER_WHEEL="${2:?'--msa-ranker-wheel requires a value'}"; shift 2 ;;
    --msa-ranker-wheel=*) RANKER_WHEEL="${1#*=}"; shift ;;
    *) echo "ERROR: unknown argument: $1"; echo "Usage: $0 --version X.Y.Z [--platform linux] [--dirty] [--msa-ranker-wheel PATH]"; exit 1 ;;
  esac
done

[[ -z "$VERSION" ]] && { echo "Usage: $0 --version X.Y.Z [--platform linux] [--dirty] [--msa-ranker-wheel PATH]"; exit 1; }

if [[ "$PLATFORM" != "linux" ]]; then
  echo "ERROR: only --platform linux is supported. The macOS/Windows shell bundles were"
  echo "       retired in M-7/S-5.5; use the Tauri desktop app (build-desktop-* in release.yml)."
  exit 1
fi

# Linux bundles are x86_64 only regardless of the host machine architecture.
ARCH="x86_64"

BUNDLE_NAME="MediaSearchAgent-${VERSION}-${PLATFORM}-${ARCH}"
BUNDLE_DIR="$(mktemp -d)/${BUNDLE_NAME}"
mkdir -p "$BUNDLE_DIR"

echo "==> Building $BUNDLE_NAME"
echo "    Repo:   $REPO_ROOT"
echo "    Output: $DIST_DIR/${BUNDLE_NAME}.tar.gz"
[[ -n "$DIRTY" ]] && echo "    Source: working tree (--dirty)"

# ── Helpers ───────────────────────────────────────────────────────────────────

# shellcheck source=scripts/versions.env
source "$REPO_ROOT/scripts/versions.env"
# shellcheck source=scripts/lib/version.sh
source "$REPO_ROOT/scripts/lib/version.sh"

download() {
  local url="$1" dest="$2" desc="${3:-$url}"
  echo "    Downloading $desc..."
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$dest" "$url"
}

# ── 1. App source ─────────────────────────────────────────────────────────────
# Default: git archive HEAD — clean snapshot, no untracked/modified files.
# --dirty: copy directly from the working tree to pick up staged/unstaged changes.

echo "==> [1/6] App source"
# The bundle ships requirements-api.txt — the lean runtime contract — not
# requirements.txt (the full dev requirements with notebooks/plotting/etc).
# install.sh reads requirements-api.txt directly; failing if absent surfaces
# a build-bundle bug immediately rather than silently shipping the wrong deps.
if [[ -n "$DIRTY" ]]; then
  for item in src pyproject.toml requirements-api.txt LICENSE NOTICE; do
    [[ -e "$REPO_ROOT/$item" ]] && cp -r "$REPO_ROOT/$item" "$BUNDLE_DIR/$item"
  done
  mkdir -p "$BUNDLE_DIR/scripts/lib"
  cp "$REPO_ROOT/scripts/start.sh" "$REPO_ROOT/scripts/stop.sh" "$BUNDLE_DIR/scripts/"
  cp "$REPO_ROOT/scripts/lib/common.sh" "$BUNDLE_DIR/scripts/lib/"
  # Exclude generated artefacts that don't belong in a bundle
  rm -rf "$BUNDLE_DIR/src/msa_apps/ui/node_modules" \
         "$BUNDLE_DIR/src/msa_apps/ui/dist" 2>/dev/null || true
  find "$BUNDLE_DIR/src" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
else
  git -C "$REPO_ROOT" archive HEAD \
    src/ pyproject.toml requirements-api.txt LICENSE NOTICE \
    | tar -x -C "$BUNDLE_DIR"
  mkdir -p "$BUNDLE_DIR/scripts/lib"
  git -C "$REPO_ROOT" archive HEAD \
    scripts/start.sh scripts/stop.sh scripts/lib/common.sh \
    | tar -x -C "$BUNDLE_DIR"
fi
# Stamp the release version into pyproject.toml so `importlib.metadata` (and
# `msa status`) report the correct version after install.
PKG_VERSION=$(pep440_version "$VERSION")
sed -i.bak "s/^version = .*/version = \"$PKG_VERSION\"/" "$BUNDLE_DIR/pyproject.toml" \
  && rm -f "$BUNDLE_DIR/pyproject.toml.bak"

# Vendored msa_ranker wheel (the learned-reranker serving library). Self-contained:
# the zero-dependency serving lib ships INSIDE the bundle so the installed venv has it
# with NO install-time network fetch. install.sh installs it after the runtime deps.
# Absent ⇒ the reranker is simply unavailable (MSA's import is guarded — INV-9).
# Auto-detects installer/wheels/msa_ranker-*.whl; --msa-ranker-wheel overrides.
if [[ -z "${RANKER_WHEEL:-}" ]]; then
  _whls=("$REPO_ROOT"/installer/wheels/msa_ranker-*.whl)
  if [[ ${#_whls[@]} -gt 1 ]]; then
    echo "ERROR: multiple msa_ranker wheels in installer/wheels/ — remove stale ones:"
    printf '         %s\n' "${_whls[@]}"; exit 1
  fi
  [[ -f "${_whls[0]}" ]] && RANKER_WHEEL="${_whls[0]}"
fi
if [[ -n "${RANKER_WHEEL:-}" ]]; then
  [[ -f "$RANKER_WHEEL" ]] || { echo "ERROR: --msa-ranker-wheel not found: $RANKER_WHEEL"; exit 1; }
  mkdir -p "$BUNDLE_DIR/wheels"
  cp "$RANKER_WHEEL" "$BUNDLE_DIR/wheels/"
  echo "    Bundled msa_ranker wheel: $(basename "$RANKER_WHEEL")"
else
  echo "    (no msa_ranker wheel found — bundle runs on the heuristic only)"
fi

# ── 2. React UI dist (must be pre-built before running this script) ───────────

echo "==> [2/6] React UI dist"
UI_DIST="$REPO_ROOT/src/msa_apps/ui/dist"
if [[ ! -d "$UI_DIST" ]]; then
  echo "ERROR: React UI dist not found at $UI_DIST"
  echo "       Build it first: npm --prefix src/msa_apps/ui ci && npm --prefix src/msa_apps/ui run build"
  exit 1
fi
mkdir -p "$BUNDLE_DIR/src/msa_apps/ui"
cp -r "$UI_DIST" "$BUNDLE_DIR/src/msa_apps/ui/dist"

# ── 3. uv binary ─────────────────────────────────────────────────────────────

echo "==> [3/6] uv $UV_VERSION"
BIN_DIR="$BUNDLE_DIR/bin"
mkdir -p "$BIN_DIR"

TMP="$(mktemp -d)"

UV_ARCHIVE="uv-x86_64-unknown-linux-musl.tar.gz"

download \
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}" \
  "$TMP/uv.tar.gz" "uv $UV_VERSION"
tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
cp "$TMP"/uv-*/uv "$BIN_DIR/uv"
chmod +x "$BIN_DIR/uv"

# ── 4. Static tool binaries ───────────────────────────────────────────────────

echo "==> [4/6] Static binaries (exiftool, mediainfo)"

# exiftool — pure Perl, same for all platforms
download \
  "https://github.com/exiftool/exiftool/archive/refs/tags/${EXIFTOOL_VERSION}.tar.gz" \
  "$TMP/exiftool.tar.gz" "exiftool $EXIFTOOL_VERSION"
tar -xzf "$TMP/exiftool.tar.gz" -C "$TMP"
cp "$TMP/exiftool-${EXIFTOOL_VERSION}/exiftool" "$BIN_DIR/exiftool"
cp -r "$TMP/exiftool-${EXIFTOOL_VERSION}/lib" "$BIN_DIR/lib"
chmod +x "$BIN_DIR/exiftool"

# mediainfo official Linux binary package from mediaarea.net.
# Do not use the MediaInfo source archive here: it is not a guaranteed
# prebuilt CLI binary, and can leave bin/mediainfo missing at release time.
MEDIAINFO_VERSION="25.04"
download \
  "https://old.mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION}/MediaInfo_CLI_${MEDIAINFO_VERSION}_Lambda_x86_64.zip" \
  "$TMP/mediainfo.zip" "mediainfo $MEDIAINFO_VERSION (linux x86_64)"
unzip -q "$TMP/mediainfo.zip" -d "$TMP/mediainfo"
MEDIAINFO_BIN="$(find "$TMP/mediainfo" -type f -name mediainfo | head -1 || true)"
[[ -n "$MEDIAINFO_BIN" ]] || { echo "ERROR: mediainfo binary not found in Linux zip package"; exit 1; }
cp "$MEDIAINFO_BIN" "$BIN_DIR/mediainfo"
chmod +x "$BIN_DIR/mediainfo"

rm -rf "$TMP"

# ── 5. Config template ────────────────────────────────────────────────────────

echo "==> [5/6] Config template"
TEMPLATE="$REPO_ROOT/installer/linux/config.linux.yaml.template"
[[ -f "$TEMPLATE" ]] || { echo "ERROR: config template not found at $TEMPLATE"; exit 1; }
cp "$TEMPLATE" "$BUNDLE_DIR/config.yaml.template"

# ── 5b. Uninstaller ───────────────────────────────────────────────────────────

echo "==> [5b] Uninstaller"
cp "$REPO_ROOT/installer/macos/shell/uninstall.sh" "$BUNDLE_DIR/uninstall.sh"
chmod +x "$BUNDLE_DIR/uninstall.sh"

# ── 6. Package into tarball ───────────────────────────────────────────────────

echo "==> [6/6] Creating tarball"
mkdir -p "$DIST_DIR"
OUTPUT="$DIST_DIR/${BUNDLE_NAME}.tar.gz"
tar -czf "$OUTPUT" -C "$(dirname "$BUNDLE_DIR")" "$BUNDLE_NAME"
rm -rf "$(dirname "$BUNDLE_DIR")"

SIZE="$(du -sh "$OUTPUT" | cut -f1)"
echo ""
echo "==> Done: $OUTPUT ($SIZE)"
