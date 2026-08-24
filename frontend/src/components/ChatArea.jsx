import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react'
import {
  FiSend, FiPaperclip, FiX, FiMessageSquare, FiFile,
  FiSliders, FiCode, FiBookOpen, FiImage, FiSearch,
  FiArrowDown, FiCpu,
} from 'react-icons/fi'
import { MessageBubble } from './MessageBubble'
import ModelSelector from './ModelSelector'
import HarnessSelector from './HarnessSelector'
import PermissionSelector from './PermissionSelector'
import SessionWorkspace from './SessionWorkspace'
import SkillBar from './SkillBar'
import {
  getMessages, chatCompletion, uploadSessionFile, createConversation, uploadSkill,
  getConversationSettings, updateConversationSettings, getEngines,
  getSkills, deleteSkill, getRunCards, getTurnActivities, decideTurnApproval,
} from '../api'
import {
  bindConversationRequestScope,
  conversationRequestOwnsRoute,
  conversationRequestWasAccepted,
  createConversationRequestScope,
  hydrateAgentRunCards,
  markAcceptedLiveRun,
  observeConversationRequestRoute,
  recordAcceptedRunReceipt,
  runStatusPresentation,
  settleAcceptedLiveRun,
  toolStatusPresentation,
  updateAgentRuns,
} from '../utils/agentRunHydration'
import {
  createSessionRefreshCoordinator,
  createSessionRefreshLoop,
  messageProjectionRevision,
  runCardMessageRevision,
  runCardProjectionRevision,
  shouldFollowMessageUpdate,
} from '../utils/sessionProjectionSync'
import { compatibleModelsForEngine, modelForEngine } from '../utils/engineSelection'
import {
  DEFAULT_PERMISSION_PRESET,
  normalizePermissionPreset,
} from '../utils/permissionPresets'
import {
  applyTurnActivity,
  attachTurnActivities,
  mergeTurnActivities,
  turnActivityHighWater,
} from '../utils/turnActivity'

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
const RUN_DTO_FIELD_LABELS = {
  error: '错误详情',
  requested_tools: '请求工具',
  effective_tools: '有效工具',
  policy: '运行策略',
  tool_events: '工具事件',
}
const IDLE_SESSION_SYNC_MS = 5000
const HIDDEN_SESSION_SYNC_MS = 30000
const FULL_SESSION_RECONCILE_MS = 30000

function withClientStreamError(
  message,
  error,
  backgroundRunExpected = false,
  acceptancePending = false,
) {
  const current = message?.content || ''
  if (STREAM_INCOMPLETE_MARKERS.some((marker) => current.includes(marker))) {
    return { ...message, streaming: false }
  }
  if (backgroundRunExpected) {
    const notice = (
      '⚠️ 实时输出连接已中断，但服务端已接受的任务仍在后台执行。'
      + '页面会从持久化运行记录自动恢复状态；确认任务终态前请勿重复提交。'
      + (error?.message ? `\n连接信息：${error.message}` : '')
    )
    return {
      ...markAcceptedLiveRun(message, message?.rootRunId),
      content: current ? `${current}\n\n---\n${notice}` : notice,
      streaming: false,
    }
  }
  if (acceptancePending) {
    const notice = (
      '⚠️ 实时输出连接在任务受理回执到达前中断，服务端是否已受理尚待确认。'
      + '页面正在核对该会话的持久化运行状态；确认前请勿重复提交。'
      + (error?.message ? `\n连接信息：${error.message}` : '')
    )
    return {
      ...message,
      content: current ? `${current}\n\n---\n${notice}` : notice,
      streaming: false,
    }
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
      {visibleRuns.map((run) => {
        const status = runStatusPresentation(run)
        const stepLabel = [run.workflow_stage, run.step_type, run.step_id]
          .filter(Boolean)
          .join(' · ')
        const batchLabel = (
          run.delegation_slot && run.delegation_batch_size
            ? `批次槽位 ${run.delegation_slot}/${run.delegation_batch_size}`
            : ''
        )
        return (
          <details key={run.id} className="rounded-xl border border-indigo-100 bg-indigo-50/40 px-3 py-2 text-xs" open={(run.lifecycle_status || run.status) === 'running'}>
            <summary className="cursor-pointer flex flex-wrap items-center gap-2 text-slate-700">
              <FiCpu className="text-indigo-500" size={13} />
              <span className="font-medium">{run.display_name || run.agent_name || run.agent_kind}</span>
              <span className={status.tone}>{status.label}</span>
              {run.recovered && (run.lifecycle_status || run.status) === 'degraded' && (
                <span className="text-sky-600">已自动恢复，仍有数据缺口</span>
              )}
              {stepLabel && <span className="text-slate-400">{stepLabel}</span>}
              {batchLabel && <span className="text-slate-400">{batchLabel}</span>}
              <span className="text-slate-400">{run.workspace_scope}</span>
            </summary>
          {run.tools?.length > 0 && (
            <>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {run.tools.slice(-24).map((tool, idx) => (
                  <span
                    key={`${tool.name}-${idx}`}
                    title={tool.detail || ''}
                    className={
                      'px-2 py-0.5 rounded-full bg-white border text-slate-600 ' +
                      (tool.status === 'failed'
                        ? 'border-red-200'
                        : tool.status === 'recovered'
                          ? 'border-sky-200'
                          : tool.status === 'rejected'
                            ? 'border-amber-200'
                            : 'border-indigo-100')
                    }
                  >
                    {tool.name}
                    {tool.attempt_index ? ` #${tool.attempt_index}` : ''}: {' '}
                    {toolStatusPresentation(tool.status)}
                    {tool.later_success_same_tool ? '；后续同工具调用成功' : ''}
                  </span>
                ))}
              </div>
              {run.tool_attempts_truncated && (
                <div className="mt-1 text-slate-400">
                  仅显示最近 {run.tools.length} / 共 {run.tool_attempt_count || run.tools.length} 次工具尝试。
                </div>
              )}
              {run.tools.some((tool) => (
                tool.detail
                && ['failed', 'rejected', 'recovered'].includes(tool.status)
              )) && (
                <div className="mt-2 space-y-1 rounded-lg border border-slate-200 bg-white/70 p-2 text-slate-600">
                  {run.tools
                    .filter((tool) => (
                      tool.detail
                      && ['failed', 'rejected', 'recovered'].includes(tool.status)
                    ))
                    .slice(-24)
                    .map((tool) => (
                      <div key={`${run.id}-${tool.tool_call_id || tool.name}-${tool.attempt_index || ''}-detail`}>
                        <span className="font-medium">
                          {tool.name}
                          {tool.attempt_index ? ` #${tool.attempt_index}` : ''}
                        </span>
                        {' · '}
                        {toolStatusPresentation(tool.status)}
                        {tool.later_success_same_tool
                          ? '（后续同工具调用成功，不代表本次调用已恢复）'
                          : ''}
                        ：{tool.detail}
                      </div>
                    ))}
                </div>
              )}
            </>
          )}
          {run.dto_truncated && (
            <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50/80 p-2 text-amber-700">
              该运行详情超过安全展示上限，已显示有界摘要
              {run.dto_truncated_fields?.length
                ? `（${run.dto_truncated_fields
                  .map((field) => RUN_DTO_FIELD_LABELS[field] || field)
                  .join('、')}）`
                : ''}
              。
            </div>
          )}
          {run.artifacts?.length > 0 && (
            <>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {run.artifacts.slice(-4).map((artifact, idx) => (
                  <span key={`${artifact.path || artifact.title}-${idx}`} className="px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-100 text-emerald-700">
                    {artifact.title || artifact.path} · {(artifact.size_bytes || 0).toLocaleString()}b
                  </span>
                ))}
              </div>
              {(run.artifacts_truncated || run.artifacts.length > 4) && (
                <div className="mt-1 text-slate-400">
                  仅显示最近 {Math.min(4, run.artifacts.length)} / 共 {run.artifact_count || run.artifacts.length} 个产物。
                </div>
              )}
            </>
          )}
          {run.verifier && (
            <div className={run.verifier.status === 'pass' ? 'mt-2 text-emerald-600' : run.verifier.status === 'failed' || run.verifier.status === 'fail' ? 'mt-2 text-red-600' : 'mt-2 text-amber-600'}>
              verifier: {run.verifier.status}{run.verifier.reason ? ` — ${run.verifier.reason}` : ''}
            </div>
          )}
          {run.preview && <div className="mt-2 max-h-28 overflow-y-auto whitespace-pre-wrap text-slate-600 bg-white/70 rounded-lg p-2">{run.preview.slice(-800)}</div>}
          {run.recovery_reason && <div className="mt-2 text-sky-700">恢复依据：{run.recovery_reason}</div>}
          {run.status_reason && !run.error && <div className="mt-2 text-slate-500">终态：{run.status_reason}</div>}
          {run.error && <div className="mt-2 text-red-600">{run.error}</div>}
          {run.usage?.total_tokens ? <div className="mt-2 text-slate-400">{run.usage.total_tokens.toLocaleString()} tokens</div> : null}
          </details>
        )
      })}
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
  const [durableRunActive, setDurableRunActive] = useState(false)
  const [durableRunUnknown, setDurableRunUnknown] = useState(false)
  const [durableRunConversation, setDurableRunConversation] = useState(null)
  const [routedModel, setRoutedModel] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [selectedEngine, setSelectedEngine] = useState('')
  const [selectedPermission, setSelectedPermission] = useState(DEFAULT_PERMISSION_PRESET)
  const [engineOptions, setEngineOptions] = useState([])
  const [engineLocked, setEngineLocked] = useState(false)
  const [settings, setSettings] = useState(null)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const endRef = useRef(null)
  const scrollContainerRef = useRef(null)
  const inpRef = useRef(null)
  const fileRef = useRef(null)
  const dragCounter = useRef(0)
  const activeConvRef = useRef(activeConv)
  const onConvRefreshRef = useRef(onConvRefresh)
  const liveRequestRef = useRef(null)
  const runCardMessageRevisionRef = useRef('')
  const runCardProjectionRevisionRef = useRef('')
  const messageProjectionRevisionRef = useRef('')
  const lastFullSessionSyncRef = useRef(0)
  const sessionSyncWakeRef = useRef(() => {})
  const shouldStickToBottomRef = useRef(true)
  const msgsRef = useRef(msgs)
  msgsRef.current = msgs
  activeConvRef.current = activeConv
  onConvRefreshRef.current = onConvRefresh
  const effectiveDurableRunUnknown = Boolean(
    activeConv
    && (
      durableRunConversation !== activeConv
      || durableRunUnknown
    )
  )
  const toolSurface = settings?.tool_surface || {}
  const compatibleModels = compatibleModelsForEngine(models, selectedEngine)

  useEffect(() => {
    let cancelled = false
    getEngines().then((engines) => {
      if (cancelled) return
      const options = Array.isArray(engines) ? engines : []
      setEngineOptions((current) => (activeConv ? current : options))
      if (!activeConv) {
        const preferred = options.find((engine) => engine.is_default && engine.available)
          || options.find((engine) => engine.available)
        setSelectedEngine(preferred?.id || '')
      }
    }).catch(() => {})
    return () => { cancelled = true }
  }, [activeConv])

  useEffect(() => {
    if (activeConv || !selectedEngine) return
    setSelectedModel((current) => (
      modelForEngine(models, selectedEngine, current, '') || current
    ))
  }, [activeConv, models, selectedEngine])

  function requestOwnsCurrentView(scope) {
    return (
      liveRequestRef.current === scope
      && conversationRequestOwnsRoute(scope, activeConvRef.current)
    )
  }

  function releaseLiveRequest(scope) {
    const ownedCurrentView = requestOwnsCurrentView(scope)
    if (liveRequestRef.current === scope) liveRequestRef.current = null
    if (!ownedCurrentView) return
    setBusy(false)
    onConvRefreshRef.current?.()
    setTimeout(() => onConvRefreshRef.current?.(), 1500)
    // The idle loop may have spent the entire Turn behind live-request
    // ownership. Explicitly wake it only after that ownership is released.
    sessionSyncWakeRef.current?.()
  }

  useEffect(() => {
    const liveRequest = liveRequestRef.current
    if (liveRequest) {
      observeConversationRequestRoute(liveRequest, activeConv)
      if (!conversationRequestOwnsRoute(liveRequest, activeConv)) {
        liveRequest.cancelled = true
        liveRequest.controller?.abort()
        liveRequestRef.current = null
        setBusy(false)
      }
    }

    if (!activeConv) {
      shouldStickToBottomRef.current = true
      setMsgs((previous) => (
        liveRequestRef.current
        && conversationRequestOwnsRoute(liveRequestRef.current, null)
        && previous.some((message) => message.streaming)
          ? previous
          : []
      ))
      setSessionSkills([])
      setSettings(null)
      setDurableRunActive(false)
      setDurableRunUnknown(false)
      setDurableRunConversation(null)
      setEngineLocked(false)
      setSelectedPermission(DEFAULT_PERMISSION_PRESET)
      const defaultModel = models.find((m) => m.is_default)?.id || models[0]?.id || ''
      setSelectedModel(defaultModel)
      runCardMessageRevisionRef.current = ''
      runCardProjectionRevisionRef.current = ''
      messageProjectionRevisionRef.current = ''
      lastFullSessionSyncRef.current = 0
      return
    }
    runCardMessageRevisionRef.current = ''
    runCardProjectionRevisionRef.current = ''
    messageProjectionRevisionRef.current = ''
    lastFullSessionSyncRef.current = 0
    shouldStickToBottomRef.current = true
    setDurableRunActive(false)
    setDurableRunUnknown(true)
    setDurableRunConversation(activeConv)
    let aborted = false
    Promise.all([
      getMessages(activeConv),
      getConversationSettings(activeConv),
      getRunCards(activeConv).then(
        (payload) => ({ available: true, payload }),
        () => ({ available: false, payload: null }),
      ),
      getTurnActivities(activeConv).then(
        (payload) => ({ available: true, payload }),
        () => ({ available: false, payload: { events: [] } }),
      ),
    ])
      .then(([server, settings, runCardResult, activityResult]) => {
        if (aborted) return
        const runCards = runCardResult.payload || {
          roots: [],
          has_active_runs: false,
        }
        runCardMessageRevisionRef.current = runCardMessageRevision(runCards)
        runCardProjectionRevisionRef.current = runCardProjectionRevision(runCards)
        messageProjectionRevisionRef.current = messageProjectionRevision(server)
        lastFullSessionSyncRef.current = Date.now()
        setMsgs((prev) => (
          liveRequestRef.current
          && conversationRequestOwnsRoute(liveRequestRef.current, activeConv)
          && prev.some((m) => m.streaming)
            ? prev
            : attachTurnActivities(
                hydrateAgentRunCards(server, runCards),
                activityResult.payload?.events || [],
                { truncated: activityResult.payload?.truncated === true },
              )
        ))
        if (runCardResult.available) {
          setDurableRunActive(Boolean(runCards?.has_active_runs))
          setDurableRunUnknown(false)
          setDurableRunConversation(activeConv)
        } else {
          // A failed status lookup is not evidence that no run exists. Keep
          // interaction blocked and let the durable poller retry.
          setDurableRunActive(false)
          setDurableRunUnknown(true)
          setDurableRunConversation(activeConv)
        }
        setSelectedModel(settings.model_id || '')
        setSelectedEngine(settings.engine_id || '')
        setEngineOptions(settings.engine_options || [])
        setEngineLocked(Boolean(settings.engine_locked))
        setSelectedPermission(normalizePermissionPreset(settings.permission_preset))
        setSettings(settings)
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
        setMsgs((prev) => (
          liveRequestRef.current
          && conversationRequestOwnsRoute(liveRequestRef.current, activeConv)
          && prev.some((m) => m.streaming)
            ? prev
            : []
        ))
        setDurableRunActive(false)
        setDurableRunUnknown(true)
        setDurableRunConversation(activeConv)
      })
    return () => {
      aborted = true
    }
  }, [activeConv, models])

  // Keep the visible Session synchronized with durable Backend projections.
  // Notifications are not authority: timer/focus/online reconciliation reads
  // the existing message and run-card APIs, coalesces overlap, and never
  // replaces a live local SSE draft. Full transcript reads are bounded by
  // message boundaries plus a slower fallback; active run cards stay fresh.
  useEffect(() => {
    if (!activeConv) return
    const convId = activeConv
    let cancelled = false
    let nextDelay = IDLE_SESSION_SYNC_MS

    const coordinator = createSessionRefreshCoordinator({
      canRefresh: () => (
        !cancelled
        && activeConvRef.current === convId
        && !liveRequestRef.current
      ),
      refresh: async ({ forceFull }) => {
        const runCards = await getRunCards(convId)
        if (cancelled || activeConvRef.current !== convId) return
        const now = Date.now()
        const messageBoundary = runCardMessageRevision(runCards)
        const runProjection = runCardProjectionRevision(runCards)
        const needsFullReconcile = (
          forceFull
          || messageBoundary !== runCardMessageRevisionRef.current
          || now - lastFullSessionSyncRef.current >= FULL_SESSION_RECONCILE_MS
        )
        let server = null
        if (needsFullReconcile) server = await getMessages(convId)
        let activities = null
        let incrementalActivities = false
        if (needsFullReconcile) {
          activities = await getTurnActivities(convId).catch(() => null)
        } else if (runCards?.has_active_runs) {
          const roots = (runCards.roots || []).filter((root) => root.active)
          const pages = await Promise.all(roots.map((root) => (
            getTurnActivities(convId, {
              rootRunId: root.root_run_id,
              after: turnActivityHighWater(
                msgsRef.current,
                root.root_run_id,
              ),
            }).catch(() => ({ events: [] }))
          )))
          activities = {
            events: pages.flatMap((page) => page.events || []),
          }
          incrementalActivities = true
        }
        if (
          cancelled
          || activeConvRef.current !== convId
          || liveRequestRef.current
        ) return

        if (server) {
          const messageRevision = messageProjectionRevision(server)
          const messageChanged = (
            messageRevision !== messageProjectionRevisionRef.current
          )
          if (
            messageChanged
            || runProjection !== runCardProjectionRevisionRef.current
          ) {
            setMsgs((prev) => (
              prev.some((message) => message.streaming)
                ? prev
                : attachTurnActivities(
                    hydrateAgentRunCards(server, runCards),
                    activities?.events || [],
                    { truncated: activities?.truncated === true },
                  )
            ))
          }
          messageProjectionRevisionRef.current = messageRevision
          lastFullSessionSyncRef.current = now
          if (messageChanged) onConvRefreshRef.current?.()
        } else if (
          activities
          || runProjection !== runCardProjectionRevisionRef.current
        ) {
          setMsgs((prev) => (
            prev.some((message) => message.streaming)
              ? prev
              : (
                  incrementalActivities
                    ? mergeTurnActivities(
                        hydrateAgentRunCards(prev, runCards),
                        activities?.events || [],
                      )
                    : attachTurnActivities(
                        hydrateAgentRunCards(prev, runCards),
                        activities?.events || [],
                        { truncated: activities?.truncated === true },
                      )
                )
          ))
        }

        runCardMessageRevisionRef.current = messageBoundary
        runCardProjectionRevisionRef.current = runProjection
        const stillActive = Boolean(runCards?.has_active_runs)
        setDurableRunActive(stillActive)
        setDurableRunUnknown(false)
        setDurableRunConversation(convId)
        const projectionDelay = runCards.poll_after_ms || IDLE_SESSION_SYNC_MS
        nextDelay = document.visibilityState === 'hidden'
          ? Math.max(projectionDelay, HIDDEN_SESSION_SYNC_MS)
          : projectionDelay
      },
    })

    const loop = createSessionRefreshLoop({
      request: (forceFull) => coordinator.request(forceFull),
      getDelay: () => nextDelay,
      initialDelay: IDLE_SESSION_SYNC_MS,
    })
    const forceReconcile = () => loop.wake()
    const onVisibilityChange = () => {
      if (document.visibilityState !== 'hidden') forceReconcile()
    }

    sessionSyncWakeRef.current = forceReconcile
    loop.start()
    window.addEventListener('focus', forceReconcile)
    window.addEventListener('online', forceReconcile)
    window.addEventListener('pageshow', forceReconcile)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      cancelled = true
      if (sessionSyncWakeRef.current === forceReconcile) {
        sessionSyncWakeRef.current = () => {}
      }
      loop.stop()
      coordinator.stop()
      window.removeEventListener('focus', forceReconcile)
      window.removeEventListener('online', forceReconcile)
      window.removeEventListener('pageshow', forceReconcile)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [activeConv])

  async function reconcileDurableRuns(convId, requestScope) {
    if (!convId) return
    const runCards = await getRunCards(convId)
    if (
      requestScope
        ? !requestOwnsCurrentView(requestScope)
        : activeConvRef.current !== convId
    ) return
    const stillActive = Boolean(runCards?.has_active_runs)
    if (stillActive) {
      const activities = await getTurnActivities(convId).catch(() => ({ events: [] }))
      setMsgs((prev) => attachTurnActivities(
        hydrateAgentRunCards(prev, runCards),
        activities.events || [],
        { truncated: activities.truncated === true },
      ))
    } else {
      const server = await getMessages(convId)
      const activities = await getTurnActivities(convId).catch(() => ({ events: [] }))
      if (
        requestScope
          ? !requestOwnsCurrentView(requestScope)
          : activeConvRef.current !== convId
      ) return
      setMsgs((prev) => (
        prev.some((message) => message.streaming)
          ? prev
          : attachTurnActivities(
              hydrateAgentRunCards(server, runCards),
              activities.events || [],
              { truncated: activities.truncated === true },
            )
      ))
    }
    setDurableRunActive(stillActive)
    setDurableRunUnknown(false)
    setDurableRunConversation(convId)
  }

  useEffect(() => () => {
    const liveRequest = liveRequestRef.current
    if (liveRequest) {
      liveRequest.cancelled = true
      liveRequest.controller?.abort()
      liveRequestRef.current = null
    }
  }, [])

  const hasNoMessages = msgs.length === 0

  // Track scroll position to show/hide "scroll to bottom" button
  useEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return

    const onScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      const pinnedToBottom = distanceFromBottom <= 200
      shouldStickToBottomRef.current = pinnedToBottom
      setShowScrollBtn(!pinnedToBottom)
    }

    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [hasNoMessages]) // re-attach when switching between welcome/chat

  // Preserve the pre-update scroll intent. Measuring only after a large
  // background append makes a previously pinned view look "far from bottom"
  // and hides the new durable message below the fold.
  const lastMsg = msgs[msgs.length - 1]
  const isStreaming = lastMsg?.streaming
  useLayoutEffect(() => {
    const el = scrollContainerRef.current
    if (!el) return
    if (shouldFollowMessageUpdate(shouldStickToBottomRef.current, isStreaming)) {
      endRef.current?.scrollIntoView({ behavior: isStreaming ? 'auto' : 'smooth' })
      shouldStickToBottomRef.current = true
      setShowScrollBtn(false)
      return
    }
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    setShowScrollBtn(distanceFromBottom > 200)
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

  const createConfiguredConversation = useCallback(async () => {
    const conv = await createConversation()
    await updateConversationSettings(conv.id, {
      ...(selectedEngine ? { engine_id: selectedEngine } : {}),
      ...(selectedModel ? { model_id: selectedModel } : {}),
      permission_preset: selectedPermission,
    })
    return conv
  }, [selectedEngine, selectedModel, selectedPermission])

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
            const conv = await createConfiguredConversation()
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
            const conv = await createConfiguredConversation()
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
  }, [activeConv, createConfiguredConversation, onConvCreated, refreshSessionSkills])

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
    if (
      (!text && images.length === 0)
      || busy
      || durableRunActive
      || effectiveDurableRunUnknown
    ) return
    const sentImages = images
    setInp('')
    setImages([])
    setBusy(true)
    const requestScope = createConversationRequestScope(activeConv)
    requestScope.controller = new AbortController()
    liveRequestRef.current = requestScope

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
    let streamConvId = activeConv
    try {
      await chatCompletion(
        text,
        activeConv,
        selectedModel || undefined,
        sentImages.length ? sentImages : null,
        (evt) => {
          if (evt.run_id) {
            recordAcceptedRunReceipt(requestScope, evt.run_id)
          }
          if (evt.conversation_id) {
            streamConvId = evt.conversation_id
            bindConversationRequestScope(requestScope, streamConvId)
          }
          if (!requestOwnsCurrentView(requestScope)) return
          if (evt.routed_model) setRoutedModel(evt.routed_model)
          if (evt.conversation_id && !convAnnounced) {
            convAnnounced = true
            onConvCreated(evt.conversation_id)
          }
          setMsgs((p) => {
            const u = [...p]
            const last = u[u.length - 1]
            if (!last || !last.streaming) return u
            let updated = { ...last }
            if (evt.run_id) {
              updated = markAcceptedLiveRun(updated, evt.run_id)
            }
            if (evt.activity_event) {
              updated.activityNodes = applyTurnActivity(
                updated.activityNodes || [],
                evt.activity_event,
              )
            }
            if (evt.tool_progress) {
              updated.tool_progress =
                (updated.tool_progress ? updated.tool_progress + '\n' : '') + evt.tool_progress
            }
            if (evt.agent_event) {
              updated.agentRuns = updateAgentRuns(updated.agentRuns || [], evt.agent_event)
              updated.rootRunId = (
                evt.agent_event.root_run_id
                || evt.agent_event.run_id
                || updated.rootRunId
              )
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
        },
        {
          signal: requestScope.controller.signal,
          engineId: selectedEngine || undefined,
        },
      )
      if (requestOwnsCurrentView(requestScope)) {
        setMsgs((p) => p.map((m) => (
          m.streaming
            ? settleAcceptedLiveRun({ ...m, streaming: false })
            : m
        )))
      }
    } catch (err) {
      if (!requestOwnsCurrentView(requestScope)) return
      const accepted = conversationRequestWasAccepted(requestScope)
      if (streamConvId) {
        setDurableRunActive(accepted)
        setDurableRunUnknown(!accepted)
        setDurableRunConversation(streamConvId)
      }
      setMsgs((p) => {
        const u = [...p]
        const last = u[u.length - 1]
        if (last && last.streaming) {
          u[u.length - 1] = withClientStreamError(
            last,
            err,
            accepted,
            !accepted,
          )
        }
        return u
      })
    } finally {
      if (requestOwnsCurrentView(requestScope)) {
        try {
          await reconcileDurableRuns(streamConvId, requestScope)
        } catch {
          // The persisted projection is progressive enhancement for a live
          // turn; keep the known/possible run blocked and let polling retry.
          if (streamConvId && requestOwnsCurrentView(requestScope)) {
            setDurableRunActive(
              conversationRequestWasAccepted(requestScope),
            )
            setDurableRunUnknown(true)
            setDurableRunConversation(streamConvId)
          }
        }
      }
      releaseLiveRequest(requestScope)
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
    if (busy || durableRunActive || effectiveDurableRunUnknown) return
    const lastAssistantIdx = msgs.reduce(
      (acc, m, i) => (
        m.role === 'assistant' && !m.durableRunPlaceholder ? i : acc
      ),
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
    const requestScope = createConversationRequestScope(activeConv)
    requestScope.controller = new AbortController()
    liveRequestRef.current = requestScope
    let convAnnounced = !!activeConv
    let streamConvId = activeConv
    try {
      await chatCompletion(
        userMsg.content,
        activeConv,
        selectedModel || undefined,
        userMsg.image_urls,
        (evt) => {
          if (evt.run_id) {
            recordAcceptedRunReceipt(requestScope, evt.run_id)
          }
          if (evt.conversation_id) {
            streamConvId = evt.conversation_id
            bindConversationRequestScope(requestScope, streamConvId)
          }
          if (!requestOwnsCurrentView(requestScope)) return
          if (evt.routed_model) setRoutedModel(evt.routed_model)
          if (evt.conversation_id && !convAnnounced) {
            convAnnounced = true
            onConvCreated(evt.conversation_id)
          }
          setMsgs((p) => {
            const u = [...p]
            const last = u[u.length - 1]
            if (!last || !last.streaming) return u
            let updated = { ...last }
            if (evt.run_id) {
              updated = markAcceptedLiveRun(updated, evt.run_id)
            }
            if (evt.activity_event) {
              updated.activityNodes = applyTurnActivity(
                updated.activityNodes || [],
                evt.activity_event,
              )
            }
            if (evt.tool_progress) {
              updated.tool_progress =
                (updated.tool_progress ? updated.tool_progress + '\n' : '') + evt.tool_progress
            }
            if (evt.agent_event) {
              updated.agentRuns = updateAgentRuns(updated.agentRuns || [], evt.agent_event)
              updated.rootRunId = (
                evt.agent_event.root_run_id
                || evt.agent_event.run_id
                || updated.rootRunId
              )
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
        },
        {
          signal: requestScope.controller.signal,
          engineId: selectedEngine || undefined,
        },
      )
      if (requestOwnsCurrentView(requestScope)) {
        setMsgs((p) => p.map((m) => (
          m.streaming
            ? settleAcceptedLiveRun({ ...m, streaming: false })
            : m
        )))
      }
    } catch (err) {
      if (!requestOwnsCurrentView(requestScope)) return
      const accepted = conversationRequestWasAccepted(requestScope)
      if (streamConvId) {
        setDurableRunActive(accepted)
        setDurableRunUnknown(!accepted)
        setDurableRunConversation(streamConvId)
      }
      setMsgs((p) => {
        const u = [...p]
        const last = u[u.length - 1]
        if (last && last.streaming) {
          u[u.length - 1] = withClientStreamError(
            last,
            err,
            accepted,
            !accepted,
          )
        }
        return u
      })
    } finally {
      if (requestOwnsCurrentView(requestScope)) {
        try {
          await reconcileDurableRuns(streamConvId, requestScope)
        } catch {
          if (streamConvId && requestOwnsCurrentView(requestScope)) {
            setDurableRunActive(
              conversationRequestWasAccepted(requestScope),
            )
            setDurableRunUnknown(true)
            setDurableRunConversation(streamConvId)
          }
        }
      }
      releaseLiveRequest(requestScope)
    }
  }

  const interactionBusy = (
    busy
    || durableRunActive
    || effectiveDurableRunUnknown
  )
  const retiredEngine = selectedEngine === 'legacy'
  const canSend = (
    (inp.trim().length > 0 || images.length > 0)
    && !interactionBusy
    && !retiredEngine
  )

  function scrollToBottom() {
    shouldStickToBottomRef.current = true
    setShowScrollBtn(false)
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  async function openWorkspace() {
    if (activeConv) {
      setWorkspaceOpen(true)
      return
    }
    try {
      const conv = await createConfiguredConversation()
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

  async function changeEngine(engineId) {
    const option = engineOptions.find((engine) => engine.id === engineId)
    if (!option?.available) return
    const nextModel = modelForEngine(
      models,
      engineId,
      selectedModel,
      option.default_model_id,
    )
    if (!nextModel) {
      setUploads((previous) => [...previous, {
        name: 'engine',
        label: 'Harness 切换失败: 没有兼容模型',
        error: true,
      }])
      return
    }
    try {
      if (activeConv) {
        const settings = await updateConversationSettings(activeConv, {
          engine_id: engineId,
          model_id: nextModel,
        })
        setEngineOptions(settings.engine_options || engineOptions)
        setEngineLocked(Boolean(settings.engine_locked))
      }
      setSelectedEngine(engineId)
      setSelectedModel(nextModel)
      onConvRefresh()
    } catch (err) {
      setUploads((previous) => [...previous, {
        name: 'engine',
        label: `Harness 切换失败: ${err.message}`,
        error: true,
      }])
    }
  }

  async function changePermission(permissionPreset) {
    const normalized = normalizePermissionPreset(permissionPreset)
    const previous = selectedPermission
    setSelectedPermission(normalized)
    if (!activeConv) return
    try {
      const next = await updateConversationSettings(activeConv, {
        permission_preset: normalized,
      })
      setSettings(next)
      setSelectedPermission(normalizePermissionPreset(next.permission_preset))
      onConvRefresh()
    } catch (err) {
      setSelectedPermission(previous)
      setUploads((current) => [...current, {
        name: 'permission',
        label: `Session 权限切换失败: ${err.message}`,
        error: true,
      }])
    }
  }

  async function handleApproval(message, approval, decision, answers = null) {
    const runId = message.rootRunId || message.run_id
    if (
      !activeConv
      || !runId
      || !approval?.request_id
      || !approval?.request_seq
    ) throw new Error('权限请求缺少持久化运行标识')
    return decideTurnApproval(
      activeConv,
      runId,
      approval.request_id,
      approval.request_seq,
      decision,
      answers,
    )
  }

  return (
    <div
      className="h-full min-h-0 flex flex-col bg-stone-50"
      data-chatds-reload-blocked={
        interactionBusy || inp.trim() || images.length > 0 ? 'true' : 'false'
      }
    >
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
                m.role === 'assistant'
                && i === msgs.length - 1
                && !m.streaming
                && !m.durableRunPlaceholder
                && !interactionBusy
              return (
                <div key={m.id}>
                  <MessageBubble
                    msg={m}
                    onRegenerate={isLastAssistant ? regenerateLast : undefined}
                    onApproval={(approval, decision, answers) => (
                      handleApproval(m, approval, decision, answers)
                    )}
                  />
                  {m.role === 'assistant' && !m.activityNodes?.length && (
                    <AgentRunCards runs={m.agentRuns} />
                  )}
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

          {!busy && effectiveDurableRunUnknown && (
            <div className="mb-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
              正在重新连接并确认后台任务状态；确认完成前暂不接受重复提交。
            </div>
          )}
          {!busy && durableRunActive && !effectiveDurableRunUnknown && (
            <div className="mb-2 rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs text-indigo-700">
              任务仍在后台执行，页面会自动同步持久化进度；完成后即可继续发送。
            </div>
          )}
          {retiredEngine && (
            <div className="mb-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              这是旧 ChatDS Harness 的历史会话，已停止执行。历史消息和产物仍可读取；请在工作区中 Fork 到 Claude Code 或 DeepSeek Harness 后继续。
            </div>
          )}

          <div className="mb-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-600 flex items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-slate-700">Session 权限</span>
              <PermissionSelector
                value={selectedPermission}
                busy={interactionBusy}
                supported={selectedEngine === 'claude_code' || selectedEngine === 'deepseek_harness'}
                onChange={changePermission}
              />
              {toolSurface.deepseek_native_tools ? (
                <span className="text-slate-400">DeepSeek 原生工具 {toolSurface.deepseek_native_tools.length} 个</span>
              ) : null}
            </div>
            <button onClick={openWorkspace} className="text-indigo-600 hover:text-indigo-700 font-medium">打开工作区</button>
          </div>

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
              disabled={retiredEngine}
              placeholder={retiredEngine ? '旧执行引擎已退役，请先 Fork 到原生引擎' : '输入消息……'}
              rows={1}
              className="flex-1 bg-transparent text-slate-800 resize-none outline-none text-[14px] placeholder-slate-400 py-2 px-1 max-h-[200px] leading-relaxed disabled:cursor-not-allowed disabled:text-slate-400"
            />

            <HarnessSelector
              engines={engineOptions}
              selectedEngine={selectedEngine}
              busy={interactionBusy}
              locked={engineLocked}
              onChange={changeEngine}
            />

            <ModelSelector
              models={compatibleModels}
              selectedModel={selectedModel}
              routedModel={routedModel}
              busy={interactionBusy}
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
          setSelectedEngine(settings.engine_id)
          setEngineOptions(settings.engine_options || [])
          setEngineLocked(Boolean(settings.engine_locked))
          setSelectedPermission(normalizePermissionPreset(settings.permission_preset))
          setSettings(settings)
          onConvRefresh()
        }}
        onConversationForked={(conversationId) => {
          onConvRefresh()
          onConvCreated(conversationId)
        }}
      />
    </div>
  )
}
