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

export async function fetchSetupStatus(): Promise<SetupStatus> {
  const res = await fetch('/api/setup/status')
  if (!res.ok) throw new Error(`setup/status ${res.status}`)
  return res.json()
}
