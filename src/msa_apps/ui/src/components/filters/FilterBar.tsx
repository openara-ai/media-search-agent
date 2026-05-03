import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { MediaFilters } from '../../api/types'
import { getPeople, getTags } from '../../api/faces'

interface FilterBarProps {
  filters: MediaFilters
  onChange: (f: MediaFilters) => void
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
  if (key === 'week')  { const f = new Date(now); f.setDate(f.getDate() - 7);          return { date_from: localDate(f), date_to: to } }
  if (key === 'month') { const f = new Date(now); f.setMonth(f.getMonth() - 1);         return { date_from: localDate(f), date_to: to } }
  if (key === 'year')  { const f = new Date(now); f.setFullYear(f.getFullYear() - 1);   return { date_from: localDate(f), date_to: to } }
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

export function FilterBar({ filters, onChange }: FilterBarProps) {
  const [peopleInput, setPeopleInput] = useState('')
  const [tagInput, setTagInput] = useState('')
  const [datePreset, setDatePreset] = useState<DatePreset | null>(
    () => inferPreset(filters.date_from, filters.date_to)
  )

  useEffect(() => {
    const derived = inferPreset(filters.date_from, filters.date_to)
    if (derived !== null) {
      setDatePreset(derived)
    } else if (!filters.date_from && !filters.date_to) {
      setDatePreset(null)
    }
  }, [filters.date_from, filters.date_to])
  const { data } = useQuery({
    queryKey: ['people'],
    queryFn: getPeople,
    staleTime: 30_000,
  })
  const knownPeople = data?.people ?? []
  const { data: tagSuggestions } = useQuery({
    queryKey: ['tags'],
    queryFn: getTags,
    staleTime: 30_000,
  })

  const normalizeFilters = (next: MediaFilters): MediaFilters => {
    const normalized: MediaFilters = { ...next }
    if (!normalized.place) delete normalized.place
    if (!normalized.tags) delete normalized.tags
    if (!normalized.date_from) delete normalized.date_from
    if (!normalized.date_to) delete normalized.date_to
    if (!normalized.media_type) delete normalized.media_type
    if (!normalized.people || normalized.people.length === 0) {
      delete normalized.people
      delete normalized.people_mode
    }
    return normalized
  }

  const set = (patch: Partial<MediaFilters>) => onChange(normalizeFilters({ ...filters, ...patch }))

  const addPerson = (rawName: string) => {
    const name = rawName.trim()
    if (!name) return
    const match = knownPeople.find(p => p.name.toLowerCase() === name.toLowerCase())
    const resolvedName = match?.name ?? name
    const current = filters.people ?? []
    if (current.some(person => person.toLowerCase() === resolvedName.toLowerCase())) {
      setPeopleInput('')
      return
    }
    set({
      people: [...current, resolvedName],
      people_mode: filters.people_mode ?? 'any',
    })
    setPeopleInput('')
  }

  const removePerson = (name: string) => {
    const nextPeople = (filters.people ?? []).filter(person => person !== name)
    set({ people: nextPeople, people_mode: nextPeople.length > 0 ? (filters.people_mode ?? 'any') : undefined })
  }

  const addTag = (rawTag: string) => {
    const tag = rawTag.trim()
    if (!tag) return
    const current = filters.tags ?? []
    if (current.some(item => item.toLowerCase() === tag.toLowerCase())) {
      setTagInput('')
      return
    }
    set({ tags: [...current, tag] })
    setTagInput('')
  }

  const removeTag = (tag: string) => {
    const nextTags = (filters.tags ?? []).filter(item => item !== tag)
    set({ tags: nextTags })
  }

  return (
    <div className="flex items-center gap-3 flex-wrap text-sm">
      <div className="flex items-center gap-2 flex-wrap rounded border border-slate-200 dark:border-zinc-700 bg-slate-50 dark:bg-zinc-900/60 px-2 py-1.5 min-w-[18rem]">
        <span className="text-xs text-zinc-500 dark:text-zinc-400">People</span>
        {(filters.people ?? []).map(name => (
          <button
            key={name}
            type="button"
            onClick={() => removePerson(name)}
            className="inline-flex items-center gap-1 rounded-full bg-slate-200 dark:bg-zinc-800 px-2 py-0.5 text-xs text-zinc-700 dark:text-zinc-200"
            title={`Remove ${name}`}
          >
            <span>{name}</span>
            <span aria-hidden="true">x</span>
          </button>
        ))}
        <input
          list="browse-people-options"
          type="text"
          placeholder={(filters.people ?? []).length > 0 ? 'Add person…' : 'People…'}
          value={peopleInput}
          onChange={e => setPeopleInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault()
              addPerson(peopleInput)
            }
          }}
          onBlur={() => {
            if (peopleInput.trim()) addPerson(peopleInput)
          }}
          className="min-w-[8rem] flex-1 bg-transparent text-xs text-zinc-700 dark:text-zinc-200 placeholder-zinc-400 dark:placeholder-zinc-600 focus:outline-none"
        />
        <datalist id="browse-people-options">
          {knownPeople.map(person => (
            <option key={person.person_id} value={person.name} />
          ))}
        </datalist>
      </div>

      <select
        value={filters.people_mode ?? 'any'}
        onChange={e => set({ people_mode: e.target.value as 'any' | 'all' | 'only' })}
        disabled={!filters.people || filters.people.length === 0}
        className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs disabled:opacity-50"
        title="How selected people should match in each media item"
      >
        <option value="any">Any selected</option>
        <option value="all">All together</option>
        <option value="only">Only these people</option>
      </select>

      <div className="flex items-center gap-2 flex-wrap rounded border border-slate-200 dark:border-zinc-700 bg-slate-50 dark:bg-zinc-900/60 px-2 py-1.5 min-w-[16rem]">
        <span className="text-xs text-zinc-500 dark:text-zinc-400">Tags</span>
        {(filters.tags ?? []).map(tag => (
          <button
            key={tag}
            type="button"
            onClick={() => removeTag(tag)}
            className="inline-flex items-center gap-1 rounded-full bg-slate-200 dark:bg-zinc-800 px-2 py-0.5 text-xs text-zinc-700 dark:text-zinc-200"
            title={`Remove ${tag}`}
          >
            <span>{tag}</span>
            <span aria-hidden="true">x</span>
          </button>
        ))}
        <input
          list="browse-tag-options"
          type="text"
          placeholder={(filters.tags ?? []).length > 0 ? 'Add tag…' : 'Tags…'}
          value={tagInput}
          onChange={e => setTagInput(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault()
              addTag(tagInput)
            }
          }}
          onBlur={() => {
            if (tagInput.trim()) addTag(tagInput)
          }}
          className="min-w-[7rem] flex-1 bg-transparent text-xs text-zinc-700 dark:text-zinc-200 placeholder-zinc-400 dark:placeholder-zinc-600 focus:outline-none"
        />
        <datalist id="browse-tag-options">
          {(tagSuggestions ?? []).map(tag => (
            <option key={tag} value={tag} />
          ))}
        </datalist>
      </div>

      {/* Media type */}
      <select
        value={filters.media_type ?? ''}
        onChange={e => set({ media_type: (e.target.value as 'image' | 'video') || undefined })}
        className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs"
      >
        <option value="">All media</option>
        <option value="image">Photos</option>
        <option value="video">Videos</option>
      </select>

      {/* Date range */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <select
          value={datePreset ?? ''}
          onChange={e => {
            const val = e.target.value as DatePreset | ''
            if (!val) {
              setDatePreset(null)
              set({ date_from: undefined, date_to: undefined })
            } else if (val !== 'custom') {
              setDatePreset(val)
              const { date_from, date_to } = presetDates(val)
              set({ date_from, date_to })
            } else {
              setDatePreset('custom')
              // Keep existing dates — user edits the date inputs directly
            }
          }}
          className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs"
        >
          <option value="">Any time</option>
          <option value="week">Past week</option>
          <option value="month">Past month</option>
          <option value="year">Past year</option>
          <option value="custom">Custom…</option>
        </select>
        {datePreset === 'custom' && (
          <>
            <input
              type="date"
              value={filters.date_from ?? ''}
              onChange={e => set({ date_from: e.target.value || undefined })}
              className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs"
            />
            <span className="text-zinc-300 dark:text-zinc-600 text-xs">→</span>
            <input
              type="date"
              value={filters.date_to ?? ''}
              onChange={e => set({ date_to: e.target.value || undefined })}
              className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs"
            />
          </>
        )}
      </div>

      {/* Place */}
      <input
        type="text"
        placeholder="Place…"
        value={filters.place ?? ''}
        onChange={e => set({ place: e.target.value || undefined })}
        className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs placeholder-zinc-400 dark:placeholder-zinc-600 w-32"
      />

      {/* Sort */}
      <select
        value={`${filters.sort_by ?? 'date'}-${filters.sort_order ?? 'desc'}`}
        onChange={e => {
          const [sort_by, sort_order] = e.target.value.split('-') as ['date' | 'path', 'asc' | 'desc']
          set({ sort_by, sort_order })
        }}
        className="bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1.5 text-zinc-700 dark:text-zinc-200 text-xs ml-auto"
      >
        <option value="date-desc">Newest first</option>
        <option value="date-asc">Oldest first</option>
        <option value="path-asc">Path A–Z</option>
      </select>

      {/* Clear */}
      {Object.values(filters).some(v => v != null) && (
        <button
          onClick={() => { onChange({}); setDatePreset(null) }}
          className="text-xs text-zinc-400 dark:text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
        >
          Clear
        </button>
      )}
    </div>
  )
}
