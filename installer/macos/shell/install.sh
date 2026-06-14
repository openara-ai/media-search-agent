#!/usr/bin/env bash
set -euo pipefail

# Media Search Agent — one-line installer for macOS and Linux
#
# One-liner:  curl -fsSL https://github.com/openara-ai/media-search-agent/releases/latest/download/install.sh | bash
# Direct run: bash install.sh [OPTIONS]

# ── Colours ───────────────────────────────────────────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

_disable_colors() { BOLD=''; GREEN=''; YELLOW=''; RED=''; DIM=''; NC=''; }
[[ ! -t 1 || -n "${NO_COLOR:-}" || "${TERM:-}" == "dumb" ]] && _disable_colors

# ── Logging ───────────────────────────────────────────────────────────────────

# All log_* functions write to stderr so they never pollute stdout in
# command-substitution contexts (e.g. `version="$(resolve_version)"`).
# setup_logging redirects stderr to the tee, so file logging still works.
log_info()  { printf "${DIM}·${NC} %s\n" "$*" >&2; }
log_ok()    { printf "${GREEN}✓${NC} %s\n" "$*" >&2; }
log_skip()  { printf "${DIM}– %s${NC}\n" "$*" >&2; }
log_warn()  { printf "${YELLOW}!${NC} %s\n" "$*" >&2; }
log_bold()  { printf "\n${BOLD}%s${NC}\n" "$*" >&2; }
die()       { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

LAUNCH_AGENT_LABEL="ai.openara.mediasearchagent"

unload_launch_agent() {
  [[ "${OS:-}" != "macos" ]] && return 0

  local plist="${1:-$HOME/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist}"
  local uid
  uid="$(id -u)"

  if [[ -f "$plist" ]]; then
    launchctl unload "$plist" 2>/dev/null || true
    launchctl bootout "gui/$uid" "$plist" 2>/dev/null || true
  fi

  # A launchd job can remain registered even after its plist is deleted or
  # moved. Remove by service label as the recovery path for that state.
  launchctl bootout "gui/$uid/$LAUNCH_AGENT_LABEL" 2>/dev/null || true
  launchctl remove "$LAUNCH_AGENT_LABEL" 2>/dev/null || true
}

detect_install_mode() {
  local markers=0
  local found=()

  if [[ "$OS" == "macos" ]]; then
    if [[ -d "$APP_BUNDLE" ]]; then
      markers=$((markers + 1))
      found+=("app_bundle")
    fi
  else
    if [[ -d "$APP_CODE_DIR" ]]; then
      markers=$((markers + 1))
      found+=("app_code_dir")
    fi
  fi

  if [[ -f "$LAUNCHER" ]]; then
    markers=$((markers + 1))
    found+=("launcher")
  fi
  if [[ -f "$CONFIG_PATH" ]]; then
    markers=$((markers + 1))
    found+=("config")
  fi
  if [[ -d "$VENV_DIR" ]]; then
    markers=$((markers + 1))
    found+=("venv")
  fi

  if [[ "$markers" -eq 0 ]]; then
    INSTALL_MODE_REASON="No existing install markers found."
    INSTALL_MODE_MARKERS="none"
    INSTALL_MODE="install"
  elif [[ "$markers" -ge 3 ]]; then
    INSTALL_MODE_REASON="Found 3 or more install markers, treating this run as an upgrade."
    INSTALL_MODE_MARKERS="$(IFS=,; echo "${found[*]}")"
    INSTALL_MODE="upgrade"
  else
    INSTALL_MODE_REASON="Found partial install state, treating this run as a repair."
    INSTALL_MODE_MARKERS="$(IFS=,; echo "${found[*]}")"
    INSTALL_MODE="repair"
  fi
}

setup_logging() {
  # Set up the tee-to-logfile only; do NOT print anything here. The visible
  # log/mode/markers/reason lines are printed by print_banner *after* the
  # banner header so the user's first console line is the installer title,
  # not implementation chatter.
  local mode="$1"
  local timestamp
  timestamp="$(date '+%Y-%m-%d_%H%M%S')"

  mkdir -p "$LOG_DIR"
  INSTALL_LOG_FILE="$LOG_DIR/${mode}-${timestamp}.log"

  # Mirror all installer output to a timestamped file so install, upgrade, and
  # repair runs are diagnosable after the terminal session is gone. Strip ANSI
  # escapes from the file copy so the saved log stays readable in plain editors.
  exec > >(tee >(sed -E $'s/\x1B\\[[0-9;]*[[:alpha:]]//g' >> "$INSTALL_LOG_FILE")) 2>&1
}

# ── Usage ─────────────────────────────────────────────────────────────────────

print_usage() {
  cat <<EOF

${BOLD}Media Search Agent Installer${NC}
${DIM}Local-first semantic search for your photos and videos${NC}

${BOLD}Usage${NC}
  curl -fsSL https://github.com/openara-ai/media-search-agent/releases/latest/download/install.sh | bash
  bash install.sh [OPTIONS]

${BOLD}Options${NC}
  -v, --version <tag>      Release tag to install
                           Default: latest published GitHub release
                           Example: --version v0.2.0

  -b, --bundle <path>      Path to a pre-downloaded bundle archive (.tar.gz)
                           Skips the GitHub download — useful for testing
                           Example: --bundle ./MediaSearchAgent-0.2.0-macos-arm64.tar.gz

      --dir <path>         Override app code directory (Linux only)
                           Default Linux: \$HOME/.local/share/MediaSearchAgent
                           Also settable via env: MSA_DIR=/path

      --skip-autostart     Skip LaunchAgent (macOS) / systemd service (Linux) registration

      --allow-downgrade    Allow installing an older version over a newer one
                           (default: refuse — downgrades can corrupt the index)

      --no-color           Disable colour output
                           Also settable via env: NO_COLOR=1

  -h, --help               Show this help and exit

${BOLD}Install paths (macOS)${NC}
  App:     \$HOME/Applications/MediaSearchAgent.app
  Code:    \$HOME/Applications/MediaSearchAgent.app/Contents/Resources
  Data:    \$HOME/Library/Application Support/MediaSearchAgent
  Config:  \$HOME/Library/Application Support/MediaSearchAgent/config.yaml
  Logs:    \$HOME/Library/Logs/MediaSearchAgent
  Venv:    \$HOME/Applications/MediaSearchAgent.app/Contents/Resources/.venv
  Launcher:\$HOME/.local/bin/msa

${BOLD}Install paths (Linux)${NC}
  Code:    \$HOME/.local/share/MediaSearchAgent
  Data:    \$HOME/.local/share/MediaSearchAgent
  Config:  \$HOME/.config/MediaSearchAgent/config.yaml
  Logs:    \$HOME/.local/share/MediaSearchAgent/logs
  Venv:    \$HOME/.local/share/MediaSearchAgent/.venv
  Launcher:\$HOME/.local/bin/msa

${BOLD}Environment overrides${NC}
  MSA_VERSION=v0.2.0       Same as --version
  MSA_DIR=/path            Same as --dir (Linux only)
  NO_COLOR=1               Same as --no-color

EOF
}

# ── Arg parsing ───────────────────────────────────────────────────────────────

OPT_VERSION="${MSA_VERSION:-}"
OPT_BUNDLE=""
OPT_DIR="${MSA_DIR:-}"
OPT_SKIP_AUTOSTART=""
OPT_ALLOW_DOWNGRADE=""

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)            print_usage; exit 0 ;;
      -v|--version)         OPT_VERSION="${2:?'--version requires a value'}"; shift 2 ;;
      --version=*)          OPT_VERSION="${1#*=}"; shift ;;
      -b|--bundle)          OPT_BUNDLE="${2:?'--bundle requires a path'}"; shift 2 ;;
      --bundle=*)           OPT_BUNDLE="${1#*=}"; shift ;;
      --dir)                OPT_DIR="${2:?'--dir requires a path'}"; shift 2 ;;
      --dir=*)              OPT_DIR="${1#*=}"; shift ;;
      --skip-autostart)     OPT_SKIP_AUTOSTART=1; shift ;;
      --allow-downgrade)    OPT_ALLOW_DOWNGRADE=1; shift ;;
      --no-color)           NO_COLOR=1; _disable_colors; shift ;;
      *) die "Unknown argument: $1  (run with --help for usage)" ;;
    esac
  done
}

# ── OS / arch ─────────────────────────────────────────────────────────────────

detect_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)  echo "linux" ;;
    *) die "Unsupported OS: $(uname -s). This installer supports macOS and Linux." ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo "arm64" ;;
    x86_64|amd64)  echo "x86_64" ;;
    *) die "Unsupported architecture: $(uname -m)" ;;
  esac
}

# OS version floor + disk space + RAM. Called once from main() after OS and
# arch have been resolved. macOS gets a hard OS-version floor; disk space is
# fatal on both; RAM is best-effort warning only.
check_system_requirements() {
  # macOS 12 (Monterey) is the floor - first version with reliable arm64
  # Python wheels for our stack. Linux is skipped: glibc 2.28+ is required
  # by torch wheels, but every distro from 2019+ satisfies this and checking
  # glibc reliably across distros is more code than it's worth.
  if [[ "$OS" == "macos" ]]; then
    local mac_ver mac_major
    mac_ver="$(sw_vers -productVersion 2>/dev/null || echo 0)"
    mac_major="${mac_ver%%.*}"
    if [[ "${mac_major:-0}" -lt 12 ]]; then
      die "macOS 12 (Monterey) or newer required. Detected: $mac_ver"
    fi
  fi

  # Free disk: bundle + venv + torch wheels + scratch. 5 GB is the
  # comfortable floor; under that, pip can fail mid-resolve with cryptic
  # disk errors.
  local target="$APP_CODE_DIR"
  [[ ! -d "$target" ]] && target="$(dirname "$target")"
  [[ ! -d "$target" ]] && target="$HOME"
  local free_kb free_gb
  free_kb="$(df -k "$target" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ -n "$free_kb" && "$free_kb" =~ ^[0-9]+$ ]]; then
    free_gb=$(( free_kb / 1024 / 1024 ))
    if [[ "$free_gb" -lt 5 ]]; then
      die "Need at least 5 GB free at $target for the install. Available: ${free_gb} GB."
    fi
  fi

  # RAM warning - small libraries index fine on less, so we don't block.
  local ram_gb=0
  if [[ "$OS" == "macos" ]]; then
    local ram_bytes
    ram_bytes="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    [[ "$ram_bytes" =~ ^[0-9]+$ ]] && ram_gb=$(( ram_bytes / 1024 / 1024 / 1024 ))
  elif [[ -r /proc/meminfo ]]; then
    local ram_kb
    ram_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    [[ "$ram_kb" =~ ^[0-9]+$ ]] && ram_gb=$(( ram_kb / 1024 / 1024 ))
  fi
  if [[ "$ram_gb" -gt 0 && "$ram_gb" -lt 8 ]]; then
    log_warn "Only ${ram_gb} GB RAM detected. 8+ GB recommended; indexing large libraries (10k+ items) may OOM."
  fi
}

# ── Banner ────────────────────────────────────────────────────────────────────

print_banner() {
  # The banner header must be the very first thing the user sees; install
  # log path + mode/markers/reason follow as part of the context block so
  # they look like context for the install, not preamble before the
  # installer announces itself.
  printf "\n${BOLD}  Media Search Agent Installer${NC}\n"
  printf "${DIM}  Local-first semantic search for your photos and videos${NC}\n\n"
  log_info "OS:    $OS / $ARCH"
  if [[ "$OS" == "macos" ]]; then
    log_info "App:   $APP_BUNDLE"
  else
    log_info "Code:  $APP_CODE_DIR"
  fi
  log_info "Data:  $APP_SUPPORT_DIR"
  log_info "Scope: current user only (other accounts on this machine need their own install)"
  if [[ -n "${INSTALL_LOG_FILE:-}" ]]; then
    log_info "Log:     $INSTALL_LOG_FILE"
    log_info "Mode:    ${INSTALL_MODE:-unknown}"
    log_info "Markers: ${INSTALL_MODE_MARKERS:-none}"
    log_info "Reason:  ${INSTALL_MODE_REASON:-unknown}"
  fi
}

# ── Download helper ───────────────────────────────────────────────────────────

download() {
  local url="$1" dest="$2" desc="${3:-$url}"
  log_info "Downloading $desc..."
  mkdir -p "$(dirname "$dest")"
  if command -v curl &>/dev/null; then
    curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$dest" "$url" \
      || die "Download failed: $url"
  elif command -v wget &>/dev/null; then
    wget -q --https-only --tries=3 -O "$dest" "$url" \
      || die "Download failed: $url"
  else
    die "curl or wget is required"
  fi
}

# Verify the downloaded bundle against SHA256SUMS.txt published alongside the
# release. Closes the obvious supply-chain hole on an unsigned installer: a
# hijacked / MITM'd / corrupted bundle now fails before tar -xzf instead of
# getting installed and run.
#
# Skipped for local --bundle installs (caller handles trust). If
# SHA256SUMS.txt is missing from the release (older releases predate this
# file), warn rather than fail so existing releases still install.
verify_bundle_sha256() {
  local bundle_file="$1"
  local bundle_name="$2"
  local release_base_url="$3"

  local sums_url="${release_base_url}/SHA256SUMS.txt"
  local sums_file; sums_file="$(mktemp)"

  # Fetch SHA256SUMS.txt directly with curl/wget rather than going through
  # download() because download() dies on 404. Older releases predate
  # SHA256SUMS.txt and we want to warn-and-continue on that specific case.
  #
  # IMPORTANT: distinguish "HTTP 404" (the legacy-release fallback path)
  # from any other failure (transient TLS / proxy / 5xx / connection
  # reset). Treating every fetch failure as "release predates checksums"
  # silently bypasses the supply-chain guard whenever the network has a
  # bad moment - exactly the failure mode this verification was added to
  # prevent. The legacy fallback warns and proceeds; everything else
  # hard-fails so the user can retry / use --bundle instead.
  local sums_http="000"
  if command -v curl &>/dev/null; then
    sums_http="$(curl -sSL --proto '=https' --tlsv1.2 --retry 2 \
      --write-out '%{http_code}' --output "$sums_file" "$sums_url" 2>/dev/null || echo "000")"
  elif command -v wget &>/dev/null; then
    # wget is the fallback when curl isn't available. --server-response
    # writes status lines to stderr; parse the last 3-digit code seen.
    local wget_err
    wget_err="$(wget -q --https-only --tries=2 --server-response \
      -O "$sums_file" "$sums_url" 2>&1 || true)"
    sums_http="$(printf '%s\n' "$wget_err" \
      | awk '/HTTP\/[0-9.]+[[:space:]]+[0-9]{3}/ {code=$2} END {print code+0}')"
    [[ -z "$sums_http" || "$sums_http" == "0" ]] && sums_http="000"
  else
    rm -f "$sums_file"
    log_warn "Neither curl nor wget found - skipping integrity check."
    return
  fi

  if [[ "$sums_http" == "404" ]]; then
    rm -f "$sums_file"
    log_warn "SHA256SUMS.txt not found (HTTP 404) at $sums_url - skipping integrity check. The release may predate signed checksums."
    return
  fi
  if [[ "$sums_http" != "200" ]]; then
    rm -f "$sums_file"
    die "Could not fetch SHA256SUMS.txt from $sums_url (HTTP $sums_http). Refusing to install an unverified bundle - retry once the network is healthy, or pass --bundle <local-path> to install a copy you've verified yourself."
  fi

  # SHA256SUMS.txt format is `<hash>  <filename>` (two spaces between).
  # Files may be prefixed with `*` (shasum -b) or `./` - strip both.
  local expected
  expected="$(awk -v name="$bundle_name" '
    /^[[:space:]]*$/ || /^#/ { next }
    {
      hash=$1
      sub(/^[*\.\/]+/, "", $2)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2)
      if ($2 == name) { print tolower(hash); exit }
    }
  ' "$sums_file")"
  rm -f "$sums_file"

  if [[ -z "$expected" ]]; then
    log_warn "$bundle_name not listed in SHA256SUMS.txt - skipping integrity check."
    return
  fi

  local actual
  if command -v shasum &>/dev/null; then
    actual="$(shasum -a 256 "$bundle_file" | awk '{print tolower($1)}')"
  elif command -v sha256sum &>/dev/null; then
    actual="$(sha256sum "$bundle_file" | awk '{print tolower($1)}')"
  else
    log_warn "Neither shasum nor sha256sum found - skipping integrity check."
    return
  fi

  if [[ "$actual" != "$expected" ]]; then
    die "Bundle SHA256 mismatch for ${bundle_name}: expected $expected, got $actual. The download may be corrupted or tampered with - aborting before extract."
  fi
  log_ok "Bundle SHA256 verified (${expected:0:12}...)"
}

# ── Version resolution ────────────────────────────────────────────────────────

resolve_version() {
  if [[ -n "$OPT_VERSION" ]]; then
    echo "$OPT_VERSION"
    return
  fi
  local version_file; version_file="$(mktemp)"
  download "https://api.github.com/repos/${GITHUB_REPO}/releases" \
    "$version_file" "release list"
  local version
  version="$(grep '"tag_name"' "$version_file" | head -1 \
    | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')"
  rm -f "$version_file"
  if [[ -z "$version" ]]; then
    die "Could not resolve latest version from GitHub. Use --version vX.Y.Z to specify one."
  fi
  echo "$version"
}

# Portable version comparator. Returns 0 (true) iff $1 < $2 in dotted-numeric
# semver order, 1 otherwise. Both args must be the output of parse_msa_version
# (already stripped of `v` prefix and pre-release suffix).
#
# This used to be `sort -V | head -1` which works on macOS 12+ and Linux with
# GNU coreutils but isn't reliably available on older BSDs / minimal Linux
# containers. Replacing it with a pure-bash comparator removes the dependency
# entirely - safer under `set -euo pipefail`.
version_lt() {
  [[ "$1" == "$2" ]] && return 1
  local i x y
  local -a a b
  IFS='.' read -ra a <<<"$1"
  IFS='.' read -ra b <<<"$2"
  for ((i=0; i<${#a[@]} || i<${#b[@]}; i++)); do
    x=${a[i]:-0}
    y=${b[i]:-0}
    # Guard against non-numeric components (would crash arithmetic compare)
    # by treating them as 0. parse_msa_version should produce pure numerics
    # but defence in depth costs nothing here.
    [[ "$x" =~ ^[0-9]+$ ]] || x=0
    [[ "$y" =~ ^[0-9]+$ ]] || y=0
    (( x < y )) && return 0
    (( x > y )) && return 1
  done
  return 1
}

# Strip the `v` prefix and any `-prerelease` suffix from a tag, returning just
# the numeric `X.Y.Z` portion. Used for the downgrade guard - pre-release
# differences (e.g. v0.7.3-test6 vs v0.7.3-test7) intentionally compare equal
# because the schema risk lives in the X.Y.Z portion.
parse_msa_version() {
  local tag="${1#v}"
  echo "${tag%%-*}"
}

# Refuse to install an older version on top of a newer one unless the user
# passed --allow-downgrade. Silent no-op for fresh installs and for legacy
# installs where the version file is missing.
check_version_downgrade() {
  local new_tag="$1"
  local version_file="$2"

  # Local-bundle installs don't reliably know their version; skip.
  [[ "$new_tag" == "(local bundle)" ]] && return 0
  [[ -f "$version_file" ]] || return 0

  local existing_tag
  existing_tag="$(head -1 "$version_file" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ -n "$existing_tag" ]] || return 0

  local new_num existing_num
  new_num="$(parse_msa_version "$new_tag")"
  existing_num="$(parse_msa_version "$existing_tag")"
  if [[ -z "$new_num" || -z "$existing_num" ]]; then
    log_warn "Could not parse versions (new=$new_tag, existing=$existing_tag); skipping downgrade check"
    return 0
  fi

  # Pure-bash dotted-numeric comparison via version_lt; avoids the sort -V
  # dependency that isn't portable to minimal BSDs / older macOS / stripped
  # Linux containers.
  if version_lt "$new_num" "$existing_num"; then
    if [[ -n "$OPT_ALLOW_DOWNGRADE" ]]; then
      log_warn "Downgrading from $existing_tag to $new_tag (forced by --allow-downgrade)"
    else
      die "Refusing to downgrade from $existing_tag to $new_tag. Re-run with --allow-downgrade to force. Downgrades can corrupt index/media.sqlite if the schema moved forward between versions."
    fi
  elif version_lt "$existing_num" "$new_num"; then
    log_info "Upgrading from $existing_tag to $new_tag"
  fi
}

# ── Bundle download + extract ─────────────────────────────────────────────────

install_bundle() {
  local version="$1"
  local bundle_archive
  local tmp; tmp="$(mktemp -d)"

  if [[ -n "$OPT_BUNDLE" ]]; then
    [[ -f "$OPT_BUNDLE" ]] || die "Bundle file not found: $OPT_BUNDLE"
    bundle_archive="$OPT_BUNDLE"
    log_info "Using local bundle: $OPT_BUNDLE"
    # Local --bundle path: skip SHA256 verification. Caller is trusted.
  else
    local bundle_name="MediaSearchAgent-${version#v}-${OS}-${ARCH}"
    local bundle_base="https://github.com/${GITHUB_REPO}/releases/download/${version}"
    local bundle_url="${bundle_base}/${bundle_name}.tar.gz"
    download "$bundle_url" "$tmp/bundle.tar.gz" "bundle $version ($OS-$ARCH)"
    bundle_archive="$tmp/bundle.tar.gz"
    verify_bundle_sha256 "$bundle_archive" "${bundle_name}.tar.gz" "$bundle_base"
  fi

  log_info "Extracting bundle..."
  tar -xzf "$bundle_archive" -C "$tmp"
  local bundle_dir
  bundle_dir="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [[ -d "$bundle_dir" ]] || die "Bundle directory not found after extract"

  # Validate required bundle contents before touching the existing install
  for item in src scripts pyproject.toml requirements-api.txt; do
    [[ -e "$bundle_dir/$item" ]] || die "Bundle is missing required item: $item"
  done
  [[ -d "$bundle_dir/src/msa_apps/ui/dist" ]] || die "Bundle is missing src/msa_apps/ui/dist (UI was not built)"
  [[ -f "$bundle_dir/bin/uv" ]] || die "Bundle is missing bin/uv"
  [[ -f "$bundle_dir/config.yaml.template" ]] || die "Bundle is missing config.yaml.template"

  BUNDLE_CONFIG_TEMPLATE="$(mktemp)"
  cp "$bundle_dir/config.yaml.template" "$BUNDLE_CONFIG_TEMPLATE"

  if [[ "$OS" == "macos" ]]; then
    # On macOS the installer owns the .app bundle shell plus the code items in
    # Contents/Resources. Preserve the existing venv across reinstall/upgrade
    # and replace only the app wrapper plus code/tool payload.
    [[ -d "$bundle_dir/MediaSearchAgent.app" ]] || die "Bundle is missing MediaSearchAgent.app"

    mkdir -p "$HOME/Applications"
    if [[ ! -d "$APP_BUNDLE" ]]; then
      cp -R "$bundle_dir/MediaSearchAgent.app" "$APP_BUNDLE"
    else
      mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
      cp "$bundle_dir/MediaSearchAgent.app/Contents/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
      cp "$bundle_dir/MediaSearchAgent.app/Contents/MacOS/MediaSearchAgent" \
        "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent"
      chmod +x "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent"
      if [[ -f "$bundle_dir/MediaSearchAgent.app/Contents/Resources/AppIcon.icns" ]]; then
        cp "$bundle_dir/MediaSearchAgent.app/Contents/Resources/AppIcon.icns" \
          "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
      fi
    fi

    # `requirements.txt` is in this list as a pure-cleanup entry (the new
    # bundle ships requirements-api.txt; the conditional `mv` skips it).
    # Without this, dev machines with older test installs would carry a
    # stale requirements.txt forward through upgrade.
    for item in src scripts pyproject.toml requirements.txt requirements-api.txt LICENSE NOTICE uninstall.sh bin wheels; do
      rm -rf "$APP_CODE_DIR/$item"
      [[ -e "$bundle_dir/$item" ]] && mv "$bundle_dir/$item" "$APP_CODE_DIR/$item"
    done
  else
    # Linux: replace only the code items so we don't wipe index/data/venv
    # (APP_CODE_DIR == APP_SUPPORT_DIR on Linux).
    mkdir -p "$APP_CODE_DIR"
    # `requirements.txt`: pure-cleanup entry — see macOS branch above.
    for item in src scripts pyproject.toml requirements.txt requirements-api.txt LICENSE NOTICE uninstall.sh bin wheels; do
      rm -rf "$APP_CODE_DIR/$item"
      [[ -e "$bundle_dir/$item" ]] && mv "$bundle_dir/$item" "$APP_CODE_DIR/$item"
    done
  fi

  [[ -f "$APP_CODE_DIR/uninstall.sh" ]] && chmod +x "$APP_CODE_DIR/uninstall.sh"
  chmod +x "$APP_CODE_DIR/bin/"* 2>/dev/null || true
  log_ok "app-private uv: $APP_CODE_DIR/bin/uv"

  # Strip macOS quarantine so Gatekeeper doesn't silently block the app.
  # Necessary when the bundle was distributed as a zip (e.g. OneDrive share)
  # and the user double-clicked to unzip — Finder stamps quarantine on all
  # extracted contents including the .tar.gz passed via --bundle.
  if [[ "$OS" == "macos" ]] && command -v xattr &>/dev/null; then
    xattr -dr com.apple.quarantine "$APP_BUNDLE" 2>/dev/null || true
    log_ok "Quarantine flag cleared"
  fi

  rm -rf "$tmp"
  log_ok "Bundle installed"
}

stop_existing_macos_app() {
  if [[ "$OS" != "macos" ]]; then
    log_skip "Previous install check — not applicable on $OS"
    return 0
  fi

  # Step 1: Unload the LaunchAgent regardless of whether the .app bundle still
  # exists on disk. The user may have deleted the bundle manually, which leaves
  # launchd holding a registered job that points to a missing app — and prevents
  # install_launch_agent from loading the new plist (launchctl load silently
  # fails if the label is already registered).
  local la_plist="$HOME/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
  log_info "Unloading existing LaunchAgent..."
  unload_launch_agent "$la_plist"
  log_ok "LaunchAgent unloaded"

  # Step 2: Stop processes from known install locations.
  # Check both the pkg install location (/Applications) and prior shell install
  # (~/Applications). Without this, the old app keeps owning port 8000 and the
  # newly-installed Swift app crashes on launch.
  local known_bundles=(
    "/Applications/MediaSearchAgent.app"
    "$HOME/Applications/MediaSearchAgent.app"
  )
  local found=0

  for bundle in "${known_bundles[@]}"; do
    [[ -d "$bundle" ]] || continue
    found=1

    local existing_root="$bundle/Contents/Resources"
    local existing_stop="$existing_root/scripts/stop.sh"
    local launcher_pattern="$bundle/Contents/MacOS/MediaSearchAgent"

    log_info "Found existing install at $bundle"

    if [[ -x "$existing_stop" ]]; then
      log_info "Running stop.sh for $bundle..."
      MSA_ROOT="$existing_root" \
      MSA_VENV_DIR="$existing_root/.venv" \
      MSA_CONFIG_PATH="$CONFIG_PATH" \
      MSA_LOG_DIR="$LOG_DIR" \
        bash "$existing_stop" >/dev/null 2>&1 || true
    fi

    if pgrep -f "$launcher_pattern" >/dev/null 2>&1; then
      log_info "Stopping running app at $bundle..."
      pkill -TERM -f "$launcher_pattern" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        sleep 1
        pgrep -f "$launcher_pattern" >/dev/null || break
      done
      pkill -KILL -f "$launcher_pattern" 2>/dev/null || true
      sleep 1
      log_ok "Stopped"
    else
      log_skip "Not running"
    fi
  done

  if [[ "$found" -eq 0 ]]; then
    log_skip "No previous app bundle found"
  fi

  # Final guard: if the target bundle is still running, abort.
  if pgrep -f "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent" >/dev/null 2>&1; then
    die "Could not stop the running menu bar app before reinstall."
  fi
}

# ── Python + venv ─────────────────────────────────────────────────────────────

setup_python() {
  local uv_bin="$APP_CODE_DIR/bin/uv"
  local recreate_venv="0"

  if "$uv_bin" python list 2>/dev/null | grep -q "cpython-${PYTHON_VERSION}"; then
    log_skip "Python $PYTHON_VERSION already managed by uv"
  else
    log_info "Installing Python $PYTHON_VERSION..."
    "$uv_bin" python install "$PYTHON_VERSION"
    log_ok "Python $PYTHON_VERSION installed"
  fi

  mkdir -p "$(dirname "$VENV_DIR")"
  if [[ -d "$VENV_DIR" ]]; then
    if [[ -x "$VENV_DIR/bin/python" && -x "$VENV_DIR/bin/msa" ]]; then
      log_skip "venv already exists at $VENV_DIR"
    else
      log_warn "Existing venv at $VENV_DIR is incomplete; recreating it"
      rm -rf "$VENV_DIR"
      recreate_venv="1"
    fi
  else
    recreate_venv="1"
  fi

  if [[ "$recreate_venv" == "1" ]]; then
    log_info "Creating venv at $VENV_DIR..."
    "$uv_bin" venv "$VENV_DIR" --python "$PYTHON_VERSION"
    log_ok "venv created"
  fi
}

should_launch_app_after_install() {
  [[ "$OS" == "macos" ]] || return 1
  [[ -d "$APP_BUNDLE" ]] || return 1
  [[ -n "$OPT_SKIP_AUTOSTART" ]] && return 1
  return 0
}

# Parse the API port from config.yaml (best-effort; defaults to 8000 if
# the config doesn't exist yet or doesn't declare a port). Mirrors the
# same parser used by Stop-RunningServices on the Windows side.
get_configured_api_port() {
  local default_port=8000
  [[ -f "$CONFIG_PATH" ]] || { echo "$default_port"; return; }
  # IMPORTANT: this awk runs on whatever `awk` ships on the host. macOS
  # default is BSD awk, which (per POSIX) treats `\b` inside a regex as
  # the backspace character, NOT a word-boundary. The previous pattern
  # `\bapi[[:space:]]*:...` therefore never matched a real `api:` line
  # on macOS and the function always returned the 8000 default, which
  # made wait_for_api_ready poll the wrong port whenever a user had
  # customised api.port in config.yaml. Anchored `^[[:space:]]*api...`
  # is POSIX-clean and matches both top-level and nested stanza headers.
  awk -v def="$default_port" '
    BEGIN { in_api = 0 }
    /^[[:space:]]*api[[:space:]]*:[[:space:]]*$/ { in_api = 1; next }
    in_api && /^[^[:space:]]/ { exit }
    in_api && /^[[:space:]]*port[[:space:]]*:[[:space:]]*[0-9]+/ {
      sub(/^[[:space:]]*port[[:space:]]*:[[:space:]]*/, "")
      print $1; exit
    }
    END { if (NR == 0) print def }
  ' "$CONFIG_PATH" 2>/dev/null | grep -E '^[0-9]+$' || echo "$default_port"
}

# Bridge the gap between "the .app process is running" (pgrep success)
# and "the browser tab is open" (Swift launcher's own /health poll). On
# a fresh first launch the API can take 10-30 s to bind the port, and
# the installer used to exit during that wait - leaving the user staring
# at a returned shell prompt with no idea what's happening. Two-stage
# end output: "installed" success line, then "Starting the app..." with
# live dots until /health responds, then "started" success line. The
# .app's own /health poll fires in parallel; by the time we see ready,
# the browser is opening within a second or two.
wait_for_api_ready() {
  local timeout="${1:-90}"
  local port
  port="$(get_configured_api_port)"
  local start_ts; start_ts="$(date +%s)"
  printf "  ${DIM}Starting the app${NC}"
  # Probe 127.0.0.1 explicitly rather than `localhost` for parity with
  # the Windows installer (PS 5.1's Invoke-WebRequest resolves
  # `localhost` to ::1 first and waits for IPv6 to time out before
  # falling back to IPv4). curl on macOS is normally robust here, but
  # using 127.0.0.1 across both platforms removes one DNS / dual-stack
  # variable and makes the poll behaviour identical. --max-time 3
  # gives breathing room over the previous 1 s, which was tight when
  # the API is still binding the port.
  #
  # Track WALL-CLOCK elapsed time, not iteration count. Each iteration
  # blocks for up to `curl --max-time 3` plus `sleep 1`, so an
  # iteration-counted timeout of 90 could spend ~360 s on a failure
  # path - and the warning saying "didn't respond within ${timeout}s"
  # would be wildly misleading. date(1) is POSIX and cheap.
  while [[ $(( $(date +%s) - start_ts )) -lt $timeout ]]; do
    if curl -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      printf "\n\n${GREEN}${BOLD}  ✓ Media Search Agent started!${NC}\n"
      printf "  ${DIM}Your browser will open at http://localhost:${port}${NC}\n\n"
      # Give the .app's parallel /health poll ~2s to fire and open the
      # browser tab before this installer exits. Without this pause the
      # shell prompt can return BEFORE the browser opens (because the
      # installer's poll won the race by a fraction of a second), and
      # the user sees "✓ Started!" → prompt → confusing silence → tab.
      # 2s is empirically enough on every Mac we've tested; if the .app
      # is slower, the tab still opens shortly afterward - the sleep
      # just trades a deterministic 2s for the perceived discontinuity.
      sleep 2
      return 0
    fi
    printf "."
    sleep 1
  done
  printf "\n"
  log_warn "API didn't respond on http://localhost:${port}/health within ${timeout}s. Check the menu bar app and the launcher log under $LOG_DIR/launch-*.log."
  return 1
}

install_packages() {
  local uv_bin="$APP_CODE_DIR/bin/uv"
  # Bundles ship requirements-api.txt — the lean runtime contract — not
  # the full dev requirements.txt. build-bundle.sh enforces this; if the
  # file is missing it's a build-bundle bug, so fail loud.
  local reqs="$APP_CODE_DIR/requirements-api.txt"
  [[ -f "$reqs" ]] || die "requirements-api.txt not found at $reqs"

  log_info "Installing Python packages (this may take several minutes)..."

  local tmp_reqs; tmp_reqs="$(mktemp)"
  if [[ "$OS" == "macos" ]]; then
    # On macOS: pin numpy<2 (OpenCV ABI), fix torch to a known range, and
    # strip facenet-pytorch from the main install so it can be installed
    # separately with --no-deps after torch is already in the venv (prevents
    # the solver from downgrading the platform torch build).
    awk '
      /^[[:space:]]*sentence-transformers([[:space:]\[<>=!~]|$)/ { next }
      /^[[:space:]]*numpy([[:space:]\[<>=!~]|$)/                 { next }
      /^[[:space:]]*torch([[:space:]\[<>=!~]|$)/                 { next }
      /^[[:space:]]*facenet-pytorch([[:space:]\[<>=!~]|$)/       { next }
      /^[[:space:]]*msa[-_]ranker([[:space:]\[<>=!~@]|$)/        { next }
      { print }
    ' "$reqs" > "$tmp_reqs"
    printf 'numpy<2\ntorch>=2.4,<2.7\n' >> "$tmp_reqs"
  else
    # Linux: strip facenet-pytorch for the same --no-deps reason. msa-ranker is
    # also stripped so the ranker is installed only by the explicit branch below
    # (wheel-or-pin, always --no-deps).
    awk '
      /^[[:space:]]*facenet-pytorch([[:space:]\[<>=!~]|$)/ { next }
      /^[[:space:]]*msa[-_]ranker([[:space:]\[<>=!~@]|$)/  { next }
      { print }
    ' "$reqs" > "$tmp_reqs"
  fi

  "$uv_bin" pip install --python "$VENV_DIR/bin/python" -r "$tmp_reqs"
  # Install facenet-pytorch after torch is in the venv to prevent the solver
  # from downgrading the torch wheel that was just placed above.
  "$uv_bin" pip install --python "$VENV_DIR/bin/python" --no-deps "facenet-pytorch>=2.6.0"
  "$uv_bin" pip install --python "$VENV_DIR/bin/python" --no-deps "$APP_CODE_DIR"
  # Learned-reranker serving library (zero-dependency, installed --no-deps). Resolved
  # in two ways, wheel-first; absent both ⇒ MSA runs on the heuristic exactly as before
  # (the import is guarded — INV-9). The ranker line was stripped from $tmp_reqs above so
  # it is installed only here, never by the bulk `-r` step.
  local ranker_whl ranker_pin
  ranker_whl=$(ls "$APP_CODE_DIR"/wheels/msa_ranker-*.whl 2>/dev/null | head -1 || true)
  # The venv is reused across upgrades, so ALWAYS clear any prior msa-ranker first, then
  # (re)install from the configured source if one is present. Uninstall-first keeps the
  # venv's ranker state matching the requirements in every case — a deactivated pin, or a
  # pin whose PEP 508 marker evaluates false on this platform (uv installs nothing) — both
  # end heuristic (INV-9; app.py logs whenever msa_ranker imports). No-op on a fresh venv
  # (uv exits 0 when the package is absent).
  "$uv_bin" pip uninstall --python "$VENV_DIR/bin/python" msa-ranker >/dev/null 2>&1 || true
  if [[ -n "$ranker_whl" ]]; then
    # Offline path: the vendored wheel ships only with private bundles.
    "$uv_bin" pip install --python "$VENV_DIR/bin/python" --no-deps "$ranker_whl"
    log_ok "Learned reranker installed ($(basename "$ranker_whl"))"
  elif ranker_pin=$(awk '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*msa[-_]ranker([[:space:]]*==|[[:space:]]*@)/ {
          # Drop a trailing inline comment ( #... preceded by space) — pip rejects it
          # on the command line, unlike in a -r file. The leading-space requirement
          # preserves a "#fragment" inside a URL/wheel spec (no preceding space).
          sub(/[[:space:]]+#.*$/, ""); sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, ""); print; exit
        }
      ' "$reqs") && [[ -n "$ranker_pin" ]]; then
    # Online path (the public mirror ships no vendored wheel): install whichever single
    # msa-ranker spec is uncommented in the runtime requirements — a PyPI ==pin, a GitHub
    # release-asset URL, or a git+ ref. ADR-011 keeps that version == the wheel's. If the
    # spec carries a marker that is false here, uv installs nothing → stays heuristic
    # (the uninstall above already cleared any stale copy).
    "$uv_bin" pip install --python "$VENV_DIR/bin/python" --no-deps "$ranker_pin"
    log_ok "Learned reranker installed from requirements pin ($ranker_pin)"
  fi
  rm -f "$tmp_reqs"
  log_ok "Python packages installed"
}

# ── Config ────────────────────────────────────────────────────────────────────

setup_config() {
  mkdir -p "$(dirname "$CONFIG_PATH")"
  if [[ -f "$CONFIG_PATH" ]]; then
    log_skip "config.yaml already exists at $CONFIG_PATH"
    rm -f "${BUNDLE_CONFIG_TEMPLATE:-}"
    return
  fi
  if [[ -n "${BUNDLE_CONFIG_TEMPLATE:-}" && -f "$BUNDLE_CONFIG_TEMPLATE" ]]; then
    cp "$BUNDLE_CONFIG_TEMPLATE" "$CONFIG_PATH"
    rm -f "$BUNDLE_CONFIG_TEMPLATE"
    log_ok "config.yaml created at $CONFIG_PATH"
  else
    die "Config template missing — cannot create $CONFIG_PATH. Re-run the installer."
  fi
}

# ── Launcher ──────────────────────────────────────────────────────────────────

install_launcher() {
  mkdir -p "$LAUNCHER_DIR"
  # MSA_CONFIG_PATH must be set explicitly: start.sh falls back to a platform
  # default that may differ from the installed location.
  cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
export MSA_ROOT="$APP_CODE_DIR"
export MSA_DATA_DIR="$APP_SUPPORT_DIR"
export MSA_CACHE_DIR="$CACHE_DIR"
export MSA_LOG_DIR="$LOG_DIR"
export MSA_VENV_DIR="$VENV_DIR"
export MSA_CONFIG_PATH="$CONFIG_PATH"
export PATH="\$HOME/.local/bin:$APP_CODE_DIR/bin:\$PATH"
export DYLD_LIBRARY_PATH="$APP_CODE_DIR/lib\${DYLD_LIBRARY_PATH:+:\$DYLD_LIBRARY_PATH}"

case "\${1:-}" in
  uninstall)
    exec bash "\$MSA_ROOT/uninstall.sh" ;;
  api)
    case "\${2:-start}" in
      start)
        shift 2 2>/dev/null || true
        exec bash "\$MSA_ROOT/scripts/start.sh" "\$@" ;;
      stop)
        exec bash "\$MSA_ROOT/scripts/stop.sh" ;;
      restart)
        bash "\$MSA_ROOT/scripts/stop.sh" 2>/dev/null || true
        exec bash "\$MSA_ROOT/scripts/start.sh" "--no-browser" ;;
      *)
        shift
        exec "$VENV_DIR/bin/msa" api "\$@" ;;
    esac ;;
  *)
    exec "$VENV_DIR/bin/msa" "\$@" ;;
esac
EOF
  chmod +x "$LAUNCHER"
  log_ok "Launcher installed at $LAUNCHER"

  if ! echo ":${PATH}:" | grep -q ":${LAUNCHER_DIR}:"; then
    local shell_rc
    if [[ "$SHELL" == */zsh ]]; then
      shell_rc="$HOME/.zshrc"
    else
      shell_rc="$HOME/.bashrc"
    fi
    local path_line='export PATH="$HOME/.local/bin:$PATH"'
    if ! grep -qF "$path_line" "$shell_rc" 2>/dev/null; then
      printf '\n%s\n' "$path_line" >> "$shell_rc"
      log_warn "Added ~/.local/bin to PATH in $shell_rc — restart your shell or run:"
      log_warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
  fi
}

# ── LaunchAgent (macOS auto-start on login) ───────────────────────────────────

install_launch_agent() {
  [[ "$OS" != "macos" ]] && return
  [[ -n "$OPT_SKIP_AUTOSTART" ]] && { log_skip "auto-start skipped (--skip-autostart)"; return; }

  local plist_dir="$HOME/Library/LaunchAgents"
  local plist="$plist_dir/${LAUNCH_AGENT_LABEL}.plist"
  mkdir -p "$plist_dir" "$LOG_DIR"

  # Unload any existing job with this label before writing the new plist.
  # Without this, launchctl load below silently fails (error suppressed) and
  # the old job definition stays in launchd until the next reboot.
  unload_launch_agent "$plist"

  # Launch the .app — it calls start.sh internally on startup.
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>           <string>$LAUNCH_AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-a</string>
    <string>$APP_BUNDLE</string>
  </array>
  <key>RunAtLoad</key>       <true/>
  <key>KeepAlive</key>       <false/>
  <key>StandardOutPath</key> <string>$LOG_DIR/launchagent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchagent-error.log</string>
</dict>
</plist>
EOF
  if launchctl load "$plist"; then
    log_ok "LaunchAgent installed — app will start on login"
  else
    log_warn "LaunchAgent load failed for $plist"
    log_warn "The app was installed and can still be launched manually from $APP_BUNDLE"
  fi
}

# ── systemd user service (Linux auto-start on login) ─────────────────────────

install_systemd_service() {
  [[ "$OS" != "linux" ]] && return
  [[ -n "$OPT_SKIP_AUTOSTART" ]] && { log_skip "auto-start skipped (--skip-autostart)"; return; }

  if ! command -v systemctl &>/dev/null; then
    log_warn "systemctl not found — skipping auto-start setup"
    return
  fi
  local svc_dir="$HOME/.config/systemd/user"
  mkdir -p "$svc_dir"

  cat > "$svc_dir/mediasearchagent.service" <<EOF
[Unit]
Description=Media Search Agent
After=network.target

[Service]
Type=simple
ExecStart=$LAUNCHER api start
Environment=MSA_ROOT=$APP_CODE_DIR
Environment=MSA_DATA_DIR=$APP_SUPPORT_DIR
Environment=MSA_CACHE_DIR=$CACHE_DIR
Environment=MSA_LOG_DIR=$LOG_DIR
Environment=MSA_VENV_DIR=$VENV_DIR
Environment=MSA_CONFIG_PATH=$CONFIG_PATH
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable mediasearchagent.service 2>/dev/null || true
  log_ok "systemd user service installed — app will start on login"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
  parse_args "$@"

  OS="$(detect_os)"
  ARCH="$(detect_arch)"

  if [[ "$OS" == "macos" && "$ARCH" == "x86_64" ]]; then
    # Distinguish "real Intel Mac" from "Apple Silicon running under Rosetta".
    # uname -m reports x86_64 in both cases, so without this branch an M-series
    # user who happens to launch the installer from an x86_64-translated shell
    # gets the wrong error.
    if [[ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" == "1" ]]; then
      die "Running under Rosetta (x86_64) on an Apple Silicon Mac. Re-run from a native arm64 shell, e.g.:
       arch -arm64 bash -c \"\$(curl -fsSL https://openara.ai/install.sh)\"
       Or open Terminal.app's Get Info and uncheck 'Open using Rosetta', then re-run."
    fi
    die "Intel Mac (x86_64) is not yet supported. Only Apple Silicon (arm64) bundles are published.
       Check https://github.com/${GITHUB_REPO:-openara-ai/media-search-agent}/releases for updates."
  fi
  if [[ "$OS" == "linux" && "$ARCH" == "arm64" ]]; then
    die "Linux arm64 is not yet supported. Only Linux x86_64 bundles are published.
       Check https://github.com/${GITHUB_REPO:-openara-ai/media-search-agent}/releases for updates."
  fi

  # ── Platform paths (ADR-009) ──────────────────────────────────────────────
  #
  # macOS: everything lives inside ~/Applications/MediaSearchAgent.app so the
  #        entire Tier-1 uninstall is just `rm -rf` on the bundle.
  #        User data (index, config, logs) stay in standard macOS locations.
  #
  # Linux: flat layout under ~/.local/share/MediaSearchAgent (APP_CODE_DIR ==
  #        APP_SUPPORT_DIR), with code items removed individually on uninstall.

  if [[ "$OS" == "macos" ]]; then
    APP_BUNDLE="$HOME/Applications/MediaSearchAgent.app"
    APP_CODE_DIR="$APP_BUNDLE/Contents/Resources"
    APP_SUPPORT_DIR="$HOME/Library/Application Support/MediaSearchAgent"
    CACHE_DIR="$HOME/Library/Caches/MediaSearchAgent"
    LOG_DIR="$HOME/Library/Logs/MediaSearchAgent"
    CONFIG_PATH="$APP_SUPPORT_DIR/config.yaml"
  else
    APP_BUNDLE=""
    APP_CODE_DIR="${OPT_DIR:-${MSA_DIR:-$HOME/.local/share/MediaSearchAgent}}"
    APP_SUPPORT_DIR="$HOME/.local/share/MediaSearchAgent"
    CACHE_DIR="$HOME/.cache/MediaSearchAgent"
    LOG_DIR="$HOME/.local/share/MediaSearchAgent/logs"
    CONFIG_PATH="$HOME/.config/MediaSearchAgent/config.yaml"
  fi

  VENV_DIR="$APP_CODE_DIR/.venv"
  LAUNCHER_DIR="$HOME/.local/bin"
  LAUNCHER="$LAUNCHER_DIR/msa"
  PYTHON_VERSION="3.12.8"
  GITHUB_REPO="openara-ai/media-search-agent"
  # Written at the end of a successful install; read at the top of the next
  # install run to detect (and refuse) version downgrades.
  VERSION_FILE="$APP_CODE_DIR/version.txt"
  BUNDLE_CONFIG_TEMPLATE=""
  INSTALL_LOG_FILE=""
  INSTALL_MODE=""
  INSTALL_MODE_REASON=""
  INSTALL_MODE_MARKERS=""
  detect_install_mode

  setup_logging "$INSTALL_MODE"

  print_banner

  # Pre-flight: OS version floor, free disk space, RAM warning. Runs after
  # paths are resolved (so the disk check knows the target drive) but
  # before any network IO so failure exits cheaply.
  check_system_requirements

  local version=""
  if [[ -n "$OPT_BUNDLE" ]]; then
    version="${OPT_VERSION:-}"
    [[ -n "$version" ]] && log_info "Version: $version" || log_info "Bundle:  $OPT_BUNDLE"
  else
    version="$(resolve_version)"
    log_info "Version: $version"
  fi

  # Downgrade guard - silent no-op for fresh installs and for legacy installs
  # without a version file. Runs whenever an existing install is detected,
  # whether marker state is complete (upgrade) or partial (repair).
  if [[ ( "$INSTALL_MODE" == "upgrade" || "$INSTALL_MODE" == "repair" ) && -n "$version" ]]; then
    check_version_downgrade "$version" "$VERSION_FILE"
  fi

  log_bold "[1/6] Previous install"
  stop_existing_macos_app

  log_bold "[2/6] App bundle"
  install_bundle "$version"

  log_bold "[3/6] Python environment"
  setup_python
  install_packages

  log_bold "[4/6] Configuration"
  setup_config

  log_bold "[5/6] Launcher"
  install_launcher

  log_bold "[6/6] Auto-start"
  install_launch_agent
  install_systemd_service

  # Record the installed version so the next install can guard against
  # downgrades. Written only after all steps above succeeded - if anything
  # failed, the previous version marker stays in place.
  if [[ "$version" != "(local bundle)" && -n "$version" ]]; then
    mkdir -p "$(dirname "$VERSION_FILE")"
    echo "$version" > "$VERSION_FILE"
  fi

  # Two-stage end output:
  #   1. "installed" success line (install steps are complete)
  #   2. Launch the .app + poll /health with live dots ("Starting the
  #      app........") until it responds
  #   3. "started" success line + browser-opening hint
  # Bridges the silent gap users used to see between install completing
  # and the browser opening - on a fresh first launch the API can take
  # 10-30 s to come up, during which the installer used to exit and
  # leave a returned shell prompt with no indication of what was
  # happening.
  printf "\n${GREEN}${BOLD}  ✓ Media Search Agent installed!${NC}\n\n"

  if [[ "$OS" == "macos" ]] && should_launch_app_after_install; then
    if ! open "$APP_BUNDLE" 2>/dev/null; then
      log_warn "Could not launch app (Gatekeeper may have blocked it). Try: open \"$APP_BUNDLE\""
    else
      local waited=0
      while [[ $waited -lt 10 ]]; do
        sleep 1; waited=$((waited + 1))
        pgrep -f "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent" >/dev/null 2>&1 && break
      done
      if pgrep -f "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent" >/dev/null 2>&1; then
        # The .app process is alive; poll /health for the API readiness
        # (which is what actually triggers the browser to open). The
        # .app's own /health poll fires in parallel; the two polls race
        # harmlessly and the browser opens within a second or two of
        # our "started" announcement.
        wait_for_api_ready 90
      else
        log_warn "App did not appear within ${waited}s; check $LOG_DIR/launch-*.log or run: open \"$APP_BUNDLE\""
      fi
    fi
  elif [[ "$OS" != "macos" ]]; then
    # Linux: no auto-launch; user runs `msa api start` themselves.
    printf "  ${DIM}Start with: msa api start  →  then open http://localhost:8000${NC}\n\n"
  fi
}

main "$@"
