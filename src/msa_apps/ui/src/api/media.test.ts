import { afterEach, describe, expect, it, vi } from 'vitest'
import { getMedia, thumbnailUrl } from './media'

describe('thumbnailUrl', () => {
  it('returns thumbnail URL for a media_id', () => {
    expect(thumbnailUrl('abc123def456')).toBe('/thumbnails/abc123def456.jpg')
  })

  it('returns thumbnail URL for a full SHA256 media_id', () => {
    const sha256 = 'a'.repeat(64)
    expect(thumbnailUrl(sha256)).toBe(`/thumbnails/${sha256}.jpg`)
  })

  it('returns null for null input', () => {
    expect(thumbnailUrl(null)).toBeNull()
  })

  it('returns null for undefined input', () => {
    expect(thumbnailUrl(undefined)).toBeNull()
  })

  it('returns null for empty string', () => {
    expect(thumbnailUrl('')).toBeNull()
  })
})

describe('getMedia', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('includes people filters in the browse query string', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ items: [], count: 0, limit: 48, offset: 0 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await getMedia(
      { people: ['Arjun', 'Maya'], people_mode: 'all', sort_by: 'date', sort_order: 'desc' },
      48,
      0,
    )

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/media?limit=48&offset=0&people=Arjun%2CMaya&people_mode=all&sort_by=date&sort_order=desc')
  })

  it('includes tag filters in the browse query string', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ items: [], count: 0, limit: 48, offset: 0 }),
    })
    vi.stubGlobal('fetch', fetchMock)

    await getMedia(
      { tags: ['walking', 'beach'], sort_by: 'date', sort_order: 'desc' },
      48,
      0,
    )

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/media?limit=48&offset=0&tags=walking%2Cbeach&sort_by=date&sort_order=desc')
  })
})
