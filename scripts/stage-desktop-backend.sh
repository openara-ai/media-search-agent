#!/usr/bin/env bash
# Stage the MSA backend for the Tauri bundled-uv sidecar (M-7/S-1 spec §1.5).
#
# PROJECT-OWNED wrapper. The vendored scripts/build-backend.sh stages only `uv` + a
# pure-stdlib shim (the template's reference backend). MSA needs the full backend source
# tree, the optional ranker wheel, the exiftool/mediainfo tools, and the platform config
# template — so this wrapper stages all of it into the Tauri resource layout:
#
#   src-tauri/bin/uv[.exe]          ← the provisioning tool (pinned UV_VERSION)
#   src-tauri/bin/exiftool + lib/                 ← pure-Perl exiftool, macOS/Linux (pinned EXIFTOOL_VERSION)
#   src-tauri/bin/exiftool.exe + exiftool_files/  ← native exiftool, Windows (same pin, SourceForge zip)
#   src-tauri/bin/mediainfo         ← mediainfo CLI (platform release)
#   src-tauri/backend/app/          ← the committed shim (NOT touched here)
#   src-tauri/backend/msa/          ← staged, version-stamped MSA project (pyproject + src +
#                                      requirements + config.yaml.template)
#   src-tauri/backend/wheels/       ← vendored msa_ranker wheel (private builds only, ADR-011)
#
# Bash-only: dev/build scripts are never PS1 (CLAUDE.md); Windows CI runs this under Git Bash.
# The committed shim (backend/app/) is never removed — only backend/msa and backend/wheels are
# regenerated. Everything staged here is gitignored build output.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/versions.env
source "$ROOT/scripts/versions.env"
# shellcheck source=scripts/lib/version.sh
source "$ROOT/scripts/lib/version.sh"

MODE="all"                    # all | source | binaries
DEST="$ROOT/src-tauri"

usage() {
  cat <<'EOF'
Usage: stage-desktop-backend.sh [--source-only|--binaries-only] [--dest DIR]
  --source-only    stage the backend source tree + config template + ranker wheel only
                   (no network; version-stamped) — the CI/test-friendly path
  --binaries-only  stage uv + exiftool + mediainfo only (downloads)
  --dest DIR       destination Tauri dir (default: repo src-tauri/)
Env: MSA_STAGE_VERSION overrides the git-tag-derived version (tests).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --source-only)   MODE="source" ;;
    --binaries-only) MODE="binaries" ;;
    --dest)          DEST="$2"; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

BIN_DIR="$DEST/bin"
BACKEND_DIR="$DEST/backend"
MSA_DIR="$BACKEND_DIR/msa"
WHEELS_DIR="$BACKEND_DIR/wheels"

download() {
  local url="$1" dest="$2" desc="${3:-$url}"
  echo "    Downloading $desc..."
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$dest" "$url"
}

# Fail loudly if a staged COMPILED tool binary (uv, mediainfo) is missing, a tiny placeholder,
# or a shell-script stub — the F-8/F-9 false-pass class. On a dirty dev tree a stale gitignored
# stub left in src-tauri/bin/ (e.g. a 10-byte "#!/bin/sh") can satisfy a bare `-f` existence
# check even when the real extraction produced NOTHING, so a break that only fires on a clean CI
# runner reads as green locally. Compiled tools are always >>100 KB and never begin with a
# shebang, so size + first-two-bytes is a portable, host-agnostic "is this a real binary" check.
# (exiftool is a Perl script on macOS/Linux and is intentionally NOT validated this way; its
# direct cp-from-explicit-path under `set -e` already hard-fails when extraction produced nothing.)
assert_compiled_binary() {
  local path="$1" min_bytes="${2:-100000}" size
  [ -f "$path" ] || { echo "ERROR: expected binary missing: $path" >&2; exit 1; }
  size="$(wc -c < "$path" | tr -d '[:space:]')"
  [ "${size:-0}" -ge "$min_bytes" ] \
    || { echo "ERROR: staged $path is ${size:-0}B (< ${min_bytes}B) — a stub, not a real binary" >&2; exit 1; }
  case "$(head -c 2 "$path" 2>/dev/null)" in
    '#!') echo "ERROR: staged $path begins with a script shebang — expected a compiled binary" >&2; exit 1 ;;
  esac
}

detect_platform() {
  case "$(uname -s)" in
    Darwin) echo macos ;;
    MINGW*|MSYS*|CYGWIN*) echo windows ;;
    *) echo linux ;;
  esac
}

resolve_raw_version() {
  # The unnormalized release label — the git tag (or the test override) with any leading 'v'
  # left intact. Callers pick the normalizer: pep440_version for Python metadata (pyproject),
  # semver_version for the Tauri app version + updater manifest.
  if [ -n "${MSA_STAGE_VERSION:-}" ]; then
    printf '%s' "$MSA_STAGE_VERSION"
    return
  fi
  git -C "$ROOT" describe --tags --abbrev=0 2>/dev/null || echo '0.0.0'
}

resolve_version() {
  # Build-time-stamped from the git tag (scripts/lib/version.sh) — the same mechanism the
  # rest of MSA uses; NOT pyproject's 0.0.0.dev0 placeholder. Overridable for tests.
  pep440_version "$(resolve_raw_version)"
}

# ── source: backend tree + config template + version stamp + ranker wheel ─────
stage_source() {
  local plat version tmpl
  plat="$(detect_platform)"
  version="$(resolve_version)"

  rm -rf "$MSA_DIR"
  mkdir -p "$MSA_DIR"

  # Backend source (exclude build products up front: node_modules + the built SPA dist — the
  # SPA is embedded in the webview via frontendDist, not served by the sidecar in shell mode —
  # and __pycache__). tar-pipe so a large node_modules is never copied then deleted.
  tar -C "$ROOT" \
    --exclude='src/msa_apps/ui/node_modules' \
    --exclude='src/msa_apps/ui/dist' \
    --exclude='*__pycache__*' \
    -cf - src | tar -C "$MSA_DIR" -xf -

  cp "$ROOT/pyproject.toml" "$MSA_DIR/pyproject.toml"
  for r in requirements.txt requirements-api.txt requirements-windows.txt LICENSE NOTICE; do
    [ -f "$ROOT/$r" ] && cp "$ROOT/$r" "$MSA_DIR/$r"
  done

  # Platform config template → the name the shim reads (provision.bootstrap_config).
  case "$plat" in
    macos)   tmpl="$ROOT/installer/macos/config.macos.yaml.template" ;;
    windows) tmpl="$ROOT/installer/windows-native/config.windows.yaml.template" ;;
    *)       tmpl="$ROOT/installer/linux/config.linux.yaml.template" ;;
  esac
  if [ -f "$tmpl" ]; then
    cp "$tmpl" "$MSA_DIR/config.yaml.template"
  else
    echo "    WARN: no config template for $plat at $tmpl" >&2
  fi

  # Version-stamp the staged pyproject so importlib.metadata / msa status report the real
  # version after the venv install (mirrors installer/*/shell/build-bundle.sh).
  sed -i.bak "s/^version = .*/version = \"$version\"/" "$MSA_DIR/pyproject.toml" \
    && rm -f "$MSA_DIR/pyproject.toml.bak"
  echo "[stage] backend source -> $MSA_DIR (version $version, $plat)"

  stage_wheels
}

stage_wheels() {
  mkdir -p "$WHEELS_DIR"
  rm -f "$WHEELS_DIR"/msa_ranker-*.whl 2>/dev/null || true
  local whl
  whl="$(ls "$ROOT"/installer/wheels/msa_ranker-*.whl 2>/dev/null | head -1 || true)"
  if [ -n "$whl" ] && [ -f "$whl" ]; then
    cp "$whl" "$WHEELS_DIR/"
    echo "[stage] ranker wheel -> $WHEELS_DIR/$(basename "$whl")"
  else
    echo "[stage] no msa_ranker wheel (public build — heuristic ranking only)"
  fi
}

# ── binaries: uv + exiftool + mediainfo (downloads) ───────────────────────────
stage_uv() {
  local plat arch archive tmp out exe
  plat="$(detect_platform)"
  arch="$(uname -m)"
  mkdir -p "$BIN_DIR"
  # Anti-stale-stub: drop any prior uv so a leftover can't survive a failed re-extraction
  # (see assert_compiled_binary).
  rm -f "$BIN_DIR/uv" "$BIN_DIR/uv.exe"
  tmp="$(mktemp -d)"
  case "$plat" in
    macos)
      [ "$arch" = "arm64" ] && archive="uv-aarch64-apple-darwin.tar.gz" || archive="uv-x86_64-apple-darwin.tar.gz" ;;
    windows)
      archive="uv-x86_64-pc-windows-msvc.zip" ;;
    *)
      [ "$arch" = "aarch64" ] && archive="uv-aarch64-unknown-linux-musl.tar.gz" || archive="uv-x86_64-unknown-linux-musl.tar.gz" ;;
  esac
  if [ "$plat" = "windows" ]; then
    download "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}" "$tmp/uv.zip" "uv $UV_VERSION"
    unzip -o -q "$tmp/uv.zip" -d "$tmp/uv-extract"
    # find (not a hardcoded top-level path) mirrors the proven build-bundle.sh and survives a
    # future archive-layout change; the uv zip currently unpacks uv.exe + uvx.exe flat.
    exe="$(find "$tmp/uv-extract" -name 'uv.exe' -type f | head -1)"
    [ -n "$exe" ] || { echo "ERROR: uv.exe not found after extraction — archive layout may have changed" >&2; exit 1; }
    cp "$exe" "$BIN_DIR/uv.exe"
    out="$BIN_DIR/uv.exe"
  else
    download "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}" "$tmp/uv.tar.gz" "uv $UV_VERSION"
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    cp "$tmp"/uv-*/uv "$BIN_DIR/uv"
    chmod +x "$BIN_DIR/uv"
    out="$BIN_DIR/uv"
  fi
  rm -rf "$tmp"
  assert_compiled_binary "$out"
  echo "[stage] uv $UV_VERSION -> $BIN_DIR"
}

stage_exiftool() {
  # Platform-split (mirrors stage_uv/stage_mediainfo). The pure-Perl exiftool script is only
  # runnable where a system Perl exists — macOS/Linux. Windows has no system Perl and the desktop
  # sidecar's shutil.which("exiftool") won't resolve an extensionless Perl script, so Windows must
  # stage the NATIVE build (exiftool.exe + its sibling exiftool_files/ runtime). (The legacy Windows
  # shell bundle that first proved this out was retired in M-7/S-5.5; this staging is now the sole
  # Windows exiftool path.)
  local plat tmp
  plat="$(detect_platform)"
  mkdir -p "$BIN_DIR"
  # Anti-stale-stub: clear any prior exiftool payload (either platform's layout — the
  # extensionless Perl script + lib/, OR the native exe + exiftool_files/) so a leftover from a
  # dirty tree or a platform switch can't masquerade as a good stage. The staging cp's below are
  # from explicit paths, so `set -e` already hard-fails when extraction produced nothing.
  rm -f "$BIN_DIR/exiftool" "$BIN_DIR/exiftool.exe"
  rm -rf "$BIN_DIR/lib" "$BIN_DIR/exiftool_files"
  tmp="$(mktemp -d)"
  if [ "$plat" = "windows" ]; then
    # Windows ExifTool ships as exiftool(-k).exe PLUS a sibling exiftool_files/ dir (the Perl
    # runtime + modules). The exe alone runs but fails on most operations — both must be staged
    # side by side in BIN_DIR (== MSA_TOOLS_DIR at runtime). SourceForge is used because
    # exiftool.org only keeps the latest release, so a pinned version 404s once a newer one ships.
    local exiftool_zip exe src_dir files_dir
    exiftool_zip="exiftool-${EXIFTOOL_VERSION}_64.zip"
    download "https://sourceforge.net/projects/exiftool/files/${exiftool_zip}/download" \
      "$tmp/exiftool.zip" "exiftool $EXIFTOOL_VERSION (windows x64)"
    unzip -o -q "$tmp/exiftool.zip" -d "$tmp/extract"
    exe="$(find "$tmp/extract" -name 'exiftool(-k).exe' | head -1)"
    { [ -n "$exe" ] && [ -f "$exe" ]; } \
      || { echo "ERROR: exiftool(-k).exe not found in archive — check EXIFTOOL_VERSION" >&2; exit 1; }
    src_dir="$(dirname "$exe")"
    files_dir="$src_dir/exiftool_files"
    [ -d "$files_dir" ] \
      || { echo "ERROR: exiftool_files/ not found alongside the exe — archive layout may have changed" >&2; exit 1; }
    cp "$exe" "$BIN_DIR/exiftool.exe"
    cp -r "$files_dir" "$BIN_DIR/exiftool_files"
    # The ExifTool zip encodes Windows read-only attributes; macOS/Linux unzip honour them and
    # cp -r preserves the directory mode bits, so the cleanup rm below (and re-runs) fail without
    # u+w on both the tmp extract and the staged copy.
    chmod -R u+w "$tmp" "$BIN_DIR/exiftool_files"
    echo "[stage] exiftool $EXIFTOOL_VERSION (windows: exiftool.exe + exiftool_files/) -> $BIN_DIR"
  else
    # macOS/Linux: system Perl runs the pure-Perl distribution directly (bin/exiftool + bin/lib).
    download "https://github.com/exiftool/exiftool/archive/refs/tags/${EXIFTOOL_VERSION}.tar.gz" \
      "$tmp/exiftool.tar.gz" "exiftool $EXIFTOOL_VERSION"
    tar -xzf "$tmp/exiftool.tar.gz" -C "$tmp"
    cp "$tmp/exiftool-${EXIFTOOL_VERSION}/exiftool" "$BIN_DIR/exiftool"
    cp -r "$tmp/exiftool-${EXIFTOOL_VERSION}/lib" "$BIN_DIR/lib"
    chmod +x "$BIN_DIR/exiftool"
    echo "[stage] exiftool $EXIFTOOL_VERSION (pure-Perl: exiftool + lib/) -> $BIN_DIR"
  fi
  rm -rf "$tmp"
}

stage_mediainfo() {
  local plat arch tmp out bin
  plat="$(detect_platform)"
  arch="$(uname -m)"
  mkdir -p "$BIN_DIR"
  # Anti-stale-stub (see assert_compiled_binary): a leftover mediainfo in a dirty dev tree must
  # NOT survive to satisfy the existence check when extraction produced nothing (this is exactly
  # how F-9 read as green locally — a stale 10-byte "#!/bin/sh" stub sat in src-tauri/bin/).
  rm -f "$BIN_DIR/mediainfo" "$BIN_DIR/mediainfo.exe"
  tmp="$(mktemp -d)"
  case "$plat" in
    macos)
      # The macOS CLI release is a .pkg installer INSIDE a .dmg, NOT a bare binary. So: mount the
      # dmg -> pkgutil --expand the .pkg -> cpio-extract its gzipped Payload -> pull mediainfo out
      # -> lipo -thin the universal binary to the host arch. Ported from the proven reference
      # installer/macos/shell/build-bundle.sh [4/6]. The prior code ran `find -name mediainfo`
      # straight over the mounted dmg, which finds NOTHING (the dmg holds only the .pkg) — F-9.
      download "https://mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION_MACOS}/MediaInfo_CLI_${MEDIAINFO_VERSION_MACOS}_Mac.dmg" \
        "$tmp/mediainfo.dmg" "mediainfo $MEDIAINFO_VERSION_MACOS"
      local mount pkg pkg_expand payload payload_dir
      mount="$tmp/mnt"; mkdir -p "$mount"
      hdiutil attach "$tmp/mediainfo.dmg" -mountpoint "$mount" -nobrowse -quiet
      pkg="$(find "$mount" -name '*.pkg' | head -1)"
      if [ -z "$pkg" ]; then
        hdiutil detach "$mount" -quiet 2>/dev/null || true
        echo "ERROR: no .pkg found inside macOS mediainfo dmg — layout may have changed" >&2; exit 1
      fi
      pkg_expand="$tmp/pkg_expand"
      pkgutil --expand "$pkg" "$pkg_expand"
      hdiutil detach "$mount" -quiet 2>/dev/null || true
      payload="$(find "$pkg_expand" -name Payload | head -1)"
      [ -n "$payload" ] || { echo "ERROR: no Payload in expanded macOS mediainfo pkg" >&2; exit 1; }
      payload_dir="$tmp/pkg_payload"; mkdir -p "$payload_dir"
      # BSD cpio (macOS) has no -D flag; cd into the destination instead.
      ( cd "$payload_dir" && gzip -dc "$payload" | cpio -id 2>/dev/null ) || true
      bin="$(find "$payload_dir" -name mediainfo -type f | head -1)"
      [ -n "$bin" ] || { echo "ERROR: mediainfo not found in macOS pkg payload" >&2; exit 1; }
      if file "$bin" | grep -q "universal binary"; then
        lipo -thin "$arch" "$bin" -output "$BIN_DIR/mediainfo"
      else
        cp "$bin" "$BIN_DIR/mediainfo"
      fi
      chmod +x "$BIN_DIR/mediainfo"
      out="$BIN_DIR/mediainfo"
      ;;
    windows)
      download "https://mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION_LINUX}/MediaInfo_CLI_${MEDIAINFO_VERSION_LINUX}_Windows_x64.zip" \
        "$tmp/mediainfo.zip" "mediainfo $MEDIAINFO_VERSION_LINUX (windows x64)"
      unzip -o -q "$tmp/mediainfo.zip" -d "$tmp/mi"
      # The zip carries MediaInfo.exe at its root; use find (not a hardcoded path) so a future
      # layout change hard-fails loudly instead of silently, mirroring the guarded Linux branch.
      bin="$(find "$tmp/mi" -iname 'mediainfo.exe' -type f | head -1)"
      [ -n "$bin" ] || { echo "ERROR: MediaInfo.exe not found in Windows zip package" >&2; exit 1; }
      cp "$bin" "$BIN_DIR/mediainfo.exe"
      out="$BIN_DIR/mediainfo.exe"
      ;;
    *)
      download "https://old.mediaarea.net/download/binary/mediainfo/${MEDIAINFO_VERSION_LINUX}/MediaInfo_CLI_${MEDIAINFO_VERSION_LINUX}_Lambda_x86_64.zip" \
        "$tmp/mediainfo.zip" "mediainfo $MEDIAINFO_VERSION_LINUX (linux x86_64)"
      unzip -o -q "$tmp/mediainfo.zip" -d "$tmp/mi"
      # The Linux zip nests the binary under bin/; find handles the subdir (a hardcoded top-level
      # cp would miss it). Already correct — kept as the reference for the other branches.
      bin="$(find "$tmp/mi" -name mediainfo -type f | head -1)"
      [ -n "$bin" ] || { echo "ERROR: mediainfo binary not found in Linux zip package" >&2; exit 1; }
      cp "$bin" "$BIN_DIR/mediainfo"
      chmod +x "$BIN_DIR/mediainfo"
      out="$BIN_DIR/mediainfo"
      ;;
  esac
  rm -rf "$tmp"
  assert_compiled_binary "$out"
  echo "[stage] mediainfo -> $BIN_DIR"
}

stage_binaries() {
  stage_uv
  stage_exiftool
  stage_mediainfo
}

# Stamp the git-tag version into src-tauri/tauri.conf.json for the packaged build (release
# path only). Uses the SemVer normalizer, which PRESERVES the pre-release suffix (e.g. -rc1):
# Tauri (v2, targets: macOS app/dmg + Windows NSIS — no WiX/MSI) accepts a SemVer pre-release,
# and the updater orders builds by it, so vX.Y.Z-rc1 and -rc2 must stamp to DISTINCT app
# versions or self-update can't tell them apart. The stamp equals ${GITHUB_REF_NAME#v} used by
# release.yml for the artifact names + latest.json, keeping all three identical.
# No-op when the config isn't at DEST (e.g. a --dest tmp test run).
stamp_tauri_config() {
  local conf="$DEST/tauri.conf.json"
  [ -f "$conf" ] || return 0
  local semver
  semver="$(semver_version "$(resolve_raw_version)")"
  sed -i.bak -E "s/(\"version\": )\"[^\"]*\"/\1\"$semver\"/" "$conf" && rm -f "$conf.bak"
  echo "[stage] stamped tauri.conf.json version -> $semver"
}

case "$MODE" in
  source)   stage_source ;;
  binaries) stage_binaries ;;
  all)      stage_binaries; stage_source; stamp_tauri_config ;;
esac
echo "[stage] done ($MODE) -> $DEST"
