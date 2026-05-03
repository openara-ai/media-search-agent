import { useQuery } from '@tanstack/react-query'
import { postSearch } from '../api/search'
import type { SearchFilters, SearchItem } from '../api/types'

export function useSearch(q: string, filters: SearchFilters) {
  return useQuery<SearchItem[]>({
    queryKey: ['search', q, filters],
    queryFn: async () => {
      if (!q.trim()) return []
      const res = await postSearch(q, filters)
      return res.results
    },
    enabled: q.trim().length > 0,
    staleTime: 30_000,
  })
}
