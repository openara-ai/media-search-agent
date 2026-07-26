import { useState, useEffect } from 'react'
import type { IndexerStatus, IndexerRunSummary } from '../api/indexer'
import { wsUrl } from '../lib/apiBase'

interface WSUpdate {
  status: IndexerStatus
  log: string[]
}

export function useIndexerWS(): WSUpdate | null {
  const [update, setUpdate] = useState<WSUpdate | null>(null)

  useEffect(() => {
    let closed = false
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null

    function connect() {
      if (closed) return
      ws = new WebSocket(wsUrl('/ws/indexer'))
      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'update') {
            setUpdate({ status: msg.status, log: msg.log ?? [] })
          }
        } catch {}
      }
      ws.onclose = () => {
        if (!closed) retryTimer = setTimeout(connect, 3000)
      }
    }

    connect()
    return () => {
      closed = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
    }
  }, [])

  return update
}

export function useIndexerRunning(): boolean {
  const ws = useIndexerWS()
  return ws?.status?.status === 'running'
}

export interface IndexerPhaseInfo {
  running: boolean
  /** The run's current summary phase, null when not running or not yet reported.
   * 'exporting' marks the Qdrant lock window — the only slice of a run where
   * search is briefly unavailable (sentinel-file handoff). */
  phase: IndexerRunSummary['phase'] | null
}

/** Running state + phase off the same WS feed (single socket per component). */
export function useIndexerPhase(): IndexerPhaseInfo {
  const ws = useIndexerWS()
  const status = ws?.status
  const running = status?.status === 'running'
  return {
    running,
    phase: running ? status?.summary?.phase ?? null : null,
  }
}
