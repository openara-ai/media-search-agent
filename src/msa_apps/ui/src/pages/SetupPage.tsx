import { useEffect } from 'react'
import { CheckCircle2, XCircle, Loader2, Circle, Wifi, ShieldCheck } from 'lucide-react'
import { useSetupWS } from '../hooks/useSetupWS'
import type { ModelInfo, ModelState } from '../api/setup'

interface Props {
  initialModels: ModelInfo[]
  onComplete: () => void
}

const STATE_ORDER: ModelState[] = ['pending', 'downloading', 'verifying', 'done', 'error']

function StatusIcon({ status }: { status: ModelState | undefined }) {
  switch (status) {
    case 'done':
      return <CheckCircle2 size={20} className="text-emerald-500 shrink-0" aria-label="Done" />
    case 'downloading':
      return <Loader2 size={20} className="text-sky-400 shrink-0 animate-spin" aria-label="Downloading" />
    case 'verifying':
      return <ShieldCheck size={20} className="text-amber-400 shrink-0 animate-pulse" aria-label="Verifying" />
    case 'error':
      return <XCircle size={20} className="text-red-400 shrink-0" aria-label="Error" />
    default:
      return <Circle size={20} className="text-slate-400 shrink-0" aria-label="Waiting" />
  }
}

function statusLabel(status: ModelState | undefined): string {
  switch (status) {
    case 'done':        return 'Verified'
    case 'downloading': return 'Downloading…'
    case 'verifying':   return 'Verifying integrity…'
    case 'error':       return 'Failed'
    default:            return 'Waiting…'
  }
}

function statusColor(status: ModelState | undefined): string {
  switch (status) {
    case 'done':        return 'text-emerald-400'
    case 'downloading': return 'text-sky-400'
    case 'verifying':   return 'text-amber-400'
    case 'error':       return 'text-red-400'
    default:            return 'text-slate-500'
  }
}

export function SetupPage({ initialModels, onComplete }: Props) {
  const { models: wsModels, complete } = useSetupWS()

  const hasErrors = wsModels
    ? Object.values(wsModels).some((m) => m.status === 'error')
    : false

  // Only advance to the main app when all models are truly ready — not on error.
  // If complete is true but errors are present the retry button must stay visible.
  useEffect(() => {
    if (complete && !hasErrors) onComplete()
  }, [complete, hasErrors, onComplete])

  // Merge initial model metadata with live WS state
  const modelRows = initialModels.map((info) => {
    const live = wsModels?.[info.id]
    const status: ModelState = live?.status ?? (info.present ? 'done' : 'pending')
    return { ...info, status, error: live?.error ?? null }
  })

  // Sort so that the currently-active model floats to top,
  // preserving original order within each state bucket
  const sorted = [...modelRows].sort(
    (a, b) => STATE_ORDER.indexOf(a.status) - STATE_ORDER.indexOf(b.status),
  )

  const doneCount = modelRows.filter((m) => m.status === 'done').length
  const totalCount = modelRows.length

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      style={{ background: 'linear-gradient(135deg, #0c1828 0%, #0f2237 50%, #0a1e2e 100%)' }}
    >
      {/* Ambient glows */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
        <div className="absolute -left-20 top-0 h-96 w-96 rounded-full bg-sky-500/10 blur-3xl" />
        <div className="absolute right-0 top-1/4 h-80 w-80 rounded-full bg-teal-500/8 blur-3xl" />
        <div className="absolute bottom-0 left-1/3 h-72 w-72 rounded-full bg-cyan-500/8 blur-3xl" />
      </div>

      <div className="relative w-full max-w-lg overflow-hidden rounded-[28px] border border-sky-200/20 bg-slate-900/80 shadow-[0_40px_120px_rgba(0,0,0,0.6)] ring-1 ring-white/5 backdrop-blur-xl">
        {/* Top accent bar */}
        <div className="h-1 w-full bg-gradient-to-r from-sky-500 via-cyan-400 to-teal-400" />

        <div className="px-8 py-8">
          {/* Header */}
          <div className="mb-7 flex items-center gap-4">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-sky-500 via-cyan-500 to-teal-400 text-white shadow-[0_8px_24px_rgba(14,165,233,0.35)]">
              <Wifi size={22} aria-hidden="true" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-white">First Launch Setup</h1>
              <p className="text-sm text-sky-300/80">
                Downloading AI models for offline use
              </p>
            </div>
          </div>

          {/* Progress summary */}
          <div className="mb-5">
            <div className="mb-2 flex items-center justify-between text-xs text-slate-400">
              <span>{doneCount} of {totalCount} models ready</span>
              {hasErrors && (
                <span className="text-red-400">Some downloads failed</span>
              )}
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-700/60">
              <div
                className="h-full rounded-full bg-gradient-to-r from-sky-500 to-teal-400 transition-all duration-700"
                style={{ width: `${(doneCount / totalCount) * 100}%` }}
              />
            </div>
          </div>

          {/* Model rows */}
          <ul className="space-y-3" role="list">
            {sorted.map((model) => (
              <li
                key={model.id}
                className="flex items-center gap-3 rounded-xl border border-slate-700/50 bg-slate-800/50 px-4 py-3"
              >
                <StatusIcon status={model.status} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-sm font-medium text-slate-100">
                      {model.label}
                    </span>
                    <span className="shrink-0 text-xs text-slate-400">
                      ~{model.size_mb} MB
                    </span>
                  </div>
                  {model.error ? (
                    <p className="mt-0.5 truncate text-xs text-red-400" title={model.error}>
                      {model.error}
                    </p>
                  ) : (
                    <p className={`mt-0.5 text-xs ${statusColor(model.status)}`}>
                      {statusLabel(model.status)}
                      {/* integrity_hint is optional - some models (facenet-pytorch
                          historically, any future model verified by its loader's
                          own hash check) return an empty string. Render the
                          ellipsis suffix only when there's actual content,
                          otherwise the "Verified" line shows a lone "…". */}
                      {model.status === 'done' && model.integrity_hint && (
                        <span className="ml-1.5 font-mono text-[10px] text-emerald-600/70">
                          {model.integrity_hint}…
                        </span>
                      )}
                    </p>
                  )}
                  {/* Source line — trust signal so users can see where bytes
                      are coming from (huggingface.co, github.com) before they
                      arrive. Hidden once the model is done (the integrity
                      hint on the status line is the relevant artifact then)
                      and on error (the error message takes priority). */}
                  {model.source && model.status !== 'done' && !model.error && (
                    <p className="mt-0.5 truncate font-mono text-[10px] text-slate-500" title={model.source}>
                      from {model.source}
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ul>

          {/* Footer */}
          <div className="mt-6 rounded-xl border border-slate-700/40 bg-slate-800/30 px-4 py-3">
            <p className="text-xs leading-relaxed text-slate-400">
              This is a <span className="text-slate-300">one-time setup</span>. After
              it completes the app works fully offline — no internet connection required.
            </p>
          </div>

          {/* Retry + Continue buttons — shown only when all downloads are settled and some failed.
              Continue lets the user reach the main app even with failed models; affected features
              (e.g. face recognition when facenet-pytorch fails) will be unavailable, but the rest
              of the app (search, browse, object detection) still works. Without this escape hatch
              the setup screen is a dead end on networks where one model host is unreachable
              (e.g. GitHub release assets blocked by a proxy).

              KNOWN LIMITATIONS — tracked in https://github.com/kumraj/media-search-agent/issues/131:
                (a) the dismissal is in-memory only; reloading the page re-traps the user on this
                    setup screen because /api/setup/status still returns ready: false.
                (b) the helper text below says "retry from Settings later", but the Settings page
                    has no setup-retry control today. The follow-up issue plans a persisted skip
                    flag (localStorage) + a recheck affordance in the main app. Deferred from the
                    fix/installer-p1-guards branch because the persistence + UI work is larger
                    than the original P1 scope. */}
          {hasErrors && complete && (
            <>
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="mt-4 w-full rounded-xl border border-red-500/40 bg-red-900/20 px-4 py-2.5 text-sm font-medium text-red-300 transition-colors hover:bg-red-900/30 hover:text-red-200"
              >
                Retry failed downloads
              </button>
              <button
                type="button"
                onClick={onComplete}
                className="mt-2 w-full rounded-xl border border-slate-600/50 bg-slate-800/40 px-4 py-2.5 text-sm font-medium text-slate-300 transition-colors hover:bg-slate-800/70 hover:text-slate-100"
              >
                Continue anyway
              </button>
              <p className="mt-2 text-center text-xs text-slate-500">
                Affected features may not work. Reload this page to retry the downloads later.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
