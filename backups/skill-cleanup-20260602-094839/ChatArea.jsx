import { useState, useEffect, useRef } from 'react'
import { FiSend, FiPaperclip, FiX, FiChevronDown, FiZap, FiMessageSquare } from 'react-icons/fi'
import { MessageBubble } from './MessageBubble'
import { getMessages, chatCompletion } from '../api'

function ModelMenu({ models, current, onSelect, onClose }) {
  return (
    <div className="absolute bottom-full right-0 mb-2 w-72 bg-white border border-stone-200 rounded-2xl shadow-xl z-20 overflow-hidden">
      <div className="px-3 py-2 border-b border-stone-100 text-[10px] uppercase tracking-wider text-slate-400 font-medium">
        选择模型
      </div>
      {models.length === 0 && (
        <div className="px-3 py-2 text-xs text-slate-400">暂无可用模型</div>
      )}
      {models.map((m) => (
        <button
          key={m.id}
          onClick={() => { onSelect(m.id); onClose() }}
          className={
            'w-full px-3 py-2.5 text-left text-sm hover:bg-stone-50 flex items-center justify-between transition ' +
            (current === m.id ? 'bg-indigo-50/50' : '')
          }
        >
          <div className="min-w-0">
            <div className="font-medium text-slate-800 truncate">{m.name}</div>
            <div className="text-[11px] text-slate-400">
              {m.provider}
              {m.is_multimodal ? ' · 多模态' : ''}
            </div>
          </div>
          {current === m.id && <div className="w-2 h-2 bg-indigo-500 rounded-full ml-2 shrink-0" />}
        </button>
      ))}
    </div>
  )
}

function SkillMenu({ skills, current, onSelect, onClose }) {
  const list = skills.length > 0 ? skills : [{ id: 'general', name: '普通对话', description: '' }]
  return (
    <div className="absolute bottom-full left-0 mb-2 w-72 bg-white border border-stone-200 rounded-2xl shadow-xl z-20 overflow-hidden">
      <div className="px-3 py-2 border-b border-stone-100 text-[10px] uppercase tracking-wider text-slate-400 font-medium">
        选择技能
      </div>
      {list.map((s) => (
        <button
          key={s.id}
          onClick={() => { onSelect(s.id); onClose() }}
          className={
            'w-full px-3 py-2.5 text-left text-sm hover:bg-stone-50 flex items-start gap-2.5 transition ' +
            (current === s.id ? 'bg-indigo-50/50' : '')
          }
        >
          <div className="w-7 h-7 rounded-lg bg-indigo-50 text-indigo-500 flex items-center justify-center shrink-0">
            <FiZap size={13} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-slate-800 font-medium">{s.name}</div>
            {s.description && (
              <div className="text-[11px] text-slate-400 leading-snug">{s.description}</div>
            )}
          </div>
          {current === s.id && (
            <div className="w-2 h-2 bg-indigo-500 rounded-full mt-2 shrink-0" />
          )}
        </button>
      ))}
    </div>
  )
}

const SAMPLE_PROMPTS = [
  '帮我写一个红黑树的 Python 实现',
  '总结一下 transformer 架构的核心思想',
  '比较 MoE 和 dense LLM 的优劣',
  '帮我润色一段英文摘要',
]

export default function ChatArea({
  activeConv,
  models,
  selectedModel,
  onModelChange,
  skills,
  selectedSkill,
  onSkillChange,
  onConvCreated,
  onConvRefresh,
}) {
  const [msgs, setMsgs] = useState([])
  const [inp, setInp] = useState('')
  const [images, setImages] = useState([])
  const [busy, setBusy] = useState(false)
  const [showModel, setShowModel] = useState(false)
  const [showSkill, setShowSkill] = useState(false)
  const endRef = useRef(null)
  const inpRef = useRef(null)
  const fileRef = useRef(null)

  useEffect(() => {
    if (!activeConv) {
      setMsgs((p) => (p.some((m) => m.streaming) ? p : []))
      return
    }
    let aborted = false
    getMessages(activeConv)
      .then((server) => {
        if (aborted) return
        setMsgs((prev) => (prev.some((m) => m.streaming) ? prev : server))
      })
      .catch(() => {
        if (aborted) return
        setMsgs((prev) => (prev.some((m) => m.streaming) ? prev : []))
      })
    return () => { aborted = true }
  }, [activeConv])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgs])

  useEffect(() => {
    const ta = inpRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'
    }
  }, [inp])

  useEffect(() => {
    if (images.length === 0 || models.length === 0) return
    const cur = models.find((m) => m.id === selectedModel)
    if (cur && !cur.is_multimodal) {
      const mm = models.find((m) => m.is_multimodal)
      if (mm) onModelChange(mm.id)
    }
  }, [images.length])

  async function handleFiles(e) {
    const files = Array.from(e.target.files || [])
    const urls = await Promise.all(
      files.map(
        (f) =>
          new Promise((resolve, reject) => {
            const r = new FileReader()
            r.onload = () => resolve(r.result)
            r.onerror = reject
            r.readAsDataURL(f)
          })
      )
    )
    setImages((p) => [...p, ...urls])
    e.target.value = ''
  }

  async function doSend(textOverride) {
    const text = (textOverride ?? inp).trim()
    if ((!text && images.length === 0) || busy) return
    const sentImages = images
    setInp('')
    setImages([])
    setBusy(true)

    const uMsg = {
      role: 'user',
      content: text,
      image_urls: sentImages.length ? sentImages : null,
      id: 'u' + Date.now(),
    }
    const aMsg = {
      role: 'assistant',
      content: '',
      reasoning: '',
      skill_chain: '',
      id: 'a' + Date.now(),
      streaming: true,
    }
    setMsgs((p) => [...p, uMsg, aMsg])

    let convAnnounced = !!activeConv
    try {
      await chatCompletion(
        text,
        activeConv,
        selectedModel,
        sentImages.length ? sentImages : null,
        selectedSkill,
        (evt) => {
          if (evt.conversation_id && !convAnnounced) {
            convAnnounced = true
            onConvCreated(evt.conversation_id)
          }
          setMsgs((p) => {
            const u = [...p]
            const last = u[u.length - 1]
            if (!last || !last.streaming) return u
            const updated = { ...last }
            if (evt.skill_delta) {
              updated.skill_chain =
                (updated.skill_chain ? updated.skill_chain + '\n' : '') + evt.skill_delta
            }
            if (evt.reasoning_delta) {
              updated.reasoning = (updated.reasoning || '') + evt.reasoning_delta
            }
            if (evt.delta) {
              updated.content = (updated.content || '') + evt.delta
            }
            u[u.length - 1] = updated
            return u
          })
        }
      )
      setMsgs((p) => p.map((m) => (m.streaming ? { ...m, streaming: false } : m)))
    } catch (err) {
      setMsgs((p) => {
        const u = [...p]
        const last = u[u.length - 1]
        if (last && last.streaming) {
          u[u.length - 1] = { ...last, content: '错误:' + err.message, streaming: false }
        }
        return u
      })
    }
    setBusy(false)
    onConvRefresh()
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      doSend()
    }
  }

  const curModelName = models.find((m) => m.id === selectedModel)?.name || selectedModel
  const curSkill = skills.find((s) => s.id === selectedSkill)
  const canSend = (inp.trim().length > 0 || images.length > 0) && !busy

  return (
    <div className="h-full min-h-0 flex flex-col bg-stone-50">
      <div className="flex-1 min-h-0 overflow-y-auto px-6 py-8">
        {msgs.length === 0 ? (
          <div className="h-full flex items-center justify-center">
            <div className="text-center max-w-xl">
              <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-500 shadow-lg mb-5">
                <FiMessageSquare className="text-white" size={26} />
              </div>
              <h1 className="text-3xl font-semibold tracking-tight bg-gradient-to-r from-slate-900 to-slate-600 bg-clip-text text-transparent mb-2">
                你好,我是 Chat ACITS
              </h1>
              <p className="text-slate-500 mb-1">尽管问吧——AI 全力服务</p>
              <p className="text-slate-400 text-xs mb-8">DeepSeek-V4 · qwen3_6 · 可接入自定义模型</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 max-w-lg mx-auto">
                {SAMPLE_PROMPTS.map((p, i) => (
                  <button
                    key={i}
                    onClick={() => doSend(p)}
                    className="px-3.5 py-2.5 text-left text-sm text-slate-700 bg-white border border-stone-200 rounded-xl hover:border-indigo-300 hover:shadow-sm transition"
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto">
            {msgs.map((m) => (
              <MessageBubble key={m.id} msg={m} />
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <div className="px-6 pb-5 bg-stone-50">
        <div className="max-w-3xl mx-auto">
          {images.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2">
              {images.map((u, i) => (
                <div key={i} className="relative">
                  <img
                    src={u}
                    alt=""
                    className="h-16 w-16 object-cover rounded-xl border border-stone-200 shadow-sm"
                  />
                  <button
                    onClick={() => setImages((p) => p.filter((_, j) => j !== i))}
                    className="absolute -top-1.5 -right-1.5 bg-white border border-stone-200 text-slate-500 rounded-full p-0.5 hover:text-red-500 shadow"
                  >
                    <FiX size={11} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {curSkill && curSkill.id !== 'general' && (
            <div className="mb-2 flex items-center gap-1">
              <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-indigo-50 text-xs text-indigo-700 rounded-full border border-indigo-100">
                <FiZap size={10} />
                {curSkill.name}
              </span>
            </div>
          )}

          <div className="flex items-end gap-1 bg-white rounded-3xl pl-2 pr-2 py-2 border border-stone-200 shadow-sm focus-within:border-indigo-300 focus-within:shadow-md transition">
            <div className="relative">
              <button
                onClick={() => setShowSkill((v) => !v)}
                className="px-2 py-2 rounded-xl hover:bg-stone-100 text-slate-500 hover:text-indigo-600 flex items-center gap-1 transition"
                title="技能"
              >
                <FiZap size={15} />
                <FiChevronDown size={11} />
              </button>
              {showSkill && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setShowSkill(false)} />
                  <SkillMenu
                    skills={skills}
                    current={selectedSkill}
                    onSelect={onSkillChange}
                    onClose={() => setShowSkill(false)}
                  />
                </>
              )}
            </div>

            <button
              onClick={() => fileRef.current?.click()}
              className="p-2 rounded-xl hover:bg-stone-100 text-slate-500 hover:text-indigo-600 transition"
              title="附加图片"
            >
              <FiPaperclip size={15} />
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              multiple
              className="hidden"
              onChange={handleFiles}
            />

            <textarea
              ref={inpRef}
              value={inp}
              onChange={(e) => setInp(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入消息……"
              rows={1}
              className="flex-1 bg-transparent text-slate-800 resize-none outline-none text-[14px] placeholder-slate-400 py-2 px-1 max-h-[200px] leading-relaxed"
            />

            <div className="relative">
              <button
                onClick={() => setShowModel((v) => !v)}
                className="px-2.5 py-2 rounded-xl hover:bg-stone-100 text-xs text-slate-600 hover:text-slate-900 flex items-center gap-1 transition"
              >
                <span className="truncate max-w-[160px]">{curModelName}</span>
                <FiChevronDown size={11} />
              </button>
              {showModel && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setShowModel(false)} />
                  <ModelMenu
                    models={models}
                    current={selectedModel}
                    onSelect={onModelChange}
                    onClose={() => setShowModel(false)}
                  />
                </>
              )}
            </div>

            <button
              onClick={() => doSend()}
              disabled={!canSend}
              className={
                'p-2.5 rounded-xl text-white transition shadow-sm ' +
                (canSend
                  ? 'bg-gradient-to-br from-indigo-500 to-violet-500 hover:shadow-md hover:scale-[1.02] active:scale-95'
                  : 'bg-stone-300 cursor-not-allowed')
              }
            >
              <FiSend size={15} />
            </button>
          </div>

          <p className="text-[11px] text-slate-400 text-center mt-2.5">
            Enter 发送 · Shift+Enter 换行
          </p>
        </div>
      </div>
    </div>
  )
}
