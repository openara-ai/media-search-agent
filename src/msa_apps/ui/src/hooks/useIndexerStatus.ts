import { useState, useEffect } from 'react'
import type { IndexerStatus } from '../api/indexer'
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
