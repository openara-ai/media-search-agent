export type ModelState = 'pending' | 'downloading' | 'verifying' | 'done' | 'error'

export interface ModelInfo {
  id: string
  label: string
  size_mb: number
  present: boolean
  integrity_hint: string
  // Download origin host + path (e.g. "huggingface.co/PekingU/rtdetr_r18vd").
  // Shown on the first-launch setup page as a trust signal so users can see
  // bytes are coming from a known model host rather than a random URL.
  source: string
}

export interface SetupStatus {
  ready: boolean
  models: ModelInfo[]
}

export interface ModelDownloadState {
  status: ModelState
  error: string | null
}

export interface SetupWSUpdate {
  type: 'update' | 'complete'
  models: Record<string, ModelDownloadState>
}

import { apiUrl } from '../lib/apiBase'

export async function fetchSetupStatus(): Promise<SetupStatus> {
  const res = await fetch(apiUrl('/api/setup/status'))
  if (!res.ok) throw new Error(`setup/status ${res.status}`)
  return res.json()
}

/**
 * Re-trigger the first-launch model downloads (resets errored models and restarts the background
 * worker). The setup screen's Retry button calls this BEFORE reloading — a plain reload only
 * re-subscribes to the manager's held complete/error state and never restarts a failed download.
 */
export async function retrySetup(): Promise<void> {
  const res = await fetch(apiUrl('/api/setup/retry'), { method: 'POST' })
  if (!res.ok) throw new Error(`setup/retry ${res.status}`)
}
