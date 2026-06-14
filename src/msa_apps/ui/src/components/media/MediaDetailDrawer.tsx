import { useEffect, useRef, useState } from 'react'
import { X, MapPin, Calendar, Tag, Expand, Copy, Check } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { getMediaFaces, imageUrl, videoUrl } from '../../api/media'
import { trackOpen } from '../../api/search'
import { FaceStrip } from './FaceStrip'
import { cn } from '../../lib/utils'

interface DrawerItem {
  id: string
  type?: 'image' | 'video' | null
  date?: string | null
  place?: string | null
  gps_lat?: number | null
  gps_lon?: number | null
  score?: number | null
  raw_similarity_score?: number | null
  similarity_score?: number | null
  person_boost?: number | null
  person_multiplier?: number | null
  expansion_boost?: number | null
  expansion_multiplier?: number | null
  tags?: string[] | null
  why?: string | null
  timestamp?: number | null
  path?: string | null
  // People page: similarity score from face search (0–1)
  similarity?: number | null
}

interface MediaDetailDrawerProps {
  item: DrawerItem | null
  onClose: () => void
  searchId?: string | null  // present only when opened from a search → enables /track/open
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'long', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function PathRow({ path }: { path: string }) {
  const [copied, setCopied] = useState(false)
  const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  const filename = slash >= 0 ? path.slice(slash + 1) : path

  const copy = () => {
    navigator.clipboard.writeText(path).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }).catch(() => {})
  }

  return (
    <div className="group relative flex items-center gap-2">
      <span className="text-xs text-zinc-400 dark:text-zinc-500 truncate min-w-0 flex-1">{filename}</span>
      <button
        onClick={copy}
        title={path}
        className="shrink-0 opacity-0 group-hover:opacity-100 transition-opacity text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200"
      >
        {copied ? <Check size={13} className="text-emerald-500" /> : <Copy size={13} />}
      </button>
      {/* Full path on hover */}
      <div className="absolute bottom-full left-0 mb-1 hidden group-hover:block z-10 max-w-xs">
        <div className="bg-zinc-800 text-zinc-200 text-xs font-mono px-2 py-1.5 rounded shadow-lg break-all">
          {path}
        </div>
      </div>
    </div>
  )
}

export function MediaDetailDrawer({ item, onClose, searchId }: MediaDetailDrawerProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [imgLightbox, setImgLightbox] = useState(false)

  // Log the open as a relevance label when this item came from a search (best-effort).
  // Track once per opened item — a later searchId change while the same item stays open
  // (background refetch / a new search) must not re-post a duplicate, mis-attributed label.
  const trackedItemRef = useRef<string | null>(null)
  useEffect(() => {
    if (!item?.id) {
      trackedItemRef.current = null  // reset when the drawer closes
      return
    }
    if (searchId && trackedItemRef.current !== item.id) {
      trackedItemRef.current = item.id
      trackOpen(searchId, item.id)
    }
  }, [item?.id, searchId])

  const { data: facesData } = useQuery({
    queryKey: ['media-faces', item?.id],
    queryFn: () => getMediaFaces(item!.id),
    enabled: !!item,
    staleTime: 60_000,
  })

  // Reset lightbox when item changes
  useEffect(() => {
    setImgLightbox(false)
  }, [item?.id])

  // Seek video to timestamp when it loads
  useEffect(() => {
    if (item?.type === 'video' && item.timestamp != null && videoRef.current) {
      videoRef.current.currentTime = item.timestamp
    }
  }, [item])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (imgLightbox) setImgLightbox(false)
        else if (item) onClose()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose, item, imgLightbox])

  return (
    <>
      {/* Backdrop */}
      <div
        className={cn('fixed inset-0 bg-black/60 z-40 transition-opacity', item ? 'opacity-100' : 'opacity-0 pointer-events-none')}
        onClick={onClose}
      />

      {/* Drawer */}
      <div className={cn(
        'fixed top-0 right-0 h-full w-full max-w-lg bg-white dark:bg-zinc-900 z-50 flex flex-col',
        'shadow-2xl transition-transform duration-300',
        item ? 'translate-x-0' : 'translate-x-full',
      )}>
        {/* Header */}
        <div className="flex items-center justify-end px-4 py-3 border-b border-slate-200 dark:border-zinc-800">
          <button onClick={onClose} className="text-zinc-400 dark:text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-100">
            <X size={18} />
          </button>
        </div>

        {item && (
          <div className="flex-1 overflow-y-auto">
            {/* Media preview */}
            <div className="bg-slate-100 dark:bg-zinc-950 flex items-center justify-center relative group/preview">
              {item.type === 'video' ? (
                <video
                  ref={videoRef}
                  src={videoUrl(item.id)}
                  controls
                  className="w-full max-h-72 object-contain"
                />
              ) : (
                <>
                  <img
                    src={imageUrl(item.id)}
                    alt=""
                    className="w-full max-h-72 object-contain cursor-zoom-in"
                    onClick={() => setImgLightbox(true)}
                  />
                  <button
                    onClick={() => setImgLightbox(true)}
                    className="absolute top-2 right-2 p-1.5 rounded-lg bg-black/40 text-white opacity-0 group-hover/preview:opacity-100 transition-opacity hover:bg-black/70"
                    title="View full size"
                  >
                    <Expand size={15} />
                  </button>
                </>
              )}
            </div>

            {/* Metadata */}
            <div className="px-4 py-4 space-y-4">
              {/* Semantic search score */}
              {item.score != null && (() => {
                const pct = Math.min(100, Math.max(0, Math.round(item.score * 100)))
                return (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 bg-slate-200 dark:bg-zinc-700 rounded-full overflow-hidden">
                      <div className="h-full bg-indigo-500 rounded-full" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-xs text-zinc-500 dark:text-zinc-400 tabular-nums">{pct}% match</span>
                  </div>
                )
              })()}

              {/* Face similarity score */}
              {item.similarity != null && (() => {
                const pct = Math.min(100, Math.max(0, Math.round(item.similarity * 100)))
                return (
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 bg-slate-200 dark:bg-zinc-700 rounded-full overflow-hidden">
                      <div className="h-full bg-emerald-500 rounded-full" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-xs text-zinc-500 dark:text-zinc-400 tabular-nums">{pct}% face similarity</span>
                  </div>
                )
              })()}

              {(item.raw_similarity_score != null || item.person_boost != null || item.expansion_boost != null) && (
                <div className="rounded-lg bg-slate-50 dark:bg-zinc-800/60 px-3 py-2">
                  <div className="text-xs uppercase tracking-wider text-zinc-400 dark:text-zinc-500 mb-2">Score details</div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                    {item.raw_similarity_score != null && (
                      <>
                        <span className="text-zinc-500 dark:text-zinc-400">Raw similarity</span>
                        <span className="text-zinc-700 dark:text-zinc-200 tabular-nums text-right">{item.raw_similarity_score.toFixed(4)}</span>
                      </>
                    )}
                    {item.similarity_score != null && (
                      <>
                        <span className="text-zinc-500 dark:text-zinc-400">Similarity</span>
                        <span className="text-zinc-700 dark:text-zinc-200 tabular-nums text-right">{item.similarity_score.toFixed(4)}</span>
                      </>
                    )}
                    {item.person_boost != null && (
                      <>
                        <span className="text-zinc-500 dark:text-zinc-400">Person boost</span>
                        <span className="text-zinc-700 dark:text-zinc-200 tabular-nums text-right">{item.person_boost.toFixed(4)}</span>
                      </>
                    )}
                    {item.expansion_boost != null && (
                      <>
                        <span className="text-zinc-500 dark:text-zinc-400">Expansion boost</span>
                        <span className="text-zinc-700 dark:text-zinc-200 tabular-nums text-right">{item.expansion_boost.toFixed(4)}</span>
                      </>
                    )}
                  </div>
                </div>
              )}

              {/* File path — hover to reveal full path + copy */}
              {item.path && <PathRow path={item.path} />}

              {/* Date */}
              {item.date && (
                <div className="flex items-start gap-2 text-sm">
                  <Calendar size={14} className="text-zinc-400 dark:text-zinc-500 mt-0.5 shrink-0" />
                  <span className="text-zinc-700 dark:text-zinc-300">{formatDate(item.date)}</span>
                </div>
              )}

              {/* Place / GPS */}
              {(item.place || item.gps_lat != null) && (() => {
                const mapsUrl = item.gps_lat != null
                  ? `https://www.google.com/maps?q=${item.gps_lat},${item.gps_lon}`
                  : null
                const content = (
                  <>
                    <MapPin size={14} className={`mt-0.5 shrink-0 ${mapsUrl ? 'text-indigo-400 hover:text-indigo-600' : 'text-zinc-400 dark:text-zinc-500'}`} />
                    <span className="text-zinc-700 dark:text-zinc-300">
                      {item.place ?? `${item.gps_lat?.toFixed(4)}, ${item.gps_lon?.toFixed(4)}`}
                    </span>
                  </>
                )
                return mapsUrl ? (
                  <a href={mapsUrl} target="_blank" rel="noopener noreferrer"
                    className="flex items-start gap-2 text-sm hover:underline"
                    title="Open in Google Maps"
                  >
                    {content}
                  </a>
                ) : (
                  <div className="flex items-start gap-2 text-sm">{content}</div>
                )
              })()}

              {/* Tags */}
              {item.tags && item.tags.length > 0 && (
                <div className="flex items-start gap-2">
                  <Tag size={14} className="text-zinc-400 dark:text-zinc-500 mt-0.5 shrink-0" />
                  <div className="flex flex-wrap gap-1">
                    {item.tags.map(t => (
                      <span key={t} className="px-2 py-0.5 bg-slate-100 dark:bg-zinc-800 rounded text-xs text-zinc-600 dark:text-zinc-300">{t}</span>
                    ))}
                  </div>
                </div>
              )}


              {/* Faces */}
              {facesData && facesData.faces.length > 0 && (
                <div>
                  <div className="text-xs text-zinc-400 dark:text-zinc-500 uppercase tracking-wider mb-2">People</div>
                  <FaceStrip faces={facesData.faces} />
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Image lightbox — sibling of drawer so CSS transform doesn't trap fixed positioning */}
      {imgLightbox && item?.type !== 'video' && (
        <div
          className="fixed inset-0 z-[60] bg-black/90 flex items-center justify-center cursor-zoom-out"
          onClick={() => setImgLightbox(false)}
        >
          <img
            src={imageUrl(item!.id)}
            alt=""
            className="max-w-full max-h-full object-contain select-none"
            onClick={e => e.stopPropagation()}
          />
          <button
            onClick={() => setImgLightbox(false)}
            className="absolute top-4 right-4 p-2 rounded-full bg-white/10 text-white hover:bg-white/20 transition-colors"
          >
            <X size={20} />
          </button>
        </div>
      )}
    </>
  )
}
