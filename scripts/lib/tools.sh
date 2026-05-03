#!/usr/bin/env bash
# scripts/lib/tools.sh — Pinned external tool versions and install helpers.
# Source this file after lib/common.sh; do not execute it directly.
#
# Adding a new tool: add a version constant here + an install/check function.
# All callers (dev-setup.sh, setup.sh, ci.yml via dev-setup.sh) pick it up
# automatically.

# shellcheck source=scripts/versions.env
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../versions.env"

dotnet_sdk_present() {
    command -v dotnet >/dev/null 2>&1 && \
        dotnet --list-sdks 2>/dev/null | awk '{print $1}' | \
        while IFS=. read -r major _; do
            [[ -n "${major:-}" ]] || continue
            if ((10#$major >= 10#$DOTNET_SDK_MAJOR)); then
                exit 0
            fi
        done
}

# Return the installed exiftool version string, or "0" if not found.
exiftool_installed_ver() {
    command -v exiftool >/dev/null 2>&1 \
        && exiftool -ver 2>/dev/null | tr -d '[:space:]' \
        || echo "0"
}

# Return 0 (true) if version string $1 >= $2 using dotted numeric comparison.
_ver_gte() {
    local left="${1:-0}"
    local right="${2:-0}"
    local IFS=.
    local -a left_parts=($left) right_parts=($right)
    local len="${#left_parts[@]}"

    if [[ "${#right_parts[@]}" -gt "$len" ]]; then
        len="${#right_parts[@]}"
    fi

    local i left_num right_num
    for ((i = 0; i < len; i++)); do
        left_num="${left_parts[i]:-0}"
        right_num="${right_parts[i]:-0}"
        ((10#$left_num > 10#$right_num)) && return 0
        ((10#$left_num < 10#$right_num)) && return 1
    done

    return 0
}

# Install exiftool $EXIFTOOL_VERSION on Linux from the GitHub tarball.
# Requires curl and sudo. Installs to /opt/exiftool-<ver>/ and symlinks
# the script into /usr/local/bin so it takes precedence over any apt package.
install_exiftool_linux() {
    local ver
    ver="$(exiftool_installed_ver)"
    if [[ "$ver" == "$EXIFTOOL_VERSION" ]]; then
        log_skip "exiftool $EXIFTOOL_VERSION already installed"
        return 0
    fi
    log_info "Installing exiftool $EXIFTOOL_VERSION (found: $ver)..."
    curl -fsSL \
        "https://github.com/exiftool/exiftool/archive/refs/tags/${EXIFTOOL_VERSION}.tar.gz" \
        -o /tmp/exiftool.tgz
    sudo tar -xzf /tmp/exiftool.tgz -C /opt
    sudo chmod +x "/opt/exiftool-${EXIFTOOL_VERSION}/exiftool"
    sudo ln -sf "/opt/exiftool-${EXIFTOOL_VERSION}/exiftool" /usr/local/bin/exiftool
    log_ok "exiftool $EXIFTOOL_VERSION installed"
}

# Ensure exiftool >= $EXIFTOOL_VERSION on macOS.
# Checks PATH; installs or upgrades via Homebrew as needed.
ensure_exiftool_macos() {
    local ver
    ver="$(exiftool_installed_ver)"
    if _ver_gte "$ver" "$EXIFTOOL_VERSION"; then
        log_skip "exiftool $ver satisfies >= $EXIFTOOL_VERSION"
        return 0
    fi
    if ! command -v brew >/dev/null 2>&1; then
        die "exiftool $ver < $EXIFTOOL_VERSION and Homebrew is not available." \
            "Install Homebrew first: https://brew.sh"
    fi
    if [[ "$ver" == "0" ]]; then
        log_info "Installing exiftool via Homebrew..."
        brew install exiftool --quiet
    else
        log_info "Upgrading exiftool $ver → latest via Homebrew (need >= $EXIFTOOL_VERSION)..."
        brew upgrade exiftool --quiet
    fi
    ver="$(exiftool_installed_ver)"
    if _ver_gte "$ver" "$EXIFTOOL_VERSION"; then
        log_ok "exiftool $ver installed"
    else
        die "exiftool $ver still < $EXIFTOOL_VERSION after Homebrew install. Run: brew info exiftool"
    fi
}

# Ensure .NET SDK major version $DOTNET_SDK_MAJOR or newer is available on macOS.
ensure_dotnet_macos() {
    if dotnet_sdk_present; then
        log_skip ".NET SDK $DOTNET_SDK_MAJOR+ already installed"
        return 0
    fi

    if ! command -v brew >/dev/null 2>&1; then
        die ".NET SDK ${DOTNET_SDK_MAJOR}+ is required and Homebrew is not available." \
            "Install Homebrew first: https://brew.sh"
    fi

    log_info "Installing .NET SDK ${DOTNET_SDK_MAJOR}+ via Homebrew..."
    brew install --cask dotnet-sdk --quiet
    if dotnet_sdk_present; then
        log_ok ".NET SDK ${DOTNET_SDK_MAJOR}+ installed"
    else
        die ".NET SDK ${DOTNET_SDK_MAJOR}+ is still unavailable after Homebrew install."
    fi
}
