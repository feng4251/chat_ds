import { useState, useRef, useEffect, useMemo } from 'react'
import { FiChevronDown, FiCheck, FiCpu, FiSearch } from 'react-icons/fi'

export default function ModelSelector({
  models = [],
  selectedModel,
  routedModel,
  busy,
  onChange,
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [activeIdx, setActiveIdx] = useState(0)
  const ref = useRef(null)
  const inputRef = useRef(null)
  const itemRefs = useRef([])
  const selected = models.find((m) => m.id === selectedModel)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return models
    return models.filter(
      (m) =>
        (m.name || '').toLowerCase().includes(q) ||
        (m.id || '').toLowerCase().includes(q) ||
        (m.provider || '').toLowerCase().includes(q)
    )
  }, [models, query])

  useEffect(() => {
    if (!open) return
    function onClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    function onKey(e) {
      if (e.key === 'Escape') {
        setOpen(false)
        inputRef.current?.blur()
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        setActiveIdx((i) => Math.min(i + 1, filtered.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setActiveIdx((i) => Math.max(i - 1, 0))
      } else if (e.key === 'Enter') {
        e.preventDefault()
        const m = filtered[activeIdx]
        if (m) {
          onChange(m.id)
          setOpen(false)
        }
      } else if (e.key === 'Home') {
        e.preventDefault()
        setActiveIdx(0)
      } else if (e.key === 'End') {
        e.preventDefault()
        setActiveIdx(filtered.length - 1)
      }
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, filtered, activeIdx, onChange])

  useEffect(() => {
    if (open) {
      setQuery('')
      const idx = Math.max(
        0,
        filtered.findIndex((m) => m.id === selectedModel)
      )
      setActiveIdx(idx)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (open && itemRefs.current[activeIdx]) {
      itemRefs.current[activeIdx].scrollIntoView({ block: 'nearest' })
    }
  }, [activeIdx, open])

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        disabled={busy || models.length === 0}
        className="flex items-center gap-1.5 bg-stone-50 border border-stone-200 rounded-xl pl-2.5 pr-2 py-1.5 text-[11px] text-slate-600 max-w-[230px] hover:border-indigo-300 disabled:opacity-50 transition"
        title={routedModel ? `实际路由: ${routedModel}` : '选择模型'}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <FiCpu className="shrink-0 text-indigo-500" size={12} />
        <span className="truncate">{selected?.name || '选择模型'}</span>
        <FiChevronDown
          className={'shrink-0 text-slate-400 transition-transform ' + (open ? 'rotate-180' : '')}
          size={11}
        />
      </button>

      {open && (
        <div
          role="listbox"
          aria-activedescendant={filtered[activeIdx] ? `model-opt-${activeIdx}` : undefined}
          className="absolute right-0 bottom-full mb-2 w-[280px] bg-white border border-stone-200 rounded-xl shadow-xl overflow-hidden z-50"
        >
          <div className="px-2.5 py-2 border-b border-stone-100 bg-stone-50">
            <div className="relative">
              <FiSearch className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" size={11} />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value)
                  setActiveIdx(0)
                }}
                placeholder="搜索模型…"
                className="w-full pl-7 pr-2 py-1.5 text-[12px] bg-white border border-stone-200 rounded-lg outline-none focus:border-indigo-400 placeholder-slate-400"
              />
            </div>
          </div>
          <div className="max-h-[280px] overflow-y-auto py-1">
            {filtered.length === 0 && (
              <div className="px-3 py-4 text-center text-[12px] text-slate-400">
                没有匹配的模型
              </div>
            )}
            {filtered.map((model, i) => {
              const isActive = model.id === selectedModel
              const isFocused = i === activeIdx
              return (
                <button
                  key={model.id}
                  ref={(el) => (itemRefs.current[i] = el)}
                  id={`model-opt-${i}`}
                  type="button"
                  onClick={() => {
                    onChange(model.id)
                    setOpen(false)
                  }}
                  onMouseEnter={() => setActiveIdx(i)}
                  className={
                    'w-full px-3 py-2 text-left flex items-start gap-2 transition ' +
                    (isFocused ? 'bg-indigo-50 text-indigo-900' : 'text-slate-700 hover:bg-stone-50')
                  }
                  role="option"
                  aria-selected={isActive}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[13px] font-medium truncate">{model.name}</span>
                      {model.is_default && (
                        <span className="px-1.5 py-0.5 text-[10px] rounded bg-indigo-100 text-indigo-700 font-medium">
                          默认
                        </span>
                      )}
                      {model.is_multimodal && (
                        <span className="px-1.5 py-0.5 text-[10px] rounded bg-purple-100 text-purple-700 font-medium">
                          多模态
                        </span>
                      )}
                    </div>
                    {model.provider && model.provider !== 'builtin' && (
                      <div className="text-[11px] text-slate-400 mt-0.5 truncate">
                        {model.provider}
                      </div>
                    )}
                  </div>
                  {isActive && <FiCheck className="mt-0.5 text-indigo-600 shrink-0" size={14} />}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
