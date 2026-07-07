import { useState, useMemo } from 'react'
import {
  FiPlus, FiTrash2, FiEdit3, FiLogOut, FiX, FiSettings, FiPackage,
  FiMessageSquare, FiGitBranch, FiSearch,
} from 'react-icons/fi'
import { renameConversation, deleteConversation, forkConversation } from '../api'

const GROUP_LABELS = {
  today: '今天',
  yesterday: '昨天',
  week: '本周',
  earlier: '更早',
}
const GROUP_ORDER = ['today', 'yesterday', 'week', 'earlier']

export default function Sidebar({
  conversations,
  activeConv,
  onNewChat,
  onSelectConv,
  onClose,
  onRefresh,
  onOpenSettings,
  onOpenSkillLibrary,
  onConversationCreated,
}) {
  const [editingId, setEditingId] = useState(null)
  const [editText, setEditText] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const user = JSON.parse(localStorage.getItem('user') || '{}')

  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    if (!q) return conversations
    return conversations.filter((c) =>
      (c.title || '新会话').toLowerCase().includes(q)
    )
  }, [conversations, searchQuery])

  const grouped = useMemo(() => {
    const now = new Date()
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
    const yesterday = new Date(today.getTime() - 86400000)
    const weekAgo = new Date(today.getTime() - 7 * 86400000)

    const groups = { today: [], yesterday: [], week: [], earlier: [] }
    for (const conv of filtered) {
      const ts = new Date(conv.updated_at || conv.created_at)
      if (ts >= today) groups.today.push(conv)
      else if (ts >= yesterday) groups.yesterday.push(conv)
      else if (ts >= weekAgo) groups.week.push(conv)
      else groups.earlier.push(conv)
    }
    return groups
  }, [filtered])

  function handleRename(conv) {
    if (editingId === conv.id) {
      renameConversation(conv.id, editText).then(onRefresh)
      setEditingId(null)
    } else {
      setEditingId(conv.id)
      setEditText(conv.title || '新会话')
    }
  }

  function handleDelete(convId) {
    if (!confirm('确认删除该会话?')) return
    deleteConversation(convId).then(() => {
      if (activeConv === convId) onNewChat()
      onRefresh()
    })
  }

  async function handleFork(conv) {
    const fork = await forkConversation(conv.id)
    await onRefresh()
    onConversationCreated?.(fork.id)
  }

  function logout() {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    window.location.href = '/login'
  }

  const hasResults = GROUP_ORDER.some((k) => grouped[k].length > 0)

  return (
    <div className="h-full bg-stone-100 border-r border-stone-200 flex flex-col text-slate-700">
      {/* Brand row */}
      <div className="px-4 pt-4 pb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-500 flex items-center justify-center shadow-sm">
            <FiMessageSquare className="text-white" size={15} />
          </div>
          <span className="font-semibold text-[15px] text-slate-900 tracking-tight">Chat ACITS</span>
        </div>
        <button
          onClick={onClose}
          className="p-1.5 rounded-lg hover:bg-stone-200 text-slate-500 hover:text-slate-800 lg:hidden"
          title="关闭"
          aria-label="关闭侧栏"
        >
          <FiX size={18} />
        </button>
      </div>

      {/* New chat button */}
      <div className="px-3 pb-2">
        <button
          onClick={onNewChat}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-white border border-stone-200 text-sm text-slate-700 hover:border-indigo-300 hover:text-indigo-700 transition shadow-sm"
        >
          <FiPlus size={15} />
          新会话
        </button>
      </div>

      {/* Search box */}
      <div className="px-3 pb-2">
        <div className="relative">
          <FiSearch className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" size={13} />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索会话…"
            className="w-full pl-8 pr-7 py-1.5 text-[13px] bg-white border border-stone-200 rounded-lg outline-none focus:border-indigo-300 placeholder-slate-400"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 text-slate-400 hover:text-slate-700"
              aria-label="清除搜索"
            >
              <FiX size={12} />
            </button>
          )}
        </div>
      </div>

      {/* Conversation list with grouping */}
      <div className="flex-1 overflow-y-auto px-2 py-1">
        {!hasResults && (
          <div className="text-xs text-slate-400 px-3 py-6 text-center">
            {searchQuery ? '没有匹配的会话' : '还没有会话,开始一段新对话吧'}
          </div>
        )}
        {GROUP_ORDER.map((key) => {
          const convs = grouped[key]
          if (convs.length === 0) return null
          return (
            <div key={key} className="mb-1">
              <div className="px-3 pt-2.5 pb-1 text-[11px] font-medium text-slate-400 uppercase tracking-wider">
                {GROUP_LABELS[key]}
              </div>
              {convs.map((conv) => {
                const isActive = activeConv === conv.id
                return (
                  <div
                    key={conv.id}
                    className={
                      'group relative rounded-lg pl-3 pr-2 py-2 cursor-pointer flex items-center justify-between text-sm transition ' +
                      (isActive
                        ? 'bg-indigo-50/70 text-slate-900 border border-indigo-100 shadow-sm'
                        : 'text-slate-600 hover:bg-stone-200/70 border border-transparent')
                    }
                    onClick={() => onSelectConv(conv.id)}
                  >
                    {isActive && (
                      <span className="absolute left-0 top-2 bottom-2 w-1 rounded-full bg-indigo-500" />
                    )}
                    {editingId === conv.id ? (
                      <input
                        className="flex-1 bg-white border border-stone-300 text-sm px-2 py-0.5 rounded outline-none text-slate-900 focus:border-indigo-500"
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleRename(conv)
                          if (e.key === 'Escape') setEditingId(null)
                        }}
                        onBlur={() => handleRename(conv)}
                        autoFocus
                        onClick={(e) => e.stopPropagation()}
                      />
                    ) : (
                      <span className="flex-1 truncate flex items-center gap-1.5">
                        <span className="truncate">{conv.title || '新会话'}</span>
                        {conv.message_count > 0 && (
                          <span
                            className={
                              'shrink-0 text-[10px] px-1.5 py-0.5 rounded-full font-medium ' +
                              (isActive ? 'bg-indigo-100 text-indigo-700' : 'bg-stone-200 text-slate-500')
                            }
                            title={`${conv.message_count} 条消息`}
                          >
                            {conv.message_count}
                          </span>
                        )}
                      </span>
                    )}
                    <div className="ml-2 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleFork(conv) }}
                        className="p-1 rounded text-slate-400 hover:text-indigo-600 hover:bg-stone-200"
                        title="创建分支"
                        aria-label={`创建分支: ${conv.title || '新会话'}`}
                      >
                        <FiGitBranch size={13} />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleRename(conv) }}
                        className="p-1 rounded text-slate-400 hover:text-slate-900 hover:bg-stone-200"
                        title="重命名"
                        aria-label={`重命名: ${conv.title || '新会话'}`}
                      >
                        <FiEdit3 size={13} />
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete(conv.id) }}
                        className="p-1 rounded text-slate-400 hover:text-red-500 hover:bg-stone-200"
                        title="删除"
                        aria-label={`删除: ${conv.title || '新会话'}`}
                      >
                        <FiTrash2 size={13} />
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>

      {/* User row */}
      <div className="px-3 py-3 border-t border-stone-200">
        <div className="flex items-center gap-2.5 text-sm">
          <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-violet-500 flex items-center justify-center text-xs font-semibold text-white shadow-sm">
            {(user.username?.[0] || 'U').toUpperCase()}
          </div>
          <div className="flex-1 min-w-0">
            <div className="truncate text-sm font-medium text-slate-800">
              {user.username || '用户'}
            </div>
          </div>
          <button
            onClick={onOpenSettings}
            className="p-1.5 rounded-lg hover:bg-stone-200 text-slate-500 hover:text-slate-800"
            title="模型管理"
            aria-label="打开模型管理"
          >
            <FiSettings size={15} />
          </button>
          <button
            onClick={onOpenSkillLibrary}
            className="p-1.5 rounded-lg hover:bg-stone-200 text-slate-500 hover:text-slate-800"
            title="Skill 库"
            aria-label="打开 Skill 库"
          >
            <FiPackage size={15} />
          </button>
          <button
            onClick={logout}
            className="p-1.5 rounded-lg hover:bg-stone-200 text-slate-500 hover:text-red-500"
            title="退出登录"
            aria-label="退出登录"
          >
            <FiLogOut size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}
