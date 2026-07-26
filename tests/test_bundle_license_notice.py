"""
Verify that LICENSE and NOTICE are present in every distributable bundle.

MIT requires the copyright notice to be included with all copies of the
software. NOTICE carries the AGPL-3.0 / non-commercial obligations for
third-party components.

Tests run against the build scripts themselves (static analysis) so they
pass in CI without actually building any bundles. Each test checks the
correct path list / archive manifest in the build script.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── helpers ───────────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── shell bundle: macOS + Linux (installer/macos/shell/build-bundle.sh) ───────

class TestMacOsLinuxShellBundle:
    SCRIPT = REPO_ROOT / "installer" / "macos" / "shell" / "build-bundle.sh"

    def test_script_exists(self):
        assert self.SCRIPT.exists(), f"Build script not found: {self.SCRIPT}"

    def test_license_in_git_archive(self):
        text = _read(self.SCRIPT)
        assert "LICENSE" in text, (
            "installer/macos/shell/build-bundle.sh must include LICENSE in the "
            "git archive command so it is present in the macOS and Linux shell bundles."
        )

    def test_notice_in_git_archive(self):
        text = _read(self.SCRIPT)
        assert "NOTICE" in text, (
            "installer/macos/shell/build-bundle.sh must include NOTICE in the "
            "git archive command so it is present in the macOS and Linux shell bundles."
        )


# Windows is desktop-app only (M-7): the legacy Windows shell bundle + its
# build-bundle.sh were retired in M-7/S-5.5, so there is no Windows shell-bundle
# LICENSE/NOTICE check anymore. The Tauri installer's license bundling is covered
# by the desktop build path.


# The legacy macOS .pkg/.dmg (Platypus) installer + installer/macos/build.sh were
# retired in M-7/S-5.5 (macOS ships as the Tauri desktop app), so there is no
# .pkg PACKAGE_PATHS LICENSE/NOTICE check anymore. The Tauri bundle's license
# inclusion is handled by the desktop build path.


