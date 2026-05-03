import type { MediaListResponse, MediaFacesResponse, MediaFilters } from './types'

export async function getMedia(
  filters: MediaFilters,
  limit: number,
  offset: number,
): Promise<MediaListResponse> {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  if (filters.media_type) params.set('media_type', filters.media_type)
  if (filters.date_from) params.set('date_from', filters.date_from)
  if (filters.date_to) params.set('date_to', filters.date_to)
  if (filters.place) params.set('place', filters.place)
  if (filters.tags && filters.tags.length > 0) params.set('tags', filters.tags.join(','))
  if (filters.people && filters.people.length > 0) params.set('people', filters.people.join(','))
  if (filters.people && filters.people.length > 0 && filters.people_mode) params.set('people_mode', filters.people_mode)
  if (filters.sort_by) params.set('sort_by', filters.sort_by)
  if (filters.sort_order) params.set('sort_order', filters.sort_order)
  const res = await fetch(`/media?${params}`)
  if (!res.ok) throw new Error(`Media listing failed: ${res.status}`)
  return res.json()
}

export async function getMediaFaces(mediaId: string): Promise<MediaFacesResponse> {
  const res = await fetch(`/media/${mediaId}/faces`)
  if (!res.ok) throw new Error(`Faces fetch failed: ${res.status}`)
  return res.json()
}

export function thumbnailUrl(mediaId: string | null | undefined): string | null {
  if (!mediaId) return null
  return `/thumbnails/${mediaId}.jpg`
}

export function faceThumbnailUrl(faceId: string): string {
  const sanitized = faceId.replace(/:/g, '_')
  return `/face_thumbnails/${sanitized}.jpg`
}

export function imageUrl(mediaId: string): string {
  return `/images/${mediaId}`
}

export function videoUrl(mediaId: string): string {
  return `/videos/${mediaId}`
}
