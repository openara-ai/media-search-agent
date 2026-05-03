import { useEffect, useRef } from 'react'
import { ExternalLink, MonitorUp, Sparkles, X } from 'lucide-react'

interface LaunchBannerProps {
  visible: boolean
  onDismiss: () => void
}

export function LaunchBanner({ visible, onDismiss }: LaunchBannerProps) {
  const dismissButtonRef = useRef<HTMLButtonElement | null>(null)
  const appOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8000'

  useEffect(() => {
    if (!visible) {
      document.title = 'Media Search Agent'
      return
    }

    document.title = 'Media Search Agent - Just Opened'
  }, [visible])

  useEffect(() => {
    if (!visible) {
      return
    }

    dismissButtonRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onDismiss()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onDismiss, visible])

  if (!visible) {
    return null
  }

  return (
    <div
      className="fixed inset-0 z-[90] flex items-center justify-center p-6"
      style={{ backgroundColor: 'rgba(2, 6, 23, 0.72)', backdropFilter: 'blur(7px)' }}
      role="dialog"
      aria-modal="true"
      aria-label="Media Search Agent launch splash"
    >
      <div className="relative w-full max-w-[60rem] overflow-hidden rounded-[32px] border border-sky-200/70 bg-white/96 text-slate-900 shadow-[0_36px_120px_rgba(15,23,42,0.52)] ring-1 ring-white/60 dark:border-sky-700/60 dark:bg-slate-950/95 dark:text-sky-50 dark:ring-sky-300/10">
        <div
          aria-hidden="true"
          className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,_rgba(56,189,248,0.32),_transparent_34%),radial-gradient(circle_at_80%_20%,_rgba(45,212,191,0.24),_transparent_28%),radial-gradient(circle_at_bottom_right,_rgba(14,165,233,0.24),_transparent_34%),linear-gradient(145deg,_rgba(248,250,252,0.98),_rgba(224,242,254,0.86))] dark:bg-[radial-gradient(circle_at_top_left,_rgba(56,189,248,0.24),_transparent_34%),radial-gradient(circle_at_80%_20%,_rgba(45,212,191,0.16),_transparent_28%),radial-gradient(circle_at_bottom_right,_rgba(14,165,233,0.16),_transparent_34%),linear-gradient(145deg,_rgba(8,47,73,0.94),_rgba(2,6,23,0.98))]"
        />
        <div className="absolute -left-8 top-8 h-32 w-32 rounded-full bg-sky-300/30 blur-3xl dark:bg-sky-400/20" aria-hidden="true" />
        <div className="absolute right-10 top-8 h-24 w-24 rounded-full bg-teal-300/35 blur-3xl dark:bg-teal-300/20" aria-hidden="true" />
        <div className="absolute bottom-0 right-0 h-36 w-36 translate-x-8 translate-y-8 rounded-full bg-cyan-300/30 blur-3xl dark:bg-cyan-300/20" aria-hidden="true" />

        <div className="relative flex min-h-[32rem] items-center justify-center px-8 py-10 sm:px-12 sm:py-12">
          <div className="flex w-full max-w-4xl items-start justify-center gap-8 sm:gap-10">
            <div className="flex h-20 w-20 shrink-0 items-center justify-center rounded-[28px] bg-gradient-to-br from-sky-500 via-cyan-500 to-teal-400 text-white shadow-[0_18px_40px_rgba(14,165,233,0.35)]">
              <div className="relative">
                <MonitorUp size={34} aria-hidden="true" />
                <Sparkles size={14} className="absolute -right-2 -top-2" aria-hidden="true" />
              </div>
            </div>

            <div className="flex max-w-3xl flex-col items-center text-center">
              <button
                type="button"
                onClick={onDismiss}
                className="inline-flex items-center gap-2 rounded-full border border-sky-300/70 bg-white/75 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.24em] text-sky-700 shadow-sm transition-colors hover:bg-white hover:text-sky-950 dark:border-sky-600/60 dark:bg-slate-900/60 dark:text-sky-200 dark:hover:bg-slate-900 dark:hover:text-white"
                aria-label="Dismiss launch splash from just launched badge"
              >
                <ExternalLink size={12} aria-hidden="true" />
                Just Launched
              </button>

              <div className="mt-6 text-3xl font-semibold tracking-tight sm:text-[2rem]">
              Media Search Agent
              </div>
              <p
                className="mt-5 text-xl leading-relaxed text-cyan-700 dark:text-cyan-200 sm:text-2xl"
                style={{ fontFamily: '"Segoe Script", "Bradley Hand", "Brush Script MT", cursive' }}
              >
                Discover your forgotten moments ...
              </p>
              <p
                className="mt-5 max-w-3xl text-lg leading-relaxed text-sky-900/90 dark:text-sky-100/90 sm:text-xl"
              >
                A local-first semantic search engine for your personal photo and video library.
                Search by natural language, browse by face, and label people — entirely on your own machine.
              </p>
              <div className="mt-10 h-4" aria-hidden="true" />
              <p className="mt-6 max-w-2xl text-sm leading-8 text-slate-600 dark:text-sky-100/75">
                No cloud. No subscription. No data leaves your device.
              </p>

              <button
                type="button"
                onClick={onDismiss}
                className="mt-6 inline-flex max-w-full items-center rounded-full border border-white/70 bg-white/85 px-4 py-2 text-sm font-medium text-sky-700 shadow-sm transition-colors hover:bg-white hover:text-sky-950 dark:border-sky-700/60 dark:bg-slate-900/70 dark:text-sky-200 dark:hover:bg-slate-900 dark:hover:text-white"
                aria-label="Dismiss launch splash from local address"
              >
                {appOrigin}
              </button>
            </div>
          </div>

          <button
            type="button"
            onClick={onDismiss}
            ref={dismissButtonRef}
            className="absolute right-6 top-6 rounded-full border border-sky-200/80 bg-white/80 p-2 text-sky-700 transition-colors hover:bg-white hover:text-sky-950 dark:border-sky-700/60 dark:bg-slate-900/60 dark:text-sky-200 dark:hover:bg-slate-900 dark:hover:text-white"
            aria-label="Dismiss launch banner"
          >
            <X size={16} />
          </button>
        </div>
      </div>
    </div>
  )
}
