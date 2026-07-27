import { useState, useEffect, useRef, useCallback } from 'react'
import {
  FiSend, FiPaperclip, FiX, FiMessageSquare, FiFile,
  FiSliders, FiCode, FiBookOpen, FiImage, FiSearch,
  FiArrowDown, FiCpu,
} from 'react-icons/fi'
import { MessageBubble } from './MessageBubble'
import ModelSelector from './ModelSelector'
import SessionWorkspace from './SessionWorkspace'
import SkillBar from './SkillBar'
import {
  getMessages, chatCompletion, uploadSessionFile, createConversation, uploadSkill,
  getConversationSettings, updateConversationSettings,
  getSkills, deleteSkill,
} from '../api'

const SAMPLE_PROMPTS = [
  { icon: FiCode,      text: '帮我写一个红黑树的 Python 实现' },
  { icon: FiBookOpen,  text: '总结一下 transformer 架构的核心思想' },
  { icon: FiImage,     text: '分析这张病理切片,提示可能的病变' },
  { icon: FiSearch,    text: '搜索近一年 GLM 系列模型的进展' },
]

const CAPABILITIES = ['GLM-5.2 主模型', 'Qwen3-5 多模态', '可接入自定义模型']
const STREAM_INCOMPLETE_MARKERS = [
  '⚠️ 本次任务执行失败：',
  '⚠️ 本次响应在流式输出过程中中断：',
]

function withClientStreamError(message, error) {
  const current = message?.content || ''
  if (STREAM_INCOMPLETE_MARKERS.some((marker) => current.includes(marker))) {
    return { ...message, streaming: false }
  }
  if (!current) {
    return {
      ...message,
      content: '错误:' + error.message,
      streaming: false,
    }
  }
  return {
    ...message,
    content:
      current +
      '\n\n---\n⚠️ 本次响应在服务端终态确认前中断：' +
      error.message +
      '\n已显示的是不完整草稿，请重新发送或点击重试。',
    streaming: false,
  }
}

function updateAgentRuns(runs, event) {
  if (!event?.run_id) return runs || []
  const payload = event.payload || {}
  const id = event.run_id
  const next = [...(runs || [])]
  let idx = next.findIndex((run) => run.id === id)
  if (idx < 0) {
    next.push({
      id,
      parent_run_id: event.parent_run_id || null,
      root_run_id: event.root_run_id || id,
      agent_kind: event.agent_kind || 'agent',
      agent_name: event.agent_name || event.agent_kind || 'agent',
      depth: event.depth || 0,
      workspace_scope: event.workspace_scope || 'shared_session',
      status: 'running',
      preview: '',
      tools: [],
      artifacts: [],
      verifier: null,
      usage: null,
      events: [],
    })
    idx = next.length - 1
  }
  const run = { ...next[idx], events: [...(next[idx].events || []), event] }
  run.parent_run_id = event.parent_run_id || run.parent_run_id
  run.root_run_id = event.root_run_id || run.root_run_id
  run.agent_kind = event.agent_kind || run.agent_kind
  run.agent_name = event.agent_name || run.agent_name
  run.depth = event.depth ?? run.depth
  run.workspace_scope = event.workspace_scope || run.workspace_scope
  if (event.event_type === 'agent.spawned' || event.event_type === 'run.started') run.status = 'running'
  if (event.event_type === 'agent.delta') run.preview = (run.preview || '') + (payload.content || '')
  if (event.event_type === 'tool.started') run.tools = [...run.tools, { name: payload.tool_name, status: 'running' }]
  if (event.event_type === 'tool.completed' || event.event_type === 'tool.failed') {
    const tools = [...run.tools]
    const toolIdx = [...tools].reverse().findIndex((tool) => tool.name === payload.tool_name && tool.status === 'running')
    if (toolIdx >= 0) {
      const realIdx = tools.length - 1 - toolIdx
      tools[realIdx] = { ...tools[realIdx], status: event.event_type === 'tool.completed' ? 'success' : 'failed', detail: payload.detail }
    } else {
      tools.push({ name: payload.tool_name, status: event.event_type === 'tool.completed' ? 'success' : 'failed', detail: payload.detail })
    }
    run.tools = tools
  }
  if (event.event_type === 'usage.updated') run.usage = payload
  if (event.event_type === 'artifact.created') {
    const artifact = {
      title: payload.title || payload.path || 'artifact',
      path: payload.path || '',
      kind: payload.kind || 'file',
      size_bytes: payload.size_bytes || payload.size || 0,
    }
    run.artifacts = [...(run.artifacts || []), artifact]
  }
  if (event.event_type === 'verifier.completed' || event.event_type === 'verifier.failed') {
    run.verifier = {
      status: event.event_type === 'verifier.failed' ? 'failed' : (payload.verdict || 'inconclusive'),
      reason: payload.reason || payload.error || '',
    }
  }
  if (event.event_type === 'run.completed') {
    run.status = 'succeeded'
    run.usage = payload.usage || run.usage
  }
  if (event.event_type === 'run.failed') {
    run.status = 'failed'
    run.error = payload.error || 'Unknown error'
  }
  if (event.event_type === 'run.cancelled') {
    run.status = 'cancelled'
    run.error = null
  }
  next[idx] = run
  return next
}

function AgentRunCards({ runs }) {
  const visibleRuns = (runs || []).filter((run) => (
    run.agent_kind !== 'primary' && (
      run.depth > 0
      || run.artifacts?.length > 0
      || run.verifier
      || run.status === 'running'
    )
  ))
  if (visibleRuns.length === 0) return null
  return (
    <div className="ml-10 -mt-3 mb-5 space-y-2">
      {visibleRuns.map((run) => (
        <details key={run.id} className="rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2 text-xs" open={run.status === 'running'}>
          <summary className="cursor-pointer flex items-center gap-2 text-slate-700">
            <FiCpu className="text-indigo-500" size={13} />
            <span className="font-medium">{run.agent_name || run.agent_kind}</span>
            <span className={run.status === 'failed' ? 'text-red-600' : run.status === 'succeeded' ? 'text-green-600' : 'text-amber-600'}>{run.status}</span>
            <span className="text-slate-400">{run.workspace_scope}</span>
          </summary>
          {run.tools?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {run.tools.slice(-8).map((tool, idx) => (
                <span key={`${tool.name}-${idx}`} className="px-2 py-0.5 rounded-full bg-white border border-indigo-100 text-slate-600">
                  {tool.name}: {tool.status}
                </span>
              ))}
            </div>
          )}
          {run.artifacts?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {run.artifacts.slice(-4).map((artifact, idx) => (
                <span key={`${artifact.path || artifact.title}-${idx}`} className="px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-100 text-emerald-700">
                  {artifact.title || artifact.path} · {(artifact.size_bytes || 0).toLocaleString()}b
                </span>
              ))}
            </div>
          )}
          {run.verifier && (
            <div className={run.verifier.status === 'pass' ? 'mt-2 text-emerald-600' : run.verifier.status === 'failed' || run.verifier.status === 'fail' ? 'mt-2 text-red-600' : 'mt-2 text-amber-600'}>
              verifier: {run.verifier.status}{run.verifier.reason ? ` — ${run.verifier.reason}` : ''}
            </div>
          )}
          {run.preview && <div className="mt-2 max-h-28 overflow-y-auto whitespace-pre-wrap text-slate-600 bg-white/70 rounded-lg p-2">{run.preview.slice(-800)}</div>}
          {run.error && <div className="mt-2 text-red-600">{run.error}</div>}
          {run.usage?.total_tokens ? <div className="mt-2 text-slate-400">{run.usage.total_tokens.toLocaleString()} tokens</div> : null}
        </details>
      ))}
    </div>
  )
}

export default function ChatArea({
  activeConv,
  models = [],
  onConvCreated,
  onConvRefresh,
}) {
  const [msgs, setMsgs] = useState([])
  const [inp, setInp] = useState('')
  const [images, setImages] = useState([])
  const [uploads, setUploads] = useState([])
  const [sessionSkills, setSessionSkills] = useState([])
  const [busy, setBusy] = useState(false)
  const [routedModel, setRoutedModel] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const endRef = useRef(null)
  const scrollContainerRef = useRef(null)
  const inpRef = useRef(null)
  const fileRef = useRef(null)
  const dragCounter = useRef(0)

  useEffect(() => {
    if (!activeConv) {
      setMsgs((p) => (p.some((m) => m.streaming) ? p : []))
      setSessionSkills([])
      const defaultModel = models.find((m) => m.is_default)?.id || models[0]?.id || ''
      setSelectedModel(defaultModel)
      return
    }
    let aborted = false
    Promise.all([getMessages(activeConv), getConversationSettings(activeConv)])
      .then(([server, settings]) => {
        if (aborted) return
        setMsgs((prev) => (prev.some((m) => m.streaming) ? prev : server))
        setSelectedModel(settings.model_id || '')
        getSkills(activeConv, settings.enabled_user_skills || [])
          .then((list) => {
            if (aborted) return
            setSessionSkills(Array.isArray(list) ? list : [])
          })
          .catch(() => {
            if (aborted) return
            setSessionSkills([])
          })
      })
      .catch(() => {
        if (aborted) return
        setMsgs((prev) => (prev.some((m) => m.streaming) ? prev : []))
      })
    return () => {
      aborted = true
      setBusy(false)
    }
  }, [activeConv, models])

  const hasNoMessages = msgs.length === 0

  // Track scroll position to show/hide "scroll to bottom" button
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return

    const onScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      setShowScrollBtn(distanceFromBottom > 200)
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [hasNoMessages]) // re-attach when switching between welcome/chat

  // Auto-scroll to bottom when new content arrives — but only if user is already
  // near the bottom (don't fight manual scroll-up)
  const lastMsg = msgs[msgs.length - 1]
  const isStreaming = lastMsg?.streaming
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    if (distanceFromBottom < 300 || isStreaming) {
      endRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [msgs, isStreaming])

  useEffect(() => {
    const ta = inpRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'
    }
  }, [inp])

  const refreshSessionSkills = useCallback(async (convId) => {
    const id = convId || activeConv
    if (!id) {
      setSessionSkills([])
      return
    }
    try {
      const settings = await getConversationSettings(id)
      const enabled = settings.enabled_user_skills || []
      const list = await getSkills(id, enabled)
      setSessionSkills(Array.isArray(list) ? list : [])
    } catch {
      setSessionSkills([])
    }
  }, [activeConv])

  // Auto-dismiss "已安装 Skill" success chips after 4 seconds so the chat
  // area doesn't accumulate stale installation notices.
  useEffect(() => {
    const timers = uploads
      .map((u, i) => (u.skill && !u.error ? i : null))
      .filter((i) => i !== null)
    if (timers.length === 0) return
    const handles = timers.map((i) =>
      setTimeout(() => {
        setUploads((p) => p.filter((_, j) => j !== i))
      }, 4000)
    )
    return () => handles.forEach(clearTimeout)
  }, [uploads])

  const handleFiles = useCallback(async (files) => {
    if (!files || files.length === 0) return

    const imageFiles = []
    const otherFiles = []
    for (const f of files) {
      if (f.type.startsWith('image/')) imageFiles.push(f)
      else otherFiles.push(f)
    }

    // Process images as data URLs
    if (imageFiles.length > 0) {
      const urls = await Promise.all(
        imageFiles.map(
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
    }

    // Upload non-image files
    let installedAnySkill = false
    let lastConvId = activeConv
    for (const f of otherFiles) {
      const isZip = f.name.toLowerCase().endsWith('.zip')
      try {
        if (isZip) {
          let sid = lastConvId
          if (!sid) {
            const conv = await createConversation()
            sid = conv.id
            lastConvId = sid
            onConvCreated(sid)
          }
          const result = await uploadSkill(f, null, sid)
          const label = result.skill
            ? `已安装 Skill: ${result.skill.name}`
            : `已上传: ${f.name}`
          setUploads((p) => [...p, { name: f.name, label, skill: result.skill }])
          if (result.skill) installedAnySkill = true
        } else {
          let convId = lastConvId
          if (!convId) {
            const conv = await createConversation()
            convId = conv.id
            lastConvId = convId
            onConvCreated(convId)
          }
          const result = await uploadSessionFile(convId, f)
          const label = result.skill
            ? `已安装 Skill: ${result.skill.name}`
            : `已上传: ${f.name} (${(result.size / 1024).toFixed(1)}KB)`
          setUploads((p) => [...p, { name: f.name, label, skill: result.skill }])
          if (result.skill) installedAnySkill = true
        }
      } catch (err) {
        setUploads((p) => [...p, { name: f.name, label: `上传失败: ${err.message}`, error: true }])
      }
    }
    // Refresh skill bar so newly installed skills appear immediately
    if (installedAnySkill) {
      setTimeout(() => refreshSessionSkills(lastConvId), 100)
    }
  }, [activeConv, onConvCreated, refreshSessionSkills])

  function onFileInput(e) {
    const files = Array.from(e.target.files || [])
    handleFiles(files)
    e.target.value = ''
  }

  // Drag and drop file upload
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return

    const onDragEnter = (e) => {
      e.preventDefault()
      e.stopPropagation()
      dragCounter.current += 1
      if (e.dataTransfer?.types?.includes('Files')) {
        setIsDragging(true)
      }
    }
    const onDragLeave = (e) => {
      e.preventDefault()
      e.stopPropagation()
      dragCounter.current -= 1
      if (dragCounter.current <= 0) {
        dragCounter.current = 0
        setIsDragging(false)
      }
    }
    const onDragOver = (e) => {
      e.preventDefault()
      e.stopPropagation()
    }
    const onDrop = (e) => {
      e.preventDefault()
      e.stopPropagation()
      dragCounter.current = 0
      setIsDragging(false)
      const files = Array.from(e.dataTransfer?.files || [])
      if (files.length > 0) handleFiles(files)
    }

    el.addEventListener('dragenter', onDragEnter)
    el.addEventListener('dragleave', onDragLeave)
    el.addEventListener('dragover', onDragOver)
    el.addEventListener('drop', onDrop)
    return () => {
      el.removeEventListener('dragenter', onDragEnter)
      el.removeEventListener('dragleave', onDragLeave)
      el.removeEventListener('dragover', onDragOver)
      el.removeEventListener('drop', onDrop)
    }
  }, [handleFiles, hasNoMessages])

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
      id: 'a' + Date.now(),
      streaming: true,
      agentRuns: [],
    }
    setMsgs((p) => [...p, uMsg, aMsg])

    let convAnnounced = !!activeConv
    try {
      await chatCompletion(
        text,
        activeConv,
        selectedModel || undefined,
        sentImages.length ? sentImages : null,
        (evt) => {
          if (evt.routed_model) setRoutedModel(evt.routed_model)
          if (evt.conversation_id && !convAnnounced) {
            convAnnounced = true
            onConvCreated(evt.conversation_id)
          }
          setMsgs((p) => {
            const u = [...p]
            const last = u[u.length - 1]
            if (!last || !last.streaming) return u
            const updated = { ...last }
            if (evt.tool_progress) {
              updated.tool_progress =
                (updated.tool_progress ? updated.tool_progress + '\n' : '') + evt.tool_progress
            }
            if (evt.agent_event) {
              updated.agentRuns = updateAgentRuns(updated.agentRuns || [], evt.agent_event)
            }
            if (evt.reasoning_delta) {
              updated.reasoning = (updated.reasoning || '') + evt.reasoning_delta
            }
            if (evt.delta) {
              updated.content = (updated.content || '') + evt.delta
            }
            if (evt.usage) updated.usage = evt.usage
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
          u[u.length - 1] = withClientStreamError(last, err)
        }
        return u
      })
    } finally {
      setBusy(false)
      onConvRefresh()
      setTimeout(() => onConvRefresh(), 1500)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      doSend()
    }
  }

  // Regenerate the last assistant message — drops it and re-asks the last user msg
  async function regenerateLast() {
    if (busy) return
    const lastAssistantIdx = msgs.reduce(
      (acc, m, i) => (m.role === 'assistant' ? i : acc),
      -1
    )
    if (lastAssistantIdx < 0) return
    let lastUserIdx = -1
    for (let i = lastAssistantIdx - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') {
        lastUserIdx = i
        break
      }
    }
    if (lastUserIdx < 0) return
    const userMsg = msgs[lastUserIdx]

    setMsgs((p) => {
      const next = [...p]
      next[lastAssistantIdx] = {
        role: 'assistant',
        content: '',
        reasoning: '',
        id: 'a' + Date.now(),
        streaming: true,
        agentRuns: [],
      }
      return next
    })

    setBusy(true)
    let convAnnounced = !!activeConv
    try {
      await chatCompletion(
        userMsg.content,
        activeConv,
        selectedModel || undefined,
        userMsg.image_urls,
        (evt) => {
          if (evt.routed_model) setRoutedModel(evt.routed_model)
          if (evt.conversation_id && !convAnnounced) {
            convAnnounced = true
            onConvCreated(evt.conversation_id)
          }
          setMsgs((p) => {
            const u = [...p]
            const last = u[u.length - 1]
            if (!last || !last.streaming) return u
            const updated = { ...last }
            if (evt.tool_progress) {
              updated.tool_progress =
                (updated.tool_progress ? updated.tool_progress + '\n' : '') + evt.tool_progress
            }
            if (evt.agent_event) {
              updated.agentRuns = updateAgentRuns(updated.agentRuns || [], evt.agent_event)
            }
            if (evt.reasoning_delta) {
              updated.reasoning = (updated.reasoning || '') + evt.reasoning_delta
            }
            if (evt.delta) {
              updated.content = (updated.content || '') + evt.delta
            }
            if (evt.usage) updated.usage = evt.usage
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
          u[u.length - 1] = withClientStreamError(last, err)
        }
        return u
      })
    } finally {
      setBusy(false)
      onConvRefresh()
      setTimeout(() => onConvRefresh(), 1500)
    }
  }

  const canSend = (inp.trim().length > 0 || images.length > 0) && !busy

  function scrollToBottom() {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  async function openWorkspace() {
    if (activeConv) {
      setWorkspaceOpen(true)
      return
    }
    try {
      const conv = await createConversation()
      onConvCreated(conv.id)
      setWorkspaceOpen(true)
    } catch (err) {
      setUploads((p) => [...p, {
        name: 'workspace',
        label: `工作区创建失败: ${err.message}`,
        error: true,
      }])
    }
  }

  async function changeModel(modelId) {
    setSelectedModel(modelId)
    if (!activeConv) return
    try {
      await updateConversationSettings(activeConv, { model_id: modelId })
      onConvRefresh()
    } catch (err) {
      setUploads((p) => [...p, {
        name: 'model',
        label: `模型切换失败: ${err.message}`,
        error: true,
      }])
    }
  }

  return (
    <div className="h-full min-h-0 flex flex-col bg-stone-50">
      <div
        ref={scrollContainerRef}
        className="flex-1 min-h-0 overflow-y-auto px-6 py-8 relative"
      >
        {/* Drag overlay */}
        {isDragging && (
          <div className="absolute inset-4 z-30 rounded-2xl border-2 border-dashed border-indigo-400 bg-indigo-50/80 backdrop-blur-sm flex items-center justify-center pointer-events-none">
            <div className="text-center">
              <FiFile className="mx-auto text-indigo-500 mb-2" size={32} />
              <div className="text-sm font-medium text-indigo-700">释放以添加文件</div>
            </div>
          </div>
        )}

        {msgs.length === 0 ? (
          <div className="h-full flex items-center justify-center">
            <div className="text-center max-w-2xl">
              <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-500 shadow-lg mb-5">
                <FiMessageSquare className="text-white" size={26} />
              </div>
              <h1 className="text-3xl font-semibold tracking-tight bg-gradient-to-r from-slate-900 to-slate-600 bg-clip-text text-transparent mb-2">
                你好,我是 Chat ACITS
              </h1>
              <p className="text-slate-500 mb-4">尽管问吧——AI 全力服务</p>
              <div className="flex flex-wrap items-center justify-center gap-2 mb-8">
                {CAPABILITIES.map((c, i) => (
                  <span
                    key={c}
                    className={
                      'px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold border shadow-sm ' +
                      (i === 0
                        ? 'bg-indigo-50 border-indigo-200 text-indigo-700'
                        : 'bg-white border-stone-200 text-slate-700')
                    }
                  >
                    {c}
                  </span>
                ))}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 max-w-xl mx-auto">
                {SAMPLE_PROMPTS.map((p, i) => {
                  const Icon = p.icon
                  return (
                    <button
                      key={i}
                      onClick={() => doSend(p.text)}
                      className="flex items-start gap-2.5 px-3.5 py-2.5 text-left text-sm text-slate-700 bg-white border border-stone-200 rounded-xl hover:border-indigo-300 hover:shadow-sm hover:-translate-y-0.5 transition"
                    >
                      <Icon className="mt-0.5 shrink-0 text-indigo-500" size={14} />
                      <span>{p.text}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto">
            {msgs.map((m, i) => {
              const isLastAssistant =
                m.role === 'assistant' && i === msgs.length - 1 && !m.streaming
              return (
                <div key={m.id}>
                  <MessageBubble
                    msg={m}
                    onRegenerate={isLastAssistant ? regenerateLast : undefined}
                  />
                  {m.role === 'assistant' && <AgentRunCards runs={m.agentRuns} />}
                </div>
              )
            })}
            <div ref={endRef} />
          </div>
        )}

        {/* Scroll-to-bottom button */}
        {showScrollBtn && (
          <button
            onClick={scrollToBottom}
            aria-label="滚动到底部"
            className="fixed bottom-24 right-8 z-20 w-10 h-10 rounded-full bg-white border border-stone-200 shadow-md hover:shadow-lg hover:scale-105 transition flex items-center justify-center text-slate-600 hover:text-indigo-600"
          >
            <FiArrowDown size={16} />
          </button>
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
                    aria-label="移除图片"
                  >
                    <FiX size={11} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {uploads.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2">
              {uploads.map((u, i) => (
                <div
                  key={i}
                  className={
                    'flex items-center gap-1.5 px-2.5 py-1.5 rounded-xl text-xs border shadow-sm ' +
                    (u.error
                      ? 'bg-red-50 border-red-200 text-red-700'
                      : u.skill
                        ? 'bg-purple-50 border-purple-200 text-purple-700'
                        : 'bg-green-50 border-green-200 text-green-700')
                  }
                >
                  <FiFile size={12} />
                  <span className="truncate max-w-[200px]">{u.label}</span>
                  <button
                    onClick={() => setUploads((p) => p.filter((_, j) => j !== i))}
                    className="ml-0.5 text-current opacity-50 hover:opacity-100"
                    aria-label="移除上传"
                  >
                    <FiX size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {activeConv && (
            <SkillBar
              skills={sessionSkills}
              convId={activeConv}
              onRefresh={refreshSessionSkills}
              onDelete={async (sk) => {
                if (!confirm(`确认从本会话删除 Skill "${sk.name}"?`)) return
                try {
                  await deleteSkill(sk.name, sk.session_id)
                  await refreshSessionSkills()
                } catch (err) {
                  alert(`删除失败: ${err.message}`)
                }
              }}
              onUnlink={async (sk) => {
                try {
                  const settings = await getConversationSettings(activeConv)
                  const next = (settings.enabled_user_skills || []).filter(
                    (n) => n !== sk.name
                  )
                  await updateConversationSettings(activeConv, {
                    enabled_user_skills: next,
                  })
                  await refreshSessionSkills()
                } catch (err) {
                  alert(`取消引用失败: ${err.message}`)
                }
              }}
            />
          )}

          <div className="flex items-end gap-1 bg-white rounded-3xl pl-2 pr-2 py-2 border border-stone-200 shadow-sm focus-within:border-indigo-300 focus-within:shadow-md transition">
            <button
              onClick={() => fileRef.current?.click()}
              className="p-2 rounded-xl hover:bg-stone-100 text-slate-500 hover:text-indigo-600 transition"
              title="附加文件"
              aria-label="附加文件"
            >
              <FiPaperclip size={15} />
            </button>
            <input
              ref={fileRef}
              type="file"
              accept="*"
              multiple
              className="hidden"
              onChange={onFileInput}
            />

            <button
              onClick={openWorkspace}
              className="p-2 rounded-xl hover:bg-stone-100 text-slate-500 hover:text-indigo-600 transition"
              title="会话工作区"
              aria-label="打开会话工作区"
            >
              <FiSliders size={15} />
            </button>

            <textarea
              ref={inpRef}
              value={inp}
              onChange={(e) => setInp(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入消息……"
              rows={1}
              className="flex-1 bg-transparent text-slate-800 resize-none outline-none text-[14px] placeholder-slate-400 py-2 px-1 max-h-[200px] leading-relaxed"
            />

            <ModelSelector
              models={models}
              selectedModel={selectedModel}
              routedModel={routedModel}
              busy={busy}
              onChange={changeModel}
            />

            <button
              onClick={() => doSend()}
              disabled={!canSend}
              aria-label="发送消息"
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
            Enter 发送 · Shift+Enter 换行 · 拖拽文件到聊天区上传
          </p>
        </div>
      </div>

      <SessionWorkspace
        open={workspaceOpen}
        onClose={() => setWorkspaceOpen(false)}
        convId={activeConv}
        models={models}
        onSettingsChanged={(settings) => {
          setSelectedModel(settings.model_id)
          onConvRefresh()
        }}
      />
    </div>
  )
}
