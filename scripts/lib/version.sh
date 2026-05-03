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
