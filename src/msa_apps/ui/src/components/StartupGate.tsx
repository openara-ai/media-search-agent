import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Loader2, Download, AlertTriangle, FolderOpen, RotateCcw, Check } from 'lucide-react'
import { apiUrl } from '../lib/apiBase'
import type { SetupStatus, ModelInfo } from '../api/setup'
import { ModelDownload } from './ModelDownload'
import { SplashShell, ProgressBar, formatElapsed, DEPS_END_PCT } from './splash'

/**
 * First-run startup gate (M-7 · spec §S-2 item 1).
 *
 * Polls `apiUrl('/health')` every second and renders a branded staged-progress
 * screen until the backend is ready. It then owns the WHOLE first-run experience in
 * ONE splash: once `/health` reports `ready` it checks `/api/setup/status`, and if the
 * AI models still need downloading it hands to the in-splash `ModelDownload` phase (the
 * bar continues 60→100) rather than revealing the app and popping a separate setup
 * screen. `children` mount only once provisioning AND the models are ready.
 *
 * Health payloads it understands:
 *   - provisioning responder: `{ status: 'provisioning'|'error', stage, pct, detail, log }`
 *   - the FastAPI backend:    `{ status: 'ready'|'starting' }`
 *
 * Never a one-shot fetch (a lesson from the shell template): it keeps polling while the responder
 * answers. Two independent budgets bound a failed launch so the UI never hangs on
 * "Starting…" forever — both flip to the error screen (Retry + the log affordance):
 *   - `connectTimeoutMs` (default 90 s): nothing answering at all — a sustained
 *     connection-refused or non-ok response (round-1 hardening).
 *   - `progressTimeoutMs` (default 120 s): the FastAPI backend is reachable and past
 *     provisioning but never flips to `ready` — e.g. `_lifespan` swallowed a startup
 *     exception so `/health` is pinned at `{status:'starting'}`. This phase carries no
 *     progress and should take seconds, so a modest budget catches the wedge quickly.
 *     It applies ONLY to `starting`/unrecognized 200s — NOT to `provisioning`: uv
 *     suppresses its progress bars when stdout is piped (our case), so a real
 *     multi-minute torch install legitimately holds the SAME stage/pct far longer than
 *     any no-progress budget. A flat pct there is normal, so the gate waits it through
 *     and only status:'error' from the responder (or the supervisor/reaper) ends it.
 *
 * In plain browser / dev mode `apiUrl('/health')` is same-origin and the backend
 * is already up, so this resolves to `ready` immediately and is invisible.
 *
 * Recovery is source-aware (round-5 finding). The error screen distinguishes WHY the launch failed:
 *   - `terminal`  — the provisioning shim called `status.fail(...)` and entered
 *     `_hold_error_state_inline()` (`app/__main__.py`): low disk, a `uv` non-zero exit, a missing
 *     config template. `/health` now serves the SAME `{status:'error'}` until the app PROCESS
 *     relaunches, so a re-poll can NEVER recover — it just re-reads the held error. The screen drops
 *     the no-op Retry and guides the honest recovery: quit and reopen the app (progress is saved in
 *     the resumable provisioning ledger and resumes). We use plain instructions rather than a
 *     `@tauri-apps/plugin-process` relaunch button because that plugin is not a dependency and isn't
 *     granted by the vendored `capabilities/` — wiring it would require editing read-only Tauri files.
 *     Text instructions also degrade gracefully in browser/dev mode (no broken Restart button).
 *   - `wedged`     — the FastAPI backend is up but pinned at `{status:'starting'}` (a swallowed
 *     lifespan exception). A re-poll can't self-heal either, but we keep the Retry (a cheap re-check
 *     in case the backend is merely slow) AND show the relaunch guidance as the real fix.
 *   - `unreachable`— nothing answered within `connectTimeoutMs` (connection-refused / persistent
 *     non-ok). The backend may still be coming up, so a re-poll Retry is the right first action.
 */

type Phase = 'connecting' | 'provisioning' | 'starting' | 'models' | 'error' | 'ready'

/**
 * Why the gate is on the error screen — drives which recovery affordance is honest:
 *   - `terminal`    a held responder `status:'error'` (shim in `_hold_error_state_inline`): relaunch
 *     guidance only; re-polling re-reads the same held error, so no bare Retry.
 *   - `wedged`      a FastAPI backend stuck at `starting`: Retry (cheap re-check) + relaunch guidance.
 *   - `unreachable` connection-refused / non-ok past the budget: Retry (the backend may still start).
 */
type ErrorKind = 'terminal' | 'wedged' | 'unreachable'

interface HealthResponse {
  status?: 'provisioning' | 'starting' | 'ready' | 'error'
  stage?: string
  pct?: number
  detail?: string
  log?: string
  /** Recent wheel filenames uv is fetching — the setup screen's rolling "files landing" list. */
  files?: string[]
}

/** How many recent files to show in the rolling list (newest active, the rest just-finished). */
const MAX_VISIBLE_FILES = 4

interface StartupGateProps {
  children: ReactNode
  /** Health poll cadence (ms). Default 1000. */
  pollIntervalMs?: number
  /** How long connection-refused is tolerated before the error screen (ms). Default 90 s. */
  connectTimeoutMs?: number
  /**
   * How long the FastAPI backend may stay reachable-but-not-ready in the `starting` phase before the
   * error screen (ms). Bounds the wedge where `_lifespan` caught a startup exception and `/health` is
   * pinned at `{status:'starting'}`. Applies ONLY to `starting`/unrecognized 200s — NOT to
   * `provisioning`, where piped-uv silence legitimately holds a flat stage/pct for minutes. Default 120 s.
   */
  progressTimeoutMs?: number
  /**
   * How long an *unreachable* /health is tolerated AFTER provisioning has been observed (ms). Once the
   * shim reports `provisioning`, a briefly-unreachable responder is a blip under heavy first-run install
   * load (the ~2 GB cu128 torch download on NVIDIA), NOT a dead backend — so the aggressive
   * `connectTimeoutMs` no longer applies and this far larger budget governs instead. It keeps a real
   * (long) install alive while still bounding a genuinely dead shim, which the supervisor does not reap
   * mid-provision (`main.rs` only logs the child `Terminated`). Persists across Retry. Default 10 min.
   */
  provStalledMs?: number
}

const STAGE_LABELS: Record<string, string> = {
  python: 'Preparing the Python runtime',
  'deps-torch': 'Installing ML libraries',
  'deps-app': 'Installing application packages',
  'models-pending': 'Finishing setup',
}

const STAGE_HINTS: Record<string, string> = {
  'deps-torch': 'One-time download, ~2 GB on NVIDIA systems.',
}

function stageLabel(stage: string | undefined): string {
  return (stage && STAGE_LABELS[stage]) || 'Setting up Media Search Agent'
}

/** The directory that holds the log file, for the "Open logs" affordance. */
function logDirOf(logPath: string | undefined): string {
  if (!logPath) return ''
  return logPath.replace(/[\\/][^\\/]*$/, '')
}

export function StartupGate({
  children,
  pollIntervalMs = 1000,
  connectTimeoutMs = 90_000,
  progressTimeoutMs = 120_000,
  provStalledMs = 600_000,
}: StartupGateProps) {
  const [phase, setPhase] = useState<Phase>('connecting')
  const [info, setInfo] = useState<HealthResponse>({})
  const [errorKind, setErrorKind] = useState<ErrorKind>('unreachable')
  const [elapsedMs, setElapsedMs] = useState(0)
  const [retryNonce, setRetryNonce] = useState(0)
  // Model list from the post-ready `/api/setup/status` check, handed to the model-download phase.
  const [initialModels, setInitialModels] = useState<ModelInfo[]>([])
  const startedRef = useRef(Date.now())
  // The overall first-run start — NOT reset on Retry (unlike startedRef) — so the model phase's
  // elapsed clock continues from provisioning instead of resetting at the hand-off.
  const overallStartRef = useRef(Date.now())
  // True while provisioning is plausibly still in progress: set on a `provisioning` tick and CLEARED
  // on any reachable non-provisioning answer (a `starting`/unrecognized 200, OR a non-ok response) —
  // once the responder answers with anything else we are no longer in the unreachable-blip regime.
  // Only a `provisioning` 200 immediately followed by unreachability (the real heavy-install pattern)
  // keeps it set. While set, a subsequently-
  // unreachable /health is treated as a heavy-install responder blip (governed by provStalledMs), not
  // a dead backend (connectTimeoutMs). A ref (not state) so it survives Retry (retryNonce re-runs the
  // effect) — that's what lets Retry recover mid-install instead of restarting the aggressive 90 s
  // countdown. Cleared after provisioning so a post-handoff sidecar crash still fails fast (~90 s),
  // not the 10-min budget (the responder→uvicorn handoff gap BEFORE the first `starting` 200 is still
  // covered, since the flag is only dropped once that 200 arrives).
  const sawProvisioningRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let refusedSince: number | null = null
    // Bound the FastAPI `starting` phase (reachable-but-not-ready): the timestamp since it first went
    // not-ready. Declared per effect run so Retry (a new retryNonce) starts every budget fresh. Reset
    // on ready/provisioning ticks so the seconds-scale budget only ever measures contiguous time in
    // the `starting` phase, never the (legitimately long) provisioning that precedes it.
    let notReadySince: number | null = null
    let lastLog = '' // preserved from the last payload that carried one, for the error screen's logs
    startedRef.current = Date.now()

    const schedule = () => {
      if (!cancelled) timer = setTimeout(tick, pollIntervalMs)
    }

    // The backend never produced a usable/ready answer this tick — either the fetch itself rejected
    // (`unreachable` true: connection refused / network error) or it answered with a non-ok HTTP
    // status (`unreachable` false). Charge it against the tolerance budget and flip to the error
    // screen once the budget is spent. Returns true when polling should stop.
    const noUsableAnswer = (unreachable: boolean): boolean => {
      const now = Date.now()
      if (refusedSince == null) refusedSince = now
      // Once provisioning has been observed, a genuinely UNREACHABLE /health is almost always a
      // responder blip under heavy first-run install load (the ~2 GB cu128 torch download on NVIDIA
      // holds `deps-torch` for minutes; the responder can briefly stop answering) — NOT a dead
      // backend. The aggressive 90 s connect budget misreads that as a failure and kills a good
      // install (observed on real NVIDIA hardware: cold run tripped the error screen mid-download, a
      // warm relaunch — instant provisioning — did not). Use the far larger provisioning budget for
      // THAT case only. A reachable-but-non-ok response (`!res.ok`) is a DIFFERENT failure — the
      // backend answered, it's just erroring — so it stays on the aggressive connectTimeoutMs even
      // after provisioning: a persistent 5xx during the provisioning→starting handoff must surface in
      // ~90 s (the round-1 guard), not wait out the 10-min provStalledMs budget. provStalledMs still
      // bounds a genuinely dead shim, which the supervisor does not reap mid-provision.
      const budget = unreachable && sawProvisioningRef.current ? provStalledMs : connectTimeoutMs
      if (now - refusedSince >= budget) {
        setInfo({ status: 'error', detail: 'Could not reach the Media Search Agent backend.' })
        setErrorKind('unreachable') // nothing usable yet — a re-poll may still catch it starting
        setPhase('error')
        return true
      }
      return false
    }

    // The FastAPI backend is reachable in its `starting` phase (or an unrecognized not-ready 200):
    // up, but `_lifespan` hasn't flipped `_ready`. This phase carries no progress and should take
    // seconds, so a backend pinned here — e.g. `_lifespan` swallowed a startup exception so `/health`
    // is stuck at {status:'starting'} — must NOT keep the UI on "Starting…" forever. Charges the
    // budget from the first not-ready tick and returns true (→ error screen) once it is spent.
    // NOTE: `provisioning` deliberately does NOT go through here — piped-uv silence holds a flat
    // stage/pct for minutes (see the top-of-file note), which is normal, not a stall.
    const startingWedged = (): boolean => {
      const now = Date.now()
      if (notReadySince == null) notReadySince = now
      if (now - notReadySince >= progressTimeoutMs) {
        setInfo({
          status: 'error',
          detail: 'Media Search Agent started but never became ready. See the log for details.',
          log: lastLog || undefined,
        })
        setErrorKind('wedged') // FastAPI up but pinned at `starting` — Retry can re-check, relaunch is the fix
        setPhase('error')
        return true
      }
      return false
    }

    async function tick() {
      if (cancelled) return
      let stop = false
      try {
        const res = await fetch(apiUrl('/health'), { cache: 'no-store' })
        if (cancelled) return // unmounted during the fetch — never setState on a dead component
        if (!res.ok) {
          // Reachable but NOT healthy (persistent 4xx/5xx during startup). A non-ok response is
          // not a ready/usable answer, so it must not reset the refused budget — otherwise a
          // stuck 500 would be masked as "starting" forever. Keep the budget running so it
          // eventually errors out; show "starting" in the meantime.
          //
          // The responder ANSWERED (reachable), so we are not in the unreachable-blip regime: drop the
          // sticky flag here too. Without this, after a 5xx-during-provisioning error a user's Retry —
          // if the responder has since degraded to fetch-reject — would re-arm the 10-min provStalledMs
          // budget off the stale ref instead of the 90 s connect budget.
          sawProvisioningRef.current = false
          // unreachable=false: the backend ANSWERED (it's just erroring), so this stays on the
          // aggressive connectTimeoutMs and never borrows the provStalledMs blip budget.
          if (noUsableAnswer(false)) {
            stop = true
          } else {
            setPhase((prev) => (prev === 'error' ? prev : 'starting'))
          }
        } else {
          const body = (await res.json().catch(() => ({}))) as HealthResponse
          if (cancelled) return // unmounted while parsing — guard before any state update
          refusedSince = null // reachable answer — reset the connection-refused budget (round-1)
          if (body.log) lastLog = body.log
          if (body.status === 'ready') {
            // The backend is up. Before revealing the app, check whether the AI models still need
            // downloading (first run) — if so, STAY on the splash and hand to the model-download
            // phase (same card, the bar keeps filling 60→100). A warm relaunch (models present)
            // reveals immediately, with no model-phase flash. A status-fetch glitch fails toward
            // the model phase, which retries the fetch itself rather than trapping the user.
            stop = true
            try {
              const sres = await fetch(apiUrl('/api/setup/status'), { cache: 'no-store' })
              if (cancelled) return
              const s = (sres.ok ? await sres.json().catch(() => null) : null) as SetupStatus | null
              if (cancelled) return
              if (s && s.ready) {
                setPhase('ready')
              } else {
                setInitialModels(s?.models ?? [])
                setPhase('models')
              }
            } catch {
              if (cancelled) return
              setInitialModels([])
              setPhase('models')
            }
          } else if (body.status === 'error') {
            // TERMINAL: the shim called status.fail(...) and is holding this exact payload in
            // _hold_error_state_inline() until the process relaunches. Re-polling can only re-read
            // the same held error — so the error screen guides a relaunch, not a no-op Retry.
            setInfo(body)
            setErrorKind('terminal')
            setPhase('error')
            stop = true
          } else if (body.status === 'provisioning') {
            // Reachable + actively provisioning via uv. Because uv suppresses its progress bars when
            // stdout is piped (our case), `_run_step` emits stage/pct only ~twice per step, so a real
            // multi-minute torch install legitimately holds the SAME stage/pct far longer than any
            // no-progress budget. A flat pct here is NORMAL, not a stall — so DON'T bound it: keep
            // showing progress and wait. Provisioning failures surface as status:'error' (above), and
            // a genuinely hung install is caught by the supervisor/reaper, not this gate.
            notReadySince = null // a fresh `starting` budget starts only once provisioning ends
            sawProvisioningRef.current = true // set while provisioning: unreachable /health now uses
            // provStalledMs (dropped once a `starting` 200 shows provisioning ended — see below).
            setInfo(body)
            setPhase('provisioning')
          } else {
            // 'starting' (provisioning done, FastAPI reachable) or any reachable-but-unrecognized 200:
            // up but not ready. Provisioning has ended, so DROP the sticky flag: a subsequently-
            // unreachable /health is now a post-provisioning sidecar crash (died during/after the
            // responder→uvicorn handoff), not a mid-install responder blip — it should fail fast on
            // connectTimeoutMs (~90 s), not wait out the 10-min provStalledMs. The handoff gap BEFORE
            // this first `starting` 200 stays covered, since the flag is only cleared here.
            sawProvisioningRef.current = false
            // This phase carries no progress and should take seconds — a wedged backend (lifespan
            // swallowed a startup error → pinned {status:'starting'}) spends the bounded budget and
            // reaches the error/Retry screen instead of "Starting…" forever.
            if (startingWedged()) {
              stop = true
            } else {
              setPhase((prev) => (prev === 'error' ? prev : 'starting'))
            }
          }
        }
      } catch {
        if (cancelled) return // unmounted during the fetch/parse — don't setState
        // The fetch itself rejected (connection refused / network error) — genuinely unreachable, so
        // unreachable=true: after provisioning was seen this gets the larger provStalledMs blip budget
        // (the NVIDIA cold-install case); otherwise it stays on the 90 s connectTimeoutMs.
        if (noUsableAnswer(true)) {
          stop = true
        } else {
          // Transient refusal (e.g. the responder→uvicorn handoff) — hold the last good screen.
          setPhase((prev) => (prev === 'provisioning' || prev === 'starting' ? prev : 'connecting'))
        }
      }
      if (cancelled) return
      setElapsedMs(Date.now() - startedRef.current)
      if (!stop) schedule()
    }

    tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [retryNonce, pollIntervalMs, connectTimeoutMs, progressTimeoutMs, provStalledMs])

  if (phase === 'ready') return <>{children}</>

  // First-run model download — same splash card, the bar continues 60→100 (spec §S-2).
  if (phase === 'models') {
    return (
      <ModelDownload
        onReady={() => setPhase('ready')}
        startedAtMs={overallStartRef.current}
        initialModels={initialModels}
      />
    )
  }

  const onRetry = () => {
    setPhase('connecting')
    setInfo({})
    setRetryNonce((n) => n + 1)
  }

  return (
    <StartupScreen
      phase={phase}
      info={info}
      errorKind={errorKind}
      elapsedMs={elapsedMs}
      onRetry={onRetry}
    />
  )
}

function StartupScreen({
  phase,
  info,
  errorKind,
  elapsedMs,
  onRetry,
}: {
  phase: Phase
  info: HealthResponse
  errorKind: ErrorKind
  elapsedMs: number
  onRetry: () => void
}) {
  const isError = phase === 'error'
  const logDir = logDirOf(info.log)
  const icon = isError ? (
    <AlertTriangle size={22} className="shrink-0 text-red-400" aria-hidden />
  ) : phase === 'provisioning' ? (
    <Download size={22} className="shrink-0 text-sky-400" aria-hidden />
  ) : (
    <Loader2 size={22} className="shrink-0 animate-spin text-sky-400" aria-hidden />
  )

  return (
    <SplashShell icon={icon}>
      {isError ? (
        <ErrorBody detail={info.detail} logDir={logDir} errorKind={errorKind} onRetry={onRetry} />
      ) : (
        <ProgressBody phase={phase} info={info} elapsedMs={elapsedMs} />
      )}
    </SplashShell>
  )
}

function ProgressBody({
  phase,
  info,
  elapsedMs,
}: {
  phase: Phase
  info: HealthResponse
  elapsedMs: number
}) {
  const heading =
    phase === 'connecting'
      ? 'Starting up'
      : phase === 'starting'
        ? 'Starting Media Search Agent'
        : stageLabel(info.stage)
  const hint = phase === 'provisioning' ? STAGE_HINTS[info.stage ?? ''] : undefined
  const showBar = phase === 'provisioning'
  // Deps fill the first DEPS_END_PCT of the one continuous bar; the model phase fills the rest.
  const displayPct = Math.round(Math.max(0, Math.min(100, info.pct ?? 0)) * (DEPS_END_PCT / 100))

  return (
    <div>
      <p className="text-base font-medium text-slate-100">{heading}</p>
      {info.detail && phase === 'provisioning' && (
        <p className="mt-1 text-sm text-slate-400">{info.detail}</p>
      )}
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}

      {phase === 'provisioning' && info.files && info.files.length > 0 && (
        <ul
          className="mt-3 grid gap-1.5 overflow-hidden rounded-lg border border-slate-800 bg-slate-950/50 p-2.5"
          aria-label="Files being downloaded"
        >
          {info.files.slice(-MAX_VISIBLE_FILES).map((file, i, shown) => {
            const active = i === shown.length - 1 // newest = still downloading
            return (
              <li
                key={file}
                className={`flex items-center gap-2 truncate font-mono text-[11px] ${
                  active ? 'text-slate-200' : 'text-slate-500'
                }`}
              >
                {active ? (
                  <Loader2 size={12} className="shrink-0 animate-spin text-sky-400" aria-hidden />
                ) : (
                  <Check size={12} className="shrink-0 text-slate-600" aria-hidden />
                )}
                <span className="truncate">{file}</span>
              </li>
            )
          })}
        </ul>
      )}

      {showBar && <ProgressBar pct={displayPct} rightLabel={`${formatElapsed(elapsedMs)} elapsed`} />}
      {!showBar && (
        <p className="mt-4 text-xs text-slate-500">{formatElapsed(elapsedMs)} elapsed</p>
      )}
      <p className="mt-6 text-xs text-slate-600">
        First launch installs Python and the ML libraries — this is a one-time step. You can leave
        this window open.
      </p>
    </div>
  )
}

function ErrorBody({
  detail,
  logDir,
  errorKind,
  onRetry,
}: {
  detail: string | undefined
  logDir: string
  errorKind: ErrorKind
  onRetry: () => void
}) {
  // A held responder error (`terminal`) can only be cleared by relaunching the process, so a re-poll
  // Retry would loop back to the SAME error — drop it and guide the honest recovery. A `wedged`
  // FastAPI backend keeps the (cheap) Retry AND shows the relaunch guidance as the real fix.
  const showRetry = errorKind !== 'terminal'
  const showRelaunch = errorKind === 'terminal' || errorKind === 'wedged'

  return (
    <div>
      <p className="text-base font-medium text-slate-100">Setup could not finish</p>
      <p className="mt-2 text-sm text-slate-400">
        {detail || 'Something went wrong while setting up Media Search Agent.'}
      </p>

      {showRelaunch && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3">
          <RotateCcw size={16} className="mt-0.5 shrink-0 text-sky-400" aria-hidden />
          <div className="min-w-0">
            <p className="text-xs text-slate-300">
              Quit and reopen Media Search Agent to try again.
            </p>
            <p className="mt-0.5 text-xs text-slate-500">
              Your progress is saved and will resume where it left off.
            </p>
          </div>
        </div>
      )}

      {logDir && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-3">
          <FolderOpen size={16} className="mt-0.5 shrink-0 text-slate-500" aria-hidden />
          <div className="min-w-0">
            <p className="text-xs text-slate-400">Open logs</p>
            <p className="mt-0.5 break-all font-mono text-xs text-slate-500" data-testid="log-dir">
              {logDir}
            </p>
            <p className="mt-1 text-xs text-slate-600">
              See <span className="font-mono">msa-desktop.log</span> for the full record.
            </p>
          </div>
        </div>
      )}

      {showRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-5 w-full rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-sm font-medium text-slate-100 hover:bg-slate-700"
        >
          Retry
        </button>
      )}
    </div>
  )
}
