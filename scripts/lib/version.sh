#!/usr/bin/env bash

# Normalize release/artifact labels to valid PEP 440 package versions.
# Artifact names may contain labels like "ci-test"; package metadata may not.
pep440_version() {
  local v="$1"
  if [[ "$v" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9.+-]*)?$ ]]; then
    v="${v#v}"
    local base="${v%%[-+]*}"
    local label="${v#"$base"}"
    label="${label#[-+]}"
    if [[ -n "$label" ]]; then
      label="$(printf '%s' "$label" | tr -cs 'a-zA-Z0-9' '.' | sed -e 's/^\.*//' -e 's/\.*$//')"
      [[ -n "$label" ]] && echo "${base}+${label}" || echo "$base"
    else
      echo "$base"
    fi
  else
    local label
    label="$(printf '%s' "$v" | tr -cs 'a-zA-Z0-9' '.' | sed -e 's/^\.*//' -e 's/\.*$//')"
    [[ -n "$label" ]] && echo "0.0.0+${label}" || echo "0.0.0"
  fi
}

# Normalize a release tag/label to a valid SemVer 2.0.0 version for the Tauri app
# version (tauri.conf.json) and the updater manifest (latest.json). Unlike
# pep440_version — which targets Python package metadata and rewrites a '-rc1'
# pre-release into a PEP 440 '+rc1' LOCAL segment — this PRESERVES the SemVer
# pre-release ('-rc1') and build ('+meta') segments verbatim. The Tauri updater
# orders builds by SemVer (rc1 < rc2 < final), so the suffix MUST survive or
# vX.Y.Z-rc1 and -rc2 stamp to the same app version and self-update can't tell
# them apart. Output equals ${GITHUB_REF_NAME#v} for a v<semver> tag, so the
# stamp, the artifact names, and latest.json all carry one identical string.
# Non-semver input (e.g. a branch name) falls back to 0.0.0.
semver_version() {
  local v="${1#v}"
  if [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    printf '%s' "$v"
  else
    printf '0.0.0'
  fi
}
