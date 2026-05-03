export interface IndexerStatus {
  status: 'idle' | 'running' | 'complete' | 'error'
  run_id: string | null
  started_at: string | null
  finished_at: string | null
  elapsed_seconds: number | null
  return_code: number | null
  summary?: IndexerRunSummary | null
}

export interface IndexerRunSummary {
  phase?: 'counting' | 'analyzing' | 'processing' | 'exporting' | 'complete'
  total_found?: number
  already_indexed?: number
  needs_processing?: number
  images_to_process?: number
  videos_to_process?: number
  estimated_remaining_seconds?: number | null
  processed_images?: number
  processed_videos?: number
  skipped?: number
  faces?: number
  tagged_items?: number
  avg_image_seconds?: number | null
  avg_video_seconds?: number | null
  avg_video_seconds_per_min?: number | null
}

export interface IndexStats {
  images: number
  videos: number
  total_video_duration: number
  faces: number
  people: number
  last_indexed_at: string | null
}

export interface MediaSource {
  name: string
  path: string
  display_path: string  // user-native format (e.g. D:\Photos on WSL2); use this for display
  read_only: boolean
  enabled: boolean
  description: string
}

export interface BrowseEntry {
  name: string
  display_path: string
  wsl_path: string
  is_dir: boolean
}

export interface BrowseResult {
  current: { display_path: string; wsl_path: string }
  parent: { display_path: string; wsl_path: string } | null
  entries: BrowseEntry[]
}

export async function startIndexer(): Promise<void> {
  const res = await fetch('/indexer/start', { method: 'POST' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to start indexer')
  }
}

export async function stopIndexer(): Promise<void> {
  const res = await fetch('/indexer/stop', { method: 'POST' })
  if (!res.ok) throw new Error(res.statusText)
}

export async function getIndexStats(): Promise<IndexStats> {
  const res = await fetch('/indexer/stats')
  if (!res.ok) throw new Error(res.statusText)
  return res.json()
}

export async function getSources(): Promise<MediaSource[]> {
  const res = await fetch('/config/sources')
  if (!res.ok) throw new Error(res.statusText)
  const data = await res.json()
  return data.sources
}

export async function addSource(body: {
  name: string
  path: string
  description?: string
  read_only?: boolean
}): Promise<void> {
  const res = await fetch('/config/sources', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to add source')
  }
}

export interface Diagnostics {
  msa_root: string
  config_file: string
  sqlite_path: string
  models_dir: string
  log_dir: string
  logs: Record<string, string>
  qdrant_url?: string
  api_url: string
}

export async function getDiagnostics(): Promise<Diagnostics> {
  const res = await fetch('/diagnostics')
  if (!res.ok) throw new Error(res.statusText)
  return res.json()
}

export interface ModelConfigEditable {
  batch_size: number
  enable_object_detection: 'auto' | boolean
  object_model: string
  object_confidence_threshold: number
  enable_face_recognition: boolean
  face_model: string
  face_confidence_threshold: number
  face_min_size: number
  face_store_metadata: boolean
}

export interface ModelConfigReadonly {
  device: string
  model_name: string
  pretrained: string
}

export interface ModelConfig {
  readonly: ModelConfigReadonly
  editable: ModelConfigEditable
  defaults: ModelConfigEditable
}

export async function getModelConfig(): Promise<ModelConfig> {
  const res = await fetch('/config/model')
  if (!res.ok) throw new Error(res.statusText)
  return res.json()
}

export async function patchModelConfig(updates: Partial<ModelConfigEditable>): Promise<void> {
  const res = await fetch('/config/model', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to save model config')
  }
}

export async function deleteSource(name: string): Promise<void> {
  const res = await fetch(`/config/sources/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(res.statusText)
}

export async function getPlatform(): Promise<{ platform: 'wsl2' | 'linux' | 'macos' | 'windows' }> {
  const res = await fetch('/platform')
  if (!res.ok) throw new Error(res.statusText)
  return res.json()
}

export async function nativePick(): Promise<{ path: string | null; cancelled: boolean }> {
  const res = await fetch('/browse/pick')
  if (res.status === 405) return { path: null, cancelled: false }  // not supported → fallback
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Native picker failed')
  }
  return res.json()
}

export async function browse(path: string): Promise<BrowseResult> {
  const res = await fetch(`/browse?path=${encodeURIComponent(path)}`)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to browse directory')
  }
  return res.json()
}
