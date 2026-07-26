/**
 * Close-while-indexing confirm (#169).
 *
 * When the desktop window is closed during an active index, the indexer keeps
 * running detached in the background — it is NOT stopped. That is intended
 * (a long index survives an app restart), but it must be disclosed so the user
 * isn't surprised by a `msa index run` pinning CPU/GPU after they "quit".
 *
 * Browser / dev mode has no Tauri window, so the whole guard is a no-op there:
 * API_BASE is empty and the `@tauri-apps/api/window` import never fires, keeping
 * the browser bundle free of any Tauri coupling (parity with the fetch-only SPA).
 */
import { useEffect, useRef } from 'react'
import { API_BASE } from '../lib/apiBase'

/**
 * Pure predicate: does closing the window during this indexer status warrant a
 * confirm? Only an actively-running index does — idle/complete/error/stopped
 * all close silently. Extracted so the decision is unit-tested without a webview.
 */
export function shouldConfirmClose(status: string | undefined): boolean {
  return status === 'running'
}

/** The disclosure shown on close-while-indexing. OK = quit (index keeps running). */
export const CLOSE_WHILE_INDEXING_MESSAGE =
  'Indexing is in progress and will keep running in the background if you quit. ' +
  'Quit anyway?'

const defaultConfirm = (message: string): boolean => window.confirm(message)

/**
 * Register a Tauri `onCloseRequested` guard while `running` is true. Shell mode
 * only. The listener is registered exactly ONCE (empty deps) and reads both the
 * live running state and the confirm function through refs, so an indexer status
 * tick — which re-renders this component several times a second during a run —
 * never tears down and re-subscribes the listener. (Re-subscribing would open a
 * repeated gap: unlisten() is synchronous but re-attach is an async import, so a
 * close mid-teardown could slip through unguarded — the exact #169 regression.)
 */
export function useCloseWhileIndexingGuard(
  running: boolean,
  confirmFn: (message: string) => boolean = defaultConfirm,
): void {
  const runningRef = useRef(running)
  runningRef.current = running
  const confirmRef = useRef(confirmFn)
  confirmRef.current = confirmFn

  useEffect(() => {
    // Browser / dev: no supervisor-injected backend origin → no Tauri window.
    if (!API_BASE) return

    let unlisten: (() => void) | undefined
    let cancelled = false

    import('@tauri-apps/api/window')
      .then(({ getCurrentWindow }) => {
        if (cancelled) return
        const appWindow = getCurrentWindow()
        return appWindow.onCloseRequested((event) => {
          if (!runningRef.current) return // idle → let the window close normally
          // Block the default close, then ask. Confirm → destroy the window
          // (the detached indexer continues, as disclosed); cancel → stay open.
          event.preventDefault()
          if (confirmRef.current(CLOSE_WHILE_INDEXING_MESSAGE)) {
            void appWindow.destroy()
          }
        })
      })
      .then((fn) => {
        if (cancelled) fn?.()
        else unlisten = fn
      })
      .catch(() => {
        // API unavailable (unexpected in shell mode): degrade to the prior
        // behaviour — the OS close proceeds, indexer detaches silently.
      })

    return () => {
      cancelled = true
      unlisten?.()
    }
  }, [])
}
