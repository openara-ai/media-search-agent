import { useState, useEffect, useCallback } from 'react'
import { ChevronRight, FolderOpen, FileImage, X, Loader2 } from 'lucide-react'
import { browse, BrowseResult } from '../api/indexer'

interface Props {
  /** Called with the display_path (user-native format) of the selected directory. */
  onSelect: (path: string) => void
  onClose: () => void
  /** Initial path to open. Empty string = server default (drives list on Windows, /mnt on WSL2, / on Linux). */
  initialPath?: string
}

export function DirectoryPicker({ onSelect, onClose, initialPath = '' }: Props) {
  const [result, setResult] = useState<BrowseResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const navigate = useCallback(async (wslPath: string) => {
    setLoading(true)
    setError(null)
    try {
      setResult(await browse(wslPath))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { navigate(initialPath) }, [navigate, initialPath])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="bg-white dark:bg-zinc-900 rounded-lg shadow-xl w-[520px] max-h-[70vh] flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200 dark:border-zinc-700">
          <span className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
            Select Folder
          </span>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Current path breadcrumb */}
        <div className="px-4 py-2 text-xs font-mono text-zinc-500 dark:text-zinc-400 bg-slate-50 dark:bg-zinc-800/50 border-b border-slate-200 dark:border-zinc-700 truncate min-h-[30px]">
          {result?.current.display_path ?? ''}
        </div>

        {/* Directory listing */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-10 text-sm text-zinc-400">
              <Loader2 size={16} className="animate-spin" />
              Loading…
            </div>
          )}
          {error && (
            <div className="px-4 py-6 text-sm text-red-500 text-center">{error}</div>
          )}
          {!loading && result && (
            <ul className="divide-y divide-slate-100 dark:divide-zinc-800">
              {result.parent && (
                <li>
                  <button
                    onClick={() => navigate(result.parent!.wsl_path)}
                    className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-zinc-500 dark:text-zinc-400 hover:bg-slate-50 dark:hover:bg-zinc-800 transition-colors text-left"
                  >
                    <FolderOpen size={14} className="shrink-0 text-zinc-400" />
                    <span className="font-mono">..</span>
                  </button>
                </li>
              )}
              {result.entries.length === 0 && (
                <li className="px-4 py-8 text-sm text-zinc-400 dark:text-zinc-500 text-center">
                  No sub-folders or media files
                </li>
              )}
              {result.entries.map(entry => (
                <li key={entry.wsl_path}>
                  {entry.is_dir ? (
                    <button
                      onClick={() => navigate(entry.wsl_path)}
                      className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-zinc-700 dark:text-zinc-200 hover:bg-slate-50 dark:hover:bg-zinc-800 transition-colors text-left"
                    >
                      <FolderOpen size={14} className="shrink-0 text-amber-500" />
                      <span className="flex-1 truncate">{entry.name}</span>
                      <ChevronRight size={14} className="shrink-0 text-zinc-400" />
                    </button>
                  ) : (
                    <div className="flex items-center gap-3 px-4 py-2 text-sm text-zinc-400 dark:text-zinc-500">
                      <FileImage size={14} className="shrink-0" />
                      <span className="flex-1 truncate">{entry.name}</span>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-3 px-4 py-3 border-t border-slate-200 dark:border-zinc-700">
          <div className="text-xs font-mono text-zinc-400 dark:text-zinc-500 truncate flex-1">
            {result?.current.display_path ?? ''}
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm text-zinc-600 dark:text-zinc-300 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
            >
              Cancel
            </button>
            <button
              disabled={!result}
              onClick={() => result && onSelect(result.current.display_path)}
              className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm rounded-md transition-colors"
            >
              Select
            </button>
          </div>
        </div>

      </div>
    </div>
  )
}
