import { apiUrl } from '../lib/apiBase'

export interface Face {
  face_id: string
  media_id: string | null
  path: string | null
  gender: string | null
  age: number | null
  person_id: string | null
  person_name: string | null
  thumbnail: string | null
}

export interface FacesResponse {
  faces: Face[]
  count: number
  offset: number
}

export interface Person {
  person_id: string
  name: string
  face_count: number | null
  thumbnail: string | null
}

export interface PeopleResponse {
  people: Person[]
}

export interface TagsResponse extends Array<string> {}

export interface FaceSimilarMatch {
  face_id: string
  score: number
  media_id: string | null
  path: string | null
  person_id: string | null
  person_name: string | null
  shot_index: number | null
  kf_index: number | null
}

export async function getFaces(params?: {
  labeled?: 'all' | 'known' | 'unknown'
  limit?: number
  offset?: number
}): Promise<FacesResponse> {
  const q = new URLSearchParams()
  if (params?.labeled && params.labeled !== 'all') q.set('labeled', params.labeled)
  if (params?.limit != null) q.set('limit', String(params.limit))
  if (params?.offset != null) q.set('offset', String(params.offset))
  const res = await fetch(apiUrl(`/faces?${q}`))
  if (!res.ok) throw new Error(`Faces fetch failed: ${res.status}`)
  return res.json()
}

export async function getPeople(): Promise<PeopleResponse> {
  const res = await fetch(apiUrl('/people'))
  if (!res.ok) throw new Error(`People fetch failed: ${res.status}`)
  return res.json()
}

export async function getTags(): Promise<TagsResponse> {
  const res = await fetch(apiUrl('/tags'))
  if (!res.ok) throw new Error(`Tags fetch failed: ${res.status}`)
  return res.json()
}

export async function createPerson(name: string): Promise<Person> {
  const res = await fetch(apiUrl('/people'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to create person')
  }
  return res.json()
}

export async function renamePerson(personId: string, name: string): Promise<Person> {
  const res = await fetch(apiUrl(`/people/${encodeURIComponent(personId)}`), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to rename person')
  }
  return res.json()
}

export async function mergePeople(
  targetId: string,
  sourceId: string,
): Promise<{ reassigned: number; target_id: string }> {
  const res = await fetch(apiUrl(`/people/${encodeURIComponent(targetId)}/merge`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_id: sourceId }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to merge people')
  }
  return res.json()
}

export async function labelFace(
  faceId: string,
  opts: { person_id?: string; name?: string },
): Promise<{ face_id: string; person_id: string; person_name: string | null }> {
  const res = await fetch(apiUrl(`/faces/${encodeURIComponent(faceId)}/label`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(opts),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to label face')
  }
  return res.json()
}

export async function labelFacesBatch(
  faceIds: string[],
  opts: { person_id?: string; name?: string },
): Promise<{ labeled: number; person_id: string; person_name: string | null }> {
  const res = await fetch(apiUrl('/faces/label-batch'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ face_ids: faceIds, ...opts }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail ?? 'Failed to label faces')
  }
  return res.json()
}

export async function unlabelFace(faceId: string): Promise<void> {
  const res = await fetch(apiUrl(`/faces/${encodeURIComponent(faceId)}/label`), { method: 'DELETE' })
  if (!res.ok) throw new Error(`Unlabel failed: ${res.status}`)
}

export interface MediaInfo {
  id: string
  path: string | null
  type: 'image' | 'video' | null
  date: string | null
  gps_lat: number | null
  gps_lon: number | null
  place: string | null
  duration: number | null
}

export async function getMediaInfo(mediaId: string): Promise<MediaInfo> {
  const res = await fetch(apiUrl(`/media/${encodeURIComponent(mediaId)}/info`))
  if (!res.ok) throw new Error(`Media info fetch failed: ${res.status}`)
  return res.json()
}

export async function findSimilarFaces(faceId: string, topK = 20): Promise<FaceSimilarMatch[]> {
  const res = await fetch(apiUrl('/faces/search'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ face_id: faceId, top_k: topK }),
  })
  if (!res.ok) throw new Error(`Similar faces search failed: ${res.status}`)
  const data = await res.json()
  return data.matches
}
