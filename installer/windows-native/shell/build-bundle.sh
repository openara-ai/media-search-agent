#!/usr/bin/env bash
set -euo pipefail

# Build Windows shell installer bundle.
# Produces a self-contained zip that install.ps1 downloads and extracts —
# no git, no Node.js, no package managers needed on the target machine.
#
# Must run in CI (Linux or macOS) — requires zip, curl.
# The bundle is Windows-targeted but the build script runs on POSIX systems.
#
# Usage (from repo root):
#   bash installer/windows-native/shell/build-bundle.sh --version 0.2.0
#   bash installer/windows-native/shell/build-bundle.sh --version 0.2.0 --dirty
#
# Outputs (in dist/shell/):
#   MediaSearchAgent-<version>-windows-x86_64.zip
#
# Bundle layout:
#   msa-<version>/
#     src/                       ← app source
#     scripts/                   ← runtime scripts (start.sh, stop.sh, lib/)
#     pyproject.toml
#     requirements.txt
#     requirements-windows.txt   ← Windows-specific requirements
#     src/msa_apps/ui/dist/      ← pre-built React UI (must exist before running)
#     bin/uv.exe                 ← uv binary for Windows x86_64
#     bin/exiftool.exe           ← ExifTool Windows binary
#     bin/MediaSearchAgentTray.exe ← system tray launcher
#     config.yaml.template       ← Windows-specific user-facing config

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DIST_DIR="$REPO_ROOT/dist/shell"

# ── Parse args ────────────────────────────────────────────────────────────────

VERSION=""
DIRTY=0

usage() {
  echo "Usage: $0 --version X.Y.Z [--dirty]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)   VERSION="${2:?'--version requires a value'}"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --dirty)     DIRTY=1; shift ;;
    *) echo "ERROR: unknown argument: $1"; usage; exit 1 ;;
  esac
done

[[ -z "$VERSION" ]] && { usage; exit 1; }

PLATFORM="windows"
ARCH="x86_64"
BUNDLE_NAME="MediaSearchAgent-${VERSION}-${PLATFORM}-${ARCH}"
BUNDLE_DIR="$(mktemp -d)/${BUNDLE_NAME}"
mkdir -p "$BUNDLE_DIR"

echo "==> Building $BUNDLE_NAME"
echo "    Repo:   $REPO_ROOT"
echo "    Output: $DIST_DIR/${BUNDLE_NAME}.zip"
if [[ "$DIRTY" -eq 1 ]]; then
  echo "    Source: working tree (--dirty)"
else
  echo "    Source: git archive HEAD"
fi

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

echo "==> [1/5] App source"
if [[ "$DIRTY" -eq 1 ]]; then
  # Use the local working tree so manual edits are included, but still exclude
  # transient frontend build/dependency directories from the source snapshot.
  tar \
    --exclude='src/msa_apps/ui/node_modules' \
    --exclude='src/msa_apps/ui/dist' \
    -cf - \
    -C "$REPO_ROOT" \
    src scripts pyproject.toml requirements.txt LICENSE NOTICE \
    | tar -x -C "$BUNDLE_DIR"
else
  git -C "$REPO_ROOT" archive HEAD \
    src/ scripts/ pyproject.toml requirements.txt LICENSE NOTICE \
    | tar -x -C "$BUNDLE_DIR"
fi
# Stamp the release version into pyproject.toml so `importlib.metadata` (and
# `msa status`) report the correct version after install.
PKG_VERSION=$(pep440_version "$VERSION")
sed -i.bak "s/^version = .*/version = \"$PKG_VERSION\"/" "$BUNDLE_DIR/pyproject.toml" \
  && rm -f "$BUNDLE_DIR/pyproject.toml.bak"

# Copy Windows-specific requirements file directly — onnxruntime-gpu is no
# longer in requirements.txt so no sed substitution is needed.
cp "$REPO_ROOT/requirements-windows.txt" "$BUNDLE_DIR/requirements-windows.txt"

# ── 2. React UI dist (must be pre-built before running this script) ───────────

echo "==> [2/5] React UI dist"
UI_DIST="$REPO_ROOT/src/msa_apps/ui/dist"
if [[ ! -d "$UI_DIST" ]]; then
  echo "ERROR: React UI dist not found at $UI_DIST"
  echo "       Build it first: npm --prefix src/msa_apps/ui ci && npm --prefix src/msa_apps/ui run build"
  exit 1
fi
mkdir -p "$BUNDLE_DIR/src/msa_apps/ui"
cp -r "$UI_DIST" "$BUNDLE_DIR/src/msa_apps/ui/dist"

# ── 3. uv binary (Windows x86_64) ────────────────────────────────────────────

echo "==> [3/5] uv $UV_VERSION (Windows x86_64)"
BIN_DIR="$BUNDLE_DIR/bin"
mkdir -p "$BIN_DIR"

TMP="$(mktemp -d)"

UV_ZIP="uv-x86_64-pc-windows-msvc.zip"
download \
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ZIP}" \
  "$TMP/uv.zip" "uv $UV_VERSION (Windows)"
unzip -q "$TMP/uv.zip" -d "$TMP/uv-extract"
# zip extracts to a flat directory with uv.exe and uvx.exe
find "$TMP/uv-extract" -name "uv.exe" | head -1 | xargs -I{} cp {} "$BIN_DIR/uv.exe"
[[ -f "$BIN_DIR/uv.exe" ]] || { echo "ERROR: uv.exe not found after extraction — archive layout may have changed"; exit 1; }

rm -rf "$TMP"

# ── 4. ExifTool (Windows x86_64) ─────────────────────────────────────────────
# Windows ExifTool ships as exiftool(-k).exe PLUS a sibling directory
# "exiftool_files/" containing the Perl runtime + modules. The exe is just
# a thin wrapper — without exiftool_files/ on disk next to it, the .exe
# runs but fails on most operations (subprocess exit 1). We must bundle
# both.
# Use SourceForge: exiftool.org only keeps the current/latest release, so
# pinned versions 404 once a newer release ships; SourceForge retains all
# past versions.

echo "==> [4/5] ExifTool $EXIFTOOL_VERSION (Windows)"
TMP="$(mktemp -d)"
EXIFTOOL_ZIP="exiftool-${EXIFTOOL_VERSION}_64.zip"
download \
  "https://sourceforge.net/projects/exiftool/files/${EXIFTOOL_ZIP}/download" \
  "$TMP/exiftool.zip" "ExifTool $EXIFTOOL_VERSION"
unzip -q "$TMP/exiftool.zip" -d "$TMP/exiftool-extract"
EXIFTOOL_EXE="$(find "$TMP/exiftool-extract" -name "exiftool(-k).exe" | head -1)"
[[ -f "$EXIFTOOL_EXE" ]] || { echo "ERROR: exiftool(-k).exe not found in archive — check EXIFTOOL_VERSION"; exit 1; }
EXIFTOOL_SRC_DIR="$(dirname "$EXIFTOOL_EXE")"
EXIFTOOL_FILES_DIR="$EXIFTOOL_SRC_DIR/exiftool_files"
[[ -d "$EXIFTOOL_FILES_DIR" ]] || { echo "ERROR: exiftool_files/ not found alongside the exe — archive layout may have changed"; exit 1; }
cp "$EXIFTOOL_EXE" "$BIN_DIR/exiftool.exe"
cp -r "$EXIFTOOL_FILES_DIR" "$BIN_DIR/exiftool_files"
# ExifTool zip encodes Windows read-only attributes; macOS unzip honours them.
# chmod before rm to avoid Permission denied on the extracted exiftool_files/ tree.
chmod -R u+w "$TMP"

rm -rf "$TMP"

# ── 4b. Config template ───────────────────────────────────────────────────────

echo "==> [4b] Config template"
TEMPLATE="$REPO_ROOT/installer/windows-native/config.windows.yaml.template"
[[ -f "$TEMPLATE" ]] || { echo "ERROR: config template not found at $TEMPLATE"; exit 1; }
cp "$TEMPLATE" "$BUNDLE_DIR/config.yaml.template"

# ── 4c. Uninstaller ───────────────────────────────────────────────────────────

echo "==> [4c] Uninstaller"
cp "$REPO_ROOT/installer/windows-native/shell/uninstall.ps1" "$BUNDLE_DIR/uninstall.ps1"

# ── 4d. Start/stop launchers for Start Menu shortcuts ───────────────────────

echo "==> [4d] Windows launcher scripts"
cp "$REPO_ROOT/installer/windows-native/start.ps1" "$BUNDLE_DIR/start.ps1"
cp "$REPO_ROOT/installer/windows-native/stop.ps1" "$BUNDLE_DIR/stop.ps1"

# ── 4e. Tray app (C# WinForms, self-contained single-file exe) ───────────────
#
# Built on a host with the .NET 8 SDK targeting win-x64.
# Requires .NET 8 SDK: https://dot.net/download
# The resulting exe is self-contained — no .NET redistributable on the user's machine.

echo "==> [4e] Tray app"
TRAY_PROJ="$REPO_ROOT/installer/windows-native/tray/MediaSearchAgentTray.csproj"
if [[ ! -f "$TRAY_PROJ" ]]; then
  echo "ERROR: Tray project not found at $TRAY_PROJ"
  exit 1
fi

if ! command -v dotnet &>/dev/null; then
  echo "ERROR: dotnet not found — install .NET 8 SDK or newer to build the tray app"
  echo "       https://dotnet.microsoft.com/download"
  exit 1
fi

TRAY_OUT="$(mktemp -d)/tray-publish"
dotnet publish "$TRAY_PROJ" \
  -r win-x64 \
  --self-contained true \
  -p:PublishSingleFile=true \
  -p:IncludeNativeLibrariesForSelfExtract=true \
  -p:Version="$VERSION" \
  -p:InformationalVersion="$VERSION" \
  -c Release \
  -o "$TRAY_OUT" \
  --nologo \
  -v quiet

TRAY_EXE="$TRAY_OUT/MediaSearchAgentTray.exe"
if [[ ! -f "$TRAY_EXE" ]]; then
  echo "ERROR: Tray exe not found after publish — check build output above"
  exit 1
fi
cp "$TRAY_EXE" "$BIN_DIR/MediaSearchAgentTray.exe"
rm -rf "$(dirname "$TRAY_OUT")"
echo "    -> bin/MediaSearchAgentTray.exe ($(du -sh "$BIN_DIR/MediaSearchAgentTray.exe" | cut -f1))"

# ── 5. Package into zip ───────────────────────────────────────────────────────

echo "==> [5/5] Creating zip"
mkdir -p "$DIST_DIR"
OUTPUT="$DIST_DIR/${BUNDLE_NAME}.zip"
(cd "$(dirname "$BUNDLE_DIR")" && zip -qr "$OUTPUT" "$BUNDLE_NAME")
rm -rf "$(dirname "$BUNDLE_DIR")"

SIZE="$(du -sh "$OUTPUT" | cut -f1)"
echo ""
echo "==> Done: $OUTPUT ($SIZE)"
