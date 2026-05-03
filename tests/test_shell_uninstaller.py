"""
Verify that shell installer uninstall scripts:
  - exist for both platforms
  - are included in each shell bundle's build script
  - implement ADR-005 removal tiers (data never deleted without explicit consent)
  - are wired into the launcher (msa uninstall)

Tests are static-analysis only — no bundles need to be built.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── Uninstaller scripts exist ─────────────────────────────────────────────────

class TestUninstallScriptsExist:
    def test_macos_linux_uninstall_sh_exists(self):
        p = REPO_ROOT / "installer" / "macos" / "shell" / "uninstall.sh"
        assert p.exists(), f"macOS/Linux shell uninstaller not found: {p}"

    def test_windows_shell_uninstall_ps1_exists(self):
        p = REPO_ROOT / "installer" / "windows-native" / "shell" / "uninstall.ps1"
        assert p.exists(), f"Windows shell uninstaller not found: {p}"


# ── ADR-005 tier invariants ───────────────────────────────────────────────────

class TestUninstallTiersMacOsLinux:
    SCRIPT = REPO_ROOT / "installer" / "macos" / "shell" / "uninstall.sh"

    def test_venv_always_removed(self):
        text = _read(self.SCRIPT)
        assert "rm -rf" in text and "VENV_DIR" in text, (
            "uninstall.sh must always remove the Python venv (Tier 1)."
        )

    def test_index_requires_prompt(self):
        text = _read(self.SCRIPT)
        assert "ask" in text and "INDEX_DIR" in text, (
            "uninstall.sh must prompt before removing the media index (Tier 2)."
        )

    def test_config_requires_prompt(self):
        text = _read(self.SCRIPT)
        assert "ask" in text and "CONFIG_PATH" in text, (
            "uninstall.sh must prompt before removing config.yaml (Tier 2)."
        )

    def test_cache_removal_requires_prompt(self):
        text = _read(self.SCRIPT)
        # Model cache is Tier 2: removable, but only after explicit user confirmation.
        # Verify the ask() guard appears near the CACHE_DIR rm command.
        assert "CACHE_DIR" in text, "uninstall.sh must reference CACHE_DIR"
        assert "ask" in text, "uninstall.sh must use ask() prompt for Tier 2 items"
        # Confirm the rm is inside a conditional block (i.e., not unconditional).
        lines = text.splitlines()
        cache_rm_indices = [
            i for i, line in enumerate(lines)
            if "CACHE_DIR" in line and "rm " in line
            and not line.lstrip().startswith(("printf", "echo", "#"))
        ]
        assert cache_rm_indices, "uninstall.sh must have a cache removal path"
        for idx in cache_rm_indices:
            context = "\n".join(lines[max(0, idx - 5):idx])
            assert "ask" in context, (
                f"uninstall.sh cache rm at line {idx + 1} must be gated by ask() prompt (Tier 2). "
                f"Context:\n{context}"
            )

    def test_launcher_delegate_uninstall(self):
        install_text = _read(REPO_ROOT / "installer" / "macos" / "shell" / "install.sh")
        assert "uninstall" in install_text, (
            "install.sh launcher must delegate 'msa uninstall' to uninstall.sh."
        )

    def test_msr_root_env_var_honoured(self):
        text = _read(self.SCRIPT)
        assert "MSA_ROOT" in text, (
            "uninstall.sh must honour MSA_ROOT so it uninstalls the correct path "
            "when invoked via 'msa uninstall' with a non-default install dir."
        )

    def test_surgical_code_removal_not_whole_dir(self):
        text = _read(self.SCRIPT)
        # The script should remove individual items, not the entire APP_CODE_DIR.
        # This is critical on Linux where APP_CODE_DIR == APP_SUPPORT_DIR.
        assert "src" in text and "scripts" in text, (
            "uninstall.sh must remove code items individually (src, scripts, bin …) "
            "rather than rm -rf APP_CODE_DIR, which would wipe user data on Linux."
        )


class TestUninstallTiersWindows:
    SCRIPT = REPO_ROOT / "installer" / "windows-native" / "shell" / "uninstall.ps1"

    def test_venv_always_removed(self):
        text = _read(self.SCRIPT)
        assert "VenvDir" in text and "Remove-Item" in text, (
            "uninstall.ps1 must always remove the Python venv (Tier 1)."
        )

    def test_index_requires_prompt(self):
        text = _read(self.SCRIPT)
        assert "Read-KeepChoice" in text and "IndexDir" in text, (
            "uninstall.ps1 must prompt before removing the media index (Tier 2)."
        )

    def test_config_requires_prompt(self):
        text = _read(self.SCRIPT)
        assert "Read-KeepChoice" in text and "ConfigPath" in text, (
            "uninstall.ps1 must prompt before removing config.yaml (Tier 2)."
        )

    def test_unattended_keeps_data(self):
        text = _read(self.SCRIPT)
        assert "Unattended" in text, (
            "uninstall.ps1 must support -Unattended flag that skips Tier 2 prompts "
            "and keeps all user data (safe default for scripted uninstall)."
        )

    def test_cache_removal_requires_prompt(self):
        text = _read(self.SCRIPT)
        # Model cache is Tier 2: removable, but only after explicit user confirmation.
        # Verify the Read-KeepChoice guard appears near the Remove-Item command.
        assert "ModelCacheDir" in text, "uninstall.ps1 must reference ModelCacheDir"
        lines = text.splitlines()
        cache_rm_indices = [
            i for i, line in enumerate(lines)
            if "ModelCacheDir" in line and "Remove-Item" in line
            and not line.lstrip().startswith(("Write-Host", "#"))
        ]
        assert cache_rm_indices, "uninstall.ps1 must have a model cache removal path"
        for idx in cache_rm_indices:
            context = "\n".join(lines[max(0, idx - 5):idx])
            assert "Read-KeepChoice" in context, (
                f"uninstall.ps1 model cache rm at line {idx + 1} must be gated by "
                f"Read-KeepChoice prompt (Tier 2). Context:\n{context}"
            )

    def test_task_scheduler_removed(self):
        text = _read(self.SCRIPT)
        assert "Unregister-ScheduledTask" in text, (
            "uninstall.ps1 must remove the Task Scheduler auto-start task (Tier 1)."
        )

    def test_run_key_autostart_removed(self):
        text = _read(self.SCRIPT)
        assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in text
        assert "Remove-ItemProperty" in text, (
            "uninstall.ps1 must remove the Run registry auto-start fallback (Tier 1)."
        )

    def test_launcher_delegate_uninstall(self):
        install_text = _read(REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1")
        assert "uninstall" in install_text.lower(), (
            "install.ps1 launcher must delegate 'msa uninstall' to uninstall.ps1."
        )

    def test_start_menu_shortcuts_removed(self):
        text = _read(self.SCRIPT)
        assert "Start Menu\\Programs\\Media Search Agent" in text
        assert "start.ps1" in text
        assert "stop.ps1" in text
        assert "Removed Start Menu shortcuts" in text, (
            "uninstall.ps1 must remove shell-installer Start Menu shortcuts and launcher scripts."
        )

    def test_bundled_tools_removed_in_tier1(self):
        text = _read(self.SCRIPT)
        for tool in ("exiftool.exe",):
            assert tool in text, (
                f"uninstall.ps1 must remove bundled {tool} from bin\\ in Tier 1. "
                "Leaving it behind prevents bin\\ (and AppDir\\) from being cleaned up."
            )

    def test_legacy_ffmpeg_removed_in_tier1(self):
        """Older installs (pre-bdcb543) bundled ffmpeg.exe/ffprobe.exe.

        Upgrading users still have those files in bin\\, so the uninstaller
        must clean them up — otherwise the launcher dir is reported as kept.
        """
        text = _read(self.SCRIPT)
        for tool in ("ffmpeg.exe", "ffprobe.exe"):
            assert tool in text, (
                f"uninstall.ps1 must remove legacy {tool} from bin\\ in Tier 1 "
                "to avoid leaving the launcher directory behind on upgrade."
            )

    def test_pip_build_artifacts_removed_in_tier1(self):
        """`uv pip install $RepoDir` creates build/ and *.egg-info/ in repo\\.

        These are installer byproducts, not user files; the uninstaller must
        clean them up so RepoDir gets removed cleanly.
        """
        text = _read(self.SCRIPT)
        assert "'build'" in text, (
            "uninstall.ps1 must include 'build' in $codeItems — pip's "
            "wheel-build leaves a build/ folder under repo\\."
        )
        assert "*.egg-info" in text, (
            "uninstall.ps1 must clean up *.egg-info directories — pip "
            "creates one under repo\\ during install."
        )


# ── Uninstaller bundled in build scripts ─────────────────────────────────────

class TestUninstallBundled:
    def test_uninstall_sh_in_macos_linux_bundle(self):
        text = _read(REPO_ROOT / "installer" / "macos" / "shell" / "build-bundle.sh")
        assert "uninstall.sh" in text, (
            "build-bundle.sh must copy uninstall.sh into the macOS/Linux bundle "
            "so users can uninstall without the original install script."
        )

    def test_uninstall_ps1_in_windows_bundle(self):
        text = _read(REPO_ROOT / "installer" / "windows-native" / "shell" / "build-bundle.sh")
        assert "uninstall.ps1" in text, (
            "build-bundle.sh must copy uninstall.ps1 into the Windows bundle "
            "so users can uninstall without the original install script."
        )
