import { useState, useCallback, useRef, useEffect } from 'react'
import { Search, AlertTriangle } from 'lucide-react'
import { useSearch } from '../hooks/useSearch'
import { useIndexerPhase } from '../hooks/useIndexerStatus'
import { MediaCard } from '../components/media/MediaCard'
import { MediaDetailDrawer } from '../components/media/MediaDetailDrawer'
import { FilterPanel } from '../components/filters/FilterPanel'
import type { SearchFilters, SearchItem } from '../api/types'

function useDebounce<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value)
  const timer = useRef<ReturnType<typeof setTimeout>>()
  const set = useCallback((v: T) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setDebounced(v), delay)
  }, [delay])
  useEffect(() => { set(value) }, [value, set])
  return debounced
}

export function SearchPage() {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [filters, setFilters] = useState<SearchFilters>({})
  const [minScore, setMinScore] = useState(15)
  const [selected, setSelected] = useState<SearchItem | null>(null)
  // Snapshot the search_id at click time so an open stays tied to the search that produced
  // the result — a later background refetch changing data.search_id can't re-attribute it.
  const [selectedSearchId, setSelectedSearchId] = useState<string | null>(null)
  const { running: indexerRunning, phase: indexerPhase } = useIndexerPhase()

  const debouncedFilters = useDebounce(filters, 300)
  const { data, isFetching, isError } = useSearch(submitted, debouncedFilters)
  const allResults = data?.results
  const searchId = data?.search_id ?? null
  const results = minScore > 0
    ? allResults?.filter(r => (r.score ?? 0) * 100 >= minScore)
    : allResults

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitted(query.trim())
  }

  return (
    <div className="flex flex-col h-full gap-4">
      {indexerRunning && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 text-amber-800 dark:text-amber-300 text-sm">
          <AlertTriangle size={15} className="shrink-0" />
          {indexerPhase === 'exporting'
            ? 'Finalizing index — search resumes shortly.'
            : 'Indexing in progress — results reflect your library before this run.'}
        </div>
      )}
      {/* Search bar */}
      <form onSubmit={handleSubmit} className="flex gap-2">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-400 dark:text-zinc-500" />
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Describe what you're looking for…"
            className="w-full bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded-lg pl-9 pr-4 py-2.5 text-zinc-900 dark:text-zinc-100 placeholder-zinc-400 dark:placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
        </div>
        <button
          type="submit"
          className="px-4 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-medium transition-colors"
        >
          Search
        </button>
      </form>

      <div className="flex gap-6 flex-1 min-h-0">
        {/* Filter panel */}
        <FilterPanel filters={filters} onChange={setFilters} minScore={minScore} onMinScoreChange={setMinScore} />

        {/* Results */}
        <div className="flex-1 overflow-y-auto">
          {!submitted && (
            <div className="flex items-center justify-center h-full text-zinc-300 dark:text-zinc-600 text-sm">
              Enter a search query to find photos and videos
            </div>
          )}

          {submitted && isFetching && (
            <div className="flex items-center justify-center h-32 text-zinc-400 dark:text-zinc-500 text-sm">
              Searching…
            </div>
          )}

          {submitted && isError && (
            <div className="text-red-500 dark:text-red-400 text-sm">Search failed. Is the API running?</div>
          )}

          {submitted && !isFetching && allResults?.length === 0 && (
            <div className="text-zinc-400 dark:text-zinc-500 text-sm">No results found for "{submitted}"</div>
          )}
          {submitted && !isFetching && (allResults?.length ?? 0) > 0 && results?.length === 0 && (
            <div className="text-zinc-400 dark:text-zinc-500 text-sm">
              All {allResults!.length} results are below the {minScore}% score threshold — try lowering it.
            </div>
          )}

          {results && results.length > 0 && (
            <>
              <div className="text-xs text-zinc-400 dark:text-zinc-500 mb-3">
                {results.length} result{results.length !== 1 ? 's' : ''}
                {isFetching && ' · updating…'}
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
                {results.map(item => (
                  <MediaCard
                    key={`${item.id}-${item.shot_id ?? 0}-${item.timestamp ?? 0}`}
                    id={item.id}
                    path={item.path}
                    type={item.type}
                    date={item.date}
                    place={item.place}
                    onClick={() => { setSelected(item); setSelectedSearchId(searchId) }}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      <MediaDetailDrawer item={selected} onClose={() => setSelected(null)} searchId={selectedSearchId} />
    </div>
  )
}
