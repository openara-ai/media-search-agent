"""Guard: Tauri before*Command --prefix paths are repo-root-relative and valid.

Regression test for the M-7/S-1 bug where ``tauri.conf.json`` used
``npm --prefix ../src/msa_apps/ui ...``. Tauri v2 runs ``beforeDevCommand`` /
``beforeBuildCommand`` with CWD = the repo root (where ``tauri`` is invoked), so
the leading ``../`` climbed one level above the repo and ``tauri dev`` / ``build``
failed to find the SPA's ``package.json``. No CI job runs a real ``tauri`` build
(needs the Rust/Tauri toolchain + a real host — a human-only DoD), so only a
config-shape test catches this. ``frontendDist`` legitimately keeps its ``../``
because Tauri resolves *path* fields relative to the config file (src-tauri/),
not the command CWD — so this guard covers only the command prefixes.
"""
from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TAURI_CONF = REPO_ROOT / "src-tauri" / "tauri.conf.json"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _prefix_path(command: str) -> str:
    toks = shlex.split(command)
    assert "--prefix" in toks, f"expected --prefix in: {command!r}"
    return toks[toks.index("--prefix") + 1]


@pytest.mark.skipif(not TAURI_CONF.exists(), reason="tauri.conf.json absent (public mirror)")
@pytest.mark.parametrize("key", ["beforeBuildCommand", "beforeDevCommand"])
def test_before_command_prefix_resolves_from_repo_root(key: str) -> None:
    conf = json.loads(TAURI_CONF.read_text())
    command = conf["build"][key]
    prefix = _prefix_path(command)
    # Tauri v2 runs before*Command from the repo root, so --prefix must be
    # repo-root-relative; a leading '..' (the S-1 regression) climbs above the repo.
    assert not prefix.startswith(".."), (
        f"{key} --prefix {prefix!r} starts with '..'; Tauri runs it from the repo "
        "root, so it climbs above the repo and can't find the SPA package.json"
    )
    pkg = REPO_ROOT / prefix / "package.json"
    assert pkg.is_file(), f"{key} --prefix {prefix!r} -> {pkg} does not exist"


# ── M-7/S-4 F-8: bundle the exiftool support tree (exiftool_files/ on Windows, lib/ on macOS) ──
# The staged bin/ holds exiftool.exe + a sibling exiftool_files/ dir on Windows (and exiftool + a
# sibling lib/ on macOS/Linux); both are REQUIRED — the exiftool wrapper fails without its runtime.
# Tauri v2 (tauri-utils resources.rs) SKIPS directories matched by a glob and only WALKS a bare
# directory path, and it hard-errors on a glob that matches nothing — so a per-platform glob like
# "bin/exiftool_files/*" would both miss the tree and break the OTHER platform's build (the same
# tauri.conf.json drives both the macOS and Windows desktop jobs). Bundling bin/ as a bare
# directory entry (like "backend") walks the whole tree recursively on every platform.

@pytest.mark.skipif(not TAURI_CONF.exists(), reason="tauri.conf.json absent (public mirror)")
def test_bundle_resources_walk_bin_dir_recursively_not_via_glob() -> None:
    conf = json.loads(TAURI_CONF.read_text())
    resources = conf["bundle"]["resources"]
    assert "bin" in resources, (
        "bundle.resources must list the bare 'bin' directory so Tauri walks it recursively and "
        "bundles the exiftool runtime (exiftool_files/ on Windows, lib/ on macOS); a glob like "
        "'bin/exiftool*' skips the matched directory and would ship a non-runnable exiftool (F-8)"
    )
    assert "backend" in resources  # the backend tree must still be bundled
    # No bin/* glob should remain: Tauri skips glob-matched dirs, so a glob can't carry the runtime.
    bin_globs = [r for r in resources if r.startswith("bin/") and "*" in r]
    assert not bin_globs, (
        f"bin/* glob(s) {bin_globs} skip glob-matched directories in Tauri v2 — use the bare 'bin' "
        "walk instead so exiftool_files/ and lib/ are bundled"
    )


# ── M-7/S-4 F-6: reject the placeholder updater pubkey on the publish path ───────────────────
# The release.yml preflight hard-fails a PUBLISHING run (IS_PUBLISH) if plugins.updater.pubkey is
# still the committed placeholder — otherwise artifacts signed with the real fleet key would be
# verified by clients against a placeholder key and auto-update would fail for every release. These
# tests mirror the guard's detection logic and assert the guard itself stays wired into the workflow.

def _pubkey_is_placeholder(pubkey: str) -> bool:
    """Reproduce the release.yml preflight check: decode the base64 minisign pubkey and
    treat it as the fail-closed placeholder if its (untrusted-comment) text says PLACEHOLDER."""
    if not pubkey:
        return True  # empty is not a real key -> guard fails
    try:
        decoded = base64.b64decode(pubkey).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — undecodable is not a real key -> guard fails
        return True
    return "PLACEHOLDER" in decoded


# Exercise the detection logic against FIXTURE keys — NOT the live committed tauri.conf.json value.
# The whole non-slow suite runs in release.yml's private tag gate BEFORE mirror/publish, so pinning
# the live pubkey to the placeholder here (the F-10 bug) would fail that gate the moment the developer
# pastes the real fleet key — the very step release.yml's IS_PUBLISH guard and RELEASING.md require.
# These fixtures reproduce the two ends of the guard's classification so the test is value-independent.

# Synthetic placeholder key: base64 whose decoded minisign untrusted-comment carries the PLACEHOLDER
# marker, exactly like the committed fail-closed default the publish guard rejects.
_PLACEHOLDER_PUBKEY = base64.b64encode(
    b"untrusted comment: MediaSearchAgent updater public key PLACEHOLDER - TODO replace in M-7/S-4\n"
    b"RWQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
).decode()

# Synthetic real-looking key: valid base64, minisign-style comment with NO PLACEHOLDER marker —
# stands in for the real fleet pubkey the developer commits before cutting a signed release.
_REAL_LOOKING_PUBKEY = base64.b64encode(
    b"untrusted comment: minisign public key ABC123\nRWQreal+key+bytes+base64==\n"
).decode()


def test_pubkey_placeholder_detection_flags_placeholder_key() -> None:
    # The placeholder fixture must be flagged — a publish run rejects it (fail-closed).
    assert _pubkey_is_placeholder(_PLACEHOLDER_PUBKEY)


def test_pubkey_placeholder_detection_passes_real_looking_key() -> None:
    # A real-looking key (no PLACEHOLDER in its decoded comment) must pass — this is the case the
    # release tag gate hits AFTER the developer pastes the real fleet pubkey; it must NOT fail.
    assert not _pubkey_is_placeholder(_REAL_LOOKING_PUBKEY)


def test_pubkey_placeholder_detection_rejects_empty_and_undecodable() -> None:
    # Fail-closed on inputs that are not a real key, mirroring the guard's empty / undecodable exits.
    assert _pubkey_is_placeholder("")
    assert _pubkey_is_placeholder("not@@valid==base64!!")


@pytest.mark.skipif(not RELEASE_YML.exists(), reason="release.yml absent (public mirror)")
def test_release_preflight_has_publish_gated_pubkey_guard() -> None:
    # Structural guard: the pubkey placeholder check must stay in the preflight job, gated to the
    # publish path only (key-less validation builds on private/forks must still succeed).
    import yaml

    wf = yaml.safe_load(RELEASE_YML.read_text())
    steps = wf["jobs"]["preflight"]["steps"]
    guard = next(
        (s for s in steps if isinstance(s, dict) and "pubkey" in str(s.get("name", "")).lower()),
        None,
    )
    assert guard is not None, "preflight is missing the updater-pubkey placeholder guard (F-6)"
    assert "IS_PUBLISH" in str(guard.get("if", "")), (
        "the pubkey guard must be IS_PUBLISH-gated so key-less validation builds still pass"
    )
    assert "PLACEHOLDER" in guard.get("run", ""), (
        "the pubkey guard must detect the committed PLACEHOLDER marker"
    )
