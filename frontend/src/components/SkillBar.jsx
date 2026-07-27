import { useState, useEffect } from 'react'
import {
  FiPackage, FiX, FiInfo, FiTrash2, FiPlus, FiArrowUpCircle, FiCheck, FiLink,
} from 'react-icons/fi'
import {
  getSkills, promoteSkill,
  getConversationSettings, updateConversationSettings,
} from '../api'
import { groupSkillsForDisplay } from '../utils/skillGrouping'

function formatDate(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } catch {
    return ''
  }
}

function SkillChip({ skill, onClick }) {
  const isSession = skill.scope === 'session' || skill.session_id
  return (
    <button
      type="button"
      onClick={onClick}
      title={skill.description || skill.name}
      aria-label={`Skill: ${skill.name}`}
      className={
        'group flex items-center gap-1.5 pl-2.5 pr-2 py-1.5 rounded-lg text-xs border shadow-sm transition hover:shadow ' +
        (isSession
          ? 'bg-purple-50 border-purple-200 text-purple-700 hover:bg-purple-100'
          : 'bg-blue-50 border-blue-200 text-blue-700 hover:bg-blue-100')
      }
    >
      <FiPackage size={12} className="shrink-0" />
      <span className="font-medium truncate max-w-[160px]">{skill.name}</span>
      <span
        className={
          'text-[9px] uppercase tracking-wide px-1 py-0.5 rounded font-medium ' +
          (isSession ? 'bg-purple-200/60 text-purple-800' : 'bg-blue-200/60 text-blue-800')
        }
      >
        {isSession ? 'session' : 'user'}
      </span>
      <FiInfo size={11} className="shrink-0 opacity-50 group-hover:opacity-100" />
    </button>
  )
}

function SkillGroupChip({ group, expanded, onToggle, onOpen }) {
  const main = group.main
  const children = group.children
  const childCount = children.length
  const isSession = main.scope === 'session' || main.session_id
  return (
    <div className="relative">
      <button
        type="button"
        onClick={childCount ? onToggle : () => onOpen(main)}
        title={main.description || main.name}
        aria-label={`Skill bundle: ${main.name}`}
        className={
          'group flex items-center gap-1.5 pl-2.5 pr-2 py-1.5 rounded-lg text-xs border shadow-sm transition hover:shadow ' +
          (isSession
            ? 'bg-purple-50 border-purple-200 text-purple-700 hover:bg-purple-100'
            : 'bg-blue-50 border-blue-200 text-blue-700 hover:bg-blue-100')
        }
      >
        <FiPackage size={12} className="shrink-0" />
        <span className="font-medium truncate max-w-[170px]">{main.name}</span>
        {childCount > 0 && (
          <span
            className={
              'text-[10px] px-1.5 py-0.5 rounded-full font-semibold ' +
              (isSession
                ? 'bg-purple-200/70 text-purple-800'
                : 'bg-blue-200/70 text-blue-800')
            }
          >
            +{childCount}
          </span>
        )}
        <span
          className={
            'text-[9px] uppercase tracking-wide px-1 py-0.5 rounded font-medium ' +
            (isSession
              ? 'bg-purple-200/60 text-purple-800'
              : 'bg-blue-200/60 text-blue-800')
          }
        >
          {isSession ? 'session' : 'user'}
        </span>
        <FiInfo
          size={11}
          className={
            'shrink-0 transition ' +
            (expanded ? 'opacity-100 rotate-180' : 'opacity-50 group-hover:opacity-100')
          }
        />
      </button>
      {expanded && childCount > 0 && (
        <div className="absolute left-0 top-full mt-1.5 z-40 w-80 max-h-80 overflow-y-auto rounded-xl border border-purple-100 bg-white shadow-xl p-2">
          <button
            type="button"
            onClick={() => onOpen(main)}
            className="w-full flex items-start gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-purple-50 transition"
          >
            <FiPackage size={13} className="mt-0.5 shrink-0 text-purple-600" />
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-slate-900 truncate">{main.name}</div>
              <div className="text-[11px] text-slate-500 line-clamp-2">主 skill</div>
            </div>
          </button>
          <div className="my-1 border-t border-stone-100" />
          <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">
            子 skills
          </div>
          {children.map((child) => (
            <button
              type="button"
              key={`${child.session_id || 'session'}-${child.name}`}
              onClick={() => onOpen(child)}
              title={child.description || child.name}
              className="w-full flex items-start gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-stone-50 transition"
            >
              <FiPackage size={13} className="mt-0.5 shrink-0 text-slate-400" />
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-slate-800 truncate">{child.name}</div>
                {child.description && (
                  <div className="text-[11px] text-slate-500 line-clamp-2">{child.description}</div>
                )}
              </div>
              {child.category && (
                <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-500">
                  {child.category}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function SkillDetail({ skill, onClose, onDelete, onUnlink, onPromote, promoting }) {
  if (!skill) return null
  const isSession = skill.scope === 'session' || skill.session_id
  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4 fade-in-up"
      onClick={onClose}
    >
      <div
        className="bg-white border border-gray-200 rounded-2xl shadow-xl w-full max-w-lg max-h-[80vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between p-4 border-b border-gray-100">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-purple-100 text-purple-700 flex items-center justify-center">
                <FiPackage size={14} />
              </div>
              <h3 className="text-base font-semibold text-gray-900 truncate">{skill.name}</h3>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span
                className={
                  'text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded font-medium ' +
                  (isSession ? 'bg-purple-100 text-purple-800' : 'bg-blue-100 text-blue-800')
                }
              >
                {isSession ? 'session 级' : 'user 级'}
              </span>
              {skill.category && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-700">
                  {skill.category}
                </span>
              )}
              {skill.version && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-700 font-mono">
                  v{skill.version}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1.5 rounded-lg hover:bg-gray-100 text-gray-500"
          >
            <FiX size={18} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {skill.description ? (
            <div>
              <div className="text-xs font-medium text-gray-500 mb-1">描述</div>
              <div className="text-sm text-gray-800 whitespace-pre-wrap leading-relaxed">
                {skill.description}
              </div>
            </div>
          ) : (
            <div className="text-sm text-gray-400 italic">没有描述</div>
          )}
          {skill.created_at && (
            <div className="text-xs text-gray-500">创建于 {formatDate(skill.created_at)}</div>
          )}
          {!isSession && (
            <div className="text-[11px] text-slate-500 bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5">
              user 级 Skill 仅从本会话解除引用，不会从 user 层级删除。要从 user 层级彻底删除，请到侧栏底部「Skill 库」。
            </div>
          )}
        </div>

        <div className="flex justify-between items-center p-3 border-t border-gray-100 bg-stone-50">
          <div className="flex gap-2">
            {isSession && onPromote && (
              <button
                onClick={onPromote}
                disabled={promoting}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition disabled:opacity-50"
              >
                <FiArrowUpCircle size={13} />
                {promoting ? '升级中…' : '升级为 user-level'}
              </button>
            )}
            {isSession && onDelete && (
              <button
                onClick={onDelete}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-red-600 hover:bg-red-50 rounded-lg transition"
              >
                <FiTrash2 size={12} />
                删除 Skill
              </button>
            )}
            {!isSession && onUnlink && (
              <button
                onClick={onUnlink}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-amber-700 bg-amber-50 hover:bg-amber-100 rounded-lg transition"
              >
                <FiLink size={12} />
                取消本会话引用
              </button>
            )}
          </div>
          <button
            onClick={onClose}
            className="px-4 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition"
          >
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}

function SkillSelectorModal({ convId, onClose, onChanged }) {
  const [userSkills, setUserSkills] = useState([])
  const [enabled, setEnabled] = useState([])
  const [loading, setLoading] = useState(true)
  const [savingId, setSavingId] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    Promise.resolve().then(async () => {
      setLoading(true)
      setError('')
      try {
        const [userList, settings] = await Promise.all([
          getSkills(null),
          getConversationSettings(convId),
        ])
        if (cancelled) return
        setUserSkills(Array.isArray(userList) ? userList.filter((s) => !s.session_id) : [])
        setEnabled(Array.isArray(settings.enabled_user_skills) ? settings.enabled_user_skills : [])
      } catch (err) {
        if (!cancelled) setError(err.message || '加载失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })
    return () => { cancelled = true }
  }, [convId])

  async function toggle(skill) {
    const next = enabled.includes(skill.name)
      ? enabled.filter((n) => n !== skill.name)
      : [...enabled, skill.name]
    setSavingId(skill.name)
    setError('')
    try {
      await updateConversationSettings(convId, { enabled_user_skills: next })
      setEnabled(next)
      onChanged?.()
    } catch (err) {
      setError(err.message || '保存失败')
    } finally {
      setSavingId(null)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4 fade-in"
      onClick={onClose}
    >
      <div
        className="bg-white border border-stone-200 rounded-2xl shadow-2xl w-full max-w-md max-h-[80vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-stone-100">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-indigo-100 text-indigo-700 flex items-center justify-center">
              <FiPlus size={14} />
            </div>
            <h3 className="text-sm font-semibold text-slate-900">选择 user-level Skill</h3>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1.5 rounded-lg hover:bg-stone-100 text-slate-500"
          >
            <FiX size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-3">
          {error && (
            <div className="mb-2 px-2.5 py-2 rounded-lg bg-red-50 border border-red-200 text-red-700 text-xs">
              {error}
            </div>
          )}
          {loading ? (
            <div className="text-center text-sm text-slate-400 py-8">加载中…</div>
          ) : userSkills.length === 0 ? (
            <div className="text-center py-8">
              <FiPackage size={28} className="mx-auto text-slate-300 mb-2" />
              <div className="text-sm text-slate-500">还没有 user-level Skill</div>
              <div className="text-xs text-slate-400 mt-1">
                打开侧栏底部 Skill 库上传第一个
              </div>
            </div>
          ) : (
            <div className="space-y-1.5">
              {userSkills.map((s) => {
                const isChecked = enabled.includes(s.name)
                const isSaving = savingId === s.name
                return (
                  <button
                    key={s.id || s.name}
                    onClick={() => toggle(s)}
                    disabled={isSaving}
                    className={
                      'w-full flex items-start gap-2.5 px-3 py-2.5 rounded-xl border text-left transition ' +
                      (isChecked
                        ? 'bg-indigo-50 border-indigo-200'
                        : 'bg-white border-stone-200 hover:border-stone-300') +
                      (isSaving ? ' opacity-60' : '')
                    }
                  >
                    <div
                      className={
                        'mt-0.5 w-4 h-4 rounded border flex items-center justify-center transition ' +
                        (isChecked
                          ? 'bg-indigo-600 border-indigo-600 text-white'
                          : 'bg-white border-stone-300')
                      }
                    >
                      {isChecked && <FiCheck size={11} />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-medium text-slate-900 truncate">
                          {s.name}
                        </span>
                        {s.version && (
                          <span className="text-[10px] font-mono text-slate-500">
                            v{s.version}
                          </span>
                        )}
                      </div>
                      {s.description && (
                        <div className="text-xs text-slate-500 mt-0.5 line-clamp-2">
                          {s.description}
                        </div>
                      )}
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        <div className="px-4 py-2.5 border-t border-stone-100 bg-stone-50/50">
          <button
            onClick={onClose}
            className="w-full py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition"
          >
            完成
          </button>
        </div>
      </div>
    </div>
  )
}

export default function SkillBar({ skills = [], convId, onRefresh, onDelete, onUnlink }) {
  const [active, setActive] = useState(null)
  const [selectorOpen, setSelectorOpen] = useState(false)
  const [expandedGroup, setExpandedGroup] = useState('')
  const [promoting, setPromoting] = useState(false)

  const {
    items: groupedItems,
    topLevelCount,
  } = groupSkillsForDisplay(skills)

  async function handlePromote(skill) {
    if (!confirm(`确认将 Skill "${skill.name}" 升级为 user-level?`)) return
    setPromoting(true)
    try {
      await promoteSkill(skill.name, skill.session_id)
      await onRefresh?.()
      setActive(null)
    } catch (err) {
      alert(`升级失败: ${err.message}`)
    } finally {
      setPromoting(false)
    }
  }

  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5 mb-2">
        <div className="flex items-center text-[11px] text-slate-500 mr-1">
          <FiPackage size={11} className="mr-1" />
          可用 Skills
          <span className="ml-1 px-1.5 py-0.5 rounded-full bg-stone-100 text-stone-600 font-medium">
            {topLevelCount}
          </span>
        </div>
        {groupedItems.map((item) => (
          item.type === 'group' ? (
            <SkillGroupChip
              key={item.key}
              group={{ main: item.main, children: item.children }}
              expanded={expandedGroup === item.key}
              onToggle={() => setExpandedGroup((current) => current === item.key ? '' : item.key)}
              onOpen={(skill) => {
                setActive(skill)
                setExpandedGroup('')
              }}
            />
          ) : (
            <SkillChip
              key={item.key}
              skill={item.skill}
              onClick={() => setActive(item.skill)}
            />
          )
        ))}
        {convId && (
          <button
            type="button"
            onClick={() => setSelectorOpen(true)}
            title="选择 user-level Skill"
            aria-label="选择 user-level Skill"
            className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs border border-dashed border-stone-300 text-slate-500 hover:border-indigo-300 hover:text-indigo-600 transition"
          >
            <FiPlus size={12} />
            选择 Skill
          </button>
        )}
      </div>

      {active && (
        <SkillDetail
          skill={active}
          promoting={promoting}
          onClose={() => setActive(null)}
          onPromote={() => handlePromote(active)}
          onDelete={
            onDelete
              ? async () => {
                  await onDelete(active)
                  setActive(null)
                }
              : undefined
          }
          onUnlink={
            onUnlink
              ? async () => {
                  await onUnlink(active)
                  setActive(null)
                }
              : undefined
          }
        />
      )}

      {selectorOpen && convId && (
        <SkillSelectorModal
          convId={convId}
          onClose={() => setSelectorOpen(false)}
          onChanged={() => onRefresh?.()}
        />
      )}
    </>
  )
}
