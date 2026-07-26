; ============================================================================
; MediaSearchAgent -- project-owned NSIS installer hooks (M-7/S-3).
;
; WHY THIS FILE EXISTS: the vendored template hook
; packaging/windows/installer-hooks.nsh carries the upstream template's identity
; and is treated as read-only (ADR-012 vendored unit). Wiring the Tauri NSIS
; installerHooks at that vendored file would make the POSTUNINSTALL cleanup target
; the upstream template's per-user data dir -- the WRONG dir for MSA. This
; project-owned override carries MSA's identity and is pointed at from the
; project-owned src-tauri/tauri.windows.conf.json, so the wrong-dir uninstall is
; resolved with ZERO edit to the vendored .nsh.
;
; The two identity constants below MUST match app.config.json `identifier` /
; tauri.conf.json `identifier` (the supervisor's app_local_data_dir() resolves to
; %LOCALAPPDATA%\<identifier>) and `productName`.
; ----------------------------------------------------------------------------
; Tier map (ADR-005 -- three tiers + the ownership rule):
;   Tier 1 (always, no prompt) -- the installed app (exe, staged uv/exiftool,
;          Start-menu shortcut, HKCU\Uninstall: all handled by NSIS itself) AND
;          the provisioned app-private runtime (venv, uv-managed CPython, uv cache,
;          WebView2 user-data dir) under %LOCALAPPDATA%\<identifier> (this hook).
;   Tier 2 (prompt, default KEEP) -- the ADR-009 DataDir %USERPROFILE%\MediaSearchAgent
;          (index + config.yaml), the live log dir, and the app-private model cache.
;          Wired (M-7/S-5.1) to the template's "Delete the application data" checkbox,
;          default unchecked; silent uninstalls keep everything (unattended = keep).
;   Tier 3 (never touch) -- shared model caches (~\.cache\huggingface etc.) and a
;          user's SEPARATE shared uv are never enumerated, so they are structurally safe.
; ============================================================================

; %LOCALAPPDATA%\<identifier> -- the app-private runtime dir (venv/python/uv-cache/WebView2).
!define MSA_APPID "ai.openara.mediasearchagent"
; productName -- used for the HKCU Run value + scheduled-task defensive teardown.
!define MSA_NAME  "MediaSearchAgent"

!macro NSIS_HOOK_PREINSTALL
  ; --- Legacy shell-bundle migration (M-7/S-3 item 3; gated M-7/S-5.4, #191) --------------
  ; Runs BEFORE file extraction. Removes the legacy shell-bundle install's runtime layout from
  ; %LOCALAPPDATA%\MediaSearchAgent (the same dir this Tauri app installs into) WITHOUT ever
  ; touching the ADR-009 DataDir (%USERPROFILE%\MediaSearchAgent: config.yaml + index). Kept in
  ; allowlist parity with the Python belt-and-braces sweep (src-tauri/backend/app/migration.py).
  ; NSIS can't read app.config.json, so the legacy AppDir path is hardcoded here.
  ; #191 GATE: one-shot, keyed on POSITIVE legacy markers -- files only the legacy shell-bundle
  ; layout ever wrote. On a desktop->desktop update the markers are gone and Cache\models + logs
  ; under the same name-keyed root are the LIVE model cache + ADR-009 log dir; an ungated run
  ; wiped the live model cache on every update. Do NOT key on the ABSENCE of uninstall.exe
  ; instead: that misfires on reinstall-after-uninstall and deletes a model cache the Tier-2
  ; uninstall deliberately kept (ADR-005).
  ; SAFETY: only NAMED legacy children are removed -- NEVER a wholesale RMDir of the root (this dir
  ; is $INSTDIR; the new app is extracted here immediately after). RMDir/r + Delete on a missing
  ; path are silent no-ops, so this is idempotent.
  ${If} ${FileExists} "$LOCALAPPDATA\MediaSearchAgent\version.txt"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\MediaSearchAgent\start.ps1"
  ${OrIf} ${FileExists} "$LOCALAPPDATA\MediaSearchAgent\repo\*.*"
    DetailPrint "MediaSearchAgent: migrating legacy shell-bundle install"

    ; Stop the legacy tray so its exe unlocks before bin\ is removed (WIN-005).
    nsExec::Exec 'taskkill /IM "MediaSearchAgentTray.exe" /F'

    ; Legacy AppDir subdirs (dev decision (b): the ~1.5 GB Cache\models\ and logs\ ARE deleted).
    ; Cache\models only -- NOT the whole Cache\ tree.
    RMDir /r "$LOCALAPPDATA\MediaSearchAgent\repo"
    RMDir /r "$LOCALAPPDATA\MediaSearchAgent\.venv"
    RMDir /r "$LOCALAPPDATA\MediaSearchAgent\uv"
    RMDir /r "$LOCALAPPDATA\MediaSearchAgent\bin"
    RMDir /r "$LOCALAPPDATA\MediaSearchAgent\Cache\models"
    RMDir /r "$LOCALAPPDATA\MediaSearchAgent\logs"

    ; Legacy AppDir root files.
    Delete "$LOCALAPPDATA\MediaSearchAgent\start.ps1"
    Delete "$LOCALAPPDATA\MediaSearchAgent\stop.ps1"
    Delete "$LOCALAPPDATA\MediaSearchAgent\version.txt"

    ; Legacy Start-Menu shortcut folder ($SMPROGRAMS = %APPDATA%\...\Start Menu\Programs).
    RMDir /r "$SMPROGRAMS\Media Search Agent"

    ; Legacy auto-start: scheduled task + HKCU Run fallback.
    nsExec::Exec 'schtasks /Delete /TN "MediaSearchAgent" /F'
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "MediaSearchAgent"

    ; Legacy user-PATH entry (%LOCALAPPDATA%\MediaSearchAgent\bin). NSIS has no built-in PATH editor,
    ; so strip it via a compact PowerShell one-liner (best-effort; the Python sweep's winreg strip is
    ; the reliable backstop). Wrapped in NSIS backticks so both ' and " nest; PS variables are written
    ; $$x so NSIS emits a literal $ (NSIS's own $VAR substitution would otherwise eat them). All PS
    ; strings are single-quoted, so no inner double-quote escaping is needed.
    nsExec::Exec `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$e = (Join-Path $$env:LOCALAPPDATA 'MediaSearchAgent\bin').TrimEnd('\'); $$p = [Environment]::GetEnvironmentVariable('Path','User'); if ($$p) { $$n = ($$p -split ';' | Where-Object { $$_ -and ($$_.TrimEnd('\') -ne $$e) }) -join ';'; [Environment]::SetEnvironmentVariable('Path',$$n,'User') }"`

    DetailPrint "MediaSearchAgent: legacy migration done (DataDir %USERPROFILE%\MediaSearchAgent untouched)"
  ${Else}
    DetailPrint "MediaSearchAgent: no legacy shell-bundle install detected; migration skipped (#191)"
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTINSTALL
!macroend

!macro NSIS_HOOK_PREUNINSTALL
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; #191 GATE: skip Tier-1 removal when this uninstaller runs as part of an update
  ; ($UpdateMode = 1 -- the in-app updater launches installers with /UPDATE, and the new
  ; installer forwards it to this uninstaller; dormant today per ADR-012 s5). The template
  ; gates its own shortcut/app-data deletion on the same variable. An INTERACTIVE manual
  ; update (reinstall page -> uninstall-first) runs this uninstaller WITHOUT /UPDATE and is
  ; indistinguishable from a real uninstall in here (NSIS uninstallers always see _?= on
  ; $CMDLINE -- the self-delete relaunch uses it too), so that flow keeps upstream's
  ; full-uninstall semantics and the runtime re-provisions on next launch. The documented
  ; silent update path (install.ps1 -> setup.exe /S) never runs the old uninstaller at all.
  ${If} $UpdateMode <> 1
    DetailPrint "MediaSearchAgent: tiered cleanup of provisioned runtime (ADR-005)"

    ; --- Stop any detached indexer BEFORE removing the runtime/data it uses ---------------
    ; Closing the app window during an index deliberately leaves `msa index run` detached
    ; (tracked at %LOCALAPPDATA%\MediaSearchAgent\logs\run\indexer.pid). It runs from the
    ; app-private venv under %LOCALAPPDATA%\<identifier>, so removing that dir (and, with the
    ; checkbox, the index/logs below) while it is live orphans the job and can corrupt the DB.
    ; Cooperative stop via the indexer.stop sentinel (the app's own Stop path), with an
    ; identity check (CommandLine must look like the indexer) so a stale/reused PID is never
    ; force-killed, and a bounded wait + escalation. Best-effort; all failures are swallowed.
    ; PS vars are $$x so NSIS emits a literal $; all PS strings are single-quoted (no inner
    ; double-quote escaping); regex/anchor `$$` -> a literal `$` for PowerShell.
    nsExec::Exec `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$r = Join-Path $$env:LOCALAPPDATA 'MediaSearchAgent\logs\run'; $$pf = Join-Path $$r 'indexer.pid'; if (Test-Path $$pf) { $$ip = (Get-Content $$pf -EA SilentlyContinue | Select-Object -First 1); if ($$ip -match '^\d+$$') { $$cl = (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $$ip) -EA SilentlyContinue).CommandLine; if ($$cl -and $$cl -match 'msa' -and $$cl -match 'index' -and $$cl -match 'run') { New-Item -ItemType File -Path (Join-Path $$r 'indexer.stop') -Force | Out-Null; $$w = 0; while ((Get-Process -Id $$ip -EA SilentlyContinue) -and $$w -lt 30) { Start-Sleep 1; $$w++ }; if (Get-Process -Id $$ip -EA SilentlyContinue) { Stop-Process -Id $$ip -Force -EA SilentlyContinue } } } }"`

    ; --- Tier 1: the app-private runtime dir ---------------------------------------------
    ; Holds EVERYTHING the shell provisioned after install -- extracted bin\uv.exe, .venv,
    ; python\cpython-*, uv-cache -- PLUS the WebView2 user-data dir (EBWebView) Tauri creates
    ; under the same %LOCALAPPDATA%\<identifier> root. One removal covers the runtime AND the
    ; WebView state. Ownership guard: this is a fixed, app-owned path under the per-user data
    ; root -- never a user dir, never the DataDir (%USERPROFILE%\MediaSearchAgent), never a
    ; shared uv install.
    IfFileExists "$LOCALAPPDATA\${MSA_APPID}\*.*" 0 +3
      DetailPrint "  removing app-private runtime + WebView2 dir: $LOCALAPPDATA\${MSA_APPID}"
      RMDir /r "$LOCALAPPDATA\${MSA_APPID}"

    ; Roaming counterpart (defensive -- the supervisor uses LOCALAPPDATA, not roaming APPDATA).
    IfFileExists "$APPDATA\${MSA_APPID}\*.*" 0 +2
      RMDir /r "$APPDATA\${MSA_APPID}"

    ; --- Tier 1: auto-start defensive teardown -------------------------------------------
    ; v1 registers no auto-start, but mirror the migration teardown so a future always-on
    ; variant can't orphan a login item pointing at a deleted app.
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "${MSA_NAME}"
    nsExec::Exec 'schtasks /Delete /TN "${MSA_NAME}" /F'

    ; --- Tier 2: persistent user data (prompt, default KEEP -- ADR-005, M-7/S-5.1) --------
    ; The template's uninstall-confirm page ships a "Delete the application data" checkbox
    ; ($DeleteAppDataCheckboxState, default UNCHECKED); MSA's Tier-2 set rides on it: the
    ; ADR-009 DataDir (%USERPROFILE%\MediaSearchAgent -- index, config.yaml, thumbnails,
    ; qdrant), the live log dir, and the app-private model cache (re-downloadable). Unchecked
    ; (the default) and silent uninstalls keep everything -- unattended = keep. Tier 3 (shared
    ; ~\.cache\* model caches, the user's media, a user-wide uv) is never enumerated here.
    ${If} $DeleteAppDataCheckboxState = 1
      DetailPrint "  removing user data (Tier 2, user opted in): index, config, logs, model cache"
      RMDir /r "$PROFILE\MediaSearchAgent"
      RMDir /r "$LOCALAPPDATA\MediaSearchAgent\logs"
      RMDir /r "$LOCALAPPDATA\MediaSearchAgent\Cache\models"
      ; Clear now-empty name-keyed parents (non-recursive: no-ops if anything else remains).
      RMDir "$LOCALAPPDATA\MediaSearchAgent\Cache"
      RMDir "$LOCALAPPDATA\MediaSearchAgent"
    ${Else}
      DetailPrint "  user data kept (Tier 2 default): index, config, logs, model cache"
    ${EndIf}

    DetailPrint "MediaSearchAgent: cleanup done -- no orphaned uv / venv / CPython / WebView2 dir"
  ${Else}
    DetailPrint "MediaSearchAgent: update in progress -- provisioned runtime kept (#191)"
  ${EndIf}
!macroend
