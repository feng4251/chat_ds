import { useEffect, useRef, useState } from 'react'
import { FiCheck, FiChevronDown, FiLayers } from 'react-icons/fi'

export default function HarnessSelector({
  engines = [],
  selectedEngine,
  busy,
  locked,
  onChange,
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const selected = engines.find((engine) => engine.id === selectedEngine)
  const retired = selectedEngine === 'legacy'

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

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        disabled={busy || locked || retired || engines.length === 0}
        className="flex items-center gap-1.5 bg-stone-50 border border-stone-200 rounded-xl pl-2.5 pr-2 py-1.5 text-[11px] text-slate-600 max-w-[190px] hover:border-indigo-300 disabled:opacity-50 transition"
        title={retired ? '旧 ChatDS Harness 已退役，请通过工作区 Fork 到原生引擎' : locked ? '已有消息的会话请通过 Fork 切换 Harness' : '选择 Harness'}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <FiLayers className="shrink-0 text-violet-500" size={12} />
        <span className="truncate">{retired ? '旧引擎（已退役）' : selected?.name || '选择 Harness'}</span>
        <FiChevronDown
          className={`shrink-0 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
          size={11}
        />
      </button>
      {open && (
        <div className="absolute right-0 bottom-full mb-2 w-[270px] bg-white border border-stone-200 rounded-xl shadow-xl overflow-hidden z-50 py-1" role="listbox">
          {engines.map((engine) => {
            const active = engine.id === selectedEngine
            return (
              <button
                key={engine.id}
                type="button"
                disabled={!engine.available}
                onClick={() => {
                  onChange(engine.id)
                  setOpen(false)
                }}
                role="option"
                aria-selected={active}
                className="w-full px-3 py-2 text-left flex items-start gap-2 text-slate-700 hover:bg-indigo-50 disabled:opacity-45 disabled:hover:bg-white transition"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[13px] font-medium truncate">{engine.name}</span>
                    {engine.is_default && <span className="text-[10px] text-indigo-600">默认</span>}
                  </div>
                  {!engine.available && (
                    <div className="mt-0.5 text-[10px] text-red-500 truncate">
                      {engine.unavailable_reason || '当前不可用'}
                    </div>
                  )}
                </div>
                {active && <FiCheck className="mt-0.5 text-indigo-600 shrink-0" size={14} />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
