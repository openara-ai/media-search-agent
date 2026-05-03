from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_ROOT = REPO_ROOT / "tests" / "infra"

EXPECTED_PS1 = [
    INFRA_ROOT / "run-local.ps1",
    INFRA_ROOT / "hyperv" / "restore-vm.ps1",
    INFRA_ROOT / "hyperv" / "wait-vm.ps1",
    INFRA_ROOT / "hyperv" / "copy-to-vm.ps1",
    INFRA_ROOT / "hyperv" / "run-in-vm.ps1",
    INFRA_ROOT / "hyperv" / "collect-artifacts.ps1",
    INFRA_ROOT / "guest" / "hello.ps1",
    INFRA_ROOT / "guest" / "install-app.ps1",
    INFRA_ROOT / "guest" / "launch-app.ps1",
    INFRA_ROOT / "guest" / "run-playwright.ps1",
    INFRA_ROOT / "guest" / "run-smoke-test.ps1",
    INFRA_ROOT / "guest" / "uninstall-app.ps1",
    INFRA_ROOT / "summary" / "write-summary.ps1",
]

RUN_LOCAL_SH = INFRA_ROOT / "run-local.sh"
E2E_PACKAGE_JSON = REPO_ROOT / "tests" / "e2e" / "package.json"
E2E_PACKAGE_LOCK = REPO_ROOT / "tests" / "e2e" / "package-lock.json"
E2E_PLAYWRIGHT_CONFIG = REPO_ROOT / "tests" / "e2e" / "playwright.config.ts"
E2E_SPEC = REPO_ROOT / "tests" / "e2e" / "specs" / "app-shell.spec.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_e2e_infra_files_exist():
    for path in EXPECTED_PS1:
        assert path.exists(), f"missing expected E2E script: {path}"
    assert RUN_LOCAL_SH.exists(), f"missing WSL wrapper: {RUN_LOCAL_SH}"
    assert E2E_PACKAGE_JSON.exists(), f"missing Playwright package: {E2E_PACKAGE_JSON}"
    assert E2E_PACKAGE_LOCK.exists(), f"missing Playwright lockfile: {E2E_PACKAGE_LOCK}"
    assert E2E_PLAYWRIGHT_CONFIG.exists(), f"missing Playwright config: {E2E_PLAYWRIGHT_CONFIG}"
    assert E2E_SPEC.exists(), f"missing Playwright spec: {E2E_SPEC}"


def test_e2e_infra_scripts_target_powershell_51():
    for path in EXPECTED_PS1:
        text = _read(path)
        assert text.startswith("#Requires -Version 5.1"), f"{path} must target PowerShell 5.1"


def test_e2e_infra_scripts_avoid_bom_prone_write_pattern():
    for path in EXPECTED_PS1:
        text = _read(path)
        assert "Set-Content -Encoding UTF8" not in text, (
            f"{path} must avoid PowerShell 5.1 BOM-producing writes"
        )
        assert "Out-File" not in text, f"{path} should avoid Out-File in PS5.1-sensitive scripts"


def test_run_local_supports_scaffold_and_installer_scenarios():
    text = _read(INFRA_ROOT / "run-local.ps1")
    assert "ValidateSet('scaffold', 'installer')" in text
    # Shell bundle replaced the Inno .exe — installer is the bundle .zip plus
    # the shell/install.ps1 bootstrap copied alongside it on the guest.
    assert "MediaSearchAgent-*-windows-x86_64.zip" in text
    assert "shell\\install.ps1" in text
    assert "install-app.ps1" in text
    assert "launch-app.ps1" in text
    assert "run-playwright.ps1" in text
    assert "run-smoke-test.ps1" in text
    assert "hello.ps1" in text
    assert "[string] $GuestPassword = ''" in text
    assert "[switch] $RunPlaywright" in text
    assert "ConvertTo-SecureString $GuestPassword -AsPlainText -Force" in text
    assert "$env:MSA_E2E_GUEST_PASSWORD" in text
    assert "playwright.json" in text


def test_wsl_wrapper_delegates_to_windows_powershell():
    text = _read(RUN_LOCAL_SH)
    assert text.startswith("#!/usr/bin/env bash")
    assert "powershell.exe" in text
    assert "run-local.ps1" in text
    assert "--scenario scaffold|installer" in text
    assert "--run-playwright" in text
    assert "--guest-password VALUE" in text
    assert 'export MSA_E2E_GUEST_PASSWORD="$GUEST_PASSWORD"' in text
    assert 'export WSLENV="MSA_E2E_GUEST_PASSWORD"' in text


def test_guest_scripts_emit_machine_readable_status_files():
    install_text = _read(INFRA_ROOT / "guest" / "install-app.ps1")
    launch_text = _read(INFRA_ROOT / "guest" / "launch-app.ps1")
    playwright_text = _read(INFRA_ROOT / "guest" / "run-playwright.ps1")
    smoke_text = _read(INFRA_ROOT / "guest" / "run-smoke-test.ps1")
    uninstall_text = _read(INFRA_ROOT / "guest" / "uninstall-app.ps1")
    hello_text = _read(INFRA_ROOT / "guest" / "hello.ps1")

    assert "install.json" in install_text
    assert "launch.json" in launch_text
    assert "playwright.json" in playwright_text
    assert "Resolve-CommandPath -Name 'npm.cmd'" in playwright_text
    assert "Resolve-CommandPath -Name 'npx.cmd'" in playwright_text
    assert "smoke.json" in smoke_text
    assert "uninstall.json" in uninstall_text
    assert "hello.json" in hello_text


def test_guest_scripts_enforce_key_postconditions():
    uninstall_text = _read(INFRA_ROOT / "guest" / "uninstall-app.ps1")
    launch_text = _read(INFRA_ROOT / "guest" / "launch-app.ps1")
    smoke_text = _read(INFRA_ROOT / "guest" / "run-smoke-test.ps1")
    install_text = _read(INFRA_ROOT / "guest" / "install-app.ps1")

    assert "Test-Path -LiteralPath $AppBinDir" in uninstall_text
    assert "LocalPort 8000" in uninstall_text
    assert "Start-Sleep -Seconds 3" in uninstall_text
    assert "Start-Sleep -Seconds 8" in launch_text
    assert 'id="root"' in smoke_text
    # Shell-bundle install does more work than the old Inno installer
    # (downloads pytorch+cuDNN via uv, etc.), so the timeout was lifted to 10 min.
    assert "WaitForExit(600000)" in install_text
    # Shell installer is invoked with -Bundle <zip>, not as an .exe.
    assert "-Bundle" in install_text
    assert "shell/install.ps1" in install_text or "BootstrapPath" in install_text


def test_copy_to_vm_cleans_up_payload_from_finally():
    text = _read(INFRA_ROOT / "hyperv" / "copy-to-vm.ps1")
    assert "$payload = $null" in text
    assert "$payload = New-HostZipPayload -InputPath $SourcePath" in text
    assert "if ($payload) {" in text


def test_playwright_package_uses_env_driven_artifact_paths():
    package_text = _read(E2E_PACKAGE_JSON)
    config_text = _read(E2E_PLAYWRIGHT_CONFIG)
    spec_text = _read(E2E_SPEC)
    playwright_guest_text = _read(INFRA_ROOT / "guest" / "run-playwright.ps1")

    assert '"@playwright/test"' in package_text
    assert 'process.env.E2E_BASE_URL' in config_text
    assert 'PLAYWRIGHT_HTML_REPORT' in config_text
    assert 'PLAYWRIGHT_JSON_REPORT' in config_text
    assert 'PLAYWRIGHT_JUNIT_REPORT' in config_text
    assert 'PLAYWRIGHT_TEST_RESULTS_DIR' in config_text
    assert "redirects root to search" in spec_text
    assert "navigates to the stable top-level pages" in spec_text
    assert "getByText('Media Sources', { exact: true })" in spec_text
    assert "Start-Process" in playwright_guest_text
    assert "-FilePath 'cmd.exe'" in playwright_guest_text
    assert "'/d /c ' + $commandLine" in playwright_guest_text
    assert "-RedirectStandardOutput $StdoutPath" in playwright_guest_text
    assert "-RedirectStandardError $StderrPath" in playwright_guest_text
    assert "[AllowEmptyString()][string] $Content = ''" in playwright_guest_text
