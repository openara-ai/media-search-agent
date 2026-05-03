import { Film } from 'lucide-react'
import { thumbnailUrl } from '../../api/media'
import { FaceStrip } from './FaceStrip'
import type { FaceOnMedia } from '../../api/types'
import { cn } from '../../lib/utils'

interface MediaCardProps {
  id: string | null | undefined
  path?: string | null
  type?: 'image' | 'video' | null
  date?: string | null
  place?: string | null
  score?: number | null
  duration?: number | null
  faces?: FaceOnMedia[]
  onClick: () => void
}

function formatDuration(s: number): string {
  const m = Math.floor(s / 60)
  const sec = Math.floor(s % 60)
  return `${m}:${sec.toString().padStart(2, '0')}`
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
  } catch {
    return ''
  }
}

export function MediaCard({ id, path: _path, type, date, place, score, duration, faces, onClick }: MediaCardProps) {
  const thumb = thumbnailUrl(id)
  const scorePct = score != null ? Math.min(100, Math.max(0, Math.round(score * 100))) : null
  return (
    <button
      onClick={onClick}
      className={cn(
        'group relative flex flex-col bg-slate-100 dark:bg-zinc-800 rounded-lg overflow-hidden',
        'ring-1 ring-slate-200 dark:ring-zinc-700 hover:ring-indigo-500 transition-all text-left',
      )}
    >
      {/* Thumbnail */}
      <div className="relative aspect-square bg-slate-200 dark:bg-zinc-900 overflow-hidden">
        {thumb && (
          <img
            src={thumb}
            alt=""
            loading="lazy"
            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
            onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
        )}
        {/* Video badge */}
        {type === 'video' && (
          <div className="absolute top-1.5 left-1.5 flex items-center gap-1 bg-black/70 rounded px-1.5 py-0.5 text-xs text-white">
            <Film size={10} />
            {duration != null ? formatDuration(duration) : 'video'}
          </div>
        )}
        {/* Score badge */}
        {scorePct != null && (
          <div className="absolute top-1.5 right-1.5 bg-indigo-600/80 rounded px-1.5 py-0.5 text-xs text-white font-mono">
            {scorePct}%
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-2 py-1.5 flex flex-col gap-1 min-h-[3rem]">
        {(date || place) && (
          <div className="text-xs text-zinc-500 dark:text-zinc-400 truncate">
            {place ?? formatDate(date)}
          </div>
        )}
        {faces && faces.length > 0 && <FaceStrip faces={faces} />}
      </div>
    </button>
  )
}
