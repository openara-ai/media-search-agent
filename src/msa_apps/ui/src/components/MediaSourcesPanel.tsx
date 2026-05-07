import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, FolderOpen, FolderSearch, Plus, Trash2 } from 'lucide-react'
import { addSource, deleteSource, getPlatform, getSources, nativePick, type MediaSource } from '../api/indexer'
import { DirectoryPicker } from './DirectoryPicker'

export function MediaSourcesPanel() {
  const queryClient = useQueryClient()

  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [description, setDescription] = useState('')
  const [addError, setAddError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [formOpen, setFormOpen] = useState(false)
  const [formOpenInitialized, setFormOpenInitialized] = useState(false)

  const { data: platformData } = useQuery({
    queryKey: ['platform'],
    queryFn: getPlatform,
    staleTime: Infinity,
  })
  const platform = platformData?.platform
  const isWindows = platform === 'windows'
  const prefersNativePicker = platform === 'windows' || platform === 'macos'

  const { data: sourcesData } = useQuery<MediaSource[]>({
    queryKey: ['sources'],
    queryFn: getSources,
  })
  const sources = sourcesData ?? []

  useEffect(() => {
    if (!formOpenInitialized && sourcesData !== undefined) {
      setFormOpen(sources.length === 0)
      setFormOpenInitialized(true)
      return
    }
    if (formOpenInitialized && sources.length === 0) {
      setFormOpen(true)
    }
  }, [formOpenInitialized, sources.length, sourcesData])

  const deleteMutation = useMutation({
    mutationFn: deleteSource,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['sources'] }),
  })

  const addMutation = useMutation({
    mutationFn: addSource,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      setName('')
      setPath('')
      setDescription('')
      setAddError(null)
      setFormOpen(false)
    },
    onError: (err: Error) => setAddError(err.message),
  })

  function handleAdd(e: React.FormEvent) {
    e.preventDefault()
    setAddError(null)
    addMutation.mutate({
      name: name.trim(),
      path: path.trim(),
      description: description.trim(),
      read_only: true,
    })
  }

  async function handleBrowseClick() {
    setAddError(null)

    if (!prefersNativePicker) {
      setPickerOpen(true)
      return
    }

    try {
      const selected = await nativePick()
      if (selected.cancelled) return
      if (selected.path) {
        setPath(selected.path)
        return
      }
      setPickerOpen(true)
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Failed to open folder picker')
      setPickerOpen(true)
    }
  }


  return (
    <div className="bg-slate-100 dark:bg-zinc-800 rounded-lg overflow-hidden">
      {/* Add Source expander (form-on-top) */}
      <button
        type="button"
        onClick={() => setFormOpen(open => !open)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
        aria-expanded={formOpen}
      >
        <div>
          <div className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
            {sources.length === 0 ? 'Add Your First Source' : 'Add Source'}
          </div>
          <div className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            {sources.length === 0
              ? 'Set up a folder for indexing'
              : 'Add another folder'}
          </div>
        </div>
        {formOpen ? (
          <ChevronUp size={16} className="text-zinc-500 dark:text-zinc-400" />
        ) : (
          <ChevronDown size={16} className="text-zinc-500 dark:text-zinc-400" />
        )}
      </button>

      {formOpen && (
        <form onSubmit={handleAdd} className="border-t border-slate-200 dark:border-zinc-700 px-4 py-4 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <input
              placeholder="Name (e.g. photos)"
              value={name}
              onChange={e => setName(e.target.value)}
              required
              className="bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-700 dark:text-zinc-200 placeholder-zinc-400 dark:placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
            <div className="flex gap-1">
              <input
                placeholder={isWindows ? 'e.g. D:\\Photos' : 'e.g. /home/user/Photos'}
                value={path}
                onChange={e => setPath(e.target.value)}
                required
                className="flex-1 min-w-0 bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-700 dark:text-zinc-200 placeholder-zinc-400 dark:placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
              <button
                type="button"
                onClick={() => void handleBrowseClick()}
                title="Browse folders"
                className="shrink-0 flex items-center gap-1 px-2.5 py-1.5 border border-slate-200 dark:border-zinc-700 rounded bg-white dark:bg-zinc-900 text-zinc-500 dark:text-zinc-400 hover:text-indigo-600 dark:hover:text-indigo-400 hover:border-indigo-400 transition-colors text-sm"
              >
                <FolderSearch size={14} />
              </button>
            </div>
          </div>

          {pickerOpen && (
            <DirectoryPicker
              initialPath={path}
              onSelect={selected => {
                setPath(selected)
                setPickerOpen(false)
              }}
              onClose={() => setPickerOpen(false)}
            />
          )}

          <input
            placeholder="Description (optional)"
            value={description}
            onChange={e => setDescription(e.target.value)}
            className="w-full bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded px-3 py-1.5 text-sm text-zinc-700 dark:text-zinc-200 placeholder-zinc-400 dark:placeholder-zinc-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />

          {addError && <div className="text-xs text-red-500 dark:text-red-400">{addError}</div>}

          <button
            type="submit"
            disabled={addMutation.isPending || !name.trim() || !path.trim()}
            className="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-md transition-colors disabled:opacity-50"
          >
            <Plus size={14} />
            Add Source
          </button>
        </form>
      )}

      {/* Source list with sub-header */}
      {sources.length > 0 && (
        <div className="border-t border-slate-200 dark:border-zinc-700">
          <div className="px-4 py-3">
            <div className="text-sm font-medium text-zinc-700 dark:text-zinc-200">Media Sources</div>
            <div className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              {sources.length} source{sources.length === 1 ? '' : 's'} configured
            </div>
          </div>
          <ul className="divide-y divide-slate-200 dark:divide-zinc-700">
            {sources.map(source => (
            <li key={source.name} className="flex items-center gap-3 px-4 py-3">
              <FolderOpen size={14} className="text-zinc-400 dark:text-zinc-500 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <div className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
                    {source.name}
                  </div>
                  {source.read_only && (
                    <span className="text-[11px] text-zinc-500 dark:text-zinc-400 bg-slate-200 dark:bg-zinc-700 px-1.5 py-0.5 rounded shrink-0">
                      Read-only
                    </span>
                  )}
                  {!source.enabled && (
                    <span className="text-[11px] text-zinc-500 dark:text-zinc-400 bg-slate-200 dark:bg-zinc-700 px-1.5 py-0.5 rounded shrink-0">
                      Disabled
                    </span>
                  )}
                </div>
                <div className="text-xs text-zinc-400 dark:text-zinc-500 truncate">
                  {source.display_path ?? source.path}
                </div>
                {source.description && (
                  <div className="text-xs text-zinc-400 dark:text-zinc-600">{source.description}</div>
                )}
              </div>
              <button
                type="button"
                onClick={() => deleteMutation.mutate(source.name)}
                disabled={deleteMutation.isPending}
                aria-label={`Remove source ${source.name}`}
                title={`Remove source ${source.name}`}
                className="text-zinc-400 dark:text-zinc-500 hover:text-red-500 dark:hover:text-red-400 transition-colors shrink-0 disabled:opacity-50"
              >
                <Trash2 size={14} />
              </button>
            </li>
          ))}
          </ul>
        </div>
      )}
    </div>
  )
}
