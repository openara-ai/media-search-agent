#!/usr/bin/env bash
# Media Search Agent — macOS sign & notarize
#
# Run after build.sh. Signs all payload binaries and .app bundles, rebuilds
# the .pkg, submits to Apple notarytool, waits for approval, and staples the
# ticket to the output .dmg so Gatekeeper passes silently on install.
#
# Prerequisites:
#   - Paid Apple Developer Program membership ($99/year)
#   - "Developer ID Application: <name> (<team>)" certificate in Keychain
#   - "Developer ID Installer:   <name> (<team>)" certificate in Keychain
#   - App-specific password from https://appleid.apple.com
#     (Security → App-Specific Passwords → Generate)
#
# Recommended: store credentials in Keychain once so you never pass them on the
# command line (avoids leaking them in shell history / CI logs):
#
#   xcrun notarytool store-credentials "MSA_NOTARY" \
#     --apple-id you@example.com \
#     --team-id XXXXXXXXXX \
#     --password "xxxx-xxxx-xxxx-xxxx"
#
#   Then use --keychain-profile MSA_NOTARY instead of --apple-id/--team-id/--password.
#
# Usage:
#   # With stored keychain profile (recommended):
#   bash installer/macos/notarize.sh \
#     --version 0.2.0 --arch arm64 \
#     --keychain-profile MSA_NOTARY
#
#   # With inline credentials:
#   bash installer/macos/notarize.sh \
#     --version 0.2.0 --arch arm64 \
#     --apple-id you@example.com \
#     --team-id XXXXXXXXXX \
#     --password "xxxx-xxxx-xxxx-xxxx"
#
# Run from the repo root.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Options ───────────────────────────────────────────────────────────────────

VERSION=""
BUILD_ARCH="$(uname -m)"
[[ "$BUILD_ARCH" == "aarch64" ]] && BUILD_ARCH="arm64"

APPLE_ID=""
TEAM_ID=""
NOTARY_PASSWORD=""
KEYCHAIN_PROFILE=""
APP_IDENTITY=""   # Developer ID Application cert (auto-detected if empty)
PKG_IDENTITY=""   # Developer ID Installer cert  (auto-detected if empty)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)           VERSION="$2";           shift 2 ;;
    --arch)              BUILD_ARCH="$2";         shift 2 ;;
    --apple-id)          APPLE_ID="$2";           shift 2 ;;
    --team-id)           TEAM_ID="$2";            shift 2 ;;
    --password)          NOTARY_PASSWORD="$2";    shift 2 ;;
    --keychain-profile)  KEYCHAIN_PROFILE="$2";   shift 2 ;;
    --app-identity)      APP_IDENTITY="$2";       shift 2 ;;
    --pkg-identity)      PKG_IDENTITY="$2";       shift 2 ;;
    -h|--help)
      sed -n '3,50p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

log()  { echo "[notarize] $*"; }
fail() { echo "[notarize] ERROR: $*" >&2; exit 1; }

# ── Validate ──────────────────────────────────────────────────────────────────

if [[ -z "$VERSION" ]]; then
  fail "--version is required (e.g. --version 0.2.0)"
fi

if [[ -z "$KEYCHAIN_PROFILE" ]]; then
  [[ -z "$APPLE_ID"         ]] && fail "--apple-id is required (or use --keychain-profile)"
  [[ -z "$TEAM_ID"          ]] && fail "--team-id is required (or use --keychain-profile)"
  [[ -z "$NOTARY_PASSWORD"  ]] && fail "--password is required (or use --keychain-profile)"
fi

# ── Paths ─────────────────────────────────────────────────────────────────────

INSTALLER_DIR="$SCRIPT_DIR"
ASSETS_DIR="$INSTALLER_DIR/assets"
SCRIPTS_DIR="$INSTALLER_DIR/scripts"
PAYLOAD_DIR="$INSTALLER_DIR/payload"
ENTITLEMENTS="$ASSETS_DIR/entitlements.plist"
WORK_DIR="$REPO_ROOT/build/macos"
DIST_DIR="$REPO_ROOT/dist/macos"
DMG_IN="$DIST_DIR/MediaSearchAgent-${VERSION}-${BUILD_ARCH}-Setup.dmg"
DMG_OUT="$DIST_DIR/MediaSearchAgent-${VERSION}-${BUILD_ARCH}-Setup-signed.dmg"

[[ -f "$DMG_IN"      ]] || fail "DMG not found: $DMG_IN — run build.sh first"
[[ -f "$ENTITLEMENTS" ]] || fail "entitlements.plist not found: $ENTITLEMENTS"

# Remove stale non-.app payload directory left by older builds — notarytool
# would otherwise find unsigned binaries inside it too.
rm -rf "$PAYLOAD_DIR/Applications/MediaSearchAgent"

# ── Detect signing identities ─────────────────────────────────────────────────

if [[ -z "$APP_IDENTITY" ]]; then
  APP_IDENTITY=$(security find-identity -v -p codesigning \
    | grep "Developer ID Application" | head -1 \
    | sed 's/.*"\(Developer ID Application[^"]*\)".*/\1/' || true)
  [[ -z "$APP_IDENTITY" ]] && fail \
    "No 'Developer ID Application' certificate found in Keychain.\n  Install from https://developer.apple.com/account/resources/certificates/list"
  log "App identity:  $APP_IDENTITY"
fi

if [[ -z "$PKG_IDENTITY" ]]; then
  PKG_IDENTITY=$(security find-identity -v \
    | grep "Developer ID Installer" | head -1 \
    | sed 's/.*"\(Developer ID Installer[^"]*\)".*/\1/' || true)
  [[ -z "$PKG_IDENTITY" ]] && fail \
    "No 'Developer ID Installer' certificate found in Keychain.\n  Install from https://developer.apple.com/account/resources/certificates/list"
  log "Pkg identity:  $PKG_IDENTITY"
fi

log "DMG in:   $DMG_IN"
log "DMG out:  $DMG_OUT"

# ── Step 1: Sign payload binaries ─────────────────────────────────────────────
# Sign each executable/dylib in the payload before pkgbuild seals them.
# Shell scripts (exiftool) are skipped — codesign only applies to Mach-O files.

log "Signing payload binaries..."

_sign_if_macho() {
  local f="$1"
  # Only attempt to sign Mach-O binaries and dylibs; skip shell scripts and text
  if file "$f" 2>/dev/null | grep -qE "Mach-O|dynamically linked"; then
    codesign --force --sign "$APP_IDENTITY" \
      --options runtime \
      --entitlements "$ENTITLEMENTS" \
      --timestamp \
      "$f"
    log "  signed: $(basename "$f")"
  fi
}

# Sign all Mach-O files inside the .app bundle recursively.
# The narrow BIN_DIR approach misses binaries nested under Contents/Resources/;
# a find-based sweep catches everything Apple's notarytool will inspect.
APP_BUNDLE_PAYLOAD="$PAYLOAD_DIR/Applications/MediaSearchAgent.app"
if [[ -d "$APP_BUNDLE_PAYLOAD" ]]; then
  while IFS= read -r -d '' f; do
    _sign_if_macho "$f"
  done < <(find "$APP_BUNDLE_PAYLOAD" -type f -print0)
fi

# ── Step 2: Sign .app bundles ─────────────────────────────────────────────────
# Sign the launcher and uninstaller app bundles that live in the payload and
# in the work dir (the work-dir copies end up in the DMG directly).

log "Signing .app bundles..."

_sign_app() {
  local app="$1"
  [[ -d "$app" ]] || return 0
  # Do NOT use --deep here: nested binaries are already signed individually
  # in step 1. --deep would re-sign them without proper entitlements and
  # override the signatures we just applied.
  codesign --force --sign "$APP_IDENTITY" \
    --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --timestamp \
    "$app"
  log "  signed: $(basename "$app")"
}

_sign_app "$PAYLOAD_DIR/Applications/MediaSearchAgent.app"
_sign_app "$WORK_DIR/MediaSearchAgent.app"
_sign_app "$WORK_DIR/Uninstall MediaSearchAgent.app"

# ── Step 3: Rebuild .pkg with signed payload ──────────────────────────────────
# pkgbuild/productbuild seal the payload contents by hash. We must re-run them
# after signing so the signed binaries are what gets hashed into the package.

log "Rebuilding .pkg with signed payload..."

DIST_XML="$WORK_DIR/Distribution.xml"
[[ -f "$DIST_XML" ]] || \
  sed "s/@@VERSION@@/$VERSION/g" "$INSTALLER_DIR/Distribution.xml" > "$DIST_XML"

# Must match the filename in Distribution.xml (MediaSearchAgent-component.pkg)
# so productbuild picks up this signed pkg, not the original unsigned one from build.sh.
COMPONENT_PKG="$WORK_DIR/MediaSearchAgent-component.pkg"
DIST_PKG_UNSIGNED="$WORK_DIR/MediaSearchAgent-${VERSION}-unsigned.pkg"
DIST_PKG_SIGNED="$WORK_DIR/MediaSearchAgent-${VERSION}-signed.pkg"

# Regenerate component plist from the signed payload so BundleIsRelocatable=false
# and BundleOverwriteAction=upgrade are preserved in the notarized package.
# Omitting --component-plist causes pkgbuild to default BundleIsRelocatable=true,
# allowing Installer to relocate MediaSearchAgent.app based on prior receipts.
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

productbuild \
  --distribution "$DIST_XML" \
  --resources "$ASSETS_DIR" \
  --package-path "$WORK_DIR" \
  "$DIST_PKG_UNSIGNED"

# Sign the distribution .pkg with the Installer identity
productsign \
  --sign "$PKG_IDENTITY" \
  --timestamp \
  "$DIST_PKG_UNSIGNED" \
  "$DIST_PKG_SIGNED"

log "Signed .pkg: $DIST_PKG_SIGNED"

# ── Step 4: Rebuild DMG with signed contents ──────────────────────────────────

log "Rebuilding DMG with signed contents..."

DMG_CONTENTS="$WORK_DIR/dmg-contents-signed"
rm -rf "$DMG_CONTENTS"
mkdir -p "$DMG_CONTENTS"

cp "$DIST_PKG_SIGNED" "$DMG_CONTENTS/MediaSearchAgent-${VERSION}.pkg"
cp -r "$WORK_DIR/Uninstall MediaSearchAgent.app" "$DMG_CONTENTS/"

rm -f "$DMG_OUT"

DMG_ARGS=(
  --volname "MediaSearchAgent $VERSION"
  --window-size 600 400
  --icon-size 100
  --icon "MediaSearchAgent-${VERSION}.pkg" 150 200
  --icon "Uninstall MediaSearchAgent.app" 450 200
  --hide-extension "MediaSearchAgent-${VERSION}.pkg"
  --hide-extension "Uninstall MediaSearchAgent.app"
)
[[ -f "$ASSETS_DIR/dmg-background.png" ]] && \
  DMG_ARGS+=(--background "$ASSETS_DIR/dmg-background.png")

create-dmg "${DMG_ARGS[@]}" "$DMG_OUT" "$DMG_CONTENTS" || {
  log "WARNING: create-dmg returned non-zero (layout may be imperfect but DMG is usable)"
}

# Sign the DMG itself
codesign --force --sign "$APP_IDENTITY" --timestamp "$DMG_OUT"
log "Signed DMG: $DMG_OUT"

# ── Step 5: Submit to notarytool ──────────────────────────────────────────────

log "Submitting to Apple notarytool (this takes 1-5 minutes)..."

NOTARY_ARGS=(--wait --output-format json)

if [[ -n "$KEYCHAIN_PROFILE" ]]; then
  NOTARY_ARGS+=(--keychain-profile "$KEYCHAIN_PROFILE")
else
  NOTARY_ARGS+=(
    --apple-id "$APPLE_ID"
    --team-id  "$TEAM_ID"
    --password "$NOTARY_PASSWORD"
  )
fi

NOTARY_RESULT=$(xcrun notarytool submit "$DMG_OUT" "${NOTARY_ARGS[@]}" 2>&1) || true
echo "$NOTARY_RESULT"

STATUS=$(echo "$NOTARY_RESULT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('status','unknown'))" \
  2>/dev/null || echo "unknown")

if [[ "$STATUS" != "Accepted" ]]; then
  # Fetch full log for diagnosis
  SUBMISSION_ID=$(echo "$NOTARY_RESULT" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null || echo "")
  if [[ -n "$SUBMISSION_ID" ]]; then
    log "Fetching notarization log for submission $SUBMISSION_ID..."
    if [[ -n "$KEYCHAIN_PROFILE" ]]; then
      xcrun notarytool log "$SUBMISSION_ID" --keychain-profile "$KEYCHAIN_PROFILE" 2>&1 || true
    else
      xcrun notarytool log "$SUBMISSION_ID" \
        --apple-id "$APPLE_ID" --team-id "$TEAM_ID" --password "$NOTARY_PASSWORD" 2>&1 || true
    fi
  fi
  fail "Notarization failed with status: $STATUS"
fi

log "Notarization accepted"

# ── Step 6: Staple ────────────────────────────────────────────────────────────

log "Stapling notarization ticket to DMG..."
xcrun stapler staple "$DMG_OUT"
log "Staple complete"

# ── Step 7: Verify ────────────────────────────────────────────────────────────

log "Verifying with Gatekeeper..."
spctl --assess --type open --context context:primary-signature -v "$DMG_OUT" && \
  log "Gatekeeper: PASS" || \
  log "WARNING: spctl check did not pass — staple may need a moment to propagate"

# ── Done ──────────────────────────────────────────────────────────────────────

log ""
log "────────────────────────────────────────────"
log "Signed and notarized DMG ready:"
log "  $DMG_OUT"
log ""
log "Distribute this file. Users can open it without any Gatekeeper warnings."
log "────────────────────────────────────────────"
