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
;   Tier 2 (prompt, default KEEP) -- the index + config.yaml + logs live in the
;          ADR-009 DataDir %USERPROFILE%\MediaSearchAgent, which this hook NEVER
;          touches. The full ADR-005 Tier-2 prompt UX lands in S-5; until then the
;          desktop uninstall keeps user data by construction (it is never enumerated).
;   Tier 3 (never touch) -- shared model caches (~\.cache\huggingface etc.) and a
;          user's SEPARATE shared uv are never enumerated, so they are structurally safe.
; ============================================================================

; %LOCALAPPDATA%\<identifier> -- the app-private runtime dir (venv/python/uv-cache/WebView2).
!define MSA_APPID "ai.openara.mediasearchagent"
; productName -- used for the HKCU Run value + scheduled-task defensive teardown.
!define MSA_NAME  "MediaSearchAgent"

!macro NSIS_HOOK_PREINSTALL
  ; --- Legacy shell-bundle migration (M-7/S-3 item 3) -------------------------------------
  ; Runs BEFORE file extraction. Removes the legacy shell-bundle install's runtime layout from
  ; %LOCALAPPDATA%\MediaSearchAgent (the same dir this Tauri app installs into) WITHOUT ever
  ; touching the ADR-009 DataDir (%USERPROFILE%\MediaSearchAgent: config.yaml + index). Kept in
  ; allowlist parity with the Python belt-and-braces sweep (src-tauri/backend/app/migration.py).
  ; NSIS can't read app.config.json, so the legacy AppDir path is hardcoded here.
  ; SAFETY: only NAMED legacy children are removed -- NEVER a wholesale RMDir of the root (this dir
  ; is $INSTDIR; the new app is extracted here immediately after). RMDir/r + Delete on a missing
  ; path are silent no-ops, so this is idempotent.
  DetailPrint "MediaSearchAgent: migrating legacy shell-bundle install (if present)"

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
!macroend

!macro NSIS_HOOK_POSTINSTALL
!macroend

!macro NSIS_HOOK_PREUNINSTALL
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  DetailPrint "MediaSearchAgent: tiered cleanup of provisioned runtime (ADR-005)"

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

  DetailPrint "MediaSearchAgent: cleanup done -- no orphaned uv / venv / CPython / WebView2 dir"
!macroend
