import { useState } from 'react'
import {
  FiCheck, FiChevronDown, FiChevronRight, FiCpu, FiLoader,
  FiTool, FiX, FiShield, FiUsers,
} from 'react-icons/fi'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'

function Markdown({ children }) {
  return (
    <ReactMarkdown
      remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
      rehypePlugins={[rehypeHighlight]}
      components={{
        p: ({ children: value }) => <p className="my-2 leading-[1.7]">{value}</p>,
        ul: ({ children: value }) => <ul className="list-disc ml-5 my-2 space-y-1">{value}</ul>,
        ol: ({ children: value }) => <ol className="list-decimal ml-5 my-2 space-y-1">{value}</ol>,
        a: (props) => <a {...props} target="_blank" rel="noreferrer" className="text-indigo-600 underline" />,
        pre: ({ children: value }) => <pre className="my-3 max-h-[60vh] overflow-auto rounded-xl bg-slate-950 p-4 text-xs text-slate-100">{value}</pre>,
        code: ({ className, children: value, ...props }) => <code className={className || 'font-mono text-[0.9em] text-rose-600'} {...props}>{value}</code>,
        table: ({ children: value }) => <div className="my-3 overflow-x-auto"><table className="w-full text-xs">{value}</table></div>,
        th: ({ children: value }) => <th className="border px-2 py-1 text-left">{value}</th>,
        td: ({ children: value }) => <td className="border px-2 py-1">{value}</td>,
      }}
    >
      {children || ''}
    </ReactMarkdown>
  )
}

function Spinner() {
  return <FiLoader className="animate-spin" size={12} />
}

function statusView(status) {
  if (['success', 'succeeded', 'completed', 'recovered', 'allowed'].includes(status)) {
    return { icon: <FiCheck size={12} />, label: status === 'allowed' ? '已允许' : '完成', tone: 'text-emerald-600' }
  }
  if (['failed', 'rejected', 'cancelled', 'denied'].includes(status)) {
    return { icon: <FiX size={12} />, label: ['denied', 'rejected'].includes(status) ? '已拒绝' : status === 'cancelled' ? '已取消' : '失败', tone: 'text-red-600' }
  }
  if (status === 'degraded') return { icon: <FiCheck size={12} />, label: '降级完成', tone: 'text-amber-600' }
  return { icon: <Spinner />, label: '执行中', tone: 'text-indigo-600' }
}

function ReasoningNode({ node, streaming }) {
  const [open, setOpen] = useState(Boolean(streaming))
  return (
    <div className="border-l-2 border-violet-200 pl-3 py-1">
      <button onClick={() => setOpen((value) => !value)} className="flex items-center gap-1 text-xs font-medium text-violet-600">
        {open ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
        思考{streaming ? '中' : ''}
      </button>
      {open && <div className="mt-1 max-h-80 overflow-y-auto whitespace-pre-wrap rounded-lg bg-violet-50/60 px-3 py-2 text-xs leading-relaxed text-slate-500">{node.text}</div>}
    </div>
  )
}

function ToolNode({ node }) {
  const event = node.payload?.event || {}
  const view = statusView(node.status)
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs shadow-sm">
      <div className="flex flex-wrap items-center gap-2">
        <FiTool className="text-indigo-500" size={12} />
        <span className="font-medium text-slate-700">{event.tool_name || '工具调用'}</span>
        <span className={`ml-auto flex items-center gap-1 ${view.tone}`}>{view.icon}{view.label}</span>
      </div>
      {(event.error || event.payload?.error || event.payload?.detail) && <div className="mt-1.5 whitespace-pre-wrap text-red-600">{event.error || event.payload?.error || event.payload?.detail}</div>}
    </div>
  )
}

function WorkflowNode({ node }) {
  const [open, setOpen] = useState(true)
  const runs = node.runs || []
  const root = runs.find((run) => Number(run.depth || 0) === 0)
  const children = runs.filter((run) => Number(run.depth || 0) > 0)
  const completed = children.filter((run) => !['running', 'queued'].includes(run.lifecycle_status || run.status)).length
  const view = statusView(root?.lifecycle_status || root?.status || node.status)
  return (
    <div className="rounded-2xl border border-indigo-200 bg-indigo-50/40 shadow-sm">
      <button onClick={() => setOpen((value) => !value)} className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs">
        <FiUsers className="text-indigo-600" size={14} />
        <span className="font-semibold text-slate-800">多代理工作流</span>
        {children.length > 0 && <span className="text-slate-500">{completed}/{children.length} 子任务</span>}
        <span className={`ml-auto flex items-center gap-1 ${view.tone}`}>{view.icon}{view.label}</span>
        {open ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
      </button>
      {open && (
        <div className="space-y-2 border-t border-indigo-100 px-3 py-3">
          {children.length === 0 && <div className="text-xs text-slate-500">主代理正在规划或直接执行当前任务。</div>}
          {children.map((run) => {
            const childView = statusView(run.lifecycle_status || run.status)
            return (
              <details key={run.id} open={(run.lifecycle_status || run.status) === 'running'} className="rounded-xl border border-white bg-white/80 px-3 py-2 text-xs">
                <summary className="flex cursor-pointer list-none items-center gap-2">
                  <FiCpu className="text-indigo-500" size={12} />
                  <span className="min-w-0 flex-1 truncate font-medium text-slate-700">{run.display_name || run.agent_name || '子代理'}</span>
                  <span className={`flex items-center gap-1 ${childView.tone}`}>{childView.icon}{childView.label}</span>
                </summary>
                {run.preview && <div className="mt-2 max-h-28 overflow-y-auto whitespace-pre-wrap text-slate-600">{run.preview}</div>}
                {run.tools?.length > 0 && <div className="mt-2 flex flex-wrap gap-1">{run.tools.map((tool) => <span key={tool.tool_call_id || `${tool.name}-${tool.attempt_index}`} className="rounded-full border border-slate-200 px-2 py-0.5 text-slate-500">{tool.name} · {statusView(tool.status).label}</span>)}</div>}
                {run.artifacts?.length > 0 && <div className="mt-2 text-emerald-700">产物：{run.artifacts.map((item) => item.title || item.path).join('、')}</div>}
                {run.error && <div className="mt-2 whitespace-pre-wrap text-red-600">{run.error}</div>}
              </details>
            )
          })}
        </div>
      )}
    </div>
  )
}

function ApprovalNode({ node, onApproval }) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const value = node.payload || {}
  const pending = value.status === 'pending'
  const view = statusView(value.status)
  async function decide(decision) {
    if (!pending || !onApproval || submitting) return
    setSubmitting(true)
    setError('')
    try {
      await onApproval(value, decision)
    } catch (reason) {
      setError(reason?.message || '权限决定提交失败')
    } finally {
      setSubmitting(false)
    }
  }
  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <FiShield className="text-amber-600" size={13} />
        <span className="font-semibold text-slate-800">权限请求 · {value.tool_name || '工具'}</span>
        {!pending && <span className={`ml-auto flex items-center gap-1 ${view.tone}`}>{view.icon}{view.label}</span>}
      </div>
      {(value.title || value.description || value.decision_reason) && <div className="mt-1.5 whitespace-pre-wrap text-slate-600">{value.title || value.description || value.decision_reason}</div>}
      {pending && <div className="mt-2 flex gap-2"><button disabled={submitting} onClick={() => decide('allow')} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-white disabled:opacity-50">仅本次允许</button><button disabled={submitting} onClick={() => decide('deny')} className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-700 disabled:opacity-50">拒绝</button>{submitting && <Spinner />}</div>}
      {error && <div className="mt-2 text-red-600">{error}</div>}
    </div>
  )
}

export default function TurnTimeline({ nodes = [], streaming = false, onApproval }) {
  return (
    <div className="space-y-2.5">
      {nodes.map((node, index) => {
        if (node.kind === 'content') return <div key={node.nodeId} className="text-[14px] text-slate-800 break-words"><Markdown>{node.text}</Markdown>{streaming && index === nodes.length - 1 && <span className="inline-block h-[1em] w-[2px] animate-pulse bg-indigo-500" />}</div>
        if (node.kind === 'reasoning') return <ReasoningNode key={node.nodeId} node={node} streaming={streaming && index === nodes.length - 1} />
        if (node.kind === 'progress') return <div key={node.nodeId} className="flex items-start gap-2 border-l-2 border-slate-200 py-1 pl-3 text-xs text-slate-500"><FiLoader className={streaming && index === nodes.length - 1 ? 'mt-0.5 animate-spin' : 'mt-0.5'} size={11} /><span className="whitespace-pre-wrap">{node.text}</span></div>
        if (node.kind === 'tool') return <ToolNode key={node.nodeId} node={node} />
        if (node.kind === 'workflow') return <WorkflowNode key={node.nodeId} node={node} />
        if (node.kind === 'approval') return <ApprovalNode key={node.nodeId} node={node} onApproval={onApproval} />
        return null
      })}
    </div>
  )
}
