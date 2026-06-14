import type { SearchFilters, SearchResponse } from './types'

export async function postSearch(q: string, filters?: SearchFilters): Promise<SearchResponse> {
  const res = await fetch('/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ q, filters: filters ?? null }),
  })
  if (!res.ok) throw new Error(`Search failed: ${res.status}`)
  return res.json()
}

/** Record that a search result was opened (the relevance label). Best-effort and
 *  fire-and-forget — never disrupts the UI if the ranker/endpoint is unavailable. */
export async function trackOpen(searchId: string, mediaId: string): Promise<void> {
  try {
    await fetch('/track/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ search_id: searchId, media_id: mediaId }),
    })
  } catch {
    // swallow — telemetry must never affect the user experience
  }
}
