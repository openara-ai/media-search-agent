import { useInfiniteQuery } from '@tanstack/react-query'
import { getMedia } from '../api/media'
import type { MediaFilters, MediaItem } from '../api/types'

const PAGE_SIZE = 48

export function useMedia(filters: MediaFilters) {
  return useInfiniteQuery<MediaItem[]>({
    queryKey: ['media', filters],
    queryFn: async ({ pageParam = 0 }) => {
      const res = await getMedia(filters, PAGE_SIZE, pageParam as number)
      return res.items
    },
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.length < PAGE_SIZE) return undefined
      return allPages.flat().length
    },
    initialPageParam: 0,
    staleTime: 30_000,
  })
}
