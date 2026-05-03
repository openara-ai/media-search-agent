#!/usr/bin/env bash
# Media Search Agent — Developer Bootstrap
#
# Sets up the complete development environment on macOS and Linux/WSL2.
# Safe to re-run — all steps are idempotent.
#
# After setup completes, drops you into the configured dev shell
# (venv activated, MSA_DEV=1) unless --bvt is passed.
#
# Usage:
#   bash scripts/dev-setup.sh        # full bootstrap + enter dev shell
#   bash scripts/dev-setup.sh --bvt  # BVT mode: tools + Python env + pytest;
#                                    # skips Node.js, UI build, git hooks, etc.
#                                    # Used only by .github/workflows/bvt.yml.
#
# 2-step developer setup:
#   git clone https://github.com/kumraj/media-search-agent
#   bash scripts/dev-setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MSA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/tools.sh"

# ── Flags ─────────────────────────────────────────────────────────────────────

BVT=0
for arg in "$@"; do
    [[ "$arg" == "--bvt" ]] && BVT=1
done

# ── Setup ─────────────────────────────────────────────────────────────────────

setup_log "dev-setup"
START_TIME=$(date +%s)

log_bold "Media Search Agent — Developer Bootstrap"
log_info "Root:     $MSA_ROOT"
log_info "Platform: $(msa_os) / $(msa_arch)"
[[ $BVT -eq 1 ]] && log_info "Mode:     --bvt (CI; skips Node.js, UI, hooks)"

case "$(msa_os)" in
    linux | macos) ;;
    *) die "Unsupported platform: $(uname -s). Supported: macOS, Linux/WSL2." ;;
esac

# ── External tools ─────────────────────────────────────────────────────────────

log_bold "External tools..."

if [[ "$(msa_os)" == "linux" ]]; then

    [[ "$(msa_arch)" != "x86_64" ]] && \
        die "Only x86_64 is supported on Linux. Found: $(msa_arch)"

    sudo apt-get update -qq

    _apt_install() {
        local pkg="$1"
        if dpkg -l "$pkg" &>/dev/null && dpkg -l "$pkg" | grep -q '^ii'; then
            log_skip "apt: $pkg"
        else
            log_info "apt: installing $pkg..."
            sudo apt-get install -y -qq "$pkg"
            log_ok "apt: $pkg installed"
        fi
    }

    # Tools required in all modes (BVT and full bootstrap)
    _apt_install curl
    _apt_install mediainfo

    if [[ $BVT -eq 0 ]]; then
        # Additional build/dev dependencies not needed for BVT (its Python
        # deps come as pre-built manylinux wheels via uv).
        _apt_install git
        _apt_install netcat-openbsd
        _apt_install python3-dev
        _apt_install build-essential
        _apt_install cmake
    fi

    install_exiftool_linux   # lib/tools.sh — pins to EXIFTOOL_VERSION

elif [[ "$(msa_os)" == "macos" ]]; then

    if ! command -v brew >/dev/null 2>&1; then
        die "Homebrew is required. Install it first: https://brew.sh"
    fi

    for tool in mediainfo; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            log_info "Installing $tool via Homebrew..."
            brew install "$tool" --quiet
            log_ok "$tool installed"
        else
            log_skip "$tool: $(command -v "$tool")"
        fi
    done

    ensure_exiftool_macos    # lib/tools.sh — checks version, brew install/upgrade

fi

# ── Verify tools ───────────────────────────────────────────────────────────────
# On macOS, python3 ships with Xcode CLT which setup.sh ensures is present.
# Run check_env.py after setup.sh on macOS so python3 is guaranteed available.

if [[ "$(msa_os)" == "linux" ]]; then
    python3 "$SCRIPT_DIR/check_env.py" || die "Tool check failed — see above"
fi

# BVT mode is Linux-only (only consumer is .github/workflows/bvt.yml dev-tools
# job, which runs on ubuntu-latest). Bail out clearly if invoked elsewhere.
if [[ $BVT -eq 1 && "$(msa_os)" != "linux" ]]; then
    die "--bvt mode is Linux-only (used by GitHub Actions ubuntu-latest)."
fi

# ── Python environment ─────────────────────────────────────────────────────────

if [[ "$(msa_os)" == "macos" ]]; then

    # Delegate Python/venv/npm/hooks/config to setup.sh (macOS bootstrap).
    # setup.sh is also called by the macOS installer postinstall and CI, so
    # we don't duplicate that logic here.
    log_bold "Python environment (via setup.sh)..."
    bash "$SCRIPT_DIR/setup.sh" --non-interactive

    # Verify tools now that Xcode CLT (and thus python3) is confirmed present.
    python3 "$SCRIPT_DIR/check_env.py" || die "Tool check failed — see above"

else

    # Linux: inline bootstrap

    log_info "uv: $UV_VERSION   Python: $PYTHON_VERSION"

    # ── uv ────────────────────────────────────────────────────────────────────

    log_bold "uv (Python manager)..."

    UV_BIN="$HOME/.local/bin/uv"

    if [[ -f "$UV_BIN" ]]; then
        INSTALLED_UV=$("$UV_BIN" --version 2>/dev/null | awk '{print $2}' || echo "0")
        if [[ "$INSTALLED_UV" == "$UV_VERSION" ]]; then
            log_skip "uv $UV_VERSION already installed"
        else
            log_info "Upgrading uv from $INSTALLED_UV to $UV_VERSION..."
            curl -LsSf \
                "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-musl.tar.gz" \
                | tar -xz -C "$HOME/.local/bin" --strip-components=1 \
                    uv-x86_64-unknown-linux-musl/uv
            log_ok "uv upgraded to $UV_VERSION"
        fi
    else
        log_info "Installing uv $UV_VERSION..."
        mkdir -p "$HOME/.local/bin"
        curl -LsSf \
            "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-musl.tar.gz" \
            | tar -xz -C "$HOME/.local/bin" --strip-components=1 \
                uv-x86_64-unknown-linux-musl/uv
        log_ok "uv $UV_VERSION installed"
    fi

    export PATH="$HOME/.local/bin:$PATH"

    # ── Python ────────────────────────────────────────────────────────────────

    log_bold "Python $PYTHON_VERSION..."

    if "$UV_BIN" python list 2>/dev/null | grep -q "cpython-${PYTHON_VERSION}"; then
        log_skip "Python $PYTHON_VERSION already managed by uv"
    else
        log_info "Installing Python $PYTHON_VERSION via uv..."
        "$UV_BIN" python install "$PYTHON_VERSION"
        log_ok "Python $PYTHON_VERSION installed"
    fi

    # ── Virtual environment ───────────────────────────────────────────────────

    log_bold "Virtual environment..."

    VENV="$MSA_ROOT/.venv"

    if [[ -d "$VENV" ]]; then
        VENV_PYTHON=$("$VENV/bin/python" --version 2>/dev/null | awk '{print $2}' || echo "unknown")
        if [[ "$VENV_PYTHON" == "$PYTHON_VERSION" ]]; then
            log_skip "venv exists with Python $VENV_PYTHON"
        else
            log_warn "venv Python $VENV_PYTHON != $PYTHON_VERSION — recreating..."
            rm -rf "$VENV"
            "$UV_BIN" venv "$VENV" --python "$PYTHON_VERSION"
            log_ok "venv recreated with Python $PYTHON_VERSION"
        fi
    else
        log_info "Creating venv at $VENV..."
        "$UV_BIN" venv "$VENV" --python "$PYTHON_VERSION"
        log_ok "venv created"
    fi

    # ── Python packages ───────────────────────────────────────────────────────

    log_bold "Python packages..."

    REQS="$MSA_ROOT/requirements-api.txt"
    [[ ! -f "$REQS" ]] && REQS="$MSA_ROOT/requirements.txt"
    log_info "Requirements: $REQS"

    # Install all packages except facenet-pytorch; then install facenet-pytorch
    # --no-deps so its solver cannot replace the CUDA-capable torch already
    # placed by requirements-api.txt (torch==2.* pinned there).
    TMP_REQS="$(mktemp)"
    grep -v '^facenet-pytorch' "$REQS" > "$TMP_REQS"
    "$UV_BIN" pip install --python "$VENV/bin/python" -r "$TMP_REQS"
    rm -f "$TMP_REQS"
    "$UV_BIN" pip install --python "$VENV/bin/python" "facenet-pytorch>=2.6.0" --no-deps

    "$UV_BIN" pip install --python "$VENV/bin/python" -e "$MSA_ROOT"

    if [[ $BVT -eq 1 ]]; then
        # BVT only needs pytest on top of the runtime install; everything else
        # in tests/requirements-ci.txt is either runtime (already installed)
        # or test-only deps that aren't exercised by the BVT scenarios
        # (httpx for FastAPI TestClient, etc.). Keeping this layer minimal
        # mirrors how the bundle BVT validators install only pytest on top
        # of the bundle's runtime.
        "$UV_BIN" pip install --python "$VENV/bin/python" pytest
    else
        TEST_REQS="$MSA_ROOT/tests/requirements-ci.txt"
        if [[ -f "$TEST_REQS" ]]; then
            log_info "Test requirements: $TEST_REQS"
            grep -v '^facenet-pytorch' "$TEST_REQS" | \
                "$UV_BIN" pip install --python "$VENV/bin/python" -r /dev/stdin
        fi
    fi
    log_ok "Python packages installed"

    if [[ $BVT -eq 1 ]]; then
        log_ok "BVT environment ready (--bvt; skipping Node.js, UI build, hooks)"
        exit 0
    fi

    # ── Node.js ───────────────────────────────────────────────────────────────

    log_bold "Node.js..."

    if node --version 2>/dev/null | grep -q "^v${NODE_MAJOR}\."; then
        log_skip "Node.js $(node --version) already installed"
    else
        log_info "Installing Node.js v${NODE_MAJOR}.x via NodeSource..."
        curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" \
            | sudo -E bash - >> "$MSA_LOG_FILE" 2>&1
        sudo apt-get install -y -qq nodejs
        log_ok "Node.js $(node --version) installed"
    fi

    # ── React UI ──────────────────────────────────────────────────────────────

    log_bold "React UI..."

    UI_DIR="$MSA_ROOT/src/msa_apps/ui"

    if [[ ! -d "$UI_DIR/node_modules" ]] || \
       [[ "$UI_DIR/package-lock.json" -nt "$UI_DIR/node_modules" ]]; then
        log_info "npm ci..."
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

    # ── Git hooks ─────────────────────────────────────────────────────────────

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
                    cp "$SRC" "$DST"
                    chmod +x "$DST"
                    log_ok "git hook: $hook installed"
                fi
            fi
        done
    else
        log_skip "Not a git repo — skipping hook installation"
    fi

    # ── config.yaml ───────────────────────────────────────────────────────────

    log_bold "Configuration..."

    CONFIG_PATH="${MSA_CONFIG_PATH:-$MSA_ROOT/config.yaml}"
    if [[ -f "$CONFIG_PATH" ]]; then
        log_skip "config.yaml exists at $CONFIG_PATH"
    else
        log_warn "config.yaml not found at $CONFIG_PATH — environment will be incomplete."
        log_warn "Restore with: git checkout config.yaml"
        log_warn "start.sh will fail until config.yaml exists."
    fi

fi

# ── Summary ────────────────────────────────────────────────────────────────────

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
log_ok "──────────────────────────────────────────────"
log_ok "Bootstrap complete in ${ELAPSED}s"
log_ok "Log: $MSA_LOG_FILE"
log_ok "──────────────────────────────────────────────"

# ── Enter dev shell ────────────────────────────────────────────────────────────
# Replace this process with the dev shell so the developer lands immediately
# in the configured environment. Skip when stdin is not a terminal (CI pipes).

if [[ -t 0 ]]; then
    exec bash "$SCRIPT_DIR/dev-cli.sh"
fi
