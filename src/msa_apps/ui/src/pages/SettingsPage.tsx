import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Copy, Check, ExternalLink, RotateCcw } from 'lucide-react'
import {
  getDiagnostics,
  getModelConfig, patchModelConfig,
  type ModelConfigEditable,
} from '../api/indexer'

// Log names that have a corresponding GET /logs/{name} endpoint
const LOG_KEYS = new Set(['app', 'uvicorn', 'qdrant', 'launch', 'install', 'stop'])

function PathRow({ label, value, logKey }: { label: string; value: string; logKey?: string }) {
  const [copied, setCopied] = useState(false)
  function copy() {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <li className="flex items-center gap-3 px-4 py-2.5">
      <span className="text-xs text-zinc-400 dark:text-zinc-500 w-24 shrink-0">{label}</span>
      <span className="text-xs font-mono text-zinc-500 dark:text-zinc-400 truncate flex-1">
        {value}
      </span>
      <div className="flex items-center gap-2 shrink-0">
        {logKey && (
          <a
            href={`/logs/${logKey}`}
            target="_blank"
            rel="noreferrer"
            title="Open log in browser"
            className="text-zinc-300 dark:text-zinc-600 hover:text-indigo-500 dark:hover:text-indigo-400 transition-colors"
          >
            <ExternalLink size={12} />
          </a>
        )}
        <button
          onClick={copy}
          title="Copy path"
          className="text-zinc-300 dark:text-zinc-600 hover:text-zinc-500 dark:hover:text-zinc-400 transition-colors"
        >
          {copied ? <Check size={12} className="text-green-500" /> : <Copy size={12} />}
        </button>
      </div>
    </li>
  )
}

// ── Model Configuration ────────────────────────────────────────────────────

const inputCls = 'bg-white dark:bg-zinc-900 border border-slate-200 dark:border-zinc-700 rounded px-2 py-1 text-sm text-zinc-700 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-indigo-500'
const labelCls = 'text-xs text-zinc-500 dark:text-zinc-400'

function ResetButton({ onClick, title = 'Reset to default' }: { onClick: () => void; title?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="text-zinc-300 dark:text-zinc-600 hover:text-indigo-500 dark:hover:text-indigo-400 transition-colors"
    >
      <RotateCcw size={12} />
    </button>
  )
}

function ModelConfigSection() {
  const queryClient = useQueryClient()
  const [editMode, setEditMode] = useState(false)
  const { data: mc } = useQuery({ queryKey: ['model-config'], queryFn: getModelConfig })

  const mutation = useMutation({
    mutationFn: patchModelConfig,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-config'] }),
  })

  function save(updates: Partial<ModelConfigEditable>) {
    mutation.mutate(updates)
  }

  function reset(key: keyof ModelConfigEditable) {
    if (!mc) return
    save({ [key]: mc.defaults[key] })
  }

  if (!mc) return null

  const e = mc.editable
  const d = mc.defaults
  const r = mc.readonly

  const faceModelOptions: string[] = e.face_recognizer_backend === 'insightface'
    ? ['buffalo_s', 'buffalo_l', 'antelopev2']
    : ['vggface2']

  return (
    <div className="bg-slate-100 dark:bg-zinc-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-200 dark:border-zinc-700 flex items-center justify-between gap-3">
        <div className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
          Model Configuration
        </div>
        <button
          type="button"
          onClick={() => setEditMode(mode => !mode)}
          className="px-3 py-1.5 text-xs rounded-md border border-slate-300 dark:border-zinc-600 text-zinc-600 dark:text-zinc-300 hover:border-indigo-400 hover:text-indigo-600 dark:hover:text-indigo-300 transition-colors"
        >
          {editMode ? 'Done' : 'Edit'}
        </button>
      </div>

      <div className={`divide-y divide-slate-200 dark:divide-zinc-700 transition-opacity ${editMode ? 'opacity-100' : 'opacity-60'}`}>

        {/* Read-only section */}
        <div className="px-4 py-3 space-y-2">
          <div className="text-xs font-semibold text-zinc-400 dark:text-zinc-500 uppercase tracking-wider mb-2">
            CLIP Model (read-only)
          </div>
          {([
            ['Device',    r.device],
            ['Model',     r.model_name],
            ['Pretrained',r.pretrained],
          ] as [string, string][]).map(([label, value]) => (
            <div key={label} className="flex items-center gap-3">
              <span className={`${labelCls} w-24 shrink-0`}>{label}</span>
              <span className="text-xs font-mono text-zinc-500 dark:text-zinc-400">{value}</span>
            </div>
          ))}
        </div>

        {/* Runtime */}
        <div className="px-4 py-3 space-y-3">
          <div className="text-xs font-semibold text-zinc-400 dark:text-zinc-500 uppercase tracking-wider">
            Runtime
          </div>
          <div className="flex items-center gap-3">
            <span className={`${labelCls} w-24 shrink-0`}>Batch size</span>
            <input
              type="number"
              min={1} max={256}
              disabled={!editMode}
              className={`${inputCls} w-20`}
              value={e.batch_size}
              onChange={ev => save({ batch_size: Number(ev.target.value) })}
            />
            <span className={`${labelCls}`}>default: {d.batch_size}</span>
            {editMode && e.batch_size !== d.batch_size && <ResetButton onClick={() => reset('batch_size')} />}
          </div>
        </div>

        {/* Object detection */}
        <div className="px-4 py-3 space-y-3">
          <div className="flex items-center gap-3">
            <span className={`${labelCls} w-24 shrink-0`}>Object detection</span>
            <select
              disabled={!editMode}
              className={`${inputCls} w-36`}
              value={String(e.enable_object_detection)}
              onChange={ev => {
                const v = ev.target.value
                save({ enable_object_detection: v === 'true' ? true : v === 'false' ? false : 'auto' })
              }}
            >
              <option value="auto">Auto (GPU only)</option>
              <option value="true">Always on</option>
              <option value="false">Disabled</option>
            </select>
            {editMode && e.enable_object_detection !== d.enable_object_detection &&
              <ResetButton onClick={() => reset('enable_object_detection')} />}
          </div>

          {e.enable_object_detection !== false && (
            <>
              <div className="flex items-center gap-3">
                <span className={`${labelCls} w-24 shrink-0`}>Model</span>
                <span className="text-xs font-mono text-zinc-500 dark:text-zinc-400">
                  {e.object_model.split('/')[1] ?? e.object_model}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <span className={`${labelCls} w-24 shrink-0`}>Confidence</span>
                <SliderWithValue
                  value={e.object_confidence_threshold}
                  disabled={!editMode}
                  onChange={v => save({ object_confidence_threshold: v })}
                />
                <span className={`${labelCls}`}>default: {d.object_confidence_threshold}</span>
                {editMode && e.object_confidence_threshold !== d.object_confidence_threshold &&
                  <ResetButton onClick={() => reset('object_confidence_threshold')} />}
              </div>
            </>
          )}
        </div>

        {/* Face recognition */}
        <div className="px-4 py-3 space-y-3">
          <div className="flex items-center gap-3">
            <span className={`${labelCls} w-24 shrink-0`}>Face recognition</span>
            <ToggleSwitch
              checked={e.enable_face_recognition}
              disabled={!editMode}
              onChange={v => save({ enable_face_recognition: v })}
            />
            {editMode && e.enable_face_recognition !== d.enable_face_recognition &&
              <ResetButton onClick={() => reset('enable_face_recognition')} />}
          </div>

          {e.enable_face_recognition && (
            <>
              <div className="flex items-center gap-3">
                <span className={`${labelCls} w-24 shrink-0`}>Model</span>
                <select
                  disabled={!editMode || (faceModelOptions.length === 1 && faceModelOptions[0] === e.face_model)}
                  className={`${inputCls} w-36`}
                  value={e.face_model}
                  onChange={ev => save({ face_model: ev.target.value })}
                >
                  {faceModelOptions.map(m => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                  {!faceModelOptions.includes(e.face_model) && (
                    <option key={e.face_model} value={e.face_model}>{e.face_model}</option>
                  )}
                </select>
                <span className={`${labelCls}`}>default: {d.face_model}</span>
                {editMode && e.face_model !== d.face_model && <ResetButton onClick={() => reset('face_model')} />}
              </div>
              <div className="flex items-center gap-3">
                <span className={`${labelCls} w-24 shrink-0`}>Confidence</span>
                <SliderWithValue
                  value={e.face_confidence_threshold}
                  disabled={!editMode}
                  onChange={v => save({ face_confidence_threshold: v })}
                />
                <span className={`${labelCls}`}>default: {d.face_confidence_threshold}</span>
                {editMode && e.face_confidence_threshold !== d.face_confidence_threshold &&
                  <ResetButton onClick={() => reset('face_confidence_threshold')} />}
              </div>
              <div className="flex items-center gap-3">
                <span className={`${labelCls} w-24 shrink-0`}>Min face size</span>
                <input
                  type="number"
                  min={1} max={500}
                  disabled={!editMode}
                  className={`${inputCls} w-20`}
                  value={e.face_min_size}
                  onChange={ev => save({ face_min_size: Number(ev.target.value) })}
                />
                <span className={`${labelCls}`}>px — default: {d.face_min_size}</span>
                {editMode && e.face_min_size !== d.face_min_size && <ResetButton onClick={() => reset('face_min_size')} />}
              </div>
              <div className="flex items-center gap-3">
                <span className={`${labelCls} w-24 shrink-0`}>Store metadata</span>
                <ToggleSwitch
                  checked={e.face_store_metadata}
                  disabled={!editMode}
                  onChange={v => save({ face_store_metadata: v })}
                />
                <span className={`${labelCls} text-zinc-400`}>gender/age estimates</span>
                {editMode && e.face_store_metadata !== d.face_store_metadata &&
                  <ResetButton onClick={() => reset('face_store_metadata')} />}
              </div>
            </>
          )}
        </div>

      </div>
    </div>
  )
}

function ToggleSwitch({ checked, onChange, disabled = false }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-disabled={disabled}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        checked ? 'bg-indigo-600' : 'bg-slate-300 dark:bg-zinc-600'
      } ${disabled ? 'cursor-not-allowed opacity-70' : ''}`}
    >
      <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
        checked ? 'translate-x-4' : 'translate-x-1'
      }`} />
    </button>
  )
}

function SliderWithValue({ value, onChange, disabled = false }: { value: number; onChange: (v: number) => void; disabled?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <input
        type="range"
        min={0} max={1} step={0.05}
        disabled={disabled}
        value={value}
        onChange={ev => onChange(Number(ev.target.value))}
        className={`w-28 accent-indigo-500 ${disabled ? 'cursor-not-allowed opacity-70' : ''}`}
      />
      <span className="text-xs text-zinc-500 dark:text-zinc-400 tabular-nums w-8">{value.toFixed(2)}</span>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────

export function SettingsPage() {
  const { data: diag } = useQuery({ queryKey: ['diagnostics'], queryFn: getDiagnostics })

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-100">Settings</h1>

      {/* Model Configuration */}
      <ModelConfigSection />

      {/* Diagnostics */}
      {diag && (
        <div className="bg-slate-100 dark:bg-zinc-800 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-slate-200 dark:border-zinc-700 text-sm font-medium text-zinc-700 dark:text-zinc-200">
            Diagnostics
          </div>
          <ul className="divide-y divide-slate-200 dark:divide-zinc-700">
            <PathRow label="Config"   value={diag.config_file} />
            <PathRow label="SQLite"   value={diag.sqlite_path} />
            <PathRow label="Models"   value={diag.models_dir} />
            {Object.entries(diag.logs).map(([key, filePath]) => (
              <PathRow
                key={key}
                label={key.charAt(0).toUpperCase() + key.slice(1) + ' log'}
                value={filePath}
                logKey={LOG_KEYS.has(key) ? key : undefined}
              />
            ))}
            {diag.qdrant_url && <PathRow label="Qdrant" value={diag.qdrant_url} />}
          </ul>
        </div>
      )}
    </div>
  )
}
