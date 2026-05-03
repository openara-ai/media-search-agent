import { useEffect, useState } from 'react'
import type { SearchFilters } from '../../api/types'

interface FilterPanelProps {
  filters: SearchFilters
  onChange: (f: SearchFilters) => void
  minScore: number
  onMinScoreChange: (v: number) => void
}

type DatePreset = 'week' | 'month' | 'year' | 'custom'

function localDate(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function presetDates(key: DatePreset): { date_from: string; date_to: string } {
  const now = new Date()
  const to = localDate(now)
  if (key === 'week')  { const f = new Date(now); f.setDate(f.getDate() - 7);           return { date_from: localDate(f), date_to: to } }
  if (key === 'month') { const f = new Date(now); f.setMonth(f.getMonth() - 1);          return { date_from: localDate(f), date_to: to } }
  if (key === 'year')  { const f = new Date(now); f.setFullYear(f.getFullYear() - 1);    return { date_from: localDate(f), date_to: to } }
  return { date_from: '', date_to: to }
}

function inferPreset(date_from?: string, date_to?: string): DatePreset | null {
  if (!date_from && !date_to) return null
  for (const key of ['week', 'month', 'year'] as const) {
    const p = presetDates(key)
    if (date_from === p.date_from && date_to === p.date_to) return key
  }
  return 'custom'
}

function Toggle({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-0.5 rounded text-xs transition-colors ${
        active
          ? 'bg-indigo-600 text-white'
          : 'bg-slate-100 dark:bg-zinc-800 text-zinc-600 dark:text-zinc-300 hover:bg-slate-200 dark:hover:bg-zinc-700'
      }`}
    >
      {label}
    </button>
  )
}

export function FilterPanel({ filters, onChange, minScore, onMinScoreChange }: FilterPanelProps) {
  const [datePreset, setDatePreset] = useState<DatePreset | null>(
    () => inferPreset(filters.date_from, filters.date_to)
  )
  const set = (patch: Partial<SearchFilters>) => onChange({ ...filters, ...patch })

  // Sync dropdown when parent changes dates externally (rehydration, external clear, URL state)
  useEffect(() => {
    const derived = inferPreset(filters.date_from, filters.date_to)
    if (derived !== null) {
      setDatePreset(derived)
    } else if (!filters.date_from && !filters.date_to) {
      setDatePreset(null)
    }
    // If derived is null but a custom date is being typed, keep local 'custom' state
  }, [filters.date_from, filters.date_to])

  const applyPreset = (key: DatePreset) => {
    setDatePreset(key)
    if (key !== 'custom') {
      const { date_from, date_to } = presetDates(key)
      set({ date_from, date_to })
    }
    // For 'custom', keep existing dates in filters — user edits the date inputs directly
  }

  const clearDate = () => { setDatePreset(null); set({ date_from: undefined, date_to: undefined }) }

  const toggleMediaType = (t: 'image' | 'video') =>
    set({ media_type: filters.media_type === t ? null : t })

  return (
    <aside className="w-52 shrink-0 flex flex-col gap-5 text-sm">
      {/* Media type */}
      <div>
        <div className="text-xs text-zinc-400 dark:text-zinc-500 uppercase tracking-wider mb-2">Media type</div>
        <div className="flex gap-2 flex-wrap">
          <Toggle label="Photos" active={filters.media_type === 'image'} onClick={() => toggleMediaType('image')} />
          <Toggle label="Videos" active={filters.media_type === 'video'} onClick={() => toggleMediaType('video')} />
        </div>
      </div>

      {/* Date range */}
      <div>
        <div className="text-xs text-zinc-400 dark:text-zinc-500 uppercase tracking-wider mb-2">Date range</div>
        <div className="flex flex-col gap-1.5">
          <select
            value={datePreset ?? ''}
            onChange={e => {
              const val = e.target.value as DatePreset | ''
              if (!val) { clearDate() } else { applyPreset(val) }
            }}
            className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1 text-zinc-700 dark:text-zinc-200 text-xs w-full"
          >
            <option value="">Any time</option>
            <option value="week">Past week</option>
            <option value="month">Past month</option>
            <option value="year">Past year</option>
            <option value="custom">Custom…</option>
          </select>
          {datePreset === 'custom' && (
            <div className="flex flex-col gap-1.5">
              <input
                type="date"
                value={filters.date_from ?? ''}
                onChange={e => set({ date_from: e.target.value || undefined })}
                className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1 text-zinc-700 dark:text-zinc-200 text-xs w-full"
              />
              <input
                type="date"
                value={filters.date_to ?? ''}
                onChange={e => set({ date_to: e.target.value || undefined })}
                className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1 text-zinc-700 dark:text-zinc-200 text-xs w-full"
              />
            </div>
          )}
        </div>
      </div>

      {/* Place */}
      <div>
        <div className="text-xs text-zinc-400 dark:text-zinc-500 uppercase tracking-wider mb-2">Place</div>
        <input
          type="text"
          placeholder="e.g. California"
          value={filters.place?.[0] ?? ''}
          onChange={e => set({ place: e.target.value ? [e.target.value] : undefined })}
          className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1 text-zinc-700 dark:text-zinc-200 text-xs w-full placeholder-zinc-400 dark:placeholder-zinc-600"
        />
      </div>

      {/* Min score */}
      <div>
        <div className="text-xs text-zinc-400 dark:text-zinc-500 uppercase tracking-wider mb-2">Min score</div>
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-zinc-400 whitespace-nowrap">0%</span>
          <button
            type="button"
            aria-label="Decrease min score"
            onClick={() => onMinScoreChange(Math.max(0, minScore - 1))}
            disabled={minScore <= 0}
            className="w-7 h-7 rounded-md border border-slate-200 dark:border-zinc-700 text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:border-indigo-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            -
          </button>
          <input
            type="range"
            min={0}
            max={100}
            step={1}
            value={minScore}
            onChange={e => onMinScoreChange(Number(e.target.value))}
            className="w-full accent-indigo-500"
          />
          <button
            type="button"
            aria-label="Increase min score"
            onClick={() => onMinScoreChange(Math.min(100, minScore + 1))}
            disabled={minScore >= 100}
            className="w-7 h-7 rounded-md border border-slate-200 dark:border-zinc-700 text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:border-indigo-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            +
          </button>
          <span className="text-xs font-mono font-medium text-zinc-700 dark:text-zinc-200 whitespace-nowrap w-8">
            {minScore}%
          </span>
        </div>
      </div>

      {/* Clear */}
      {(Object.values(filters).some(v => v != null) || minScore !== 15) && (
        <button
          onClick={() => { onChange({}); onMinScoreChange(15); setDatePreset(null) }}
          className="text-xs text-zinc-400 dark:text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 text-left"
        >
          Clear filters
        </button>
      )}
    </aside>
  )
}
