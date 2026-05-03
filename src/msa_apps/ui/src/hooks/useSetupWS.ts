import { useState, useEffect } from 'react'
import type { ModelDownloadState, SetupWSUpdate } from '../api/setup'

export interface SetupWSState {
  models: Record<string, ModelDownloadState> | null
  complete: boolean
}

/**
 * Connects to /ws/setup and streams per-model download progress.
 * Reconnects automatically on disconnect (while complete is false).
 */
export function useSetupWS(): SetupWSState {
  const [state, setState] = useState<SetupWSState>({ models: null, complete: false })

  useEffect(() => {
    let closed = false
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null

    function connect() {
      if (closed) return
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${proto}//${location.host}/ws/setup`)

      ws.onmessage = (e) => {
        try {
          const msg: SetupWSUpdate = JSON.parse(e.data)
          setState({ models: msg.models, complete: msg.type === 'complete' })
        } catch {}
      }

      ws.onclose = () => {
        // Clear any existing timer before scheduling a new one — rapid flaps
        // (e.g. server restart) would otherwise stack timers and spawn multiple
        // concurrent WebSocket connections.
        if (retryTimer) clearTimeout(retryTimer)
        // Reconnect only if setup is not yet complete
        if (!closed) {
          retryTimer = setTimeout(() => {
            setState((prev) => {
              if (prev.complete) return prev  // already done, stop reconnecting
              connect()
              return prev
            })
          }, 3000)
        }
      }
    }

    connect()

    return () => {
      closed = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
    }
  }, [])

  return state
}
