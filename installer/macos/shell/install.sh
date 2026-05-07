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

log_info()  { printf "${DIM}·${NC} %s\n" "$*"; }
log_ok()    { printf "${GREEN}✓${NC} %s\n" "$*"; }
log_skip()  { printf "${DIM}– %s${NC}\n" "$*"; }
log_warn()  { printf "${YELLOW}!${NC} %s\n" "$*"; }
log_bold()  { printf "\n${BOLD}%s${NC}\n" "$*"; }
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
  local mode="$1"
  local timestamp
  timestamp="$(date '+%Y-%m-%d_%H%M%S')"

  mkdir -p "$LOG_DIR"
  INSTALL_LOG_FILE="$LOG_DIR/${mode}-${timestamp}.log"

  # Mirror all installer output to a timestamped file so install, upgrade, and
  # repair runs are diagnosable after the terminal session is gone. Strip ANSI
  # escapes from the file copy so the saved log stays readable in plain editors.
  exec > >(tee >(sed -E $'s/\x1B\\[[0-9;]*[[:alpha:]]//g' >> "$INSTALL_LOG_FILE")) 2>&1

  log_info "Log: $INSTALL_LOG_FILE"
  log_info "Mode: $mode"
  log_info "Mode markers: ${INSTALL_MODE_MARKERS:-none}"
  log_info "Mode reason: ${INSTALL_MODE_REASON:-unknown}"
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

# ── Banner ────────────────────────────────────────────────────────────────────

print_banner() {
  printf "\n${BOLD}  Media Search Agent Installer${NC}\n"
  printf "${DIM}  Local-first semantic search for your photos and videos${NC}\n\n"
  log_info "OS:   $OS / $ARCH"
  if [[ "$OS" == "macos" ]]; then
    log_info "App:  $APP_BUNDLE"
  else
    log_info "Code: $APP_CODE_DIR"
  fi
  log_info "Data: $APP_SUPPORT_DIR"
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

# ── Bundle download + extract ─────────────────────────────────────────────────

install_bundle() {
  local version="$1"
  local bundle_archive
  local tmp; tmp="$(mktemp -d)"

  if [[ -n "$OPT_BUNDLE" ]]; then
    [[ -f "$OPT_BUNDLE" ]] || die "Bundle file not found: $OPT_BUNDLE"
    bundle_archive="$OPT_BUNDLE"
    log_info "Using local bundle: $OPT_BUNDLE"
  else
    local bundle_name="MediaSearchAgent-${version#v}-${OS}-${ARCH}"
    local bundle_url="https://github.com/${GITHUB_REPO}/releases/download/${version}/${bundle_name}.tar.gz"
    download "$bundle_url" "$tmp/bundle.tar.gz" "bundle $version ($OS-$ARCH)"
    bundle_archive="$tmp/bundle.tar.gz"
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
    for item in src scripts pyproject.toml requirements.txt requirements-api.txt LICENSE NOTICE uninstall.sh bin; do
      rm -rf "$APP_CODE_DIR/$item"
      [[ -e "$bundle_dir/$item" ]] && mv "$bundle_dir/$item" "$APP_CODE_DIR/$item"
    done
  else
    # Linux: replace only the code items so we don't wipe index/data/venv
    # (APP_CODE_DIR == APP_SUPPORT_DIR on Linux).
    mkdir -p "$APP_CODE_DIR"
    # `requirements.txt`: pure-cleanup entry — see macOS branch above.
    for item in src scripts pyproject.toml requirements.txt requirements-api.txt LICENSE NOTICE uninstall.sh bin; do
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

print_post_install_message() {
  if should_launch_app_after_install; then
    printf "  ${BOLD}Launching…${NC}\n"
    printf "  ${DIM}Look for the search icon in your menu bar.${NC}\n"
    printf "  ${DIM}The app will start the service and open the browser automatically.${NC}\n\n"
  else
    printf "  ${DIM}Launch the app from %s when you're ready.${NC}\n\n" "$APP_BUNDLE"
  fi
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
      { print }
    ' "$reqs" > "$tmp_reqs"
    printf 'numpy<2\ntorch>=2.4,<2.7\n' >> "$tmp_reqs"
  else
    # Linux: strip facenet-pytorch for the same --no-deps reason.
    awk '
      /^[[:space:]]*facenet-pytorch([[:space:]\[<>=!~]|$)/ { next }
      { print }
    ' "$reqs" > "$tmp_reqs"
  fi

  "$uv_bin" pip install --python "$VENV_DIR/bin/python" -r "$tmp_reqs"
  # Install facenet-pytorch after torch is in the venv to prevent the solver
  # from downgrading the torch wheel that was just placed above.
  "$uv_bin" pip install --python "$VENV_DIR/bin/python" --no-deps "facenet-pytorch>=2.6.0"
  "$uv_bin" pip install --python "$VENV_DIR/bin/python" --no-deps "$APP_CODE_DIR"
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
  BUNDLE_CONFIG_TEMPLATE=""
  INSTALL_LOG_FILE=""
  INSTALL_MODE=""
  INSTALL_MODE_REASON=""
  INSTALL_MODE_MARKERS=""
  detect_install_mode

  setup_logging "$INSTALL_MODE"

  print_banner

  local version=""
  if [[ -n "$OPT_BUNDLE" ]]; then
    version="${OPT_VERSION:-}"
    [[ -n "$version" ]] && log_info "Version: $version" || log_info "Bundle:  $OPT_BUNDLE"
  else
    version="$(resolve_version)"
    log_info "Version: $version"
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

  printf "\n${GREEN}${BOLD}✓ Media Search Agent installed!${NC}\n\n"

  if should_launch_app_after_install; then
    if ! open "$APP_BUNDLE" 2>/dev/null; then
      log_warn "Could not launch app (Gatekeeper may have blocked it). Try: open \"$APP_BUNDLE\""
    else
      # open returns before the app process is running — wait briefly then verify
      local waited=0
      while [[ $waited -lt 10 ]]; do
        sleep 1; waited=$((waited + 1))
        pgrep -f "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent" >/dev/null 2>&1 && break
      done
      if pgrep -f "$APP_BUNDLE/Contents/MacOS/MediaSearchAgent" >/dev/null 2>&1; then
        log_ok "App launched — look for the search icon in your menu bar"
      else
        log_warn "App did not appear within ${waited}s after launch."
        log_warn "Check for errors in: $LOG_DIR/launch-*.log"
        log_warn "Or run manually: open \"$APP_BUNDLE\""
      fi
    fi
  fi

  if [[ "$OS" == "macos" ]]; then
    print_post_install_message
  fi

  if [[ "$OS" == "macos" ]]; then
    printf "  ${BOLD}Next steps${NC}\n"
    printf "  ${DIM}1. Use the Media Search Agent menu bar app to open the browser and manage the service.${NC}\n"
    printf "  ${DIM}2. Add your media folders on the Indexer page, then run the indexer.${NC}\n"
  else
    printf "  ${BOLD}Next steps${NC}\n"
    printf "  ${DIM}1. Add your media folders on the Indexer page, then run the indexer.${NC}\n"
    printf "  ${DIM}2. Or use the CLI:${NC}\n"
    printf "     ${DIM}msa api start | stop | restart${NC}\n"
    printf "     ${DIM}msa index run --help${NC}\n"
    printf "     ${DIM}msa uninstall${NC}\n"
  fi
  printf "\n"
  printf "  ${DIM}Config: %s${NC}\n" "$CONFIG_PATH"
  printf "  ${DIM}Logs:   %s${NC}\n" "$LOG_DIR"
  printf "\n"
}

main "$@"
