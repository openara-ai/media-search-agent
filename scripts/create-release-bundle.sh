#!/usr/bin/env bash
set -euo pipefail

# Create a shareable friends-and-family package from prebuilt shell bundles.
#
# Usage:
#   bash scripts/create-release-bundle.sh --version 0.7.2
#   bash scripts/create-release-bundle.sh --version v0.7.2 --include-linux
#
# Output:
#   dist/friends-and-family/MediaSearchAgent-<version>-friends-and-family/
#   dist/friends-and-family/MediaSearchAgent-<version>-friends-and-family.zip

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHELL_DIST_DIR="$REPO_ROOT/dist/shell"
OUTPUT_ROOT="$REPO_ROOT/dist/friends-and-family"

VERSION=""
INCLUDE_LINUX=false
OUTPUT_NAME=""

print_usage() {
  cat <<'EOF'
Create a shareable friends-and-family package from prebuilt shell bundles.

Usage:
  bash scripts/create-release-bundle.sh [OPTIONS]

Options:
  --version <version>     Bundle version to package (required)
                          Accepts 0.7.2 or v0.7.2

  --include-linux         Include the Linux x86_64 shell bundle if present

  --name <name>           Override output folder/archive name
                          Default: MediaSearchAgent-<version>-friends-and-family

  -h, --help              Show this help and exit

Prerequisites:
  Build the shell bundles first:
    bash installer/macos/shell/build-bundle.sh --version <version>
    bash installer/windows-native/shell/build-bundle.sh --version <version>
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:?'--version requires a value'}"
      shift 2
      ;;
    --version=*)
      VERSION="${1#*=}"
      shift
      ;;
    --include-linux)
      INCLUDE_LINUX=true
      shift
      ;;
    --name)
      OUTPUT_NAME="${2:?'--name requires a value'}"
      shift 2
      ;;
    --name=*)
      OUTPUT_NAME="${1#*=}"
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      print_usage >&2
      exit 1
      ;;
  esac
done

[[ -n "$VERSION" ]] || {
  echo "ERROR: --version is required" >&2
  print_usage >&2
  exit 1
}

VERSION_BARE="${VERSION#v}"
PACKAGE_NAME="${OUTPUT_NAME:-MediaSearchAgent-${VERSION_BARE}-friends-and-family}"
PACKAGE_DIR="$OUTPUT_ROOT/$PACKAGE_NAME"
ARCHIVE_PATH="$OUTPUT_ROOT/${PACKAGE_NAME}.zip"

MAC_BUNDLE="$SHELL_DIST_DIR/MediaSearchAgent-${VERSION_BARE}-macos-arm64.tar.gz"
LINUX_BUNDLE="$SHELL_DIST_DIR/MediaSearchAgent-${VERSION_BARE}-linux-x86_64.tar.gz"
WIN_BUNDLE="$SHELL_DIST_DIR/MediaSearchAgent-${VERSION_BARE}-windows-x86_64.zip"

have_bundle=false
[[ -f "$MAC_BUNDLE" ]] && have_bundle=true
[[ -f "$WIN_BUNDLE" ]] && have_bundle=true
[[ "$INCLUDE_LINUX" == true && -f "$LINUX_BUNDLE" ]] && have_bundle=true

if [[ "$have_bundle" != true ]]; then
  echo "ERROR: no matching shell bundles found for version $VERSION_BARE in $SHELL_DIST_DIR" >&2
  exit 1
fi

checksum_cmd() {
  if command -v shasum >/dev/null 2>&1; then
    echo "shasum -a 256"
  elif command -v sha256sum >/dev/null 2>&1; then
    echo "sha256sum"
  else
    echo ""
  fi
}

create_zip_archive() {
  local source_dir="$1"
  local archive_path="$2"

  if command -v ditto >/dev/null 2>&1; then
    rm -f "$archive_path"
    ditto -c -k --sequesterRsrc --keepParent "$source_dir" "$archive_path"
    return
  fi

  if command -v zip >/dev/null 2>&1; then
    rm -f "$archive_path"
    (
      cd "$(dirname "$source_dir")"
      zip -rq "$archive_path" "$(basename "$source_dir")"
    )
    return
  fi

  echo "ERROR: neither ditto nor zip is available to create the archive" >&2
  exit 1
}

mkdir -p "$OUTPUT_ROOT"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

cp "$REPO_ROOT/installer/macos/shell/install.sh" "$PACKAGE_DIR/install.sh"
cp "$REPO_ROOT/installer/windows-native/shell/install.ps1" "$PACKAGE_DIR/install.ps1"

README_PATH="$PACKAGE_DIR/README.txt"
SHA_PATH="$PACKAGE_DIR/SHA256SUMS.txt"

included_files=()

if [[ -f "$MAC_BUNDLE" ]]; then
  cp "$MAC_BUNDLE" "$PACKAGE_DIR/"
  included_files+=("$(basename "$MAC_BUNDLE")")
fi

if [[ "$INCLUDE_LINUX" == true && -f "$LINUX_BUNDLE" ]]; then
  cp "$LINUX_BUNDLE" "$PACKAGE_DIR/"
  included_files+=("$(basename "$LINUX_BUNDLE")")
fi

if [[ -f "$WIN_BUNDLE" ]]; then
  cp "$WIN_BUNDLE" "$PACKAGE_DIR/"
  included_files+=("$(basename "$WIN_BUNDLE")")
fi

{
  printf "Media Search Agent Friends-and-Family Installer Bundle\n"
  printf "Version: %s\n\n" "$VERSION_BARE"
  printf "This package includes local installers and prebuilt bundles so you can install\n"
  printf "Media Search Agent without GitHub access or a public website.\n\n"
  printf "What is included\n"
  printf -- "- install.sh\n"
  printf -- "- install.ps1\n"
  for file in "${included_files[@]}"; do
    printf -- "- %s\n" "$file"
  done
  printf -- "- SHA256SUMS.txt\n\n"
  printf "Before you start\n"
  printf -- "- Extract this package to a normal folder on your computer.\n"
  printf -- "- Do not rename the included bundle files.\n"
  printf -- "- These installers are user-space only. They do not require admin rights.\n\n"

  if [[ -f "$MAC_BUNDLE" ]]; then
    printf "************************************************************************************************\n"
    printf "macOS (Apple Silicon)\n"
    printf "************************************************************************************************\n"
    printf "1. Open Terminal.\n"
    printf "2. Change into the extracted folder.\n"
    printf "3. Run:\n\n"
    printf "   bash install.sh --bundle ./%s\n\n" "$(basename "$MAC_BUNDLE")"
    printf "4. When the installer finishes, it launches the Media Search Agent app in your default browser\n"
    printf "   automatically at http://localhost:8000. This is a local URL and not an internet URL.\n\n"
    printf "   Note: This app is fully local. It connects to internet only at first install to download required\n"
    printf "   AI models (that are large in size). After that it does not require internet connectivity, and\n"
    printf "   you can operate it fully locally and offline.\n\n"
    printf "5. Additionally, the installer creates a Media Search Agent icon (a magnifying glass) in the macOS\n"
    printf "   menu bar. You can use the menu bar to control the service (start, stop, quit etc).\n"
    printf "6. If you quit the app using menu bar, you can launch it again later by double-clicking the following\n"
    printf "   app (use Finder). It will add Media Search Agent icon to menu bar again.\n\n"
    printf "   ~/Applications/MediaSearchAgent.app\n\n"
  fi

  if [[ "$INCLUDE_LINUX" == true && -f "$LINUX_BUNDLE" ]]; then
    printf "************************************************************************************************\n"
    printf "Linux (x86_64)\n"
    printf "************************************************************************************************\n"
    printf "1. Open a terminal.\n"
    printf "2. Change into the extracted folder.\n"
    printf "3. Run:\n\n"
    printf "   bash install.sh --bundle ./%s\n\n" "$(basename "$LINUX_BUNDLE")"
    printf "4. After install, start the app with:\n\n"
    printf "   ~/.local/bin/msa api start\n\n"
    printf "5. Then open:\n\n"
    printf "   http://localhost:8000\n\n"
  fi

  if [[ -f "$WIN_BUNDLE" ]]; then
    printf "************************************************************************************************\n"
    printf "Windows (PowerShell 5.1+)\n"
    printf "************************************************************************************************\n"
    printf "1. Extract the package to a normal folder such as Downloads.\n"
    printf "2. Open Windows PowerShell.\n"
    printf "3. Change into the extracted folder.\n"
    printf "4. Run:\n\n"
    printf "   powershell -ExecutionPolicy Bypass -File .\\install.ps1 -Bundle .\\%s\n\n" "$(basename "$WIN_BUNDLE")"
    printf "5. The installer sets up Media Search Agent for your user account.\n"
    printf "6. Launch it later using its normal app launcher.\n\n"
  fi

  printf "Notes\n"
  if [[ -f "$MAC_BUNDLE" && ! -f "$WIN_BUNDLE" && "$INCLUDE_LINUX" != true ]]; then
    printf -- "- This package contains the macOS installer only.\n"
  elif [[ -f "$MAC_BUNDLE" && -f "$WIN_BUNDLE" && "$INCLUDE_LINUX" != true ]]; then
    printf -- "- This package contains macOS and Windows installers only.\n"
  fi
  printf -- "- The msa command-line launcher is for power users and troubleshooting.\n"
  if [[ -f "$WIN_BUNDLE" ]]; then
    printf -- "- If Windows shows a SmartScreen warning, use \"More info\" and then \"Run anyway\"\n"
    printf "  only if you trust the sender and the checksum matches.\n"
  fi
  printf -- "- If you already have an older install, rerunning the installer should preserve\n"
  printf "  your existing config and data.\n\n"
  printf "************************************************************************************************\n"
  printf "Checksum verification\n"
  printf "************************************************************************************************\n"
  if [[ -f "$MAC_BUNDLE" && "$INCLUDE_LINUX" == true && -f "$LINUX_BUNDLE" ]]; then
    printf -- "- macOS / Linux:\n\n"
  elif [[ -f "$MAC_BUNDLE" ]]; then
    printf -- "- macOS:\n\n"
  elif [[ "$INCLUDE_LINUX" == true && -f "$LINUX_BUNDLE" ]]; then
    printf -- "- Linux:\n\n"
  fi
  printf "   shasum -a 256 -c SHA256SUMS.txt\n\n"
  if [[ -f "$WIN_BUNDLE" ]]; then
    printf -- "- Windows:\n\n"
    printf "   Get-FileHash .\\%s -Algorithm SHA256\n\n" "$(basename "$WIN_BUNDLE")"
  fi
  printf "If you need help, send back the exact command you ran and the full error text.\n"
} > "$README_PATH"

checksum_tool="$(checksum_cmd)"
if [[ -z "$checksum_tool" ]]; then
  echo "ERROR: no SHA-256 tool found (need shasum or sha256sum)" >&2
  exit 1
fi

(
  cd "$PACKAGE_DIR"
  rm -f "$SHA_PATH"
  for file in "${included_files[@]}"; do
    $checksum_tool "$file" >> "$SHA_PATH"
  done
)

create_zip_archive "$PACKAGE_DIR" "$ARCHIVE_PATH"

echo "Created package directory: $PACKAGE_DIR"
echo "Created shareable archive: $ARCHIVE_PATH"
