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
              <details key={run.id} defaultOpen={(run.lifecycle_status || run.status) === 'running'} className="rounded-xl border border-white bg-white/80 px-3 py-2 text-xs">
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

function QuestionNode({ node, onApproval }) {
  const [selected, setSelected] = useState({})
  const [custom, setCustom] = useState({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const value = node.payload || {}
  const questions = value.questions || []
  const pending = value.status === 'pending'

  function choose(question, label) {
    setCustom((current) => ({ ...current, [question.question]: '' }))
    setSelected((current) => {
      if (!question.multi_select) return { ...current, [question.question]: label }
      const previous = Array.isArray(current[question.question]) ? current[question.question] : []
      const next = previous.includes(label)
        ? previous.filter((item) => item !== label)
        : [...previous, label]
      return { ...current, [question.question]: next }
    })
  }

  async function submit(decision) {
    if (!pending || !onApproval || submitting) return
    const answers = {}
    if (decision === 'allow') {
      for (const question of questions) {
        const freeText = (custom[question.question] || '').trim()
        const choice = selected[question.question]
        const answer = freeText || (Array.isArray(choice) ? choice.join(', ') : choice || '')
        if (!answer) {
          setError('请回答每一个问题')
          return
        }
        answers[question.question] = answer
      }
    }
    setSubmitting(true)
    setError('')
    try {
      await onApproval(value, decision, decision === 'allow' ? answers : null)
    } catch (reason) {
      setError(reason?.message || '回答提交失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="rounded-xl border border-sky-200 bg-sky-50 px-3 py-3 text-xs">
      <div className="flex items-center gap-2 font-semibold text-slate-800">
        <FiUsers className="text-sky-600" size={13} />原生引擎需要你的输入
        {!pending && <span className={value.status === 'denied' ? 'ml-auto text-red-600' : 'ml-auto text-emerald-600'}>{value.status === 'denied' ? '未回答' : '已回答'}</span>}
      </div>
      {pending && <div className="mt-3 space-y-4">
        {questions.map((question) => {
          const current = selected[question.question]
          return <fieldset key={question.question} className="space-y-2">
            <legend className="font-medium text-slate-700">{question.header ? `${question.header} · ` : ''}{question.question}</legend>
            {question.options.map((option) => {
              const checked = question.multi_select
                ? Array.isArray(current) && current.includes(option.label)
                : current === option.label
              return <label key={option.label} className="flex cursor-pointer items-start gap-2 rounded-lg border border-sky-100 bg-white px-2.5 py-2">
                <input
                  type={question.multi_select ? 'checkbox' : 'radio'}
                  name={`question-${node.nodeId}-${question.question}`}
                  checked={checked}
                  onChange={() => choose(question, option.label)}
                  className="mt-0.5"
                />
                <span><span className="font-medium text-slate-700">{option.label}</span>{option.description && <span className="ml-1 text-slate-500">{option.description}</span>}</span>
              </label>
            })}
            <input
              value={custom[question.question] || ''}
              onChange={(event) => {
                setCustom((currentCustom) => ({ ...currentCustom, [question.question]: event.target.value }))
                setSelected((currentSelected) => ({ ...currentSelected, [question.question]: question.multi_select ? [] : '' }))
              }}
              placeholder="其他答案（可直接输入）"
              maxLength={4000}
              className="w-full rounded-lg border border-sky-200 bg-white px-2.5 py-2 outline-none focus:border-sky-400"
            />
          </fieldset>
        })}
        <div className="flex gap-2">
          <button disabled={submitting} onClick={() => submit('allow')} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-white disabled:opacity-50">提交回答</button>
          <button disabled={submitting} onClick={() => submit('deny')} className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-700 disabled:opacity-50">不回答</button>
          {submitting && <Spinner />}
        </div>
      </div>}
      {error && <div className="mt-2 text-red-600">{error}</div>}
    </div>
  )
}

function PermissionApprovalNode({ node, onApproval }) {
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const value = node.payload || {}
  const pending = value.status === 'pending'
  const isUserAction = value.interaction_kind === 'user_action'
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
        <span className="font-semibold text-slate-800">{isUserAction ? '原生引擎请求确认' : `权限请求 · ${value.tool_name || '工具'}`}</span>
        {!pending && <span className={`ml-auto flex items-center gap-1 ${view.tone}`}>{view.icon}{view.label}</span>}
      </div>
      {(value.title || value.description || value.decision_reason) && <div className="mt-1.5 whitespace-pre-wrap text-slate-600">{value.title || value.description || value.decision_reason}</div>}
      {pending && <div className="mt-2 flex gap-2"><button disabled={submitting} onClick={() => decide('allow')} className="rounded-lg bg-indigo-600 px-3 py-1.5 text-white disabled:opacity-50">{isUserAction ? '确认' : '仅本次允许'}</button><button disabled={submitting} onClick={() => decide('deny')} className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-slate-700 disabled:opacity-50">{isUserAction ? '返回修改' : '拒绝'}</button>{submitting && <Spinner />}</div>}
      {error && <div className="mt-2 text-red-600">{error}</div>}
    </div>
  )
}

function ApprovalNode({ node, onApproval }) {
  if (node.payload?.interaction_kind === 'question') {
    return <QuestionNode node={node} onApproval={onApproval} />
  }
  return <PermissionApprovalNode node={node} onApproval={onApproval} />
}

export default function TurnTimeline({ nodes = [], streaming = false, onApproval }) {
  return (
    <div className="space-y-2.5">
      {nodes.map((node, index) => {
        if (node.kind === 'content') return <div key={node.nodeId} className="text-[14px] text-slate-800 break-words"><Markdown>{node.text}</Markdown>{streaming && index === nodes.length - 1 && <span className="inline-block h-[1em] w-[2px] animate-pulse bg-indigo-500" />}</div>
        if (node.kind === 'reasoning') return <ReasoningNode key={node.nodeId} node={node} streaming={streaming && index === nodes.length - 1} />
        if (node.kind === 'progress') {
          const view = statusView(node.status)
          return <div key={node.nodeId} className="flex items-start gap-2 border-l-2 border-slate-200 py-1 pl-3 text-xs text-slate-500"><span className={`mt-0.5 ${view.tone}`}>{view.icon}</span><span className="whitespace-pre-wrap">{node.text}</span></div>
        }
        if (node.kind === 'tool') return <ToolNode key={node.nodeId} node={node} />
        if (node.kind === 'workflow') return <WorkflowNode key={node.nodeId} node={node} />
        if (node.kind === 'approval') return <ApprovalNode key={node.nodeId} node={node} onApproval={onApproval} />
        return null
      })}
    </div>
  )
}
