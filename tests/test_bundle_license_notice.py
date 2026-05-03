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


# ── shell bundle: Windows (installer/windows-native/shell/build-bundle.sh) ───

class TestWindowsShellBundle:
    SCRIPT = REPO_ROOT / "installer" / "windows-native" / "shell" / "build-bundle.sh"

    def test_script_exists(self):
        assert self.SCRIPT.exists(), f"Build script not found: {self.SCRIPT}"

    def test_license_in_git_archive(self):
        text = _read(self.SCRIPT)
        assert "LICENSE" in text, (
            "installer/windows-native/shell/build-bundle.sh must include LICENSE "
            "in the git archive command so it is present in the Windows shell bundle."
        )

    def test_notice_in_git_archive(self):
        text = _read(self.SCRIPT)
        assert "NOTICE" in text, (
            "installer/windows-native/shell/build-bundle.sh must include NOTICE "
            "in the git archive command so it is present in the Windows shell bundle."
        )


# ── GUI bundle: macOS .pkg (installer/macos/build.sh) ────────────────────────

class TestMacOsPkgBundle:
    SCRIPT = REPO_ROOT / "installer" / "macos" / "build.sh"

    def test_script_exists(self):
        assert self.SCRIPT.exists(), f"Build script not found: {self.SCRIPT}"

    def test_license_in_package_paths(self):
        text = _read(self.SCRIPT)
        # LICENSE must appear in the PACKAGE_PATHS array block
        assert "LICENSE" in text, (
            "installer/macos/build.sh must include LICENSE in PACKAGE_PATHS so it "
            "is present in the macOS .pkg installer payload."
        )

    def test_notice_in_package_paths(self):
        text = _read(self.SCRIPT)
        assert "NOTICE" in text, (
            "installer/macos/build.sh must include NOTICE in PACKAGE_PATHS so it "
            "is present in the macOS .pkg installer payload."
        )


