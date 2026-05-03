import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MediaDetailDrawer } from './MediaDetailDrawer'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>
  )
}

const baseItem = {
  id: 'img-01',
  type: 'image' as const,
  date: '2024-03-15T10:30:00',
  place: null as string | null,
  gps_lat: null as number | null,
  gps_lon: null as number | null,
  score: null as number | null,
  tags: null as string[] | null,
  path: '/photos/vacation/beach.jpg',
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({ faces: [] }) })
  ))
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
    writable: true,
    configurable: true,
  })
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('MediaDetailDrawer', () => {
  it('does not render media content when item is null', () => {
    wrap(<MediaDetailDrawer item={null} onClose={vi.fn()} />)
    expect(screen.queryByRole('img')).toBeNull()
  })

  describe('PathRow', () => {
    it('shows only the filename', () => {
      wrap(<MediaDetailDrawer item={baseItem} onClose={vi.fn()} />)
      expect(screen.getByText('beach.jpg')).toBeInTheDocument()
    })

    it('renders the full path in the hover tooltip', () => {
      wrap(<MediaDetailDrawer item={baseItem} onClose={vi.fn()} />)
      expect(screen.getByText('/photos/vacation/beach.jpg')).toBeInTheDocument()
    })

    it('copies full path to clipboard when copy button is clicked', async () => {
      wrap(<MediaDetailDrawer item={baseItem} onClose={vi.fn()} />)
      await userEvent.click(screen.getByTitle('/photos/vacation/beach.jpg'))
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('/photos/vacation/beach.jpg')
    })

    it('handles paths with backslashes (Windows)', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, path: 'C:\\Users\\Photos\\beach.jpg' }} onClose={vi.fn()} />)
      expect(screen.getByText('beach.jpg')).toBeInTheDocument()
    })
  })

  describe('date row', () => {
    it('shows a formatted date when date is present', () => {
      wrap(<MediaDetailDrawer item={baseItem} onClose={vi.fn()} />)
      expect(screen.getByText(/2024/)).toBeInTheDocument()
    })

    it('hides date row when date is null', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, date: null }} onClose={vi.fn()} />)
      expect(screen.queryByText(/2024/)).toBeNull()
    })
  })

  describe('GPS / place row', () => {
    it('renders a Google Maps link when coordinates are present', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, gps_lat: 37.77, gps_lon: -122.42 }} onClose={vi.fn()} />)
      const link = screen.getByTitle('Open in Google Maps')
      expect(link).toHaveAttribute('href', expect.stringContaining('37.77'))
      expect(link).toHaveAttribute('href', expect.stringContaining('-122.42'))
      expect(link).toHaveAttribute('target', '_blank')
    })

    it('link URL uses google.com/maps', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, gps_lat: 51.5, gps_lon: -0.1 }} onClose={vi.fn()} />)
      const link = screen.getByTitle('Open in Google Maps')
      expect(link).toHaveAttribute('href', expect.stringContaining('google.com/maps'))
    })

    it('shows place name as plain text (no link) when only place is set', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, place: 'San Francisco' }} onClose={vi.fn()} />)
      expect(screen.getByText('San Francisco')).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: /google maps/i })).toBeNull()
    })

    it('hides location row when neither place nor GPS is present', () => {
      wrap(<MediaDetailDrawer item={baseItem} onClose={vi.fn()} />)
      expect(screen.queryByRole('link', { name: /google maps/i })).toBeNull()
    })
  })

  describe('tags', () => {
    it('shows tag chips when tags are present', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, tags: ['cat', 'person'] }} onClose={vi.fn()} />)
      expect(screen.getByText('cat')).toBeInTheDocument()
      expect(screen.getByText('person')).toBeInTheDocument()
    })

    it('hides tags section when tags is null', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, tags: null }} onClose={vi.fn()} />)
      expect(screen.queryByText('cat')).toBeNull()
    })

    it('hides tags section when tags is empty array', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, tags: [] }} onClose={vi.fn()} />)
      expect(screen.queryByText('cat')).toBeNull()
    })
  })

  describe('debug why field', () => {
    it('does not render the why string', () => {
      const item = { ...baseItem, why: "['Alice'] | None | src=img score=0.91" }
      wrap(<MediaDetailDrawer item={item} onClose={vi.fn()} />)
      expect(screen.queryByText(/src=img/)).toBeNull()
    })
  })

  describe('score bar', () => {
    it('shows score percentage when score is present', () => {
      wrap(<MediaDetailDrawer item={{ ...baseItem, score: 0.85 }} onClose={vi.fn()} />)
      expect(screen.getByText('85% match')).toBeInTheDocument()
    })

    it('hides score bar when score is null', () => {
      wrap(<MediaDetailDrawer item={baseItem} onClose={vi.fn()} />)
      expect(screen.queryByText(/% match/)).toBeNull()
    })
  })
})
