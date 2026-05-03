import { useState, useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { useMedia } from '../hooks/useMedia'
import { MediaCard } from '../components/media/MediaCard'
import { MediaDetailDrawer } from '../components/media/MediaDetailDrawer'
import { FilterBar } from '../components/filters/FilterBar'
import type { MediaFilters, MediaItem } from '../api/types'

function hasActiveFilters(filters: MediaFilters): boolean {
  return Object.values(filters).some(v =>
    v !== undefined && v !== '' && (Array.isArray(v) ? v.length > 0 : true)
  )
}

export function BrowsePage() {
  const [filters, setFilters] = useState<MediaFilters>({})
  const [selected, setSelected] = useState<MediaItem | null>(null)
  const loaderRef = useRef<HTMLDivElement>(null)

  const {
    data,
    isFetching,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
    isError,
  } = useMedia(filters)

  const items = data?.pages.flat() ?? []
  const filtersActive = hasActiveFilters(filters)
  const initialLoad = isFetching && items.length === 0

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    const el = loaderRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      entries => { if (entries[0].isIntersecting && hasNextPage && !isFetchingNextPage) fetchNextPage() },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage])

  return (
    <div className="flex flex-col h-full gap-4">
      {/* Filter bar */}
      <FilterBar filters={filters} onChange={setFilters} />

      {/* Grid */}
      <div className="flex-1 overflow-y-auto">
        {initialLoad && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
            {Array.from({ length: 12 }).map((_, i) => (
              <div key={i} className="aspect-square rounded-lg bg-zinc-800 animate-pulse" />
            ))}
          </div>
        )}

        {isError && !initialLoad && items.length === 0 && (
          <div className="flex flex-col items-center justify-center h-40 gap-2 text-center px-6">
            <p className="text-red-300 dark:text-red-400 text-sm font-medium">Could not load media</p>
            <p className="text-zinc-500 dark:text-zinc-600 text-xs">
              Check that the API is running, then refresh the page and try again.
            </p>
          </div>
        )}

        {!isError && !initialLoad && items.length === 0 && (
          filtersActive ? (
            <div className="flex items-center justify-center h-32 text-zinc-400 dark:text-zinc-500 text-sm">
              No media matches your filters
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-48 gap-2 text-center px-6">
              <p className="text-zinc-300 dark:text-zinc-400 text-sm font-medium">Your library is empty</p>
              <p className="text-zinc-500 dark:text-zinc-600 text-xs">
                Add media folders on the{' '}
                <Link to="/indexer" className="text-blue-400 hover:text-blue-300 underline">
                  Indexer page
                </Link>
                , then run the indexer to get started.
              </p>
            </div>
          )
        )}

        {items.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
            {items.map(item => (
              <MediaCard
                key={item.id}
                id={item.id}
                path={item.path}
                type={item.type}
                date={item.date}
                place={item.place}
                duration={item.duration ?? undefined}
                onClick={() => setSelected(item)}
              />
            ))}
          </div>
        )}

        {/* Infinite scroll sentinel */}
        <div ref={loaderRef} className="py-4 flex justify-center">
          {isFetchingNextPage && (
            <span className="text-zinc-300 dark:text-zinc-600 text-xs">Loading more…</span>
          )}
          {!hasNextPage && items.length > 0 && (
            <span className="text-zinc-200 dark:text-zinc-700 text-xs">{items.length} items</span>
          )}
        </div>
      </div>

      <MediaDetailDrawer
        item={selected ? {
          id: selected.id,
          type: selected.type,
          date: selected.date,
          place: selected.place,
          gps_lat: selected.gps_lat,
          gps_lon: selected.gps_lon,
          tags: selected.tags,
          path: selected.path,
        } : null}
        onClose={() => setSelected(null)}
      />
    </div>
  )
}
