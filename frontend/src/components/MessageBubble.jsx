import { useState, useEffect } from 'react'
import {
  FiChevronDown, FiChevronRight, FiCopy, FiCheck, FiCpu, FiX,
  FiTool, FiRefreshCw, FiAlertCircle,
} from 'react-icons/fi'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'

function CodeBlock({ children }) {
  const [copied, setCopied] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const codeNode = Array.isArray(children) ? children[0] : children
  const langClass = codeNode?.props?.className || ''
  const lang = langClass.replace(/^language-/, '').replace(/\s.*$/, '')
  const raw = extractText(codeNode)
  const lineCount = raw ? raw.split('\n').length : 0
  const isLong = lineCount > 30

  function copy() {
    navigator.clipboard?.writeText(raw)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="relative group my-3 rounded-xl overflow-hidden border border-slate-800/40 shadow-sm bg-slate-950">
      <div className="flex items-center justify-between px-3.5 py-1.5 bg-slate-900 border-b border-slate-800">
        <span className="text-[10px] text-slate-400 uppercase tracking-wider font-medium">
          {lang || 'code'}
          {isLong && (
            <span className="ml-2 text-slate-500 normal-case">{lineCount} 行</span>
          )}
        </span>
        <div className="flex items-center gap-3">
          {isLong && (
            <button
              onClick={() => setCollapsed((v) => !v)}
              className="text-[10px] text-slate-400 hover:text-slate-100 flex items-center gap-1 transition"
            >
              {collapsed ? <FiChevronRight size={11} /> : <FiChevronDown size={11} />}
              {collapsed ? '展开' : '折叠'}
            </button>
          )}
          <button
            onClick={copy}
            className="text-[10px] text-slate-400 hover:text-slate-100 flex items-center gap-1 transition"
          >
            {copied ? <FiCheck size={11} /> : <FiCopy size={11} />}
            {copied ? '已复制' : '复制'}
          </button>
        </div>
      </div>
      <pre
        className={
          'bg-transparent px-4 py-3 overflow-auto text-[12.5px] leading-relaxed text-slate-100 m-0 ' +
          (collapsed ? 'max-h-[120px]' : 'max-h-[60vh]')
        }
      >
        {children}
      </pre>
      {collapsed && (
        <div className="px-4 py-1.5 bg-slate-900/50 border-t border-slate-800 text-center">
          <button
            onClick={() => setCollapsed(false)}
            className="text-[10px] text-slate-400 hover:text-slate-100"
          >
            点击展开全部 {lineCount} 行
          </button>
        </div>
      )}
    </div>
  )
}

function Lightbox({ src, onClose }) {
  if (!src) return null
  return (
    <div
      className="fixed inset-0 z-[60] bg-black/85 backdrop-blur-sm flex items-center justify-center cursor-zoom-out fade-in-up"
      onClick={onClose}
    >
      <img
        src={src}
        alt=""
        onClick={(e) => e.stopPropagation()}
        className="max-h-[92vh] max-w-[92vw] object-contain rounded-lg shadow-2xl cursor-default"
      />
      <button
        onClick={onClose}
        className="absolute top-4 right-4 w-9 h-9 rounded-full bg-white/10 hover:bg-white/20 text-white flex items-center justify-center backdrop-blur-md"
        title="关闭 (Esc)"
        aria-label="关闭"
      >
        <FiX size={18} />
      </button>
    </div>
  )
}

function extractText(node) {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (typeof node === 'object' && node.props) return extractText(node.props.children)
  return ''
}

function unwrapMarkdownCodeBlocks(content) {
  if (!content) return content
  return content.replace(
    /```markdown\s*\n([\s\S]*?)```/g,
    (_, body) => body.trimEnd() + '\n'
  )
}

function TypingCursor() {
  return (
    <span
      className="inline-block w-[2px] h-[1em] ml-0.5 align-middle bg-indigo-500 animate-pulse rounded-sm"
      aria-hidden="true"
    />
  )
}

function ModelBadge({ modelId }) {
  if (!modelId) return null
  return (
    <span className="px-1.5 py-0.5 rounded text-[10px] bg-slate-100 text-slate-500 border border-slate-200 font-mono">
      {modelId}
    </span>
  )
}

function TokenUsage({ usage }) {
  if (!usage || !usage.total_tokens) return null
  const inp = usage.input_tokens || 0
  const out = usage.output_tokens || 0
  return (
    <span className="text-[10px] text-slate-400" title="输入/输出/总计 tokens">
      {inp.toLocaleString()}↑ {out.toLocaleString()}↓ · {usage.total_tokens.toLocaleString()} tok
    </span>
  )
}

const mdComponents = {
  pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
  code: ({ className, children, ...props }) => {
    const isBlock = typeof className === 'string' && className.startsWith('language-')
    if (isBlock) {
      return (
        <code className={className} {...props}>
          {children}
        </code>
      )
    }
    return (
      <code className="font-mono text-[0.9em] text-rose-600" {...props}>
        {children}
      </code>
    )
  },
  a: (props) => (
    <a
      {...props}
      target="_blank"
      rel="noreferrer"
      className="text-indigo-600 hover:text-indigo-700 underline underline-offset-2 decoration-indigo-300 hover:decoration-indigo-500 transition"
    />
  ),
  h1: ({ children }) => (
    <h1 className="text-xl font-semibold mt-4 mb-2 tracking-tight text-slate-900">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-lg font-semibold mt-3.5 mb-2 tracking-tight text-slate-900">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-base font-semibold mt-3 mb-1.5 tracking-tight text-slate-900">{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 className="text-sm font-semibold mt-2 mb-1 text-slate-900">{children}</h4>
  ),
  ul: ({ children }) => <ul className="list-disc ml-5 my-2 space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal ml-5 my-2 space-y-1">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  p: ({ children }) => <p className="my-2 leading-[1.7]">{children}</p>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-indigo-300 pl-3.5 my-3 text-slate-500 italic">
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
    <th className="border-b border-slate-200 px-3 py-2 text-left font-semibold text-slate-700">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-b border-slate-100 px-3 py-2 text-slate-700">{children}</td>
  ),
  hr: () => <hr className="border-slate-200 my-4" />,
}

export function MessageBubble({ msg, onRegenerate }) {
  const [copied, setCopied] = useState(false)
  const [userShowReasoning, setUserShowReasoning] = useState(null)
  const [showToolProgress, setShowToolProgress] = useState(false)
  const [lightbox, setLightbox] = useState(null)
  const canRegenerate = !!onRegenerate && !!msg.content && !msg.streaming
  const isError = !msg.streaming && msg.content?.startsWith('错误:')

  useEffect(() => {
    if (!lightbox) return
    const onKey = (e) => { if (e.key === 'Escape') setLightbox(null) }
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = ''
      window.removeEventListener('keydown', onKey)
    }
  }, [lightbox])

  const reasoningInProgress = !!msg.streaming && !!msg.reasoning && !msg.content
  const autoShowReasoning = reasoningInProgress
  const showReasoning = userShowReasoning === null ? autoShowReasoning : userShowReasoning
  const reasoningLabel = reasoningInProgress ? '思考中' : '思考完成'
  const toggleReasoning = () =>
    setUserShowReasoning((o) => (o === null ? !autoShowReasoning : !o))

  function handleCopy() {
    navigator.clipboard.writeText(msg.content || '')
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  if (msg.role === 'user') {
    return (
      <>
        <div className="flex justify-end mb-5 fade-in-up">
          <div className="max-w-[78%]">
            {msg.image_urls && msg.image_urls.length > 0 && (
              <div className="flex flex-wrap gap-2 mb-1.5 justify-end">
                {msg.image_urls.map((u, i) => (
                  <img
                    key={i}
                    src={u}
                    alt=""
                    onClick={() => setLightbox(u)}
                    className="max-w-[220px] rounded-xl border border-slate-200 shadow-sm cursor-zoom-in hover:opacity-90 transition"
                  />
                ))}
              </div>
            )}
            <div className="bg-gradient-to-br from-indigo-500 to-violet-500 text-white rounded-2xl rounded-br-md px-4 py-2.5 text-[14px] leading-relaxed whitespace-pre-wrap shadow-sm">
              {msg.content}
            </div>
          </div>
        </div>
        <Lightbox src={lightbox} onClose={() => setLightbox(null)} />
      </>
    )
  }

  const hasToolProgress = !!msg.tool_progress
  const hasReasoning = !!msg.reasoning
  const toolLines = hasToolProgress ? msg.tool_progress.split('\n').filter(Boolean) : []

  return (
    <div className="flex justify-start mb-5 fade-in-up">
      <div className="max-w-[88%] w-full flex gap-3">
        <div className="w-7 h-7 mt-0.5 shrink-0 rounded-lg bg-gradient-to-br from-slate-700 to-slate-900 flex items-center justify-center shadow-sm">
          <FiCpu className="text-white" size={13} />
        </div>

        <div className="flex-1 min-w-0">
          {hasToolProgress && (
            <div className="mb-1.5">
              <button
                onClick={() => setShowToolProgress((v) => !v)}
                className="text-xs text-indigo-600 hover:text-indigo-800 flex items-center gap-1.5 font-medium"
              >
                {showToolProgress ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
                <FiTool size={11} />
                工具执行
                {msg.streaming && !msg.content && <span className="text-slate-400">…</span>}
              </button>
              {showToolProgress && (
                <div className="mt-1.5 py-2 px-3 bg-indigo-50/70 border border-indigo-100 rounded-xl text-xs text-slate-600 leading-relaxed space-y-0.5">
                  {toolLines.map((ln, i) => (
                    <div key={i}>{ln}</div>
                  ))}
                </div>
              )}
            </div>
          )}

          {hasReasoning && (
            <div className="mb-1.5">
              <button
                onClick={toggleReasoning}
                className="text-xs text-slate-500 hover:text-slate-800 flex items-center gap-1.5 font-medium"
              >
                {showReasoning ? <FiChevronDown size={12} /> : <FiChevronRight size={12} />}
                {reasoningLabel}
                {reasoningInProgress && <span className="text-slate-400">…</span>}
              </button>
              {showReasoning && (
                <div className="mt-1.5 p-3 bg-stone-50 border border-stone-200 rounded-xl text-xs text-slate-500 whitespace-pre-wrap leading-relaxed max-h-[320px] overflow-y-auto">
                  {msg.reasoning}
                </div>
              )}
            </div>
          )}

          {isError ? (
            <div className="flex items-start gap-2 px-3.5 py-2.5 bg-red-50 border border-red-200 rounded-xl text-sm text-red-700">
              <FiAlertCircle className="mt-0.5 shrink-0" size={14} />
              <span className="flex-1 whitespace-pre-wrap">{msg.content.replace(/^错误:/, '').trim()}</span>
            </div>
          ) : msg.content ? (
            <div className="text-[14px] text-slate-800 break-words">
              <ReactMarkdown
                remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
                rehypePlugins={[rehypeHighlight]}
                components={mdComponents}
              >
                {unwrapMarkdownCodeBlocks(msg.content)}
              </ReactMarkdown>
              {msg.streaming && <TypingCursor />}
            </div>
          ) : msg.streaming ? (
            <div className="flex items-center gap-1 py-1.5">
              <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
              <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
              <span className="w-1.5 h-1.5 bg-slate-400 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
            </div>
          ) : null}

          {!msg.streaming && msg.content && !isError && (
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
              <button
                onClick={handleCopy}
                className="flex items-center gap-1 hover:text-slate-700 transition"
              >
                {copied ? <FiCheck size={12} /> : <FiCopy size={12} />}
                {copied ? '已复制' : '复制全文'}
              </button>
              {canRegenerate && (
                <button
                  onClick={onRegenerate}
                  className="flex items-center gap-1 hover:text-indigo-600 transition"
                >
                  <FiRefreshCw size={11} />
                  重新生成
                </button>
              )}
              {msg.model_id && <ModelBadge modelId={msg.model_id} />}
              <TokenUsage usage={msg.usage} />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
