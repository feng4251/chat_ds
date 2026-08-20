import { useEffect, useState } from 'react'
import {
  FiX, FiSave, FiTrash2, FiPlus, FiPlay, FiDownload,
  FiFolder, FiTarget, FiClock, FiSliders, FiActivity, FiServer, FiZap,
  FiEye, FiEdit3, FiColumns, FiArchive, FiCheckSquare,
} from 'react-icons/fi'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'
import {
  getConversationSettings, updateConversationSettings,
  getWorkspace, readWorkspaceFile, getWorkspaceFileBlobUrl, downloadWorkspaceFile, writeWorkspaceFile, deleteWorkspaceFile,
  getGoal, updateGoal, clearGoal, getRuns, getRunEvents, getArtifacts, getTasks, downloadTrajectory,
  getSchedules, createSchedule, updateSchedule, deleteSchedule, runSchedule,
  getHooks, createHook, updateHook, deleteHook,
  getMcpServers, addMcpServer, deleteMcpServer,
  forkConversation,
} from '../api'
import {
  DEFAULT_PERMISSION_PRESET,
  normalizePermissionPreset,
} from '../utils/permissionPresets'

function isMarkdownFile(path) {
  if (!path) return false
  const ext = path.toLowerCase().split('.').pop()
  return ext === 'md' || ext === 'markdown' || ext === 'mdx'
}

const mdComponents = {
  pre: ({ children }) => (
    <pre className="bg-slate-950 text-slate-100 rounded-lg p-3 my-2 overflow-auto text-[12px] leading-relaxed">
      {children}
    </pre>
  ),
  code: ({ className, children, ...props }) => {
    const isBlock = typeof className === 'string' && className.startsWith('language-')
    if (isBlock) {
      return <code className={className} {...props}>{children}</code>
    }
    return <code className="font-mono text-[0.9em] text-rose-600" {...props}>{children}</code>
  },
  a: (props) => (
    <a {...props} target="_blank" rel="noreferrer" className="text-indigo-600 hover:text-indigo-700 underline" />
  ),
  h1: ({ children }) => <h1 className="text-xl font-semibold mt-4 mb-2 text-slate-900">{children}</h1>,
  h2: ({ children }) => <h2 className="text-lg font-semibold mt-3 mb-2 text-slate-900">{children}</h2>,
  h3: ({ children }) => <h3 className="text-base font-semibold mt-3 mb-1 text-slate-900">{children}</h3>,
  h4: ({ children }) => <h4 className="text-sm font-semibold mt-2 mb-1 text-slate-900">{children}</h4>,
  ul: ({ children }) => <ul className="list-disc ml-5 my-2 space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal ml-5 my-2 space-y-1">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed text-sm">{children}</li>,
  p: ({ children }) => <p className="my-2 leading-[1.7] text-sm">{children}</p>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-indigo-300 pl-3 my-3 text-slate-500 italic text-sm">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto my-3 rounded-lg border border-slate-200">
      <table className="text-xs border-collapse w-full">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-slate-50">{children}</thead>,
  th: ({ children }) => (
    <th className="border-b border-slate-200 px-3 py-2 text-left font-semibold text-slate-700">{children}</th>
  ),
  td: ({ children }) => (
    <td className="border-b border-slate-100 px-3 py-2 text-slate-700">{children}</td>
  ),
  hr: () => <hr className="border-slate-200 my-4" />,
  img: ({ src, alt }) => (
    <img src={src} alt={alt || ''} className="max-w-full rounded-lg my-2 border border-slate-200" />
  ),
}

export default function SessionWorkspace({
  open,
  onClose,
  convId,
  models,
  onSettingsChanged,
  onConversationForked,
}) {
  const [tab, setTab] = useState('settings')
  const [loadedConvId, setLoadedConvId] = useState('')
  const [settings, setSettings] = useState(null)
  const [workspace, setWorkspace] = useState({ files: [] })
  const [selectedFile, setSelectedFile] = useState('')
  const [fileContent, setFileContent] = useState('')
  const [fileMeta, setFileMeta] = useState(null)
  const [fileBlobUrl, setFileBlobUrl] = useState('')
  const [previewMode, setPreviewMode] = useState('split')
  const [goal, setGoal] = useState({})
  const [runs, setRuns] = useState([])
  const [runTree, setRunTree] = useState([])
  const [runEvents, setRunEvents] = useState({ runId: '', events: [] })
  const [artifacts, setArtifacts] = useState([])
  const [tasks, setTasks] = useState([])
  const [jobs, setJobs] = useState([])
  const [hooks, setHooks] = useState([])
  const [mcpServers, setMcpServers] = useState([])
  const [jobForm, setJobForm] = useState({ name: '', prompt: '', schedule: 'every 1h', timezone: 'Asia/Shanghai' })
  const [hookForm, setHookForm] = useState({
    name: '', url: '', secret: '', events: 'run.completed,run.failed',
  })
  const [mcpForm, setMcpForm] = useState({
    name: '', url: '', command: '', args: '', transport: 'http',
  })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  async function downloadFile(path) {
    if (!path) return
    setError('')
    try {
      await downloadWorkspaceFile(convId, path)
    } catch (downloadError) {
      setError(downloadError.message || '下载文件失败')
    }
  }

  useEffect(() => {
    if (!open || !convId) return
    let cancelled = false
    const resources = [
      ['运行配置', () => getConversationSettings(convId)],
      ['Workspace', () => getWorkspace(convId)],
      ['Goal', () => getGoal(convId)],
      ['Runs', () => getRuns(convId)],
      ['Artifacts', () => getArtifacts(convId)],
      ['Tasks', () => getTasks(convId)],
      ['Schedules', () => getSchedules(convId)],
      ['Hooks', () => getHooks()],
      ['MCP', () => getMcpServers(convId)],
    ]
    Promise.allSettled(resources.map(([, load]) => load())).then((results) => {
      if (cancelled) return
      const values = results.map((result) => (
        result.status === 'fulfilled' ? result.value : null
      ))
      const [s, w, g, r, a, t, j, h, m] = values
      setSettings(s)
      setWorkspace(w || { files: [] })
      setGoal(g || {})
      if (r) {
        setRuns(Array.isArray(r) ? r : (r.runs || []))
        setRunTree(Array.isArray(r) ? [] : (r.tree || []))
      } else {
        setRuns([])
        setRunTree([])
      }
      setArtifacts(a?.artifacts || [])
      setTasks(t?.tasks || [])
      setJobs(j || [])
      setHooks(h || [])
      setMcpServers(m?.servers || [])

      const failures = results.flatMap((result, index) => (
        result.status === 'rejected'
          ? [`${resources[index][0]}：${result.reason?.message || '加载失败'}`]
          : []
      ))
      setError(failures.length ? `部分面板加载失败：${failures.join('；')}` : '')
      setLoadedConvId(convId)
    })
    return () => { cancelled = true }
  }, [open, convId])

  useEffect(() => {
    return () => {
      if (fileBlobUrl) URL.revokeObjectURL(fileBlobUrl)
    }
  }, [fileBlobUrl])

  async function saveSettings() {
    setError('')
    setSaving(true)
    try {
      const next = await updateConversationSettings(convId, {
        engine_id: settings.engine_id,
        model_id: settings.model_id,
        permission_preset: settings.permission_preset,
      })
      setSettings(next)
      setError('')
      onSettingsChanged?.(next)
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  async function forkToEngine(targetEngineId) {
    setSaving(true)
    try {
      const target = (settings.engine_options || []).find(
        (engine) => engine.id === targetEngineId,
      )
      const compatible = target?.compatible_model_ids || []
      const targetModelId = compatible.includes(settings.model_id)
        ? settings.model_id
        : target?.default_model_id
      if (!targetModelId) throw new Error('目标执行引擎没有已配置的兼容模型')
      const fork = await forkConversation(
        convId,
        null,
        true,
        targetEngineId,
        targetModelId,
      )
      onClose?.()
      onConversationForked?.(fork.id)
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  async function openFile(path) {
    setSelectedFile(path)
    if (fileBlobUrl) URL.revokeObjectURL(fileBlobUrl)
    setFileBlobUrl('')
    setFileContent('')
    setFileMeta(null)
    try {
      const data = await readWorkspaceFile(convId, path)
      setFileMeta(data)
      if (data.editable) {
        setFileContent(data.content || '')
      }
      if (!data.editable || ['pdf', 'image', 'office', 'binary'].includes(data.preview_kind)) {
        const url = await getWorkspaceFileBlobUrl(convId, path)
        setFileBlobUrl(url)
      }
    } catch (e) { setError(e.message) }
  }

  async function saveFile() {
    if (!selectedFile) return
    setSaving(true)
    try {
      await writeWorkspaceFile(convId, selectedFile, fileContent)
      setWorkspace(await getWorkspace(convId))
    } catch (e) { setError(e.message) } finally { setSaving(false) }
  }

  async function newFile() {
    const name = prompt('相对工作区路径，例如 notes/decision.md')
    if (!name) return
    await writeWorkspaceFile(convId, name, '')
    setWorkspace(await getWorkspace(convId))
    openFile(name)
  }

  async function removeFile() {
    if (!selectedFile || !confirm(`删除 ${selectedFile}?`)) return
    try {
      await deleteWorkspaceFile(convId, selectedFile)
      setSelectedFile('')
      setFileContent('')
      setFileMeta(null)
      if (fileBlobUrl) URL.revokeObjectURL(fileBlobUrl)
      setFileBlobUrl('')
      setWorkspace(await getWorkspace(convId))
    } catch (e) { setError(e.message) }
  }

  async function openRunEvents(runId) {
    try {
      const data = await getRunEvents(convId, runId)
      setRunEvents({ runId, events: data.events || [] })
    } catch (e) { setError(e.message) }
  }

  async function saveGoal() {
    try {
      const next = await updateGoal(convId, {
        objective: goal.objective || '',
        status: goal.objective ? (goal.status || 'active') : undefined,
        note: goal.note || '',
        token_budget: goal.token_budget ? Number(goal.token_budget) : undefined,
      })
      setGoal(next)
    } catch (e) { setError(e.message) }
  }

  async function addJob(e) {
    e.preventDefault()
    try {
      await createSchedule({ ...jobForm, conversation_id: convId, model_id: settings?.model_id })
      setJobForm({ name: '', prompt: '', schedule: 'every 1h', timezone: 'Asia/Shanghai' })
      setJobs(await getSchedules(convId))
    } catch (e2) { setError(e2.message) }
  }

  async function addHook(e) {
    e.preventDefault()
    try {
      await createHook({
        name: hookForm.name,
        url: hookForm.url,
        secret: hookForm.secret || null,
        conversation_id: convId,
        events: hookForm.events.split(',').map((v) => v.trim()).filter(Boolean),
      })
      setHookForm({ name: '', url: '', secret: '', events: 'run.completed,run.failed' })
      setHooks(await getHooks())
    } catch (err) { setError(err.message) }
  }

  async function addMcp(e) {
    e.preventDefault()
    try {
      let args = null
      if (mcpForm.args.trim()) {
        args = JSON.parse(mcpForm.args)
        if (!Array.isArray(args) || args.some((value) => typeof value !== 'string')) {
          throw new Error('Args 必须是字符串数组，例如 ["/path/server.py"]')
        }
      }
      await addMcpServer({
        name: mcpForm.name,
        session_id: convId,
        url: mcpForm.url || null,
        command: mcpForm.command || null,
        args,
        transport: mcpForm.transport || null,
      })
      setMcpForm({ name: '', url: '', command: '', args: '', transport: 'http' })
      const data = await getMcpServers(convId)
      setMcpServers(data.servers || [])
    } catch (err) { setError(err.message) }
  }

  if (!open) return null
  const tabs = [
    ['settings', FiSliders, '运行配置'],
    ['workspace', FiFolder, '工作区'],
    ['artifacts', FiArchive, '产物'],
    ['tasks', FiCheckSquare, '任务'],
    ['goal', FiTarget, '目标'],
    ['automation', FiClock, '自动化'],
    ['mcp', FiServer, 'MCP'],
    ['hooks', FiZap, 'Hooks'],
    ['runs', FiActivity, '轨迹'],
  ]

  const previewKind = fileMeta?.preview_kind || (isMarkdownFile(selectedFile) ? 'markdown' : 'text')
  const isMd = previewKind === 'markdown'
  const isEditable = fileMeta?.editable !== false
  const canPreviewContent = selectedFile && previewMode !== 'edit'
  const showPreview = Boolean(canPreviewContent)
  const showEditor = isEditable && selectedFile && previewMode !== 'preview'
  const gridTemplate = showPreview && showEditor
    ? 'grid-cols-[200px_1fr_1fr] min-h-[600px]'
    : 'grid-cols-[200px_1fr] min-h-[600px]'

  return (
    <div className="fixed inset-0 z-50 bg-black/35 flex justify-end" onClick={onClose}>
      <div className="h-full w-full max-w-4xl bg-white shadow-2xl flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="px-5 py-4 border-b border-stone-200 flex items-center justify-between">
          <div>
            <div className="font-semibold text-slate-900">Session Workspace</div>
            <div className="text-[11px] text-slate-400 font-mono">{convId}</div>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-stone-100"><FiX /></button>
        </div>
        <div className="flex border-b border-stone-200 px-3 overflow-x-auto">
          {tabs.map(([id, Icon, label]) => (
            <button key={id} onClick={() => setTab(id)} className={`px-3 py-2.5 text-xs flex items-center gap-1.5 border-b-2 whitespace-nowrap ${tab === id ? 'border-indigo-500 text-indigo-700' : 'border-transparent text-slate-500'}`}>
              <Icon size={13} />{label}
            </button>
          ))}
        </div>
        {loadedConvId === convId && error && <div className="mx-5 mt-3 p-2 rounded bg-red-50 text-red-700 text-xs">{error}</div>}
        <div className="flex-1 overflow-y-auto p-5">
          {loadedConvId !== convId && (
            <div className="py-10 text-center text-sm text-slate-500">正在加载 Session Workspace…</div>
          )}
          {loadedConvId === convId && tab === 'settings' && settings && (
            <div className="space-y-5">
              <Section title="执行引擎">
                <select
                  value={settings.engine_id || ''}
                  disabled={settings.engine_locked}
                  onChange={(e) => {
                    const engineId = e.target.value
                    const target = (settings.engine_options || []).find(
                      (engine) => engine.id === engineId,
                    )
                    const compatible = target?.compatible_model_ids || []
                    setSettings({
                      ...settings,
                      engine_id: engineId,
                      model_id: compatible.includes(settings.model_id)
                        ? settings.model_id
                        : target?.default_model_id || settings.model_id,
                    })
                  }}
                  className="w-full border border-stone-300 rounded-lg px-3 py-2 text-sm disabled:bg-stone-100 disabled:text-stone-500"
                >
                  {settings.engine_id === 'legacy' && (
                    <option value="legacy" disabled>旧 ChatDS Harness（已退役）</option>
                  )}
                  {(settings.engine_options || []).map((engine) => (
                    <option key={engine.id} value={engine.id} disabled={!engine.available}>
                      {engine.name}{engine.available ? '' : '（不可用）'}
                    </option>
                  ))}
                </select>
                <div className="mt-1.5 text-[11px] text-slate-500">
                  {settings.engine_locked
                    ? '首个 Turn 后引擎即固定；如需切换，请 Fork 会话，避免混合原生检查点。'
                    : '引擎可在首个 Turn 前选择。'}
                </div>
                {settings.engine_locked && (settings.engine_options || []).some(
                  (engine) => engine.available && engine.id !== settings.engine_id,
                ) && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {(settings.engine_options || [])
                      .filter((engine) => engine.available && engine.id !== settings.engine_id)
                      .map((engine) => (
                        <button
                          key={engine.id}
                          type="button"
                          disabled={saving}
                          onClick={() => forkToEngine(engine.id)}
                          className="px-2.5 py-1.5 rounded border border-indigo-200 text-xs text-indigo-700 hover:bg-indigo-50 disabled:opacity-50"
                        >
                          Fork 并切换到 {engine.name}
                        </button>
                      ))}
                  </div>
                )}
              </Section>
              <Section title="主模型">
                <select disabled={settings.engine_id === 'legacy'} value={settings.model_id} onChange={(e) => setSettings({ ...settings, model_id: e.target.value })} className="w-full border border-stone-300 rounded-lg px-3 py-2 text-sm disabled:bg-stone-100 disabled:text-stone-500">
                  {models
                    .filter((model) => {
                      const active = (settings.engine_options || []).find(
                        (engine) => engine.id === settings.engine_id,
                      )
                      return !active?.compatible_model_ids
                        || active.compatible_model_ids.includes(model.id)
                    })
                    .map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
              </Section>
              {settings.engine_id === 'legacy' ? (
                <Section title="Session 权限">
                  <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-relaxed text-amber-800">
                    旧执行引擎已退役。请 Fork 并切换到 Claude Code 或 DeepSeek Harness 后选择只读、可写需授权或 Session 内完整权限。
                  </div>
                </Section>
              ) : (
                <Section title="Session 权限">
                  <div className="grid grid-cols-1 gap-2">
                    {[
                      ['read_only', 'Read only / 只读', '仅允许读取当前 Session 工作区和只读信息源；写入、执行、调度等权限请求会被策略拒绝。'],
                      [
                        'workspace_write',
                        'Write but need allow / 可写但需授权',
                        settings.engine_id === 'claude_code'
                          ? '可读写当前 Session 工作区；Claude 原生权限请求由页面逐次确认后执行。'
                          : '以只读沙箱起步；写入由 DeepSeek Harness 原生升级请求逐次授权，越界操作自动拒绝。',
                      ],
                      ['session_full', 'Full access / Session 内完整权限', '在当前 Session 的工作区、沙箱和出网边界内免逐次确认；仍不能访问其他 Session、宿主机目录或 Docker Socket。'],
                    ].map(([value, title, description]) => (
                      <label key={value} className={`cursor-pointer rounded-xl border p-3 ${normalizePermissionPreset(settings.permission_preset) === value ? 'border-indigo-400 bg-indigo-50' : 'border-stone-200 bg-white'}`}>
                        <div className="flex items-center gap-2 text-sm font-medium text-slate-800">
                          <input
                            type="radio"
                            name="permission-preset"
                            value={value}
                            checked={(settings.permission_preset || DEFAULT_PERMISSION_PRESET) === value}
                            onChange={() => setSettings({ ...settings, permission_preset: value })}
                          />
                          {title}
                        </div>
                        <div className="mt-1 pl-5 text-[11px] leading-relaxed text-slate-500">{description}</div>
                      </label>
                    ))}
                  </div>
                  <div className="mt-2 rounded-lg bg-stone-100 px-3 py-2 text-[11px] text-slate-600">
                    所有级别始终只挂载当前用户的当前 Session 工作区；无法访问其他用户、其他 Session、宿主机目录或 Docker Socket。
                  </div>
                </Section>
              )}
              {settings.engine_id === 'deepseek_harness' && (
                <Section title="DeepSeek 原生视图（只读）">
                  <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                    <div className="flex flex-wrap gap-2">
                      {(settings.tool_surface?.deepseek_native_tools || []).map((tool) => (
                        <span key={tool} className="inline-flex items-center rounded-full border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600">{tool}</span>
                      ))}
                    </div>
                    <div className="mt-2 text-xs text-slate-500">这是固定于当前部署镜像的上游原生工具图，仅用于观察；网页不对它做二次编排。权限由上面的 Session 预设、单 Workspace 挂载和原生审批共同约束。</div>
                  </div>
                </Section>
              )}
              <div className="text-xs text-slate-500">累计 Token：{settings.usage.total_tokens.toLocaleString()}（输入 {settings.usage.input_tokens.toLocaleString()} / 输出 {settings.usage.output_tokens.toLocaleString()}）</div>
              {settings.engine_id !== 'legacy' && (
                <button onClick={saveSettings} disabled={saving} className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm flex items-center gap-2"><FiSave />保存运行配置</button>
              )}
            </div>
          )}

          {loadedConvId === convId && tab === 'workspace' && (
            <div className={`grid ${gridTemplate} gap-3`}>
              {/* File list */}
              <div className="border border-stone-200 rounded-xl overflow-hidden flex flex-col">
                <div className="p-2 border-b flex justify-between items-center">
                  <span className="text-xs font-medium">文件</span>
                  <button onClick={newFile} title="新建文件" className="p-1 rounded hover:bg-stone-100"><FiPlus size={14} /></button>
                </div>
                <div className="flex-1 overflow-y-auto max-h-[620px]">
                  {workspace.files.map((f) => (
                    <div key={f.path} className="flex w-full items-stretch group">
                      <button
                        onClick={() => openFile(f.path)}
                        className={`flex min-w-0 flex-1 items-center gap-2 text-left px-3 py-1.5 text-xs border-l-2 ${selectedFile === f.path ? 'bg-indigo-50 text-indigo-700 border-indigo-500 font-medium' : 'hover:bg-stone-50 border-transparent text-slate-700'}`}
                      >
                        <span className="min-w-0 flex-1 truncate">{f.path}</span>
                        {f.preview_kind && (
                          <span className="shrink-0 text-[10px] text-slate-400">{f.preview_kind}</span>
                        )}
                      </button>
                      <button
                        onClick={() => downloadFile(f.path)}
                        title={`下载 ${f.path}`}
                        aria-label={`下载 ${f.path}`}
                        className="shrink-0 px-2 text-slate-400 hover:text-indigo-700 hover:bg-indigo-50"
                      >
                        <FiDownload size={13} />
                      </button>
                    </div>
                  ))}
                  {workspace.files.length === 0 && (
                    <div className="p-3 text-xs text-slate-400 italic">工作区为空</div>
                  )}
                </div>
              </div>

              {/* Editor pane */}
              {showEditor && (
                <div className="flex flex-col border border-stone-200 rounded-xl overflow-hidden">
                  <div className="p-2 border-b flex justify-between items-center bg-stone-50">
                    <span className="text-xs font-mono truncate flex-1">{selectedFile || '选择文件'}</span>
                    <div className="flex gap-1">
                      {isMd && (
                        <div className="flex items-center gap-0.5 mr-2 bg-white rounded border border-stone-200 p-0.5">
                          <button
                            onClick={() => setPreviewMode('edit')}
                            title="仅编辑"
                            className={`p-1 rounded ${previewMode === 'edit' ? 'bg-indigo-100 text-indigo-700' : 'text-slate-500 hover:bg-stone-100'}`}
                          >
                            <FiEdit3 size={12} />
                          </button>
                          <button
                            onClick={() => setPreviewMode('split')}
                            title="分屏"
                            className={`p-1 rounded ${previewMode === 'split' ? 'bg-indigo-100 text-indigo-700' : 'text-slate-500 hover:bg-stone-100'}`}
                          >
                            <FiColumns size={12} />
                          </button>
                          <button
                            onClick={() => setPreviewMode('preview')}
                            title="仅预览"
                            className={`p-1 rounded ${previewMode === 'preview' ? 'bg-indigo-100 text-indigo-700' : 'text-slate-500 hover:bg-stone-100'}`}
                          >
                            <FiEye size={12} />
                          </button>
                        </div>
                      )}
                      <button onClick={() => downloadFile(selectedFile)} disabled={!selectedFile} title="下载到本地" className="p-1 rounded hover:bg-stone-100 disabled:opacity-40"><FiDownload size={14} /></button>
                      <button onClick={saveFile} disabled={!selectedFile} title="保存" className="p-1 rounded hover:bg-stone-100 disabled:opacity-40"><FiSave size={14} /></button>
                      <button onClick={removeFile} disabled={!selectedFile} title="删除" className="p-1 rounded hover:bg-stone-100 disabled:opacity-40"><FiTrash2 size={14} /></button>
                    </div>
                  </div>
                  <textarea
                    value={fileContent}
                    onChange={(e) => setFileContent(e.target.value)}
                    disabled={!selectedFile}
                    className="flex-1 min-h-[560px] resize-none p-3 font-mono text-xs outline-none border-0 focus:ring-0"
                    placeholder={selectedFile ? '' : '点击左侧文件查看内容'}
                    spellCheck={false}
                  />
                </div>
              )}

              {/* Preview pane */}
              {showPreview && (
                <div className="flex flex-col border border-stone-200 rounded-xl overflow-hidden bg-white">
                  <div className="p-2 border-b flex justify-between items-center bg-stone-50">
                    <span className="text-xs font-medium flex items-center gap-1.5">
                      <FiEye size={12} /> 预览
                    </span>
                    <span className="text-[10px] text-slate-400 font-mono truncate max-w-[200px]">{selectedFile}</span>
                  </div>
                  <div className={`flex-1 ${['pdf', 'image'].includes(previewKind) ? 'overflow-hidden p-0' : 'overflow-y-auto p-4'} ${isMd ? 'prose prose-sm max-w-none' : ''}`}>
                    {previewKind === 'markdown' ? (
                      fileContent ? (
                        <ReactMarkdown
                          remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
                          rehypePlugins={[rehypeHighlight]}
                          components={mdComponents}
                        >
                          {fileContent}
                        </ReactMarkdown>
                      ) : (
                        <div className="text-xs text-slate-400 italic">无内容</div>
                      )
                    ) : previewKind === 'text' ? (
                      <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-slate-700">{fileContent || '无内容'}</pre>
                    ) : previewKind === 'pdf' && fileBlobUrl ? (
                      <iframe src={fileBlobUrl} title={selectedFile} className="w-full h-full min-h-[560px] bg-stone-50" />
                    ) : previewKind === 'image' && fileBlobUrl ? (
                      <div className="h-full min-h-[560px] flex items-center justify-center bg-stone-50 p-4">
                        <img src={fileBlobUrl} alt={selectedFile} className="max-w-full max-h-full rounded-lg shadow-sm" />
                      </div>
                    ) : previewKind === 'office' ? (
                      <FileFallback fileBlobUrl={fileBlobUrl} selectedFile={selectedFile} label="Office 文件暂不做在线转码预览" />
                    ) : (
                      <FileFallback fileBlobUrl={fileBlobUrl} selectedFile={selectedFile} label="该文件类型不可直接预览" />
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {loadedConvId === convId && tab === 'artifacts' && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <div className="text-sm font-medium text-slate-800">Session artifacts</div>
                <button onClick={async () => setArtifacts((await getArtifacts(convId)).artifacts || [])} className="secondary">刷新</button>
              </div>
              {artifacts.map((artifact) => (
                <button
                  key={artifact.id}
                  onClick={() => {
                    if (artifact.path) {
                      setTab('workspace')
                      openFile(artifact.path)
                    }
                  }}
                  className="w-full text-left p-3 border border-stone-200 rounded-xl bg-white hover:bg-stone-50"
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-slate-800 truncate">{artifact.title || artifact.path || artifact.id}</div>
                      <div className="text-xs text-slate-500 mt-1 truncate">{artifact.path || artifact.kind} · {artifact.preview_kind || artifact.mime_type || 'artifact'}</div>
                    </div>
                    <div className="text-right text-[11px] text-slate-400 shrink-0">
                      <div>{(artifact.size_bytes || 0).toLocaleString()} bytes</div>
                      <div className="font-mono">{(artifact.run_id || '').slice(0, 8)}</div>
                    </div>
                  </div>
                  {artifact.summary && <div className="mt-2 text-xs text-slate-500 line-clamp-2">{artifact.summary}</div>}
                </button>
              ))}
              {artifacts.length === 0 && <div className="text-xs text-slate-400 italic">暂无 artifact</div>}
            </div>
          )}

          {loadedConvId === convId && tab === 'tasks' && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <div className="text-sm font-medium text-slate-800">Durable tasks</div>
                <button onClick={async () => setTasks((await getTasks(convId)).tasks || [])} className="secondary">刷新</button>
              </div>
              {tasks.map((task) => (
                <button key={task.id} onClick={() => openRunEvents(task.run_id)} className="w-full text-left p-3 border border-stone-200 rounded-xl bg-white hover:bg-stone-50">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-slate-800 truncate">{task.title || task.task_key}</div>
                      <div className="text-xs text-slate-500 mt-1">{task.kind} · {task.agent_name || 'agent'} · <span className="font-mono">{(task.run_id || '').slice(0, 8)}</span></div>
                    </div>
                    <span className={task.status === 'succeeded' ? 'text-green-600 text-xs' : task.status === 'failed' ? 'text-red-600 text-xs' : task.status === 'blocked' ? 'text-amber-600 text-xs' : 'text-indigo-600 text-xs'}>{task.status}</span>
                  </div>
                  {task.summary && <div className="mt-2 text-xs text-slate-500 line-clamp-2">{task.summary}</div>}
                  {task.error && <div className="mt-2 text-xs text-red-600 line-clamp-2">{task.error}</div>}
                </button>
              ))}
              {tasks.length === 0 && <div className="text-xs text-slate-400 italic">暂无 durable task</div>}
            </div>
          )}

          {loadedConvId === convId && tab === 'goal' && (
            <div className="space-y-4">
              <Field label="持久目标"><textarea value={goal.objective || ''} onChange={(e) => setGoal({ ...goal, objective: e.target.value })} rows={5} className="input" /></Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="状态"><select value={goal.status || 'active'} onChange={(e) => setGoal({ ...goal, status: e.target.value })} className="input"><option value="active">active</option><option value="paused">paused</option><option value="blocked">blocked</option><option value="complete">complete</option></select></Field>
                <Field label="Token 预算"><input type="number" value={goal.token_budget || ''} onChange={(e) => setGoal({ ...goal, token_budget: e.target.value })} className="input" /></Field>
              </div>
              <Field label="备注"><input value={goal.note || ''} onChange={(e) => setGoal({ ...goal, note: e.target.value })} className="input" /></Field>
              <div className="text-xs text-slate-500">已使用：{(goal.tokens_used || 0).toLocaleString()} tokens</div>
              <div className="flex gap-2"><button onClick={saveGoal} className="primary"><FiSave />保存目标</button><button onClick={async () => { await clearGoal(convId); setGoal({}) }} className="secondary"><FiTrash2 />清除</button></div>
            </div>
          )}

          {loadedConvId === convId && tab === 'automation' && (
            <div className="space-y-5">
              <form onSubmit={addJob} className="p-4 border border-stone-200 rounded-xl space-y-3">
                <div className="font-medium text-sm">新建定时任务</div>
                <div className="grid grid-cols-2 gap-3"><Field label="名称"><input required value={jobForm.name} onChange={(e) => setJobForm({ ...jobForm, name: e.target.value })} className="input" /></Field><Field label="计划"><input required value={jobForm.schedule} onChange={(e) => setJobForm({ ...jobForm, schedule: e.target.value })} className="input" placeholder="every 1h / 0 9 * * *" /></Field></div>
                <Field label="任务提示"><textarea required rows={3} value={jobForm.prompt} onChange={(e) => setJobForm({ ...jobForm, prompt: e.target.value })} className="input" /></Field>
                <button className="primary"><FiPlus />创建</button>
              </form>
              <div className="space-y-2">
                {jobs.map((job) => <div key={job.id} className="p-3 border border-stone-200 rounded-xl flex gap-3 items-start">
                  <div className="flex-1 min-w-0"><div className="font-medium text-sm">{job.name}</div><div className="text-xs text-slate-500 mt-1">{job.schedule_kind}: {job.schedule_value} · 下次 {job.next_run_at || '已暂停'}</div><div className="text-xs text-slate-400 truncate mt-1">{job.prompt}</div></div>
                  <button onClick={() => runSchedule(job.id)} title="立即运行" className="p-1.5 rounded hover:bg-stone-100"><FiPlay size={14} /></button>
                  <button onClick={async () => { await updateSchedule(job.id, { enabled: !job.enabled }); setJobs(await getSchedules(convId)) }} title={job.enabled ? '暂停' : '恢复'} className={`p-1.5 rounded hover:bg-stone-100 ${job.enabled ? 'text-green-600' : 'text-slate-400'}`}><FiClock size={14} /></button>
                  <button onClick={async () => { await deleteSchedule(job.id); setJobs(await getSchedules(convId)) }} title="删除" className="p-1.5 rounded hover:bg-stone-100 text-red-500"><FiTrash2 size={14} /></button>
                </div>)}
              </div>
            </div>
          )}

          {loadedConvId === convId && tab === 'mcp' && (
            <div className="space-y-5">
              <form onSubmit={addMcp} className="p-4 border border-stone-200 rounded-xl space-y-3">
                <div className="font-medium text-sm">会话级原生 MCP Server</div>
                <div className="text-[11px] leading-relaxed text-slate-500">声明由 Backend 持久化，并在每个 Claude Code / DeepSeek Harness Turn 中投影到隔离运行时；页面不维持常驻 MCP 进程。</div>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="名称"><input required value={mcpForm.name} onChange={(e) => setMcpForm({ ...mcpForm, name: e.target.value })} className="input" /></Field>
                  <Field label="传输"><select value={mcpForm.transport} onChange={(e) => setMcpForm({ ...mcpForm, transport: e.target.value })} className="input"><option value="http">HTTP</option><option value="sse">SSE</option><option value="stdio">stdio</option></select></Field>
                </div>
                <Field label="URL（HTTP/SSE）"><input value={mcpForm.url} onChange={(e) => setMcpForm({ ...mcpForm, url: e.target.value })} className="input" placeholder="https://mcp.example.com/mcp" /></Field>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Command（stdio 可执行文件）"><input value={mcpForm.command} onChange={(e) => setMcpForm({ ...mcpForm, command: e.target.value })} className="input" placeholder="python" /></Field>
                  <Field label="Args（JSON 字符串数组）"><input value={mcpForm.args} onChange={(e) => setMcpForm({ ...mcpForm, args: e.target.value })} className="input" placeholder='["/path/server.py"]' /></Field>
                </div>
                <button className="primary"><FiPlus />添加并连接</button>
              </form>
              <div className="space-y-2">
                {mcpServers.map((server) => <div key={server.name} className="p-3 border border-stone-200 rounded-xl flex gap-3 items-center">
                  <div className="flex-1"><div className="text-sm font-medium">{server.name}</div><div className="text-xs text-slate-500">{server.transport || 'auto'} · {server.scope === 'user' ? '用户级' : '会话级'} · Turn 内隔离绑定</div></div>
                  <button onClick={async () => { await deleteMcpServer(server.name, convId); const data = await getMcpServers(convId); setMcpServers(data.servers || []) }} className="p-1.5 rounded hover:bg-stone-100 text-red-500"><FiTrash2 size={14} /></button>
                </div>)}
              </div>
            </div>
          )}

          {loadedConvId === convId && tab === 'hooks' && (
            <div className="space-y-5">
              <form onSubmit={addHook} className="p-4 border border-stone-200 rounded-xl space-y-3">
                <div className="font-medium text-sm">生命周期 Webhook</div>
                <Field label="名称"><input required value={hookForm.name} onChange={(e) => setHookForm({ ...hookForm, name: e.target.value })} className="input" /></Field>
                <Field label="URL"><input required type="url" value={hookForm.url} onChange={(e) => setHookForm({ ...hookForm, url: e.target.value })} className="input" /></Field>
                <Field label="事件（逗号分隔）"><input required value={hookForm.events} onChange={(e) => setHookForm({ ...hookForm, events: e.target.value })} className="input" /></Field>
                <Field label="签名密钥（可选）"><input type="password" value={hookForm.secret} onChange={(e) => setHookForm({ ...hookForm, secret: e.target.value })} className="input" /></Field>
                <button className="primary"><FiPlus />创建 Hook</button>
              </form>
              <div className="space-y-2">
                {hooks.filter((hook) => !hook.conversation_id || hook.conversation_id === convId).map((hook) => <div key={hook.id} className="p-3 border border-stone-200 rounded-xl flex gap-3 items-center">
                  <div className="flex-1 min-w-0"><div className="text-sm font-medium">{hook.name}</div><div className="text-xs text-slate-500 truncate">{hook.events.join(', ')} · {hook.url}</div></div>
                  <button onClick={async () => { await updateHook(hook.id, { enabled: !hook.enabled }); setHooks(await getHooks()) }} title={hook.enabled ? '暂停' : '启用'} className={`p-1.5 rounded hover:bg-stone-100 ${hook.enabled ? 'text-green-600' : 'text-slate-400'}`}><FiZap size={14} /></button>
                  <button onClick={async () => { await deleteHook(hook.id); setHooks(await getHooks()) }} className="p-1.5 rounded hover:bg-stone-100 text-red-500"><FiTrash2 size={14} /></button>
                </div>)}
              </div>
            </div>
          )}

          {loadedConvId === convId && tab === 'runs' && (
            <div className="grid grid-cols-[1fr_1.1fr] gap-4">
              <div className="space-y-3">
                <button onClick={() => downloadTrajectory(convId)} className="primary"><FiDownload />导出脱敏轨迹 JSON</button>
                {(runTree.length ? runTree : runs).map((run) => (
                  <RunNode key={run.id} run={run} onOpenEvents={openRunEvents} />
                ))}
                {runs.length === 0 && <div className="text-xs text-slate-400 italic">暂无运行轨迹</div>}
              </div>
              <div className="border border-stone-200 rounded-xl overflow-hidden min-h-[420px]">
                <div className="px-3 py-2 border-b bg-stone-50 text-xs font-medium">事件日志 {runEvents.runId ? <span className="font-mono text-slate-400">{runEvents.runId.slice(0, 8)}</span> : null}</div>
                <div className="p-3 space-y-2 max-h-[640px] overflow-y-auto">
                  {runEvents.events.map((event) => (
                    <div key={event.id} className="text-[11px] border border-stone-100 rounded-lg p-2 bg-white">
                      <div className="flex justify-between gap-2"><span className="font-medium text-slate-700">{event.seq}. {event.event_type}</span><span className="text-slate-400">{event.tool_name || ''}</span></div>
                      <pre className="mt-1 whitespace-pre-wrap break-words text-slate-500 font-mono">{JSON.stringify(event.payload || {}, null, 2)}</pre>
                    </div>
                  ))}
                  {!runEvents.runId && <div className="text-xs text-slate-400 italic">点击左侧 run 查看 normalized events</div>}
                  {runEvents.runId && runEvents.events.length === 0 && <div className="text-xs text-slate-400 italic">该 run 暂无事件</div>}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function FileFallback({ fileBlobUrl, selectedFile, label }) {
  return (
    <div className="h-full min-h-[560px] flex items-center justify-center bg-stone-50 p-6 text-center">
      <div className="max-w-sm">
        <div className="text-sm font-medium text-slate-700">{label}</div>
        <div className="mt-2 text-xs text-slate-500 break-all">{selectedFile}</div>
        {fileBlobUrl && (
          <a
            href={fileBlobUrl}
            target="_blank"
            rel="noreferrer"
            download={selectedFile?.split('/').pop() || undefined}
            className="inline-flex items-center gap-1.5 mt-4 px-3 py-1.5 rounded-lg bg-indigo-600 text-white text-xs font-medium hover:bg-indigo-700"
          >
            <FiDownload size={12} /> 打开 / 下载
          </a>
        )}
      </div>
    </div>
  )
}

function Section({ title, children }) {
  return <div><div className="text-xs font-semibold text-slate-600 mb-2">{title}</div>{children}</div>
}

function Field({ label, children }) {
  return <label className="block"><span className="block text-xs text-slate-500 mb-1">{label}</span>{children}</label>
}

function RunNode({ run, onOpenEvents }) {
  return (
    <div className="border border-stone-200 rounded-xl p-3 bg-white">
      <button onClick={() => onOpenEvents(run.id)} className="w-full text-left">
        <div className="flex justify-between text-sm gap-2">
          <span className="font-medium truncate">{run.agent_name || run.agent_kind || run.resolved_model_id || run.requested_model_id}</span>
          <span className={run.status === 'succeeded' ? 'text-green-600' : run.status === 'failed' ? 'text-red-600' : 'text-amber-600'}>{run.status}</span>
        </div>
        <div className="text-xs text-slate-500 mt-1">
          depth {run.depth || 0} · {run.workspace_scope || 'shared_session'} · {(run.usage?.total_tokens || 0).toLocaleString()} tokens · {run.finish_reason || '—'}
        </div>
        <div className="text-[10px] text-slate-400 mt-1 font-mono truncate">{run.id}</div>
      </button>
      {run.tool_events?.length > 0 && <div className="mt-2 text-[11px] text-slate-500 bg-stone-50 p-2 rounded">{run.tool_events.slice(-5).join(' · ')}</div>}
      {run.error && <div className="mt-2 text-xs text-red-600">{run.error}</div>}
      {run.children?.length > 0 && (
        <div className="mt-3 ml-4 pl-3 border-l border-indigo-100 space-y-2">
          {run.children.map((child) => <RunNode key={child.id} run={child} onOpenEvents={onOpenEvents} />)}
        </div>
      )}
    </div>
  )
}
