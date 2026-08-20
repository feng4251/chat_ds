import { useEffect, useRef, useState } from 'react'
import { FiCheck, FiChevronDown, FiShield } from 'react-icons/fi'

import {
  permissionPreset,
  SESSION_PERMISSION_PRESETS,
} from '../utils/permissionPresets'

export default function PermissionSelector({
  value,
  busy = false,
  supported = true,
  onChange,
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const selected = permissionPreset(value)

  useEffect(() => {
    if (!open) return undefined
    const close = (event) => {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false)
    }
    const escape = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  const title = supported
    ? '选择当前 Session 的原生 Harness 权限'
    : '旧执行引擎已退役；请 Fork 到 Claude Code 或 DeepSeek Harness'

  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        disabled={busy || !supported}
        title={title}
        aria-label="Session 权限"
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-indigo-300 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <FiShield className="shrink-0 text-indigo-500" size={12} />
        <span>{supported ? selected.shortLabel : '旧引擎已退役'}</span>
        <FiChevronDown
          className={`shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
          size={11}
        />
      </button>
      {open && supported && (
        <div
          role="listbox"
          aria-label="Session 权限选项"
          className="absolute bottom-full left-0 z-50 mb-2 w-[330px] overflow-hidden rounded-xl border border-stone-200 bg-white py-1 shadow-xl"
        >
          {SESSION_PERMISSION_PRESETS.map((preset) => {
            const active = preset.id === selected.id
            return (
              <button
                key={preset.id}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => {
                  onChange(preset.id)
                  setOpen(false)
                }}
                className="flex w-full items-start gap-2 px-3 py-2 text-left text-slate-700 transition hover:bg-indigo-50"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-[12px] font-medium">{preset.label}</div>
                  <div className="mt-0.5 text-[10px] leading-relaxed text-slate-500">
                    {preset.description}
                  </div>
                </div>
                {active && <FiCheck className="mt-0.5 shrink-0 text-indigo-600" size={14} />}
              </button>
            )
          })}
          <div className="border-t border-stone-100 px-3 py-2 text-[10px] leading-relaxed text-slate-500">
            三档权限始终受当前 Session Workspace、容器沙箱和部署出网策略约束。
          </div>
        </div>
      )}
    </div>
  )
}
