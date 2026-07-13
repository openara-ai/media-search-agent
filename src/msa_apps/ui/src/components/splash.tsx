import type { ReactNode } from 'react'

/**
 * Where the deps phase ends and the model-download phase begins on the ONE continuous 0–100 bar:
 * provisioning fills 0→DEPS_END_PCT (StartupGate), the AI models fill DEPS_END_PCT→100
 * (ModelDownload). Roughly matches the byte split — ~2 GB torch/deps vs ~1 GB of models.
 */
export const DEPS_END_PCT = 60

/**
 * Shared chrome for the first-run splash (M-7 · spec §S-2). Both phases of first run render inside
 * the SAME card so the experience reads as one continuous screen rather than two hand-offs:
 *   1. StartupGate — provisioning (Python + PyTorch/CUDA + deps) via `/health`.
 *   2. ModelDownload — the AI-model download via `/api/setup/status` + `/ws/setup`.
 *
 * Keeping the shell and the bar here (not duplicated per phase) is what guarantees the card, the
 * header, and the progress bar are pixel-identical across the hand-off — the bar just keeps filling.
 */

export function SplashShell({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div
      role="status"
      aria-label="Media Search Agent startup"
      className="fixed inset-0 flex flex-col items-center justify-center gap-6 bg-slate-950 px-6 text-slate-200"
    >
      <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-slate-900/60 p-8 shadow-xl">
        <div className="mb-6 flex items-center gap-3">
          {icon}
          <h1 className="text-lg font-semibold text-slate-100">Media Search Agent</h1>
        </div>
        {children}
      </div>
    </div>
  )
}

/**
 * The one continuous progress bar. Its fill carries the `msa-provisioning-bar` activity stripe
 * (index.css) so it always reads as "alive" even when the width holds flat during a silent
 * multi-GB transfer. `rightLabel` is the elapsed time; both phases pass the SAME origin so the
 * clock never resets across the hand-off.
 */
export function ProgressBar({ pct, rightLabel }: { pct: number; rightLabel?: string }) {
  const clamped = Math.max(0, Math.min(100, Math.round(pct)))
  return (
    <div className="mt-5">
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          data-testid="provision-bar"
          className="msa-provisioning-bar h-full rounded-full bg-sky-500 transition-[width] duration-500"
          style={{ width: `${clamped}%` }}
        />
      </div>
      <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
        <span>{clamped}%</span>
        {rightLabel && <span>{rightLabel}</span>}
      </div>
    </div>
  )
}

/** Shared elapsed formatter used by both phases (e.g. "4m 12s"). */
export function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}
