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
