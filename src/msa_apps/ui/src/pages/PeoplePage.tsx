import { useState, useRef, useEffect } from 'react'
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Tag, Search, X, Loader2, Check, ChevronDown, ArrowLeft, ChevronRight, Expand, UserCircle, AlertTriangle,
} from 'lucide-react'
import { useIndexerPhase } from '../hooks/useIndexerStatus'
import {
  getFaces, getPeople, labelFace, labelFacesBatch, unlabelFace, renamePerson, findSimilarFaces, getMediaInfo,
  type Face, type Person, type FaceSimilarMatch, type MediaInfo,
} from '../api/faces'
import { faceThumbnailUrl } from '../api/media'
import { apiUrl } from '../lib/apiBase'
import { MediaDetailDrawer } from '../components/media/MediaDetailDrawer'
import { cn } from '../lib/utils'

type Filter = 'all' | 'known' | 'unknown'
type ThumbSize = 'sm' | 'md' | 'lg' | 'xl'

const SIZE_CLASS: Record<ThumbSize, string> = {
  sm: 'w-16 aspect-[3/4]',
  md: 'w-24 aspect-[3/4]',
  lg: 'w-32 aspect-[3/4]',
  xl: 'w-40 aspect-[3/4]',
}

// Static max-w mapping avoids Tailwind JIT missing dynamically generated class names
const LABEL_MAX_W: Record<ThumbSize, string> = {
  sm: 'max-w-16',
  md: 'max-w-24',
  lg: 'max-w-32',
  xl: 'max-w-40',
}
const PAGE_SIZE = 200

function clampSimilarityThreshold(value: number): number {
  return Math.min(0.99, Math.max(0.20, value))
}

// ── Label Popover ─────────────────────────────────────────────────────────────

function LabelPopover({
  face,
  people,
  onAssign,
  onRemove,
  onClose,
}: {
  face: Face
  people: Person[]
  onAssign: (personId: string | null, name?: string) => void
  onRemove: () => void
  onClose: () => void
}) {
  const [input, setInput] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  useEffect(() => { inputRef.current?.focus() }, [])

  const filtered = input.trim()
    ? people.filter(p => p.name.toLowerCase().includes(input.toLowerCase()))
    : people
  const exactMatch = people.find(p => p.name.toLowerCase() === input.trim().toLowerCase())
  const showCreate = input.trim() && !exactMatch

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="bg-white dark:bg-zinc-900 rounded-xl shadow-2xl w-80 border border-slate-200 dark:border-zinc-700 flex flex-col overflow-hidden">
        <div className="flex items-center gap-3 px-4 py-3 border-b border-slate-200 dark:border-zinc-800">
          <img
            src={face.thumbnail ? apiUrl(face.thumbnail) : faceThumbnailUrl(face.face_id)}
            alt=""
            className="w-10 h-10 rounded-full object-cover bg-slate-200 dark:bg-zinc-800 shrink-0"
            onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
          />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium text-zinc-900 dark:text-zinc-100 truncate">
              {face.person_name ?? 'Unknown face'}
            </div>
            {face.age != null && (
              <div className="text-xs text-zinc-500">
                {`~${Math.round(face.age)}y`}{face.gender && ` · ${face.gender}`}
              </div>
            )}
          </div>
          <button onClick={onClose} className="text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-200 shrink-0">
            <X size={16} />
          </button>
        </div>

        <div className="px-3 py-2 border-b border-slate-200 dark:border-zinc-800">
          <input
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && showCreate) onAssign(null, input.trim()) }}
            placeholder="Type a name…"
            className="w-full text-sm bg-transparent outline-none text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400"
          />
        </div>

        <div className="overflow-y-auto max-h-52">
          {filtered.slice(0, 20).map(p => (
            <button
              key={p.person_id}
              onClick={() => onAssign(p.person_id)}
              className={cn(
                'w-full flex items-center gap-3 px-4 py-2 text-sm hover:bg-slate-100 dark:hover:bg-zinc-800 transition-colors text-left',
                face.person_id === p.person_id && 'bg-indigo-50 dark:bg-indigo-900/20 text-indigo-700 dark:text-indigo-300',
              )}
            >
              <img
                src={p.thumbnail ? apiUrl(p.thumbnail) : ''}
                alt=""
                className="w-7 h-7 rounded-full object-cover bg-slate-200 dark:bg-zinc-700 shrink-0"
                onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
              />
              <span className="flex-1 truncate">{p.name}</span>
              {face.person_id === p.person_id && <Check size={14} className="shrink-0 text-indigo-500" />}
            </button>
          ))}
          {showCreate && (
            <button
              onClick={() => onAssign(null, input.trim())}
              className="w-full flex items-center gap-3 px-4 py-2 text-sm hover:bg-slate-100 dark:hover:bg-zinc-800 transition-colors text-indigo-600 dark:text-indigo-400"
            >
              <span className="w-7 h-7 rounded-full bg-indigo-100 dark:bg-indigo-900/30 flex items-center justify-center shrink-0 text-indigo-600">+</span>
              <span>Create "{input.trim()}"</span>
            </button>
          )}
          {filtered.length === 0 && !showCreate && (
            <div className="px-4 py-3 text-sm text-zinc-400">No people found</div>
          )}
        </div>

        {face.person_id && (
          <div className="border-t border-slate-200 dark:border-zinc-800 px-4 py-2">
            <button onClick={onRemove} className="text-xs text-red-500 hover:text-red-600">Remove label</button>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Similar label card ────────────────────────────────────────────────────────

function SimilarLabelCard({
  match,
  isSelected,
  isKnown = false,
  thumbSize = 'md',
  mediaLoading,
  onClick,
  onMediaClick,
  onIndividualLabel,
}: {
  match: FaceSimilarMatch
  isSelected: boolean
  isKnown?: boolean
  thumbSize?: ThumbSize
  mediaLoading: boolean
  onClick: (e: React.MouseEvent) => void
  onMediaClick: (e: React.MouseEvent) => void
  onIndividualLabel: (e: React.MouseEvent) => void
}) {
  const safe = match.face_id.replace(/:/g, '_')
  const thumb = apiUrl(`/face_thumbnails/${safe}.jpg`)
  const szClass = SIZE_CLASS[thumbSize]
  const wClass = szClass.split(' ')[0]  // e.g. 'w-24'

  return (
    <div
      onClick={isKnown ? undefined : onClick}
      className={cn(
        'relative flex flex-col items-center gap-1 select-none',
        isKnown ? 'cursor-default' : 'cursor-pointer',
      )}
    >
      {/* Checkbox — unknown faces only */}
      {!isKnown && (
        <div className={cn(
          'absolute top-1 left-1 z-10 w-4 h-4 rounded border-2 flex items-center justify-center',
          isSelected ? 'bg-indigo-500 border-indigo-500' : 'bg-white/80 border-zinc-400',
        )}>
          {isSelected && <Check size={10} className="text-white" />}
        </div>
      )}

      {/* Thumbnail */}
      <div
        onClick={onMediaClick}
        className={cn(
          'relative rounded-xl overflow-hidden bg-slate-200 dark:bg-zinc-800 ring-2 transition-all group/thumb cursor-pointer',
          szClass,
          !isKnown && isSelected ? 'ring-indigo-500' : 'ring-transparent',
        )}
      >
        {mediaLoading ? (
          <div className="w-full h-full flex items-center justify-center">
            <Loader2 className="animate-spin text-zinc-400" size={16} />
          </div>
        ) : (
          <>
            <img
              src={thumb}
              alt={match.person_name ?? 'face'}
              className="w-full h-full object-cover"
              onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
            />
            <div className="absolute inset-0 bg-black/0 group-hover/thumb:bg-black/30 transition-colors flex items-center justify-center gap-2">
              <Expand size={14} className="text-white opacity-0 group-hover/thumb:opacity-100 transition-opacity" />
              {isKnown && (
                <button
                  onClick={e => { e.stopPropagation(); onIndividualLabel(e) }}
                  title="Label this face"
                  className="text-white opacity-0 group-hover/thumb:opacity-100 transition-opacity hover:text-indigo-300"
                >
                  <Tag size={14} />
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {/* Name + label icon row — unknown faces only */}
      {!isKnown && (
        <div className={cn('flex items-center justify-between gap-1 group/label', wClass)}>
          <span className="text-xs truncate text-zinc-700 dark:text-zinc-200 min-w-0">
            {match.person_name ?? <span className="italic text-zinc-400">Unknown</span>}
          </span>
          <button
            onClick={onIndividualLabel}
            title="Label this face"
            className="shrink-0 opacity-0 group-hover/label:opacity-100 transition-opacity text-zinc-400 hover:text-indigo-500"
          >
            <Tag size={11} />
          </button>
        </div>
      )}
    </div>
  )
}

// ── Similar Faces View ────────────────────────────────────────────────────────

function SimilarFacesView({
  sourceFace,
  people,
  backLabel,
  onBack,
}: {
  sourceFace: Face
  people: Person[]
  backLabel: string
  onBack: () => void
}) {
  const qc = useQueryClient()
  const [threshold, setThreshold] = useState(0.65)
  const [thumbSize, setThumbSize] = useState<ThumbSize>('md')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [knownCollapsed, setKnownCollapsed] = useState(false)
  const [knownShowAll, setKnownShowAll] = useState(false)
  const [belowCollapsed, setBelowCollapsed] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerInput, setPickerInput] = useState('')
  const lastClickRef = useRef<{ listKey: string; faceId: string } | null>(null)
  const [drawerItem, setDrawerItem] = useState<(MediaInfo & { similarity: number }) | null>(null)
  const [loadingMediaIds, setLoadingMediaIds] = useState<Set<string>>(new Set())
  const [inlineTarget, setInlineTarget] = useState<FaceSimilarMatch | null>(null)

  const handleThumbnailClick = async (m: FaceSimilarMatch, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!m.media_id) return
    const mid = m.media_id
    setLoadingMediaIds(s => { const n = new Set(s); n.add(mid); return n })
    try {
      const info = await getMediaInfo(mid)
      setDrawerItem({ ...info, similarity: m.score })
    } catch { /* ignore */ }
    finally { setLoadingMediaIds(s => { const n = new Set(s); n.delete(mid); return n }) }
  }

  const { data: matches, isLoading } = useQuery({
    queryKey: ['face-similar', sourceFace.face_id],
    queryFn: () => findSimilarFaces(sourceFace.face_id, 10000),
    staleTime: 60_000,
  })

  useEffect(() => {
    if (!matches) return
    const auto = new Set(matches.filter(m => m.score >= threshold && m.person_id === null).map(m => m.face_id))
    setSelected(auto)
  }, [matches, threshold])

  const bulkLabelMutation = useMutation({
    mutationFn: async ({ personId, name }: { personId: string | null; name?: string }) => {
      const faceIds = Array.from(selected)
      await labelFacesBatch(faceIds, personId ? { person_id: personId } : { name })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['faces'] })
      qc.invalidateQueries({ queryKey: ['people'] })
      qc.invalidateQueries({ queryKey: ['face-similar'] })
      setPickerOpen(false)
      setPickerInput('')
    },
  })

  const singleLabelMutation = useMutation({
    mutationFn: ({ faceId, personId, name }: { faceId: string; personId?: string; name?: string }) =>
      labelFace(faceId, personId ? { person_id: personId } : { name: name! }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['face-similar', sourceFace.face_id] })
      qc.invalidateQueries({ queryKey: ['faces'] })
      qc.invalidateQueries({ queryKey: ['people'] })
      setInlineTarget(null)
    },
  })

  const filteredPeople = pickerInput.trim()
    ? people.filter(p => p.name.toLowerCase().includes(pickerInput.toLowerCase()))
    : people
  const exactMatch = people.find(p => p.name.toLowerCase() === pickerInput.trim().toLowerCase())
  const showCreate = pickerInput.trim() && !exactMatch

  // Known: faces already labeled as THIS person (any score)
  const knownAll     = (matches ?? []).filter(m => sourceFace.person_id && m.person_id === sourceFace.person_id)
  // Unknown split by threshold
  const unknownAbove = (matches ?? []).filter(m => m.person_id === null && m.score >= threshold)
  const below        = (matches ?? []).filter(m => m.person_id === null && m.score >= 0.20 && m.score < threshold)

  const handleCardClick = (listKey: string, list: FaceSimilarMatch[], m: FaceSimilarMatch, e: React.MouseEvent) => {
    if (e.shiftKey && lastClickRef.current?.listKey === listKey) {
      const fromIdx = list.findIndex(x => x.face_id === lastClickRef.current!.faceId)
      const toIdx   = list.findIndex(x => x.face_id === m.face_id)
      if (fromIdx >= 0 && toIdx >= 0) {
        const [lo, hi] = fromIdx < toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx]
        const isSelecting = !selected.has(m.face_id)
        setSelected(prev => {
          const next = new Set(prev)
          for (let i = lo; i <= hi; i++) {
            isSelecting ? next.add(list[i].face_id) : next.delete(list[i].face_id)
          }
          return next
        })
        lastClickRef.current = { listKey, faceId: m.face_id }
        return
      }
    }
    setSelected(prev => {
      const next = new Set(prev)
      next.has(m.face_id) ? next.delete(m.face_id) : next.add(m.face_id)
      return next
    })
    lastClickRef.current = { listKey, faceId: m.face_id }
  }

  const selectSection   = (list: FaceSimilarMatch[]) => setSelected(prev => { const n = new Set(prev); list.forEach(m => n.add(m.face_id)); return n })
  const unselectSection = (list: FaceSimilarMatch[]) => setSelected(prev => { const n = new Set(prev); list.forEach(m => n.delete(m.face_id)); return n })
  const thresholdPercent = Math.round(threshold * 100)

  const SectionHeader = ({
    title, list, collapsed, onToggle, selectable = true,
  }: { title: string; list: FaceSimilarMatch[]; collapsed?: boolean; onToggle?: () => void; selectable?: boolean }) => (
    <div className="flex items-center gap-2 mb-3 mt-1">
      {onToggle && (
        <button onClick={onToggle} className="text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200">
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
      )}
      <span className="text-xs font-semibold uppercase tracking-wider text-zinc-400 dark:text-zinc-500">
        {title} <span className="font-normal">({list.length})</span>
      </span>
      {selectable && (
        <div className="flex items-center gap-1 ml-2">
          <button
            onClick={() => selectSection(list)}
            className="text-xs text-indigo-500 hover:text-indigo-700 dark:hover:text-indigo-300 px-1"
          >Select all</button>
          <span className="text-zinc-300 dark:text-zinc-600">·</span>
          <button
            onClick={() => unselectSection(list)}
            className="text-xs text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200 px-1"
          >Unselect all</button>
        </div>
      )}
      <div className="flex-1 h-px bg-slate-200 dark:bg-zinc-800 ml-1" />
      {selectable && list.some(m => selected.has(m.face_id)) && (
        <span className="text-xs text-indigo-400 shrink-0">
          {list.filter(m => selected.has(m.face_id)).length} selected
        </span>
      )}
    </div>
  )

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-4 px-5 py-3 border-b border-slate-200 dark:border-zinc-800 shrink-0 flex-wrap gap-y-2">
        <button
          onClick={onBack}
          className="flex items-center gap-1.5 text-sm text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors shrink-0"
        >
          <ArrowLeft size={16} />
          {backLabel}
        </button>

        <div className="flex items-center gap-2 shrink-0">
          <img
            src={sourceFace.thumbnail ? apiUrl(sourceFace.thumbnail) : faceThumbnailUrl(sourceFace.face_id)}
            alt=""
            className="w-8 h-8 rounded-full object-cover bg-slate-200 dark:bg-zinc-800"
            onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
          />
          <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            Similar to {sourceFace.person_name ?? 'this face'}
          </span>
          {matches && (
            <span className="text-xs text-zinc-400">{selected.size} selected · {matches.length} total</span>
          )}
        </div>

        {/* Threshold range (fixed 20% left, sliding right) */}
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-zinc-400 whitespace-nowrap">20%</span>
          <button
            type="button"
            aria-label="Decrease similarity threshold"
            onClick={() => setThreshold(current => clampSimilarityThreshold(current - 0.01))}
            disabled={thresholdPercent <= 20}
            className="w-7 h-7 rounded-md border border-slate-200 dark:border-zinc-700 text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:border-indigo-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            -
          </button>
          <input
            type="range" min={20} max={99} step={1}
            value={thresholdPercent}
            onChange={e => setThreshold(clampSimilarityThreshold(Number(e.target.value) / 100))}
            className="w-28 accent-indigo-500"
          />
          <button
            type="button"
            aria-label="Increase similarity threshold"
            onClick={() => setThreshold(current => clampSimilarityThreshold(current + 0.01))}
            disabled={thresholdPercent >= 99}
            className="w-7 h-7 rounded-md border border-slate-200 dark:border-zinc-700 text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:border-indigo-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            +
          </button>
          <span className="text-xs font-mono font-medium text-zinc-700 dark:text-zinc-200 whitespace-nowrap w-8">
            {thresholdPercent}%
          </span>
        </div>

        {/* Size buttons */}
        <div className="flex items-center gap-1">
          {([['sm','S'],['md','M'],['lg','L'],['xl','XL']] as [ThumbSize,string][]).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setThumbSize(key)}
              className={cn(
                'px-1.5 h-7 text-xs rounded transition-colors font-medium',
                thumbSize === key
                  ? 'bg-slate-200 dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100'
                  : 'text-zinc-400 hover:bg-slate-100 dark:hover:bg-zinc-800',
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Label action */}
        <div className="ml-auto relative">
          {sourceFace.person_id ? (
            <button
              onClick={() => bulkLabelMutation.mutate({ personId: sourceFace.person_id! })}
              disabled={selected.size === 0 || bulkLabelMutation.isPending}
              className="flex items-center gap-1.5 text-sm bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-3 py-1.5 rounded-lg transition-colors"
            >
              <Tag size={13} />
              {bulkLabelMutation.isPending
                ? 'Labeling…'
                : `Label ${selected.size > 0 ? selected.size : ''} faces as ${sourceFace.person_name}`}
            </button>
          ) : (
            <>
              <button
                onClick={() => setPickerOpen(o => !o)}
                disabled={selected.size === 0 || bulkLabelMutation.isPending}
                className="flex items-center gap-1.5 text-sm bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-3 py-1.5 rounded-lg transition-colors"
              >
                <Tag size={13} />
                Label {selected.size > 0 ? `${selected.size} faces` : 'selected'} as…
                <ChevronDown size={13} />
              </button>

              {pickerOpen && (
                <div className="absolute top-full mt-1 right-0 w-64 bg-white dark:bg-zinc-900 rounded-lg shadow-xl border border-slate-200 dark:border-zinc-700 z-50 overflow-hidden">
                  <div className="px-3 py-2 border-b border-slate-200 dark:border-zinc-800">
                    <input
                      autoFocus
                      value={pickerInput}
                      onChange={e => setPickerInput(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter' && showCreate) bulkLabelMutation.mutate({ personId: null, name: pickerInput.trim() }) }}
                      placeholder="Type a name…"
                      className="w-full text-sm bg-transparent outline-none text-zinc-900 dark:text-zinc-100 placeholder:text-zinc-400"
                    />
                  </div>
                  <div className="max-h-48 overflow-y-auto">
                    {filteredPeople.slice(0, 10).map(p => (
                      <button
                        key={p.person_id}
                        onClick={() => bulkLabelMutation.mutate({ personId: p.person_id })}
                        className="w-full text-left px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 hover:bg-slate-100 dark:hover:bg-zinc-800 flex items-center gap-2"
                      >
                        <img
                          src={p.thumbnail ? apiUrl(p.thumbnail) : ''}
                          alt=""
                          className="w-6 h-6 rounded-full object-cover bg-slate-200 dark:bg-zinc-700 shrink-0"
                          onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
                        />
                        {p.name}
                      </button>
                    ))}
                    {showCreate && (
                      <button
                        onClick={() => bulkLabelMutation.mutate({ personId: null, name: pickerInput.trim() })}
                        className="w-full text-left px-3 py-2 text-sm text-indigo-600 dark:text-indigo-400 hover:bg-slate-100 dark:hover:bg-zinc-800"
                      >
                        + Create "{pickerInput.trim()}"
                      </button>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Grid */}
      <div className="flex-1 overflow-y-auto px-5 py-5">
        {isLoading && (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="animate-spin text-zinc-400" size={28} />
          </div>
        )}
        {matches && matches.length === 0 && (
          <div className="text-sm text-zinc-400 text-center py-16">No similar faces found</div>
        )}

        {matches && matches.length > 0 && (
          <>
            {/* Known section — all faces labeled as this person */}
            {knownAll.length > 0 && (
              <div className="mb-6">
                <SectionHeader
                  title={sourceFace.person_name ?? 'Known'}
                  list={knownAll}
                  collapsed={knownCollapsed}
                  onToggle={() => setKnownCollapsed(v => !v)}
                  selectable={false}
                />
                {!knownCollapsed && (
                  <div className="flex flex-wrap gap-4">
                    {(knownShowAll ? knownAll : knownAll.slice(0, 20)).map(m => (
                      <SimilarLabelCard
                        key={m.face_id}
                        match={m}
                        isSelected={false}
                        isKnown
                        thumbSize={thumbSize}
                        mediaLoading={loadingMediaIds.has(m.media_id ?? '')}
                        onClick={() => {}}
                        onMediaClick={e => handleThumbnailClick(m, e)}
                        onIndividualLabel={e => { e.stopPropagation(); setInlineTarget(m) }}
                      />
                    ))}
                    {knownAll.length > 20 && (
                      <button
                        onClick={() => setKnownShowAll(v => !v)}
                        className={cn(
                          SIZE_CLASS[thumbSize],
                          'rounded-xl border-2 border-dashed border-slate-300 dark:border-zinc-700 flex flex-col items-center justify-center gap-1 text-zinc-400 hover:text-indigo-500 hover:border-indigo-400 transition-colors text-xs font-medium',
                        )}
                      >
                        {knownShowAll
                          ? <>Show fewer</>
                          : <><span className="text-sm font-semibold">+{knownAll.length - 20}</span><span>Show all</span></>}
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* Unknown section — open, auto-selected */}
            {unknownAbove.length > 0 && (
              <div className="mb-6">
                <SectionHeader title="Unknown" list={unknownAbove} />
                <div className="flex flex-wrap gap-4">
                  {unknownAbove.map(m => (
                    <SimilarLabelCard
                      key={m.face_id}
                      match={m}
                      isSelected={selected.has(m.face_id)}
                      thumbSize={thumbSize}
                      mediaLoading={loadingMediaIds.has(m.media_id ?? '')}
                      onClick={e => handleCardClick('unknown', unknownAbove, m, e)}
                      onMediaClick={e => handleThumbnailClick(m, e)}
                      onIndividualLabel={e => { e.stopPropagation(); setInlineTarget(m) }}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Below threshold — unlabeled faces below threshold */}
            {below.length > 0 && (
              <div>
                <SectionHeader
                  title="Below threshold"
                  list={below}
                  collapsed={belowCollapsed}
                  onToggle={() => setBelowCollapsed(v => !v)}
                />
                {!belowCollapsed && (
                  <div className="flex flex-wrap gap-4">
                    {below.map(m => (
                      <SimilarLabelCard
                        key={m.face_id}
                        match={m}
                        isSelected={selected.has(m.face_id)}
                        thumbSize={thumbSize}
                        mediaLoading={loadingMediaIds.has(m.media_id ?? '')}
                        onClick={e => handleCardClick('below', below, m, e)}
                        onMediaClick={e => handleThumbnailClick(m, e)}
                        onIndividualLabel={e => { e.stopPropagation(); setInlineTarget(m) }}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <MediaDetailDrawer
        item={drawerItem ? {
          id: drawerItem.id, type: drawerItem.type, date: drawerItem.date,
          place: drawerItem.place, gps_lat: drawerItem.gps_lat, gps_lon: drawerItem.gps_lon,
          path: drawerItem.path, similarity: drawerItem.similarity,
        } : null}
        onClose={() => setDrawerItem(null)}
      />

      {inlineTarget && (
        <LabelPopover
          face={{
            face_id: inlineTarget.face_id,
            media_id: inlineTarget.media_id,
            path: inlineTarget.path,
            gender: null, age: null,
            person_id: inlineTarget.person_id,
            person_name: inlineTarget.person_name,
            thumbnail: `/face_thumbnails/${inlineTarget.face_id.replace(/:/g, '_')}.jpg`,
          }}
          people={people}
          onAssign={(personId, name) =>
            singleLabelMutation.mutate({ faceId: inlineTarget.face_id, personId: personId ?? undefined, name })}
          onRemove={() =>
            unlabelFace(inlineTarget.face_id).then(() => {
              qc.invalidateQueries({ queryKey: ['face-similar', sourceFace.face_id] })
              qc.invalidateQueries({ queryKey: ['faces'] })
              qc.invalidateQueries({ queryKey: ['people'] })
              setInlineTarget(null)
            })}
          onClose={() => setInlineTarget(null)}
        />
      )}
    </div>
  )
}

// ── Face card (browse grid) ───────────────────────────────────────────────────

function FaceCard({
  face,
  size,
  expandLoading,
  onLabel,
  onSimilar,
  onExpand,
}: {
  face: Face
  size: ThumbSize
  expandLoading: boolean
  onLabel: (face: Face) => void
  onSimilar: (face: Face) => void
  onExpand: (face: Face) => void
}) {
  const thumb = face.thumbnail ? apiUrl(face.thumbnail) : faceThumbnailUrl(face.face_id)
  const canExpand = face.media_id != null

  return (
    <div className="group flex flex-col items-center gap-1 select-none">
      <div
        onClick={canExpand ? () => onExpand(face) : undefined}
        onKeyDown={canExpand ? (e) => {
          // Only react when focus is on the container itself — not on the
          // nested Label / Find-similar buttons (their Enter/Space bubbles).
          if (e.target !== e.currentTarget) return
          // Held keys (auto-repeat) would otherwise fire one expand per repeat
          // tick — debounce at the keyboard layer to match button semantics.
          if (e.repeat) return
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onExpand(face)
          }
        } : undefined}
        role={canExpand ? 'button' : undefined}
        tabIndex={canExpand ? 0 : undefined}
        aria-label={canExpand ? 'Expand face' : undefined}
        className={cn(
          'relative rounded-xl overflow-hidden bg-slate-200 dark:bg-zinc-800',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500',
          SIZE_CLASS[size],
          canExpand && 'cursor-pointer',
        )}
      >
        <img
          src={thumb}
          alt={face.person_name ?? 'face'}
          className="w-full h-full object-cover"
          onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
        />
        {canExpand && (
          <div className="pointer-events-none absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center">
            {expandLoading
              ? <Loader2 className="animate-spin text-white" size={16} />
              : <Expand size={16} className="text-white opacity-0 group-hover:opacity-100 transition-opacity" />}
          </div>
        )}
        <div className="absolute inset-x-0 bottom-0 flex opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            title="Label"
            onClick={e => { e.stopPropagation(); onLabel(face) }}
            className="flex-1 py-1.5 bg-black/70 text-white hover:bg-black/90 flex items-center justify-center"
          >
            <Tag size={12} />
          </button>
          <button
            title="Find similar"
            onClick={e => { e.stopPropagation(); onSimilar(face) }}
            className="flex-1 py-1.5 bg-black/70 text-white hover:bg-black/90 flex items-center justify-center border-l border-white/20"
          >
            <Search size={12} />
          </button>
        </div>
      </div>
      <div className={cn('text-xs truncate text-center', LABEL_MAX_W[size])}>
        {face.person_name
          ? <span className="text-zinc-700 dark:text-zinc-200">{face.person_name}</span>
          : <span className="text-zinc-400 italic">Unknown</span>}
      </div>
    </div>
  )
}

// ── Browse view (face grid with infinite scroll) ──────────────────────────────

function BrowseView({
  people,
  onSimilar,
  onBack,
}: {
  people: Person[]
  onSimilar: (face: Face) => void
  onBack: () => void
}) {
  const qc = useQueryClient()
  const [filter, setFilter] = useState<Filter>('unknown')
  const [thumbSize, setThumbSize] = useState<ThumbSize>('md')
  const [labelTarget, setLabelTarget] = useState<Face | null>(null)
  const [drawerItem, setDrawerItem] = useState<MediaInfo | null>(null)
  // Track only the latest expand-target's media id (drawer is last-click-wins
  // anyway). Using a Set risked clearing the spinner too early when the same
  // face was clicked twice in succession: the first request's finally would
  // remove the id while the second was still in flight.
  const [loadingMediaId, setLoadingMediaId] = useState<string | null>(null)
  const loaderRef = useRef<HTMLDivElement>(null)
  // Monotonic token so only the latest expand-click's response wins if the
  // user clicks several faces in quick succession — and so an in-flight
  // request can be invalidated when the drawer is explicitly closed.
  const expandReqRef = useRef(0)

  const handleExpand = async (face: Face) => {
    if (!face.media_id) return
    const mid = face.media_id
    const reqId = ++expandReqRef.current
    setLoadingMediaId(mid)
    try {
      const info = await getMediaInfo(mid)
      if (reqId === expandReqRef.current) setDrawerItem(info)
    } catch { /* ignore */ }
    finally {
      if (reqId === expandReqRef.current) setLoadingMediaId(null)
    }
  }

  // Closing the drawer must cancel any in-flight expand request, otherwise a
  // late /media/{id}/info response would reopen the drawer after the user
  // dismissed it.
  const closeDrawer = () => {
    expandReqRef.current++
    setDrawerItem(null)
  }

  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading } = useInfiniteQuery({
    queryKey: ['faces', filter],
    queryFn: ({ pageParam = 0 }) => getFaces({ labeled: filter, limit: PAGE_SIZE, offset: pageParam as number }),
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((s, p) => s + p.faces.length, 0)
      return last.faces.length === PAGE_SIZE ? loaded : undefined
    },
    initialPageParam: 0,
    staleTime: 30_000,
  })

  const labelMutation = useMutation({
    mutationFn: ({ faceId, personId, name }: { faceId: string; personId: string | null; name?: string }) =>
      labelFace(faceId, personId ? { person_id: personId } : { name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['faces'] })
      qc.invalidateQueries({ queryKey: ['people'] })
      qc.invalidateQueries({ queryKey: ['face-similar'] })
      setLabelTarget(null)
    },
  })

  const unlabelMutation = useMutation({
    mutationFn: (faceId: string) => unlabelFace(faceId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['faces'] })
      qc.invalidateQueries({ queryKey: ['people'] })
      qc.invalidateQueries({ queryKey: ['face-similar'] })
      setLabelTarget(null)
    },
  })

  useEffect(() => {
    const el = loaderRef.current
    if (!el) return
    const obs = new IntersectionObserver(
      entries => { if (entries[0].isIntersecting && hasNextPage && !isFetchingNextPage) fetchNextPage() },
      { rootMargin: '200px' },
    )
    obs.observe(el)
    return () => obs.disconnect()
  }, [fetchNextPage, hasNextPage, isFetchingNextPage])

  const allFaces = data?.pages.flatMap(p => p.faces) ?? []

  const FILTERS: { key: Filter; label: string }[] = [
    { key: 'all', label: 'All' },
    { key: 'known', label: 'Known' },
    { key: 'unknown', label: 'Unknown' },
  ]
  const SIZES: { key: ThumbSize; label: string }[] = [
    { key: 'sm', label: 'S' },
    { key: 'md', label: 'M' },
    { key: 'lg', label: 'L' },
    { key: 'xl', label: 'XL' },
  ]

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200 dark:border-zinc-800 shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={onBack}
            className="flex items-center gap-1.5 text-sm text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
          >
            <ArrowLeft size={16} />
            People
          </button>
          <div className="w-px h-4 bg-slate-200 dark:bg-zinc-700" />
          <div className="flex items-center gap-1">
            {FILTERS.map(f => (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className={cn(
                  'px-3 py-1 text-sm rounded-full transition-colors',
                  filter === f.key
                    ? 'bg-indigo-600 text-white'
                    : 'text-zinc-600 dark:text-zinc-400 hover:bg-slate-100 dark:hover:bg-zinc-800',
                )}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-1">
          {SIZES.map(s => (
            <button
              key={s.key}
              onClick={() => setThumbSize(s.key)}
              className={cn(
                'w-7 h-7 text-xs rounded transition-colors font-medium',
                thumbSize === s.key
                  ? 'bg-slate-200 dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100'
                  : 'text-zinc-400 hover:bg-slate-100 dark:hover:bg-zinc-800',
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>

      {/* Grid */}
      <div className="flex-1 overflow-y-auto px-5 py-5">
        {isLoading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="animate-spin text-zinc-400" size={28} />
          </div>
        ) : allFaces.length === 0 ? (
          <div className="text-sm text-zinc-400 text-center py-16">No faces found</div>
        ) : (
          <div className="flex flex-wrap gap-4">
            {allFaces.map(face => (
              <FaceCard
                key={face.face_id}
                face={face}
                size={thumbSize}
                expandLoading={face.media_id != null && face.media_id === loadingMediaId}
                onLabel={setLabelTarget}
                onSimilar={onSimilar}
                onExpand={handleExpand}
              />
            ))}
          </div>
        )}
        <div ref={loaderRef} className="py-4 flex justify-center">
          {isFetchingNextPage && <Loader2 className="animate-spin text-zinc-400" size={20} />}
        </div>
      </div>

      {labelTarget && (
        <LabelPopover
          face={labelTarget}
          people={people}
          onAssign={(personId, name) => labelMutation.mutate({ faceId: labelTarget.face_id, personId, name })}
          onRemove={() => unlabelMutation.mutate(labelTarget.face_id)}
          onClose={() => setLabelTarget(null)}
        />
      )}

      <MediaDetailDrawer item={drawerItem} onClose={closeDrawer} />
    </div>
  )
}

// ── Person card (overview grid) ───────────────────────────────────────────────

function PersonCard({
  person,
  size = 'md',
  onSelect,
  onRename,
}: {
  person: Person
  size?: ThumbSize
  onSelect: (face: Face) => void
  onRename: (personId: string, newName: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [nameValue, setNameValue] = useState(person.name)
  const wClass = SIZE_CLASS[size].split(' ')[0]

  const commitRename = () => {
    const trimmed = nameValue.trim()
    if (trimmed && trimmed !== person.name) onRename(person.person_id, trimmed)
    else setNameValue(person.name)
    setEditing(false)
  }

  const handleSelect = () => {
    if (!person.thumbnail) return
    const faceId = person.thumbnail
      .replace('/face_thumbnails/', '')
      .replace('.jpg', '')
      .replace(/_/g, ':')
    onSelect({
      face_id: faceId,
      media_id: null,
      path: null,
      gender: null,
      age: null,
      person_id: person.person_id,
      person_name: person.name,
      thumbnail: person.thumbnail,
    })
  }

  return (
    <div className="group flex flex-col items-center gap-2 select-none">
      <button
        onClick={handleSelect}
        disabled={!person.thumbnail}
        className={cn(
          'relative rounded-xl overflow-hidden bg-slate-200 dark:bg-zinc-800 ring-2 ring-transparent group-hover:ring-indigo-400 transition-all disabled:opacity-50 disabled:cursor-default',
          SIZE_CLASS[size],
        )}
      >
        {person.thumbnail ? (
          <img
            src={person.thumbnail ? apiUrl(person.thumbnail) : undefined}
            alt={person.name}
            className="w-full h-full object-cover"
            onError={e => { (e.target as HTMLImageElement).style.visibility = 'hidden' }}
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-3xl text-zinc-400 font-light">
            {person.name.charAt(0).toUpperCase()}
          </div>
        )}
        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/30 transition-colors flex items-center justify-center">
          <Search size={20} className="text-white opacity-0 group-hover:opacity-100 transition-opacity" />
        </div>
      </button>

      {editing ? (
        <input
          value={nameValue}
          onChange={e => setNameValue(e.target.value)}
          onBlur={commitRename}
          onKeyDown={e => {
            if (e.key === 'Enter') commitRename()
            if (e.key === 'Escape') { setNameValue(person.name); setEditing(false) }
          }}
          autoFocus
          className={cn('text-xs text-center bg-white dark:bg-zinc-800 border border-indigo-400 rounded px-1 py-0.5 outline-none text-zinc-900 dark:text-zinc-100', wClass)}
        />
      ) : (
        <button
          onClick={() => setEditing(true)}
          title="Click to rename"
          className={cn('text-sm font-medium text-zinc-800 dark:text-zinc-100 hover:text-indigo-600 dark:hover:text-indigo-400 truncate text-center', LABEL_MAX_W[size])}
        >
          {person.name}
        </button>
      )}

      <div className="text-xs text-zinc-400 dark:text-zinc-500 -mt-1">
        {person.face_count ?? 0} face{person.face_count !== 1 ? 's' : ''}
      </div>
    </div>
  )
}

// ── Overview view ─────────────────────────────────────────────────────────────

function OverviewView({
  people,
  isLoading,
  unknownCount,
  onSelectPerson,
  onRename,
  onBrowseUnknown,
}: {
  people: Person[]
  isLoading: boolean
  unknownCount: number
  onSelectPerson: (face: Face) => void
  onRename: (personId: string, name: string) => void
  onBrowseUnknown: () => void
}) {
  const [thumbSize, setThumbSize] = useState<ThumbSize>('md')

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-5 py-3 border-b border-slate-200 dark:border-zinc-800 shrink-0">
        <h1 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
          People
          {people.length > 0 && (
            <span className="ml-2 text-sm font-normal text-zinc-400">({people.length})</span>
          )}
        </h1>
        <div className="flex items-center gap-1">
          {([['sm','S'],['md','M'],['lg','L'],['xl','XL']] as [ThumbSize,string][]).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setThumbSize(key)}
              className={cn(
                'px-1.5 h-7 text-xs rounded transition-colors font-medium',
                thumbSize === key
                  ? 'bg-slate-200 dark:bg-zinc-700 text-zinc-900 dark:text-zinc-100'
                  : 'text-zinc-400 hover:bg-slate-100 dark:hover:bg-zinc-800',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-5 py-6">
        {isLoading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="animate-spin text-zinc-400" size={28} />
          </div>
        ) : (
          <div className="flex flex-wrap gap-8">
            {/* Unknown faces entry card */}
            <div className="flex flex-col items-center gap-2 select-none">
              <button
                onClick={onBrowseUnknown}
                className={cn(
                  'group relative rounded-xl overflow-hidden bg-slate-100 dark:bg-zinc-800 ring-2 ring-transparent hover:ring-indigo-400 transition-all border-2 border-dashed border-slate-300 dark:border-zinc-700 flex items-center justify-center',
                  SIZE_CLASS[thumbSize],
                )}
              >
                <UserCircle size={40} className="text-zinc-400 group-hover:text-indigo-400 transition-colors" />
              </button>
              <span className="text-sm font-medium text-zinc-500 dark:text-zinc-400">Unknown faces</span>
              <div className="text-xs text-zinc-400 dark:text-zinc-500 -mt-1">
                {unknownCount} face{unknownCount !== 1 ? 's' : ''}
              </div>
            </div>

            {/* Known people */}
            {people.map(person => (
              <PersonCard
                key={person.person_id}
                person={person}
                size={thumbSize}
                onSelect={onSelectPerson}
                onRename={onRename}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export function PeoplePage() {
  const qc = useQueryClient()
  const { running: indexerRunning, phase: indexerPhase } = useIndexerPhase()
  const [mode, setMode] = useState<'overview' | 'browse' | 'similar'>('overview')
  const [similarSource, setSimilarSource] = useState<Face | null>(null)
  const [backTo, setBackTo] = useState<'overview' | 'browse'>('overview')

  const { data: peopleData, isLoading: peopleLoading } = useQuery({
    queryKey: ['people'],
    queryFn: getPeople,
    staleTime: 30_000,
  })
  const people = peopleData?.people ?? []
  const { data: unknownData } = useQuery({
    queryKey: ['faces', 'unknown-count'],
    queryFn: () => getFaces({ labeled: 'unknown', limit: 1, offset: 0 }),
    staleTime: 30_000,
  })
  const unknownCount = unknownData?.count ?? 0

  const renameMutation = useMutation({
    mutationFn: ({ personId, name }: { personId: string; name: string }) => renamePerson(personId, name),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['people'] }),
  })

  const enterSimilar = (face: Face, from: 'overview' | 'browse') => {
    setSimilarSource(face)
    setBackTo(from)
    setMode('similar')
  }

  const similarBanner = indexerRunning && (
    <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 text-amber-800 dark:text-amber-300 text-sm mb-4">
      <AlertTriangle size={15} className="shrink-0" />
      {indexerPhase === 'exporting'
        ? 'Finalizing index — face search resumes shortly.'
        : 'Indexing in progress — face results reflect your library before this run.'}
    </div>
  )

  if (mode === 'similar' && similarSource) {
    return (
      <>
        {similarBanner}
        <SimilarFacesView
          sourceFace={similarSource}
          people={people}
          backLabel={backTo === 'browse' ? 'Unknown Faces' : 'People'}
          onBack={() => setMode(backTo)}
        />
      </>
    )
  }

  if (mode === 'browse') {
    return (
      <BrowseView
        people={people}
        onSimilar={face => enterSimilar(face, 'browse')}
        onBack={() => setMode('overview')}
      />
    )
  }

  return (
    <>
      <OverviewView
        people={people}
        isLoading={peopleLoading}
        unknownCount={unknownCount}
        onSelectPerson={face => enterSimilar(face, 'overview')}
        onRename={(personId, name) => renameMutation.mutate({ personId, name })}
        onBrowseUnknown={() => setMode('browse')}
      />
    </>
  )
}
