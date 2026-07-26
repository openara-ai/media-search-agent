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

    def test_launcher_delete_is_deferred_to_avoid_batch_file_not_found(self):
        """Regression: `msa uninstall` invokes msa.cmd, which runs
        `powershell -File uninstall.ps1`. cmd.exe streams batch files and
        needs to read past the powershell call (exit /b %ERRORLEVEL%, the
        closing `)`, etc.) after PowerShell returns. Deleting msa.cmd
        inline inside uninstall.ps1 left cmd.exe with a deleted file to
        seek into, producing the spurious "The batch file cannot be
        found." output (typically twice) at the very end of an otherwise
        successful uninstall run.

        Fix: defer the launcher delete via a detached cmd with a short
        timeout, so the parent cmd.exe finishes reading the script
        before msa.cmd disappears."""
        text = _read(self.SCRIPT)

        # Must NOT inline-delete the launcher path.
        assert "Remove-Item $Launcher -Force" not in text, (
            "uninstall.ps1 must not inline-delete the launcher; cmd.exe "
            "still has msa.cmd open and prints 'The batch file cannot "
            "be found.' when the file disappears under it."
        )

        # Must schedule a deferred delete via cmd + timeout.
        assert "Start-Process -FilePath \"cmd.exe\"" in text or \
               "Start-Process -FilePath 'cmd.exe'" in text
        assert "timeout /t" in text and "/nobreak" in text, (
            "Deferred delete must use cmd's `timeout /nobreak` (survives "
            "the parent PowerShell exiting; Start-Sleep would not)."
        )
        assert "del /q" in text and "$Launcher" in text, (
            "Deferred command must actually delete the launcher path."
        )

    def test_api_kill_verify_is_wrapped_in_nested_fatal_catch(self):
        """Regression caught in PR #132 review: the outer try/catch on the
        Tier-1 API stop block was broad enough to swallow exceptions from
        `Stop-Process` and `$proc.WaitForExit(5000)` themselves. If
        WaitForExit threw, the outer catch downgraded it to a Write-Warn
        and uninstall continued into Tier-1 venv removal - exactly the
        half-delete-the-venv-around-a-still-running-python state this
        guard exists to prevent.

        The fix: wrap the kill + verify block in its own inner try/catch
        whose catch handler calls Write-Fail (terminator) so it can't be
        downgraded to a warning. The outer catch keeps its role for
        environmental oddities (Get-NetTCPConnection / Get-Process)."""
        text = _read(self.SCRIPT)

        # The dedicated nested-catch failure path must Write-Fail with the
        # specific identifier so a reader can find this guard's purpose.
        assert "Process-stop verification failed" in text, (
            "uninstall.ps1 Tier-1 API stop must wrap the kill + WaitForExit "
            "block in a nested try/catch whose handler Write-Fail's with "
            "'Process-stop verification failed' on unexpected exceptions, "
            "so verification throws can't be downgraded to a Write-Warn by "
            "the outer environmental-error catch."
        )
        # And the verification-failed branch must Write-Fail, not Write-Warn.
        idx = text.index("Process-stop verification failed")
        nearby = text[max(0, idx - 50):idx + 80]
        assert "Write-Fail" in nearby, (
            "The 'Process-stop verification failed' branch must use Write-Fail "
            "so it aborts uninstall before Tier-1 destruction."
        )

    def test_api_kill_does_not_terminate_unrelated_python_on_port(self):
        """Regression: the prior gate ORed `name matches python/uvicorn` with
        `path under AppDir`, which meant any python process listening on the
        configured port - including unrelated Django / FastAPI dev / Jupyter
        sessions - would be killed by uninstall. The name-only fallback must
        only fire when $proc.Path is unresolvable (the orphan-with-half-deleted
        venv recovery case).

        Plus: the path-match must use `$AppDir\\*` (with backslash), not
        `$AppDir*` (without), so sibling directories like `MediaSearchAgentX`
        don't spuriously satisfy the check."""
        text = _read(self.SCRIPT)

        # Strong-signal positive: path under AppDir with the trailing backslash
        # so `<AppDir>Neighbor\foo` does NOT match.
        assert '($procPath -and ($procPath -like "$AppDir\\*"))' in text, (
            "uninstall.ps1 must use `$AppDir\\*` (with backslash) for the "
            "path positive check; the previous `$AppDir*` form (no backslash) "
            "spuriously matched sibling directories."
        )

        # Name fallback must be guarded by (-not $procPath) so resolvable
        # external python processes are NOT terminated.
        assert "(-not $procPath) -and ($proc.ProcessName -match" in text, (
            "uninstall.ps1's kill-by-name fallback must be gated on empty "
            "$procPath so it can't terminate unrelated python processes "
            "(Django / FastAPI dev / Jupyter) that happen to listen on the "
            "configured API port."
        )

    def test_api_kill_waits_for_exit_and_aborts_on_failure(self):
        """Regression: uninstall.ps1 used `Stop-Process -Force -ErrorAction
        SilentlyContinue` and then proceeded to `Remove-Item $VenvDir
        -Recurse -Force`. If Stop-Process silently failed (anti-virus
        blocking, kernel-mode handle, etc.), the venv was half-deleted
        around a still-running python - shipping a zombie that blocked the
        port for the next install run.

        The fix: surface kill failures via -ErrorAction Stop, then verify
        the process actually exited via WaitForExit(5000), and Write-Fail
        + exit BEFORE proceeding to any destructive removal step if it
        didn't. Half-destructive uninstalls leave a broken state that's
        worse than a clean abort with a remediation message."""
        text = _read(self.SCRIPT)

        # Surface Stop-Process failures - the old -ErrorAction SilentlyContinue
        # swallowed Access Denied and similar.
        assert "Stop-Process -Id $proc.Id -Force -ErrorAction Stop" in text, (
            "uninstall.ps1 Tier 1 API stop must use -ErrorAction Stop so "
            "kill failures surface instead of being silently swallowed."
        )

        # Verify the process actually exited before we proceed to delete files.
        assert "$proc.WaitForExit(5000)" in text, (
            "uninstall.ps1 must verify the API process exited via "
            "WaitForExit before continuing to delete the venv around it."
        )

        # Hard-abort path: must exist and surface taskkill remediation so
        # the user can recover without guessing.
        assert "function Write-Fail" in text
        assert "exit 1" in text
        assert "taskkill /F /PID" in text, (
            "uninstall.ps1 must tell the user how to escalate when "
            "Stop-Process can't reap the API process."
        )

        # Ordering: Write-Fail must appear in source BEFORE the venv removal
        # step, since the abort must happen before any destructive action.
        # We assert the Tier 1 stop section appears before the Tier 1 venv
        # removal section - both are anchored on distinctive substrings.
        stop_section_idx = text.index("Tier 1 - Stopping app")
        venv_section_idx = text.index("Tier 1 - Removing Python venv")
        assert stop_section_idx < venv_section_idx, (
            "Tier 1 stop-API section must appear before the Tier 1 "
            "venv-removal section so kill-failure aborts before destruction."
        )
        # The WaitForExit + Write-Fail must live in the stop section, not after.
        wait_idx = text.index("$proc.WaitForExit(5000)")
        assert stop_section_idx < wait_idx < venv_section_idx, (
            "WaitForExit + Write-Fail must guard the API-kill step before "
            "the venv removal runs."
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

    # The Windows shell bundle + its build-bundle.sh were retired in M-7/S-5.5
    # (Windows is desktop-app only); the Tauri NSIS uninstaller replaces uninstall.ps1
    # bundling (covered by tests/test_desktop_uninstaller.py).
