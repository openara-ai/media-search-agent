#!/usr/bin/env bash
# Media Search Agent — macOS Setup
# Sets up the application environment on macOS.
# Safe to run multiple times — every step is idempotent.
# Qdrant is embedded (no Docker required).
#
# Usage:
#   bash scripts/setup.sh [--non-interactive]
#
#   --non-interactive  Skip interactive prompts (Xcode CLT dialog, "press enter"
#                      pauses). Used by the .pkg postinstall script and CI.
#                      Exits non-zero if a required tool is absent.
#
# Requires:
#   - macOS 12 (Monterey) or later
#   - Xcode Command Line Tools (prompted if absent, or pre-installed in CI)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MSA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/tools.sh"

# ── Flags ─────────────────────────────────────────────────────────────────────

NON_INTERACTIVE=false
for arg in "$@"; do
  case "$arg" in
    --non-interactive) NON_INTERACTIVE=true ;;
  esac
done

# When running non-interactively (CI=true is also treated as non-interactive)
if [[ "${CI:-}" == "true" ]]; then
  NON_INTERACTIVE=true
fi

# ── Pinned versions ───────────────────────────────────────────────────────────


# ── Setup ─────────────────────────────────────────────────────────────────────

setup_log "install"
START_TIME=$(date +%s)

log_bold "Media Search Agent — macOS Setup"
log_info "Root:    $MSA_ROOT"
log_info "OS:      $(msa_os) / $(msa_arch)"
log_info "Python:  $PYTHON_VERSION"

# ── Guard: must be macOS ──────────────────────────────────────────────────────

if [[ "$(msa_os)" != "macos" ]]; then
  die "This script is for macOS only. Use scripts/dev-setup.sh on Linux/WSL2."
fi

ARCH="$(msa_arch)"
if [[ "$ARCH" != "arm64" && "$ARCH" != "x86_64" ]]; then
  die "Unsupported architecture: $ARCH"
fi

# ── macOS version check ───────────────────────────────────────────────────────

log_bold "macOS version..."

MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
if [[ "$MACOS_MAJOR" -lt 12 ]]; then
  die "macOS 12 (Monterey) or later is required. Found: $(sw_vers -productVersion)"
fi
log_ok "macOS $(sw_vers -productVersion)"

# ── Xcode Command Line Tools ──────────────────────────────────────────────────

log_bold "Xcode Command Line Tools..."

if xcode-select -p &>/dev/null; then
  log_skip "Xcode CLT already installed at $(xcode-select -p)"
elif [[ "$NON_INTERACTIVE" == "true" ]]; then
  # In non-interactive mode the preinstall script must have already ensured
  # Xcode CLT is present. Fail fast so the installer shows an error dialog.
  die "Xcode Command Line Tools are required but not installed. Run: xcode-select --install"
else
  log_info "Xcode CLT not found — triggering install dialog..."
  log_warn "A system dialog will appear. Click 'Install' and wait for it to complete."
  log_warn "Then re-run this script."
  xcode-select --install 2>/dev/null || true
  die "Re-run this script after Xcode CLT installation completes."
fi

# ── Bundled binaries ──────────────────────────────────────────────────────────
# In Phase 1C (macOS installer), exiftool/mediainfo are bundled inside the
# .pkg payload and placed in $MSA_ROOT/bin/. For Phase 1A (developer setup), we
# fall back to Homebrew if available, otherwise warn.

log_bold "Binary dependencies (exiftool, mediainfo)..."

BIN_DIR="$MSA_ROOT/bin"
mkdir -p "$BIN_DIR"

_check_or_warn_binary() {
  local cmd="$1"
  local brew_name="${2:-$cmd}"
  # Check bundled first, then PATH
  if [[ -f "$BIN_DIR/$cmd" ]]; then
    log_skip "$cmd: found in $BIN_DIR (bundled)"
  elif command -v "$cmd" &>/dev/null; then
    log_skip "$cmd: found in PATH ($(command -v "$cmd"))"
  elif command -v brew &>/dev/null; then
    log_info "$cmd: not found — installing via Homebrew..."
    brew install "$brew_name" --quiet
    log_ok "$cmd installed via Homebrew"
  else
    log_warn "$cmd not found. Install via: brew install $brew_name"
    log_warn "Or place a static binary at $BIN_DIR/$cmd"
  fi
}

_check_or_warn_binary mediainfo

# exiftool: use ensure_exiftool_macos (version-gated) unless the bundled binary
# is already present (populated by build.sh for the .pkg installer).
if [[ -f "$BIN_DIR/exiftool" ]]; then
  log_skip "exiftool: found in $BIN_DIR (bundled)"
else
  ensure_exiftool_macos
fi

# .NET SDK is only needed on macOS developer checkouts that build the Windows
# shell bundle tray app. End-user installs run this script too, so never make
# Homebrew/.NET a postinstall prerequisite for normal app usage.
if [[ -e "$MSA_ROOT/.git" ]]; then
  log_bold ".NET SDK..."
  ensure_dotnet_macos
else
  log_skip ".NET SDK not needed for installed app runtime"
fi

# ── uv ────────────────────────────────────────────────────────────────────────

log_bold "uv (Python manager)..."

# Prefer the app-private uv bundled in $BIN_DIR (populated by build.sh for the
# .pkg installer). If absent (dev checkout / CI), fall back to a download into
# $HOME/.local/bin so setup.sh remains self-contained for development use.
if [[ -f "$BIN_DIR/uv" ]]; then
  UV_BIN="$BIN_DIR/uv"
  log_skip "uv: using bundled $UV_BIN"
else
  UV_BIN="$HOME/.local/bin/uv"

  # Determine uv archive name for this platform
  if [[ "$ARCH" == "arm64" ]]; then
    UV_ARCHIVE="uv-aarch64-apple-darwin.tar.gz"
  else
    UV_ARCHIVE="uv-x86_64-apple-darwin.tar.gz"
  fi
  UV_URL="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ARCHIVE}"

  if [[ -f "$UV_BIN" ]]; then
    INSTALLED_UV=$("$UV_BIN" --version 2>/dev/null | awk '{print $2}' || echo "0")
    if [[ "$INSTALLED_UV" == "$UV_VERSION" ]]; then
      log_skip "uv $UV_VERSION already installed"
    else
      log_info "Upgrading uv from $INSTALLED_UV to $UV_VERSION..."
      TMP="$(mktemp -d)"
      download "$UV_URL" "$TMP/uv.tar.gz" "uv $UV_VERSION"
      tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
      cp "$TMP"/uv-*/uv "$UV_BIN"
      rm -rf "$TMP"
      log_ok "uv upgraded to $UV_VERSION"
    fi
  else
    log_info "Installing uv $UV_VERSION (dev fallback — not a bundled installer)..."
    mkdir -p "$HOME/.local/bin"
    TMP="$(mktemp -d)"
    download "$UV_URL" "$TMP/uv.tar.gz" "uv $UV_VERSION"
    tar -xzf "$TMP/uv.tar.gz" -C "$TMP"
    cp "$TMP"/uv-*/uv "$UV_BIN"
    chmod +x "$UV_BIN"
    rm -rf "$TMP"
    log_ok "uv $UV_VERSION installed"
  fi

  export PATH="$HOME/.local/bin:$PATH"
fi

# ── Python ────────────────────────────────────────────────────────────────────

log_bold "Python $PYTHON_VERSION..."

if "$UV_BIN" python list 2>/dev/null | grep -q "cpython-${PYTHON_VERSION}"; then
  log_skip "Python $PYTHON_VERSION already managed by uv"
else
  log_info "Installing Python $PYTHON_VERSION via uv..."
  "$UV_BIN" python install "$PYTHON_VERSION"
  log_ok "Python $PYTHON_VERSION installed"
fi

# ── Virtual environment ───────────────────────────────────────────────────────

log_bold "Virtual environment..."

VENV="$(msa_venv_dir)"
mkdir -p "$(dirname "$VENV")"

if [[ -d "$VENV" ]]; then
  VENV_PYTHON=$("$VENV/bin/python" --version 2>/dev/null | awk '{print $2}' || echo "unknown")
  if [[ "$VENV_PYTHON" == "$PYTHON_VERSION" ]]; then
    log_skip "venv exists with Python $VENV_PYTHON"
  else
    log_warn "venv Python $VENV_PYTHON != $PYTHON_VERSION — recreating..."
    rm -rf "$VENV"
    "$UV_BIN" venv "$VENV" --python "$PYTHON_VERSION"
    log_ok "venv recreated"
  fi
else
  log_info "Creating venv at $VENV..."
  "$UV_BIN" venv "$VENV" --python "$PYTHON_VERSION"
  log_ok "venv created"
fi

# ── Python packages ───────────────────────────────────────────────────────────

log_bold "Python packages..."

REQS="$MSA_ROOT/requirements-api.txt"
if [[ ! -f "$REQS" ]]; then
  REQS="$MSA_ROOT/requirements.txt"
fi

log_info "Requirements: $REQS"

# Build a filtered manifest for this platform:
#   - numpy<2: native wheels in the mac runtime still load NumPy 1.x ABIs
#   - torch>=2.4,<2.7: stay compatible with the current transitive transformers stack
#   - skip sentence-transformers: not imported by the packaged runtime today, and
#     recent versions can drag in a transformers/torch compatibility mismatch
#   - facenet-pytorch excluded here; installed --no-deps after torch to prevent
#     the facenet-pytorch torch-solver from downgrading the already-installed torch
TMP_REQS="$(mktemp)"
awk '
  /^[[:space:]]*sentence-transformers([[:space:]\[<>=!~]|$)/ { next }
  /^[[:space:]]*numpy([[:space:]\[<>=!~]|$)/ { next }
  /^[[:space:]]*torch([[:space:]\[<>=!~]|$)/ { next }
  /^[[:space:]]*facenet-pytorch([[:space:]\[<>=!~]|$)/ { next }
  { print }
' "$REQS" > "$TMP_REQS"
cat >> "$TMP_REQS" <<'EOF'
numpy<2
torch>=2.4,<2.7
EOF

"$UV_BIN" pip install --python "$VENV/bin/python" -r "$TMP_REQS"
rm -f "$TMP_REQS"

# Install facenet-pytorch without letting its solver re-resolve torch.
"$UV_BIN" pip install --python "$VENV/bin/python" "facenet-pytorch>=2.6.0" --no-deps

if msa_is_macos_system_install; then
  # /Applications/MediaSearchAgent is root-owned. Even a non-editable
  # `pip install /path` fails because setuptools writes egg-info to the
  # source tree during wheel build. Copy only pyproject.toml + src/ to
  # a tmp directory (writable by the current user), build the wheel there,
  # install it, then clean up. --no-deps avoids re-downloading packages
  # already installed from requirements above.
  TMP_SRC=$(mktemp -d)
  cp "$MSA_ROOT/pyproject.toml" "$TMP_SRC/"
  cp "$MSA_ROOT/README.md" "$TMP_SRC/" 2>/dev/null || true
  cp -r "$MSA_ROOT/src" "$TMP_SRC/"
  "$UV_BIN" pip install --python "$VENV/bin/python" --no-deps "$TMP_SRC"
  rm -rf "$TMP_SRC"
else
  "$UV_BIN" pip install --python "$VENV/bin/python" -e "$MSA_ROOT"

  TEST_REQS="$MSA_ROOT/tests/requirements-ci.txt"
  if [[ -f "$TEST_REQS" ]]; then
    log_info "Test requirements: $TEST_REQS"
    # facenet-pytorch was already installed --no-deps above; filter it here so
    # the solver does not re-evaluate its torch constraints and downgrade torch.
    grep -v '^[[:space:]]*facenet-pytorch' "$TEST_REQS" \
      | "$UV_BIN" pip install --python "$VENV/bin/python" -r /dev/stdin
    "$UV_BIN" pip install --python "$VENV/bin/python" "facenet-pytorch>=2.6.0" --no-deps
  fi
fi

log_ok "Python packages installed"

# ── Node.js + React UI ───────────────────────────────────────────────────────
# On a system install (/Applications/...) the React dist is pre-built inside
# the .pkg payload — $MSA_ROOT is root-owned so npm/node_modules cannot write
# there. Skip entirely; the bundled dist is served directly by FastAPI.

log_bold "React UI..."

UI_DIR="$MSA_ROOT/src/msa_apps/ui"

if msa_is_macos_system_install; then
  if [[ -d "$UI_DIR/dist" ]]; then
    log_skip "React UI — pre-built in installer package"
  else
    if [[ "$NON_INTERACTIVE" == "true" ]]; then
      echo "ERROR: React UI dist not found at $UI_DIR/dist." >&2
      echo "       Re-install the application to restore the bundled UI." >&2
      exit 1
    fi
    log_warn "React UI dist not found at $UI_DIR/dist — the web UI will not load."
    log_warn "Re-install the application to restore the bundled UI."
  fi
else
  if node --version 2>/dev/null | grep -q "^v${NODE_MAJOR}\."; then
    log_skip "Node.js $(node --version) already installed"
  elif command -v brew &>/dev/null; then
    log_info "Installing Node.js v${NODE_MAJOR} via Homebrew..."
    brew install node@${NODE_MAJOR} --quiet
    # Only force-link if the active node is absent or the wrong major version — avoids
    # downgrading a developer who already has Node 22+ linked.
    ACTIVE_NODE_MAJOR=$(node --version 2>/dev/null | grep -oE '^v[0-9]+' | tr -d 'v' || echo "0")
    if [[ "$ACTIVE_NODE_MAJOR" -lt "$NODE_MAJOR" ]]; then
      brew link node@${NODE_MAJOR} --force --overwrite 2>/dev/null || true
    fi
    log_ok "Node.js $(node --version) installed"
  else
    log_warn "Node.js v${NODE_MAJOR} not found and Homebrew is not available."
    log_warn "Install Node.js from https://nodejs.org/ then re-run this script."
  fi

  if command -v node &>/dev/null; then
    if [[ ! -d "$UI_DIR/node_modules" ]]; then
      log_info "npm ci (installing packages)..."
      npm --prefix "$UI_DIR" ci >> "$MSA_LOG_FILE" 2>&1
      log_ok "npm packages installed"
    else
      log_skip "node_modules already present"
    fi

    UI_DIST="$UI_DIR/dist"
    if [[ ! -d "$UI_DIST" ]] || [[ "$UI_DIR/package.json" -nt "$UI_DIST/index.html" ]]; then
      log_info "Building React UI..."
      npm --prefix "$UI_DIR" run build >> "$MSA_LOG_FILE" 2>&1
      log_ok "React UI built → $UI_DIST"
    else
      log_skip "React UI dist is up to date"
    fi
  else
    if [[ "$NON_INTERACTIVE" == "true" ]] && [[ ! -d "$UI_DIR/dist" ]]; then
      echo "ERROR: Node.js is required to build the React UI but was not found." >&2
      echo "       Install Node.js ${NODE_MAJOR}+ and re-run setup.sh." >&2
      exit 1
    fi
    log_warn "Node.js not available — skipping React UI build."
    log_warn "The web UI will not work until Node.js is installed and setup.sh is re-run."
  fi
fi

# ── Git hooks ─────────────────────────────────────────────────────────────────

log_bold "Git hooks..."

HOOKS_DIR="$MSA_ROOT/.git/hooks"
if [[ -d "$HOOKS_DIR" ]]; then
  for hook in pre-commit pre-push; do
    SRC="$SCRIPT_DIR/$hook"
    DST="$HOOKS_DIR/$hook"
    if [[ -f "$SRC" ]]; then
      if [[ -f "$DST" ]] && diff -q "$SRC" "$DST" &>/dev/null; then
        log_skip "git hook: $hook (up to date)"
      else
        cp "$SRC" "$DST" && chmod +x "$DST"
        log_ok "git hook: $hook installed"
      fi
    fi
  done
else
  log_skip "Not a git repo — skipping hook installation"
fi

# ── config.yaml ───────────────────────────────────────────────────────────────

log_bold "Configuration..."

CONFIG_PATH="$(msa_platform_config_path)"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"
mkdir -p "$CONFIG_DIR"

if [[ -f "$CONFIG_PATH" ]]; then
  log_skip "config.yaml already exists — not overwritten ($CONFIG_PATH)"
else
  if [[ -f "$MSA_ROOT/config.yaml.template" ]]; then
    # Installer-bundled template (normal end-user install path).
    cp "$MSA_ROOT/config.yaml.template" "$CONFIG_PATH"
    log_ok "config.yaml created from installer template at $CONFIG_PATH"
  else
    # No template present. On a real installer build this should not happen.
    # On a dev checkout config.yaml should already exist from git — if it doesn't,
    # the checkout is incomplete and start.sh will fail.
    log_warn "config.yaml not found and no installer template present."
    log_warn "If you are running from a git checkout, restore config.yaml from git."
    log_warn "The API will not start until config.yaml exists at $CONFIG_PATH"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

log_ok "──────────────────────────────────────────────"
log_ok "macOS setup complete in ${ELAPSED}s"
log_ok ""
log_ok "Next steps:"
log_ok "  1. Edit config.yaml at $CONFIG_PATH to add your media source paths"
log_ok "  2. Run: bash scripts/start.sh"
log_ok ""
log_ok "Log: $MSA_LOG_FILE"
log_ok "──────────────────────────────────────────────"
