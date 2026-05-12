import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Play, Square, ChevronDown, ChevronUp, Image, Film, User, Users, Maximize2, Minimize2 } from 'lucide-react'
import {
  startIndexer, stopIndexer, getIndexStats, getSources,
} from '../api/indexer'
import { useIndexerWS } from '../hooks/useIndexerStatus'
import { MediaSourcesPanel } from '../components/MediaSourcesPanel'
import { cn } from '../lib/utils'

function formatElapsed(s: number): string {
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const sec = s % 60
  if (m < 60) return `${m}m ${sec}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

function formatDuration(s: number | null | undefined): string {
  if (s == null) return '—'
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  const sec = Math.round(s % 60)
  if (m < 60) return `${m}m ${sec}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

function formatCount(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString()
}

function formatRate(s: number | null | undefined): string {
  if (s == null) return '—'
  return `${formatDuration(s)}/min`
}

function parseActivityLine(line: string | null): { timestamp: string | null; fileName: string | null; message: string } | null {
  if (!line) return null

  const timestampMatch = line.match(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})/)
  const timestamp = timestampMatch?.[1] ?? null

  const messageIdx = line.indexOf(' - ')
  const message = messageIdx >= 0 ? line.slice(messageIdx + 3).trim() : line.trim()

  const pathMatch = line.match(/'path':\s*'([^']+)'/)
  const rawPath = pathMatch?.[1] ?? null
  const fileName = rawPath
    ? rawPath.split(/[/\\]/).filter(Boolean).pop() ?? rawPath
    : null

  return { timestamp, fileName, message }
}



const STAT_ITEMS: { key: 'images' | 'videos' | 'faces' | 'people'; label: string; Icon: React.ElementType }[] = [
  { key: 'images',  label: 'Photos',  Icon: Image  },
  { key: 'videos',  label: 'Videos',  Icon: Film   },
  { key: 'faces',   label: 'Faces',   Icon: User   },
  { key: 'people',  label: 'People',  Icon: Users  },
]

export function IndexerPage() {
  const wsUpdate = useIndexerWS()
  const statusInfo = wsUpdate?.status ?? null
  const logLines   = wsUpdate?.log ?? []

  const [logOpen, setLogOpen] = useState(false)
  const [logExpanded, setLogExpanded] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  const prevStatusRef = useRef<string | null>(null)

  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ['indexer-stats'],
    queryFn: getIndexStats,
    refetchInterval: 60_000,
  })
  const { data: sources = [] } = useQuery({
    queryKey: ['sources'],
    queryFn: getSources,
  })

  const startMutation = useMutation({ mutationFn: startIndexer })
  const stopMutation  = useMutation({ mutationFn: stopIndexer  })

  // Refetch stats and reset start mutation when indexer transitions running → terminal
  useEffect(() => {
    const prev = prevStatusRef.current
    const curr = statusInfo?.status ?? null
    if (prev === 'running' && curr !== 'running') {
      if (curr === 'complete' || curr === 'stopped') refetchStats()
      startMutation.reset()
    }
    prevStatusRef.current = curr
  }, [statusInfo?.status, refetchStats, startMutation.reset])

  // Auto-scroll log to bottom when new lines arrive
  useEffect(() => {
    if (logOpen && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logLines, logOpen])

  const status    = statusInfo?.status ?? 'idle'
  const isRunning = status === 'running'
  const hasSources = sources.length > 0
  const elapsed   = statusInfo?.elapsed_seconds ?? null
  const filteredLines = logLines.filter(l => l.trim())
  const lastLine = filteredLines.length > 0 ? filteredLines[filteredLines.length - 1] : null
  const activityLine = parseActivityLine(lastLine)
  const summary = statusInfo?.summary ?? null
  const eta = summary?.estimated_remaining_seconds ?? null
  const showSummaryGrid = (isRunning || status === 'complete') && summary && (
    summary.total_found != null ||
    summary.already_indexed != null ||
    summary.skipped != null ||
    summary.images_to_process != null ||
    summary.videos_to_process != null ||
    summary.processed_images != null ||
    summary.processed_videos != null
  )

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-100">Indexer</h1>

      {/* Stats bar */}
      <div className="space-y-1.5">
        <div className="grid grid-cols-4 gap-3">
          {STAT_ITEMS.map(({ key, label, Icon }) => (
            <div key={key} className="bg-slate-100 dark:bg-zinc-800 rounded-lg p-3 flex flex-col gap-1">
              <div className="flex items-center gap-1.5 text-zinc-500 dark:text-zinc-400 text-xs">
                <Icon size={12} />
                {label}
              </div>
              <div className="text-2xl font-semibold text-zinc-900 dark:text-zinc-100 tabular-nums">
                {stats?.[key] != null ? (stats[key] as number).toLocaleString() : '—'}
              </div>
              {key === 'videos' && stats?.total_video_duration != null && stats.total_video_duration > 0 && (
                <div className="text-xs text-zinc-500 dark:text-zinc-400 tabular-nums">
                  {formatDuration(stats.total_video_duration)}
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="text-xs text-zinc-400 dark:text-zinc-500 text-right">
          {stats?.last_indexed_at
            ? `Last indexed: ${new Date(stats.last_indexed_at + 'Z').toLocaleString()}`
            : !isRunning ? 'Not yet indexed' : null}
        </div>
      </div>

      <MediaSourcesPanel />

      {/* Control card */}
      <div className="bg-slate-100 dark:bg-zinc-800 rounded-lg p-4 space-y-3">
        <div className="flex items-center justify-between">
          {/* Status */}
          <div className="flex items-center gap-3">
            <span className={cn(
              'w-2.5 h-2.5 rounded-full shrink-0',
              status === 'idle'     && 'bg-zinc-400 dark:bg-zinc-500',
              status === 'running'  && 'bg-green-500 animate-pulse',
              status === 'complete' && 'bg-indigo-500 dark:bg-indigo-400',
              status === 'stopped'  && 'bg-amber-500 dark:bg-amber-400',
              status === 'error'    && 'bg-red-500 dark:bg-red-400',
            )} />
            <div>
              <div className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
                {status === 'idle'     && 'Ready'}
                {status === 'running'  && 'Running…'}
                {status === 'complete' && 'Complete'}
                {status === 'stopped'  && 'Stopped'}
                {status === 'error'    && 'Error'}
              </div>
              {elapsed != null && (
                <div className="text-xs text-zinc-500 dark:text-zinc-400">
                  {isRunning
                    ? `${formatElapsed(elapsed)} elapsed`
                    : `Finished in ${formatElapsed(elapsed)}`}
                </div>
              )}
              {isRunning && eta != null && (
                <div className="text-xs text-zinc-500 dark:text-zinc-400">
                  Estimated remaining time: about {formatDuration(eta)}
                </div>
              )}
            </div>
          </div>

          {/* Action button */}
          {isRunning ? (
            <button
              onClick={() => stopMutation.mutate()}
              disabled={stopMutation.isPending}
              className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-500 text-white text-sm rounded-md transition-colors disabled:opacity-50"
            >
              <Square size={14} />
              Stop
            </button>
          ) : (
            <button
              onClick={() => startMutation.mutate()}
              disabled={!hasSources || startMutation.isPending || (startMutation.isSuccess && !isRunning)}
              className="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-md transition-colors disabled:opacity-50"
            >
              <Play size={14} />
              {status === 'idle' ? 'Run Indexer' : 'Run Again'}
            </button>
          )}
        </div>

        {isRunning && summary?.phase === 'counting' && (
          <div className="text-xs text-zinc-500 dark:text-zinc-400 bg-white dark:bg-zinc-900 px-3 py-2 rounded">
            Scanning library...
          </div>
        )}

        {!hasSources && !isRunning && (
          <div className="text-xs text-amber-600 dark:text-amber-400 bg-white dark:bg-zinc-900 px-3 py-2 rounded">
            Setup required. Add at least one media source above to enable indexing.
          </div>
        )}

        {isRunning && summary?.phase === 'analyzing' && (
          <div className="text-xs text-zinc-500 dark:text-zinc-400 bg-white dark:bg-zinc-900 px-3 py-2 rounded">
            Found about {formatCount(summary.total_found)} media items. Checking what needs indexing...
          </div>
        )}

        {showSummaryGrid && summary && (
          <div className="grid grid-cols-2 gap-3">
            <div className="bg-white dark:bg-zinc-900 rounded-lg p-3">
              <div className="text-xs text-zinc-500 dark:text-zinc-400">Media found</div>
              <div className="mt-1 text-lg font-semibold text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.total_found)}
              </div>
            </div>
            <div className="bg-white dark:bg-zinc-900 rounded-lg p-3">
              <div className="text-xs text-zinc-500 dark:text-zinc-400">
                Already up to date
              </div>
              <div className="mt-1 text-lg font-semibold text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.already_indexed ?? summary.skipped)}
              </div>
            </div>
            <div className="bg-white dark:bg-zinc-900 rounded-lg p-3">
              <div className="text-xs text-zinc-500 dark:text-zinc-400">
                {status === 'complete'
                  ? 'Photos processed'
                  : summary.already_indexed != null
                    ? 'Photos to process'
                    : 'Photos found'}
              </div>
              <div className="mt-1 text-lg font-semibold text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.images_to_process ?? summary.processed_images)}
              </div>
            </div>
            <div className="bg-white dark:bg-zinc-900 rounded-lg p-3">
              <div className="text-xs text-zinc-500 dark:text-zinc-400">
                {status === 'complete'
                  ? 'Videos processed'
                  : summary.already_indexed != null
                    ? 'Videos to process'
                    : 'Videos found'}
              </div>
              <div className="mt-1 text-lg font-semibold text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.videos_to_process ?? summary.processed_videos)}
              </div>
            </div>
          </div>
        )}

        {status === 'complete' && summary && (
          <div className="bg-white dark:bg-zinc-900 rounded-lg p-3 space-y-2">
            <div className="text-sm font-medium text-zinc-900 dark:text-zinc-100">Run summary</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              <div className="text-zinc-500 dark:text-zinc-400">Items processed</div>
              <div className="text-right text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.needs_processing)}
              </div>
              <div className="text-zinc-500 dark:text-zinc-400">Faces detected</div>
              <div className="text-right text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.faces)}
              </div>
              <div className="text-zinc-500 dark:text-zinc-400">Tagged items</div>
              <div className="text-right text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatCount(summary.tagged_items)}
              </div>
              <div className="text-zinc-500 dark:text-zinc-400">Average image time</div>
              <div className="text-right text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatDuration(summary.avg_image_seconds)}
              </div>
              <div className="text-zinc-500 dark:text-zinc-400">Average video time</div>
              <div className="text-right text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatDuration(summary.avg_video_seconds)}
              </div>
              <div className="text-zinc-500 dark:text-zinc-400">Video processing rate</div>
              <div className="text-right text-zinc-900 dark:text-zinc-100 tabular-nums">
                {formatRate(summary.avg_video_seconds_per_min)}
              </div>
            </div>
          </div>
        )}

        {/* Current activity (last log line while running) */}
        {isRunning && activityLine && (
          <div className="bg-white dark:bg-zinc-900 px-3 py-2 rounded space-y-0.5">
            <div className="text-[11px] text-zinc-400 dark:text-zinc-500 font-mono truncate">
              {[activityLine.timestamp, activityLine.fileName].filter(Boolean).join('  ') || 'Current activity'}
            </div>
            <div className="text-xs text-zinc-500 dark:text-zinc-400 font-mono leading-5 line-clamp-2 break-words">
              {activityLine.message}
            </div>
          </div>
        )}

        {/* Error detail */}
        {status === 'error' && statusInfo?.return_code != null && (
          <div className="text-xs text-red-500 dark:text-red-400">
            Exited with code {statusInfo.return_code}. Check the log below for details.
          </div>
        )}

        {/* Start error (e.g. 409 already running) */}
        {startMutation.error && (
          <div className="text-xs text-red-500 dark:text-red-400">{String(startMutation.error)}</div>
        )}

        {/* Stop error (e.g. sentinel write failed on Windows — indexer was not asked to stop) */}
        {stopMutation.error && (
          <div className="text-xs text-red-500 dark:text-red-400">{String(stopMutation.error)}</div>
        )}
      </div>

      {/* Log viewer (collapsible + expandable) */}
      {logLines.length > 0 && (
        <div className={cn(
          "bg-slate-100 dark:bg-zinc-800 rounded-lg overflow-hidden",
          logExpanded && "fixed inset-4 z-50 flex flex-col shadow-2xl"
        )}>
          <div className="flex items-center justify-between px-4 py-3 shrink-0">
            <button
              onClick={() => setLogOpen(o => !o)}
              className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
            >
              {logOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              <span>Log ({logLines.length} lines)</span>
            </button>
            <button
              onClick={() => { setLogExpanded(e => !e); setLogOpen(true) }}
              className="text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 transition-colors"
              title={logExpanded ? "Collapse" : "Expand"}
            >
              {logExpanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </button>
          </div>
          {logOpen && (
            <div ref={logRef} className={cn(
              "overflow-y-auto overflow-x-auto px-4 pb-4",
              logExpanded ? "flex-1" : "max-h-72"
            )}>
              <pre className="text-xs text-zinc-500 dark:text-zinc-400 font-mono whitespace-pre leading-5">
                {logLines.join('\n')}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
