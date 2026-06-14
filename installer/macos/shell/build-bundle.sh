#!/usr/bin/env bash
set -euo pipefail

# Build shell installer bundles for macOS and Linux.
# Produces a self-contained tarball that install.sh downloads and extracts —
# no git, no Node.js, no package managers needed on the target machine.
#
# Usage (from repo root):
#   bash installer/macos/shell/build-bundle.sh --version 0.2.0
#   bash installer/macos/shell/build-bundle.sh --version 0.2.0 --platform linux
#   bash installer/macos/shell/build-bundle.sh --version 0.2.0 --dirty
#     --dirty  Copy app source from the working tree instead of git archive HEAD.
#              Picks up staged and unstaged changes. Use for local dev testing only.
#
# Outputs (in dist/shell/):
#   MediaSearchAgent-<version>-macos-arm64.tar.gz
#   MediaSearchAgent-<version>-linux-x86_64.tar.gz
#
# Bundle layout:
#   msa-<version>/
#     src/                       ← app source
#     scripts/                   ← runtime scripts (start.sh, stop.sh, lib/)
#     pyproject.toml
#     requirements.txt
#     src/msa_apps/ui/dist/      ← pre-built React UI (must exist before running)
#     bin/uv                     ← uv binary (platform-specific)
#     bin/exiftool               ← exiftool (pure Perl)
#     bin/lib/                   ← exiftool Perl libs
#     bin/mediainfo              ← static mediainfo
#     config.yaml.template       ← platform-specific user-facing config

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist/shell"

# ── Parse args ────────────────────────────────────────────────────────────────

VERSION=""
PLATFORM=""   # macos | linux — defaults to current host OS
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
    *) echo "ERROR: unknown argument: $1"; echo "Usage: $0 --version X.Y.Z [--platform macos|linux] [--dirty] [--msa-ranker-wheel PATH]"; exit 1 ;;
  esac
done

[[ -z "$VERSION" ]] && { echo "Usage: $0 --version X.Y.Z [--platform macos|linux] [--dirty] [--msa-ranker-wheel PATH]"; exit 1; }

if [[ -z "$PLATFORM" ]]; then
  case "$(uname -s)" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *) echo "ERROR: unsupported OS $(uname -s) — pass --platform explicitly"; exit 1 ;;
  esac
fi

# Linux bundles are x86_64 only regardless of the host machine architecture.
# macOS bundles use the host arch (arm64 on Apple Silicon, the only supported target).
if [[ "$PLATFORM" == "linux" ]]; then
  ARCH="x86_64"
else
  case "$(uname -m)" in
    arm64|aarch64) ARCH="arm64" ;;
    *) ARCH="x86_64" ;;
  esac
fi

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

if [[ "$PLATFORM" == "macos" ]]; then
  [[ "$ARCH" == "arm64" ]] \
    && UV_ARCHIVE="uv-aarch64-apple-darwin.tar.gz" \
    || UV_ARCHIVE="uv-x86_64-apple-darwin.tar.gz"
else
  [[ "$ARCH" == "arm64" ]] \
    && UV_ARCHIVE="uv-aarch64-unknown-linux-musl.tar.gz" \
    || UV_ARCHIVE="uv-x86_64-unknown-linux-musl.tar.gz"
fi

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

if [[ "$PLATFORM" == "macos" ]]; then
  # mediainfo — extract from official macOS DMG
  MEDIAINFO_VERSION="24.01"
  DMG_URL="https://mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION}/MediaInfo_CLI_${MEDIAINFO_VERSION}_Mac.dmg"
  download "$DMG_URL" "$TMP/mediainfo.dmg" "mediainfo $MEDIAINFO_VERSION"
  MOUNT="$(mktemp -d)"
  hdiutil attach "$TMP/mediainfo.dmg" -mountpoint "$MOUNT" -nobrowse -quiet
  PKG="$(find "$MOUNT" -name "*.pkg" | head -1)"
  PKG_EXPAND="$TMP/pkg_expand"
  pkgutil --expand "$PKG" "$PKG_EXPAND"
  hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
  PAYLOAD="$(find "$PKG_EXPAND" -name Payload | head -1)"
  PAYLOAD_DIR="$TMP/pkg_payload"
  mkdir -p "$PAYLOAD_DIR"
  # BSD cpio (macOS) does not support -D; use cd instead
  (cd "$PAYLOAD_DIR" && gzip -dc "$PAYLOAD" | cpio -id 2>/dev/null) || true
  MEDIAINFO_BIN="$(find "$PAYLOAD_DIR" -name mediainfo -type f | head -1)"
  if [[ -f "$MEDIAINFO_BIN" ]]; then
    if file "$MEDIAINFO_BIN" | grep -q "universal binary"; then
      lipo -thin "$ARCH" "$MEDIAINFO_BIN" -output "$BIN_DIR/mediainfo"
    else
      cp "$MEDIAINFO_BIN" "$BIN_DIR/mediainfo"
    fi
    chmod +x "$BIN_DIR/mediainfo"
  fi
  [[ -f "$BIN_DIR/mediainfo" ]] || { echo "ERROR: could not obtain mediainfo binary"; exit 1; }

else
  # Linux — static builds, no sudo required

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
fi

rm -rf "$TMP"

# ── 4b. Swift menu bar app ───────────────────────────────────────────────────
# Compile main.swift into a native arm64 NSStatusItem app and embed it in the
# bundle. install.sh extracts it to ~/Applications/MediaSearchAgent.app and
# writes a msa-paths.env sidecar so the binary finds the shell-bundle layout.
# Only built on macOS (swiftc not available on Linux runners).

if [[ "$PLATFORM" == "macos" ]]; then
  echo "==> [4b/6] Swift menu bar app"
  SWIFT_SRC="$REPO_ROOT/installer/macos/launcher_app/main.swift"
  APP_DIR="$BUNDLE_DIR/MediaSearchAgent.app"
  mkdir -p "$APP_DIR/Contents/MacOS"
  mkdir -p "$APP_DIR/Contents/Resources"

  swiftc \
    -framework AppKit \
    -framework Foundation \
    -target "arm64-apple-macos12.0" \
    -O \
    -o "$APP_DIR/Contents/MacOS/MediaSearchAgent" \
    "$SWIFT_SRC" \
    || { echo "ERROR: swiftc failed — Swift menu bar app not included"; rm -rf "$APP_DIR"; }

  if [[ -d "$APP_DIR" ]]; then
    ICON="$REPO_ROOT/installer/macos/assets/icon.icns"
    [[ -f "$ICON" ]] && cp "$ICON" "$APP_DIR/Contents/Resources/AppIcon.icns"

    cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>        <string>ai.openara.mediasearchagent</string>
    <key>CFBundleName</key>              <string>MediaSearchAgent</string>
    <key>CFBundleDisplayName</key>       <string>MediaSearchAgent</string>
    <key>CFBundleVersion</key>           <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleExecutable</key>        <string>MediaSearchAgent</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>CFBundleIconFile</key>          <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>    <string>12.0</string>
    <key>LSUIElement</key>               <true/>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>NSPrincipalClass</key>          <string>NSApplication</string>
    <key>LSArchitectures</key>
    <array><string>arm64</string></array>
</dict>
</plist>
PLIST
    chmod +x "$APP_DIR/Contents/MacOS/MediaSearchAgent"
    echo "    Swift menu bar app built"
  fi
fi

# ── 5. Config template ────────────────────────────────────────────────────────

echo "==> [5/6] Config template"
# Use a platform-specific template. installer/linux/config.linux.yaml.template
# exists for Linux; macOS falls back to installer/macos/config.macos.yaml.template.
if [[ "$PLATFORM" == "linux" && -f "$REPO_ROOT/installer/linux/config.linux.yaml.template" ]]; then
  TEMPLATE="$REPO_ROOT/installer/linux/config.linux.yaml.template"
else
  TEMPLATE="$REPO_ROOT/installer/macos/config.macos.yaml.template"
fi
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
