#!/usr/bin/env bash
# Media Search Agent — macOS installer build script
#
# Produces:
#   dist/macos/MediaSearchAgent-<VERSION>-Setup.dmg
#
# Prerequisites (installed automatically if missing):
#   - Homebrew (must be pre-installed)
#   - create-dmg   (brew install create-dmg)
#   - Platypus CLI (optional — used if already installed interactively from Platypus.app;
#                   if absent build.sh constructs the .app bundle manually)
#
# Usage:
#   bash installer/macos/build.sh [--version 1.0.0] [--skip-binaries]
#
#   --skip-binaries   Skip downloading static binaries (use existing files in
#                     installer/macos/bin/ and installer/macos/lib/)
#
# Run from the repo root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=scripts/versions.env
source "$REPO_ROOT/scripts/versions.env"

# ── Options ───────────────────────────────────────────────────────────────────

VERSION="1.0.0"
SKIP_BINARIES=false
# arm64 (Apple Silicon) only. Intel Macs are 4+ years old and not a target
# for this ML-heavy app. Add --arch support back if Intel is ever needed.
BUILD_ARCH="arm64"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)       VERSION="$2"; shift 2 ;;
    --skip-binaries) SKIP_BINARIES=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────

INSTALLER_DIR="$SCRIPT_DIR"
PAYLOAD_DIR="$INSTALLER_DIR/payload"
SCRIPTS_DIR="$INSTALLER_DIR/scripts"
ASSETS_DIR="$INSTALLER_DIR/assets"
BIN_DIR="$INSTALLER_DIR/bin"
LIB_DIR="$INSTALLER_DIR/lib"

DIST_DIR="$REPO_ROOT/dist/macos"
WORK_DIR="$REPO_ROOT/build/macos"

mkdir -p "$DIST_DIR" "$WORK_DIR" "$BIN_DIR" "$LIB_DIR"

log()  { echo "[build] $*"; }
fail() { echo "[build] ERROR: $*" >&2; exit 1; }

log "Building MediaSearchAgent $VERSION for macOS (arm64)"
log "Repo: $REPO_ROOT"

# ── 1. Check / install build tools ───────────────────────────────────────────

log "Checking build tools..."

if ! command -v brew &>/dev/null; then
  fail "Homebrew is required. Install from https://brew.sh"
fi

for tool in create-dmg; do
  if ! command -v "$tool" &>/dev/null; then
    log "Installing $tool via Homebrew..."
    brew install "$tool"
  fi
done

# Note: Platypus CLI is NOT installed here. It requires the interactive
# "Install Command Line Tool" step from within Platypus.app, which cannot be
# automated. build.sh detects it opportunistically; if absent it builds the
# .app bundle manually (identical output, no Platypus dependency).

log "Build tools OK"

# ── 2. Download static binaries ───────────────────────────────────────────────
# Downloads arch-specific exiftool and mediainfo.
# These are not committed to git and are downloaded fresh each build.

if [[ "$SKIP_BINARIES" == "true" ]]; then
  log "Skipping binary download (--skip-binaries)"
else
  log "Downloading static binaries..."

  # ── exiftool (arch-neutral Perl script) ───────────────────────────────────────
  if [[ ! -f "$BIN_DIR/exiftool" ]]; then
    log "Downloading exiftool $EXIFTOOL_VERSION..."
    TMP=$(mktemp -d)
    # GitHub tag archives are permanent; exiftool.org only hosts the latest release.
    curl -fsSL "https://github.com/exiftool/exiftool/archive/refs/tags/${EXIFTOOL_VERSION}.tar.gz" -o "$TMP/exiftool.tar.gz"
    tar -xzf "$TMP/exiftool.tar.gz" -C "$TMP"
    cp "$TMP/exiftool-${EXIFTOOL_VERSION}/exiftool" "$BIN_DIR/exiftool"
    chmod +x "$BIN_DIR/exiftool"
    # Bundle the ExifTool lib directory — exiftool looks for lib/ next to itself.
    cp -r "$TMP/exiftool-${EXIFTOOL_VERSION}/lib" "$BIN_DIR/lib"
    rm -rf "$TMP"
    log "exiftool installed"
  else
    log "exiftool: already present"
  fi

  # ── mediainfo (arm64) + libmediainfo.dylib ────────────────────────────────────
  # The official mediaarea.net DMG ships a universal binary inside a .pkg.
  # We thin it to arm64 and also extract libmediainfo.dylib (needed by pymediainfo).
  MEDIAINFO_VERSION="24.11"
  if [[ ! -f "$BIN_DIR/mediainfo" || ! -f "$LIB_DIR/libmediainfo.dylib" ]]; then
    log "Downloading MediaInfo $MEDIAINFO_VERSION..."
    TMP=$(mktemp -d)

    DMG_URL="https://mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION}/MediaInfo_CLI_${MEDIAINFO_VERSION}_Mac.dmg"
    curl -fsSL "$DMG_URL" -o "$TMP/mediainfo.dmg"

    MOUNT_POINT="$TMP/dmg_mount"
    mkdir -p "$MOUNT_POINT"
    hdiutil attach "$TMP/mediainfo.dmg" -mountpoint "$MOUNT_POINT" -nobrowse -quiet

    PKG_PATH=$(find "$MOUNT_POINT" -name "*.pkg" | head -1)
    if [[ -z "$PKG_PATH" ]]; then
      hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true
      fail "mediainfo .pkg not found inside the DMG"
    fi

    PKG_EXPAND="$TMP/pkg_expanded"
    pkgutil --expand "$PKG_PATH" "$PKG_EXPAND"
    hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true

    PAYLOAD_DIR_TMP="$TMP/payload"
    mkdir -p "$PAYLOAD_DIR_TMP"
    PAYLOAD=$(find "$PKG_EXPAND" -name "Payload" | head -1)
    (cd "$PAYLOAD_DIR_TMP" && cat "$PAYLOAD" | gunzip | cpio -id 2>/dev/null)

    MEDIAINFO_BIN=$(find "$PAYLOAD_DIR_TMP" -name "mediainfo" -type f | head -1)
    if [[ -z "$MEDIAINFO_BIN" ]]; then
      rm -rf "$TMP"
      fail "mediainfo binary not found inside extracted pkg Payload"
    fi

    # Thin the universal binary to arm64 only.
    if file "$MEDIAINFO_BIN" | grep -q "universal binary"; then
      lipo -thin arm64 "$MEDIAINFO_BIN" -output "$BIN_DIR/mediainfo"
      log "  mediainfo: thinned universal → arm64"
    else
      cp "$MEDIAINFO_BIN" "$BIN_DIR/mediainfo"
    fi
    chmod +x "$BIN_DIR/mediainfo"

    # Extract libmediainfo.dylib — pymediainfo loads it via ctypes at runtime.
    MEDIAINFO_DYLIB=$(find "$PAYLOAD_DIR_TMP" -name "libmediainfo.dylib" | head -1)
    if [[ -n "$MEDIAINFO_DYLIB" ]]; then
      cp "$MEDIAINFO_DYLIB" "$LIB_DIR/libmediainfo.dylib"
      log "  libmediainfo.dylib extracted to $LIB_DIR"
    else
      log "  WARNING: libmediainfo.dylib not found in pkg payload — pymediainfo may fail"
    fi

    rm -rf "$TMP"
    log "mediainfo installed"
  else
    log "mediainfo: already present (binary + dylib)"
  fi

  # ── uv (arm64) ───────────────────────────────────────────────────────────────
  if [[ ! -f "$BIN_DIR/uv" ]]; then
    log "Downloading uv $UV_VERSION..."
    TMP=$(mktemp -d)
    curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-apple-darwin.tar.gz" \
      -o "$TMP/uv.tar.gz"
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
    cp "$TMP"/uv-*/uv "$BIN_DIR/uv"
    chmod +x "$BIN_DIR/uv"
    rm -rf "$TMP"
    log "uv $UV_VERSION installed"
  else
    log "uv: already present"
  fi

  log "Static binaries ready"
fi

# ── 3. Assemble .pkg payload ──────────────────────────────────────────────────
# Per Phase 2F: APP_DIR = MediaSearchAgent.app/Contents/Resources/
# All app code (Python source, scripts, config template, binaries) lives inside
# the .app bundle. libmediainfo.dylib goes to Contents/Resources/lib/ so that
# start.sh's existing DYLD_LIBRARY_PATH="$MSA_ROOT/lib:..." resolves it without
# any script changes (MSA_ROOT = Contents/Resources/ via SCRIPT_DIR/.. in start.sh).

log "Assembling .pkg payload..."

# Build the bundle in $WORK_DIR; copy into payload at end of step 4.
# LAUNCHER_APP is defined here so steps 3 and 4 share the same path.
LAUNCHER_APP="$WORK_DIR/MediaSearchAgent.app"
APP_INSTALL_DIR="$LAUNCHER_APP/Contents/Resources"

# Clean and create the full bundle skeleton.
# Also remove stale non-.app directory left by older builds.
rm -rf "$LAUNCHER_APP" "$PAYLOAD_DIR/Applications/MediaSearchAgent.app" \
       "$PAYLOAD_DIR/Applications/MediaSearchAgent"
mkdir -p "$APP_INSTALL_DIR"
mkdir -p "$LAUNCHER_APP/Contents/MacOS"
mkdir -p "$APP_INSTALL_DIR/lib"

PACKAGE_PATHS=(
  src/
  README.md
  LICENSE
  NOTICE
  pyproject.toml
  requirements.txt
  requirements-api.txt
  scripts/setup.sh
  scripts/start.sh
  scripts/stop.sh
  scripts/lib/common.sh
  installer/macos/uninstaller.sh
)

# Build React UI — ui/dist is gitignored so it must be built before git archive.
log "Building React UI..."
(cd "$REPO_ROOT/src/msa_apps/ui" && npm ci && npm run build) \
  || { log "ERROR: React UI build failed"; exit 1; }

# Export only the files needed by the packaged macOS runtime.
# Keep this allowlist tight so the installer does not ship Linux/Windows dev scripts,
# test helpers, or one-off maintenance utilities the app never invokes on macOS.
# Note: ui/dist is gitignored so git archive won't include it; copied separately below.
git -C "$REPO_ROOT" archive --format=tar HEAD "${PACKAGE_PATHS[@]}" \
  | tar -x -C "$APP_INSTALL_DIR"

# Override scripts with the working-tree versions so that uncommitted changes
# (e.g. fixes in setup.sh) are picked up without requiring a commit first.
# git archive only sees HEAD, so any local edits would be silently excluded.
for f in "${PACKAGE_PATHS[@]}"; do
  src="$REPO_ROOT/$f"
  if [[ -f "$src" ]]; then
    cp "$src" "$APP_INSTALL_DIR/$f"
  elif [[ -d "$src" ]]; then
    cp -r "$src/." "$APP_INSTALL_DIR/$f/"
  fi
done

# Remove dev-only node_modules — build tools (esbuild, rollup, fsevents) must
# not ship in the installer; Apple notarytool rejects unsigned third-party binaries.
rm -rf "$APP_INSTALL_DIR/src/msa_apps/ui/node_modules"

# Copy pre-built UI dist (gitignored, built above) into the payload.
cp -r "$REPO_ROOT/src/msa_apps/ui/dist" "$APP_INSTALL_DIR/src/msa_apps/ui/dist"

# Copy the checked-in macOS config template into the app payload.
# This file lives at installer/macos/config.macos.yaml.template and is the
# canonical user-facing config with macOS-specific comments and examples.
MACOS_TEMPLATE="$INSTALLER_DIR/config.macos.yaml.template"
if [[ ! -f "$MACOS_TEMPLATE" ]]; then
  log "ERROR: config.macos.yaml.template not found at $MACOS_TEMPLATE"
  exit 1
fi
cp "$MACOS_TEMPLATE" "$APP_INSTALL_DIR/config.yaml.template"
log "config.yaml.template copied into payload"

# Stage arch-specific binaries so postinstall can select the correct set.
mkdir -p "$APP_INSTALL_DIR/installer/macos"
cp -r "$BIN_DIR" "$APP_INSTALL_DIR/installer/macos/bin"

if [[ -f "$LIB_DIR/libmediainfo.dylib" ]]; then
  cp "$LIB_DIR/libmediainfo.dylib" "$APP_INSTALL_DIR/lib/libmediainfo.dylib"
  log "libmediainfo.dylib embedded in Contents/Resources/lib/"
else
  log "WARNING: libmediainfo.dylib not found in $LIB_DIR — run without --skip-binaries to build it"
fi

log "Payload assembled at $APP_INSTALL_DIR"

# ── 4. Build launcher .app ───────────────────────────────────────────────────
# Compiles installer/macos/launcher_app/main.swift into a native NSStatusItem
# menu bar app using swiftc. No Platypus, no third-party tools, no interactive
# setup steps. swiftc is available on the macos-14 GitHub Actions runner via
# Xcode Command Line Tools (same toolchain used for the rest of the build).

SWIFT_SRC="$INSTALLER_DIR/launcher_app/main.swift"
ICON_ICNS="$ASSETS_DIR/icon.icns"
# LAUNCHER_APP and APP_INSTALL_DIR are defined in step 3 above.

log "Building launcher app..."

_build_app_bundle() {
  local app_path="$1"
  local bundle_id="$2"
  local display_name="$3"
  local script="$4"
  local lsui="${5:-false}"   # true = menu bar / no dock icon (LSUIElement)

  rm -rf "$app_path"
  mkdir -p "$app_path/Contents/MacOS"
  mkdir -p "$app_path/Contents/Resources"

  # ── Compile a minimal ARM64 Mach-O launcher wrapper ──────────────────────────
  # macOS determines an .app's architecture by reading the Mach-O header of the
  # Contents/MacOS executable. A shell script has no Mach-O header, so macOS
  # cannot determine its architecture and may trigger the Rosetta installation
  # dialog on Apple Silicon. We compile a tiny C stub instead.
  local exe="$app_path/Contents/MacOS/launcher"
  local launcher_src
  launcher_src=$(mktemp /tmp/msa_launcher_XXXXXX.c)
  cat > "$launcher_src" <<'LAUNCHER_C'
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <mach-o/dyld.h>
#include <libgen.h>
int main(int argc, char *argv[]) {
    char exe_path[4096];
    uint32_t sz = sizeof(exe_path);
    if (_NSGetExecutablePath(exe_path, &sz) != 0) return 1;
    char dir_buf[4096];
    memcpy(dir_buf, exe_path, strlen(exe_path) + 1);
    char *dir = dirname(dir_buf);
    char script_path[4096];
    snprintf(script_path, sizeof(script_path), "%s/../Resources/script", dir);
    /* pass original argv through so callers can supply arguments */
    char **new_argv = (char **)malloc((argc + 3) * sizeof(char *));
    if (!new_argv) return 1;
    new_argv[0] = "/bin/bash";
    new_argv[1] = script_path;
    for (int i = 1; i < argc; i++) new_argv[i + 1] = argv[i];
    new_argv[argc + 1] = NULL;
    execv("/bin/bash", new_argv);
    perror("execv");
    return 1;
}
LAUNCHER_C

  if clang -arch arm64 -O2 -o "$exe" "$launcher_src" 2>/dev/null; then
    log "  launcher: compiled Mach-O (arm64)"
  else
    # If clang is absent (should not happen on macOS with Xcode CLT) fall
    # back to a shell-script stub. This means Rosetta might be triggered
    # but the app will still function.
    log "  WARNING: clang not found — falling back to shell-script launcher"
    cat > "$exe" <<EXEOF
#!/bin/bash
exec /bin/bash "\$(dirname "\$0")/../Resources/script" "\$@"
EXEOF
  fi
  rm -f "$launcher_src"
  chmod +x "$exe"

  # Embed the actual script
  cp "$script" "$app_path/Contents/Resources/script"
  chmod +x "$app_path/Contents/Resources/script"

  # Copy icon if present
  if [[ -f "$ICON_ICNS" ]]; then
    cp "$ICON_ICNS" "$app_path/Contents/Resources/AppIcon.icns"
  fi

  cat > "$app_path/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>       <string>${bundle_id}</string>
    <key>CFBundleName</key>             <string>${display_name}</string>
    <key>CFBundleDisplayName</key>      <string>${display_name}</string>
    <key>CFBundleVersion</key>          <string>${VERSION}</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleExecutable</key>       <string>launcher</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleIconFile</key>         <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>   <string>12.0</string>
    <key>LSUIElement</key>              <${lsui}/>
    <key>NSHighResolutionCapable</key>  <true/>
    <key>LSArchitectures</key>
    <array>
        <string>arm64</string>
    </array>
</dict>
</plist>
PLIST

  echo "$app_path"
}

# Build the main launcher as a native Swift app (NSStatusItem menu bar app).
# Unlike _build_app_bundle (which wraps a shell script via a C stub), this
# compiles main.swift directly to the Contents/MacOS executable — no Platypus,
# no third-party tools, no interactive setup steps required.
_build_swift_launcher() {
  local app_path="$1"

  # Contents/Resources is already populated by step 3 — do not rm -rf.
  # Only ensure MacOS dir exists (created in step 3 but guard here for safety).
  mkdir -p "$app_path/Contents/MacOS"
  mkdir -p "$app_path/Contents/Resources"

  [[ -f "$SWIFT_SRC" ]] || fail "Swift source not found: $SWIFT_SRC"

  local exe="$app_path/Contents/MacOS/MediaSearchAgent"

  log "  Compiling Swift launcher (arm64)..."
  swiftc -framework AppKit -framework Foundation \
    -target "arm64-apple-macos12.0" \
    -O \
    -o "$exe" \
    "$SWIFT_SRC" \
    || fail "swiftc failed — check Swift source at $SWIFT_SRC"

  chmod +x "$exe"
  log "  launcher: compiled Swift Mach-O (arm64)"

  if [[ -f "$ICON_ICNS" ]]; then
    cp "$ICON_ICNS" "$app_path/Contents/Resources/AppIcon.icns"
  fi

  cat > "$app_path/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>        <string>com.mediasearchagent.app</string>
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
    <array>
        <string>arm64</string>
    </array>
</dict>
</plist>
PLIST

  echo "$app_path"
}

_build_swift_launcher "$LAUNCHER_APP"

# Copy the assembled bundle into the .pkg payload.
mkdir -p "$PAYLOAD_DIR/Applications"
cp -r "$LAUNCHER_APP" "$PAYLOAD_DIR/Applications/MediaSearchAgent.app"
log "Launcher app (with app code in Contents/Resources) added to payload"

# ── 5. Build uninstaller .app ─────────────────────────────────────────────────

UNINSTALLER_SH="$INSTALLER_DIR/uninstaller.sh"
UNINSTALLER_APP="$WORK_DIR/Uninstall MediaSearchAgent.app"

log "Building uninstaller app..."
_build_app_bundle "$UNINSTALLER_APP" \
  "com.mediasearchagent.uninstall" "Uninstall MediaSearchAgent" \
  "$UNINSTALLER_SH" "false"
log "Uninstaller app built"

# ── 6. Build component .pkg ───────────────────────────────────────────────────

COMPONENT_PKG="$WORK_DIR/MediaSearchAgent-component.pkg"

log "Building component .pkg..."
# Generate a component plist and mark BundleIsRelocatable = false so the
# installer always places MediaSearchAgent.app at the path defined in the
# payload (/Applications/MediaSearchAgent.app) regardless of old receipts.
COMPONENT_PLIST="$WORK_DIR/component.plist"
pkgbuild --analyze --root "$PAYLOAD_DIR" "$COMPONENT_PLIST"
/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$COMPONENT_PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :0:BundleOverwriteAction upgrade" "$COMPONENT_PLIST" 2>/dev/null || true

pkgbuild \
  --root "$PAYLOAD_DIR" \
  --component-plist "$COMPONENT_PLIST" \
  --scripts "$SCRIPTS_DIR" \
  --identifier "com.mediasearchagent.app" \
  --version "$VERSION" \
  --install-location "/" \
  "$COMPONENT_PKG"
log "Component .pkg: $COMPONENT_PKG"

# ── 7. Build distribution .pkg ────────────────────────────────────────────────

DIST_PKG="$WORK_DIR/MediaSearchAgent-${VERSION}.pkg"

log "Building distribution .pkg..."
DIST_XML="$WORK_DIR/Distribution.xml"
cat > "$DIST_XML" <<DISTXML
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
    <title>Media Search Agent</title>
    <organization>com.mediasearchagent</organization>
    <domains enable_localSystem="true"/>
    <!-- arm64 (Apple Silicon) only. hostArchitectures prevents the Rosetta
         installation dialog by declaring the supported architecture. -->
    <options customize="never" require-scripts="true" rootVolumeOnly="true"
             hostArchitectures="arm64"/>

    <!-- Minimum OS requirement; also enforced by the preinstall script. -->
    <os-version min="12.0"/>
    <allowed-os-versions>
        <os-version min="12.0"/>
    </allowed-os-versions>

    <!-- Welcome / ReadMe pages shown in the installer UI. -->
    <welcome file="welcome.html" mime-type="text/html"/>
    <readme  file="readme.html"  mime-type="text/html"/>

    <pkg-ref id="com.mediasearchagent.app"/>

    <choices-outline>
        <line choice="com.mediasearchagent.app"/>
    </choices-outline>

    <choice id="com.mediasearchagent.app"
            visible="false"
            title="Media Search Agent"
            description="Installs Media Search Agent into /Applications.">
        <pkg-ref id="com.mediasearchagent.app"/>
    </choice>

    <pkg-ref id="com.mediasearchagent.app"
             version="${VERSION}"
             onConclusion="none">MediaSearchAgent-component.pkg</pkg-ref>

</installer-gui-script>
DISTXML
productbuild \
  --distribution "$DIST_XML" \
  --resources "$ASSETS_DIR" \
  --package-path "$WORK_DIR" \
  "$DIST_PKG"
log "Distribution .pkg: $DIST_PKG"

# ── 8. Assemble DMG contents ──────────────────────────────────────────────────

DMG_CONTENTS="$WORK_DIR/dmg-contents"
rm -rf "$DMG_CONTENTS"
mkdir -p "$DMG_CONTENTS"

cp "$DIST_PKG" "$DMG_CONTENTS/"
cp -r "$UNINSTALLER_APP" "$DMG_CONTENTS/"

# ── 9. Build .dmg ─────────────────────────────────────────────────────────────

DMG_OUT="$DIST_DIR/MediaSearchAgent-${VERSION}-arm64-Setup.dmg"
rm -f "$DMG_OUT"

log "Building .dmg..."
DMG_ARGS=(
  --volname "MediaSearchAgent $VERSION"
  --window-size 600 400
  --icon-size 100
  --icon "MediaSearchAgent-${VERSION}.pkg" 150 200
  --icon "Uninstall MediaSearchAgent.app" 450 200
  --hide-extension "MediaSearchAgent-${VERSION}.pkg"
  --hide-extension "Uninstall MediaSearchAgent.app"
)

if [[ -f "$ASSETS_DIR/dmg-background.png" ]]; then
  DMG_ARGS+=(--background "$ASSETS_DIR/dmg-background.png")
fi

create-dmg "${DMG_ARGS[@]}" "$DMG_OUT" "$DMG_CONTENTS" || {
  # create-dmg exits 1 if it couldn't set background/layout (non-fatal)
  log "WARNING: create-dmg returned non-zero (layout may be imperfect)"
}

log "DMG built: $DMG_OUT"
log ""
log "Build complete:"
log "  $DMG_OUT  ($(du -sh "$DMG_OUT" | cut -f1))"
