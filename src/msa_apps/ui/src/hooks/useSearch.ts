import { useQuery } from '@tanstack/react-query'
import { postSearch } from '../api/search'
import type { SearchFilters, SearchResponse } from '../api/types'

export function useSearch(q: string, filters: SearchFilters) {
  return useQuery<SearchResponse>({
    queryKey: ['search', q, filters],
    queryFn: async () => {
      if (!q.trim()) return { results: [] }
      return postSearch(q, filters)
    },
    enabled: q.trim().length > 0,
    staleTime: 30_000,
  })
}
