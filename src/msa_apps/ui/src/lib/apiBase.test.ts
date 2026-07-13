import { afterEach, describe, expect, it, vi } from 'vitest'

// apiBase reads window.__API_BASE__ at module-eval time, so each scenario
// resets the module registry and re-imports under a stubbed global.
async function loadWith(apiBase: string | undefined) {
  vi.resetModules()
  if (apiBase === undefined) {
    // @ts-expect-error test cleanup
    delete (window as any).__API_BASE__
  } else {
    ;(window as any).__API_BASE__ = apiBase
  }
  return import('./apiBase')
}

afterEach(() => {
  // @ts-expect-error test cleanup
  delete (window as any).__API_BASE__
})

describe('apiUrl', () => {
  it('returns the path unchanged when __API_BASE__ is absent (browser/dev)', async () => {
    const { apiUrl } = await loadWith(undefined)
    expect(apiUrl('/search')).toBe('/search')
    expect(apiUrl('/thumbnails/abc.jpg')).toBe('/thumbnails/abc.jpg')
  })

  it('prefixes the injected backend origin in shell mode', async () => {
    const { apiUrl } = await loadWith('http://127.0.0.1:54321')
    expect(apiUrl('/search')).toBe('http://127.0.0.1:54321/search')
    expect(apiUrl('/thumbnails/abc.jpg')).toBe('http://127.0.0.1:54321/thumbnails/abc.jpg')
  })
})

describe('wsUrl', () => {
  it('derives ws:// from location when __API_BASE__ is absent', async () => {
    const { wsUrl } = await loadWith(undefined)
    // jsdom default origin is http://localhost:3000 (or similar) — assert shape.
    expect(wsUrl('/ws/indexer')).toMatch(/^ws:\/\/[^/]+\/ws\/indexer$/)
  })

  it('derives ws:// from the injected http backend origin in shell mode', async () => {
    const { wsUrl } = await loadWith('http://127.0.0.1:54321')
    expect(wsUrl('/ws/setup')).toBe('ws://127.0.0.1:54321/ws/setup')
  })

  it('derives wss:// from an https backend origin', async () => {
    const { wsUrl } = await loadWith('https://127.0.0.1:54321')
    expect(wsUrl('/ws/setup')).toBe('wss://127.0.0.1:54321/ws/setup')
  })
})
