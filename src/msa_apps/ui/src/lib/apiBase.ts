/**
 * The single frontend seam for the backend's location
 * (M-7 · DESKTOP_SHELL_ARCHITECTURE §5 "the one frontend seam").
 *
 * The Tauri supervisor injects `window.__API_BASE__ = "http://127.0.0.1:<port>"`
 * before the SPA loads (the backend runs on a supervisor-assigned ephemeral port).
 * In plain browser / dev mode the variable is absent, so `API_BASE` is the empty
 * string and every URL stays origin-relative — byte-identical to today. This keeps
 * one transport for every mode: the SPA never hardcodes a host/port.
 *
 * All backend calls (fetch + WebSocket) and every static asset URL (thumbnails,
 * face crops, images, videos) go through `apiUrl()` / `wsUrl()`.
 */

declare global {
  interface Window {
    /** Injected by the Tauri supervisor: `http://127.0.0.1:<ephemeral-port>`. */
    __API_BASE__?: string
    /** Injected by the Tauri supervisor from app.config.json. */
    __APP_NAME__?: string
    /** Injected by the Tauri supervisor (CARGO_PKG_VERSION). */
    __APP_VERSION__?: string
  }
}

/** Backend origin, or `''` for same-origin (browser / dev). */
export const API_BASE: string =
  (typeof window !== 'undefined' && window.__API_BASE__) || ''

/** Prefix an API/asset path with the backend origin (no-op when same-origin). */
export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

/**
 * Build a WebSocket URL for `path`. Derives `ws(s)://<host>` from `API_BASE`
 * when the supervisor injected one (shell mode), else from `location` (browser).
 */
export function wsUrl(path: string): string {
  if (API_BASE) {
    const u = new URL(API_BASE)
    const proto = u.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${proto}//${u.host}${path}`
  }
  const proto =
    typeof location !== 'undefined' && location.protocol === 'https:' ? 'wss:' : 'ws:'
  const host = typeof location !== 'undefined' ? location.host : ''
  return `${proto}//${host}${path}`
}
