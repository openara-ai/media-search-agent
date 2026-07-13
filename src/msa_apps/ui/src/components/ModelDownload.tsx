import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, XCircle, Loader2, Circle, ShieldCheck, Download } from 'lucide-react'
import { fetchSetupStatus, retrySetup, type SetupStatus, type ModelState, type ModelInfo } from '../api/setup'
import { useSetupWS } from '../hooks/useSetupWS'
import { SplashShell, ProgressBar, formatElapsed, DEPS_END_PCT } from './splash'

/**
 * First-run model-download phase (M-7 · spec §S-2), rendered INSIDE the startup splash by
 * StartupGate once `/health` reports the backend ready. It replaces the old standalone SetupPage:
 * the download already runs on its own in the API lifespan, so this is a pure progress subscriber
 * over `/ws/setup` (+ an initial `/api/setup/status` for the model list) that gates the app until
 * the models are on disk.
 *
 * The unified bar continues from where provisioning left off: deps fill 0→`BASE`%, the models fill
 * `BASE`→100 weighted by download size (CLIP ~850 MB dominates), so the single bar never resets.
 */

// Where the deps phase ends and the model phase begins on the one continuous 0–100 bar.
const BASE = DEPS_END_PCT
const SPAN = 100 - BASE

interface Props {
  /** Called when the models are ready (all done, or the user chose to continue with failures). */
  onReady: () => void
  /** The overall first-run start (ms); shared with StartupGate so the elapsed clock never resets. */
  startedAtMs: number
  /** Model list from StartupGate's `/api/setup/status` check — avoids a refetch and a flash. */
  initialModels: ModelInfo[]
}

function StatusIcon({ status }: { status: ModelState }) {
  switch (status) {
    case 'done':
      return <CheckCircle2 size={18} className="shrink-0 text-emerald-500" aria-label="Done" />
    case 'downloading':
      return <Loader2 size={18} className="shrink-0 animate-spin text-sky-400" aria-label="Downloading" />
    case 'verifying':
      return <ShieldCheck size={18} className="shrink-0 animate-pulse text-amber-400" aria-label="Verifying" />
    case 'error':
      return <XCircle size={18} className="shrink-0 text-red-400" aria-label="Failed" />
    default:
      return <Circle size={18} className="shrink-0 text-slate-500" aria-label="Waiting" />
  }
}

function statusLabel(status: ModelState): string {
  switch (status) {
    case 'done': return 'Verified'
    case 'downloading': return 'Downloading…'
    case 'verifying': return 'Verifying integrity…'
    case 'error': return 'Failed'
    default: return 'Waiting…'
  }
}

function statusColor(status: ModelState): string {
  switch (status) {
    case 'done': return 'text-emerald-400'
    case 'downloading': return 'text-sky-400'
    case 'verifying': return 'text-amber-400'
    case 'error': return 'text-red-400'
    default: return 'text-slate-500'
  }
}

export function ModelDownload({ onReady, startedAtMs, initialModels }: Props) {
  // Reuse the list StartupGate already fetched; only hit the network if it couldn't (empty list).
  const { data, isError } = useQuery<SetupStatus>({
    queryKey: ['setup/status'],
    queryFn: fetchSetupStatus,
    retry: 2,
    retryDelay: 1000,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    // Poll setup status as a backstop to /ws/setup, stopping once it reports ready (Codex P2). The
    // WS drives the live per-model checklist, but it can disconnect or never send `complete` while
    // the background downloader finishes — and in the normal path the seeded initialData + staleTime
    // would otherwise pin `data.ready` at false forever. Polling lets the authoritative `ready:true`
    // eventually surface and reveal the app regardless of WS health.
    refetchInterval: (query) => (query.state.data?.ready ? false : 3000),
    initialData: initialModels.length ? { ready: false, models: initialModels } : undefined,
  })
  const { models: wsModels, complete } = useSetupWS()
  const [continued, setContinued] = useState(false)

  const [elapsedMs, setElapsedMs] = useState(() => Math.max(0, Date.now() - startedAtMs))
  useEffect(() => {
    const id = setInterval(() => setElapsedMs(Math.max(0, Date.now() - startedAtMs)), 1000)
    return () => clearInterval(id)
  }, [startedAtMs])

  const models = data?.models ?? initialModels
  const rows = models.map((info) => {
    const live = wsModels?.[info.id]
    const status: ModelState = live?.status ?? (info.present ? 'done' : 'pending')
    return { ...info, status, error: live?.error ?? null }
  })
  // Error state comes from the checklist rows AND the raw WS states. The second term matters in the
  // fallback path where StartupGate's /api/setup/status check failed → `initialModels` is empty →
  // `rows` is empty and can't reflect a failure; without it, a `complete` carrying model errors
  // would slip past the retry/continue UI and auto-reveal the app (Codex P2).
  const wsHasErrors = wsModels ? Object.values(wsModels).some((m) => m.status === 'error') : false
  const hasErrors = rows.some((r) => r.status === 'error') || wsHasErrors
  // Reveal when EITHER /ws/setup reports `complete` OR the authoritative /api/setup/status says the
  // model files are already on disk (`ready: true`). The HTTP signal matters in the fallback path
  // (Codex P2): StartupGate got here because its first status check failed, but a later successful
  // refetch can confirm readiness — the app must not stay stuck just because /ws/setup closed and
  // never sent `complete`.
  const allDone = !hasErrors && (complete || data?.ready === true)

  // Fallback path (Codex P2): StartupGate reached here with empty `initialModels` because its
  // /api/setup/status check failed; if this component's own retried refetch ALSO fails we have no
  // model metadata to render. In the same backend-config failure /ws/setup typically closes without
  // ever sending `complete`, so without this the splash would hang forever on "Downloading AI models"
  // with no error or recourse. Surface the failure (with Retry + Continue) once the retries exhaust.
  const statusFailed = isError && models.length === 0 && !allDone
  const showFailureUI = (hasErrors && complete) || statusFailed

  // Reveal the app once every model is ready — or when the user accepts running with failures.
  useEffect(() => {
    if (allDone || continued) onReady()
  }, [allDone, continued, onReady])

  // Size-weighted fraction so the bar tracks bytes, not model count (CLIP ~850 MB ≫ the others).
  const totalSize = models.reduce((s, m) => s + m.size_mb, 0) || 1
  const doneSize = rows.filter((r) => r.status === 'done').reduce((s, m) => s + m.size_mb, 0)
  const pct = BASE + (doneSize / totalSize) * SPAN

  const active = rows.find((r) => r.status === 'downloading' || r.status === 'verifying')
  const detail = active ? active.label : 'Preparing the AI models…'

  // Retry: actually restart the download on the backend (start_if_needed) BEFORE reloading — a plain
  // reload only re-subscribes to the manager's held complete/error state. The reload then gives a
  // fresh /ws/setup that streams the restarted run. Best-effort: reload even if the POST fails, since
  // the fresh mount re-checks /api/setup/status regardless.
  const onRetry = async () => {
    try {
      await retrySetup()
    } catch {
      /* best effort — the reload re-checks status */
    }
    window.location.reload()
  }

  return (
    <SplashShell icon={<Download size={22} className="shrink-0 text-sky-400" aria-hidden />}>
      <p className="text-base font-medium text-slate-100">Downloading AI models</p>
      <p className="mt-1 text-sm text-slate-400">{detail}</p>

      <ProgressBar pct={pct} rightLabel={`${formatElapsed(elapsedMs)} elapsed`} />

      <ul className="mt-4 grid gap-2 overflow-hidden" aria-label="AI models">
        {rows.map((model) => (
          <li
            key={model.id}
            className="flex min-w-0 items-start gap-3 rounded-xl border border-slate-700/50 bg-slate-800/50 px-3.5 py-3"
          >
            <StatusIcon status={model.status} />
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-baseline justify-between gap-2">
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-slate-100">
                  {model.label}
                </span>
                <span className="shrink-0 text-xs text-slate-400">~{model.size_mb} MB</span>
              </div>
              {model.error ? (
                <p className="mt-0.5 truncate text-xs text-red-400" title={model.error}>
                  {model.error}
                </p>
              ) : (
                <p className={`mt-0.5 truncate text-xs ${statusColor(model.status)}`}>
                  {statusLabel(model.status)}
                  {model.status === 'done' && model.integrity_hint && (
                    <span className="ml-1.5 font-mono text-[10px] text-emerald-600/70">
                      {model.integrity_hint}…
                    </span>
                  )}
                </p>
              )}
              {model.source && model.status !== 'done' && !model.error && (
                <p className="mt-0.5 truncate font-mono text-[10px] text-slate-500" title={model.source}>
                  from {model.source}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>

      {showFailureUI ? (
        <div className="mt-5 grid gap-2">
          {statusFailed && (
            <p className="text-sm text-red-300">
              Couldn’t load the setup status from Media Search Agent. The backend may still be
              starting, or setup hit an error.
            </p>
          )}
          <button
            type="button"
            onClick={onRetry}
            className="w-full rounded-xl border border-red-500/40 bg-red-900/20 px-4 py-2.5 text-sm font-medium text-red-300 hover:bg-red-900/30"
          >
            {statusFailed ? 'Try again' : 'Retry failed downloads'}
          </button>
          <button
            type="button"
            onClick={() => setContinued(true)}
            className="w-full rounded-xl border border-slate-600/50 bg-slate-800/40 px-4 py-2.5 text-sm font-medium text-slate-300 hover:bg-slate-800/70"
          >
            Continue anyway
          </button>
          <p className="text-center text-xs text-slate-500">
            Affected features may not work until you retry.
          </p>
        </div>
      ) : (
        <p className="mt-5 text-xs text-slate-600">
          One-time setup — after this the app works fully offline, no connection needed.
        </p>
      )}
    </SplashShell>
  )
}
