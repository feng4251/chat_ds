import { useEffect, useRef, useState } from 'react'
import { FiX, FiUpload, FiTrash2, FiPackage, FiAlertCircle } from 'react-icons/fi'
import { getSkills, uploadSkill, deleteSkill } from '../api'

function formatDate(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  } catch {
    return ''
  }
}

export default function SkillLibrary({ open, onClose }) {
  const [skills, setSkills] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)

  async function load() {
    setLoading(true)
    setError('')
    try {
      const list = await getSkills(null)
      setSkills(Array.isArray(list) ? list.filter((s) => !s.session_id) : [])
    } catch (err) {
      setError(err.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) load()
  }, [open])

  async function handleUpload(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setError('仅支持 .zip 格式的 Skill 包')
      return
    }
    setUploading(true)
    setError('')
    try {
      await uploadSkill(file, null, null)
      await load()
    } catch (err) {
      setError(err.message || '上传失败')
    } finally {
      setUploading(false)
    }
  }

  async function handleDelete(skill) {
    if (!confirm(`确认删除 user-level Skill "${skill.name}"?`)) return
    try {
      await deleteSkill(skill.name, null)
      await load()
    } catch (err) {
      setError(err.message || '删除失败')
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4 fade-in"
      onClick={onClose}
    >
      <div
        className="bg-white border border-stone-200 rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-stone-100">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-indigo-100 text-indigo-700 flex items-center justify-center">
              <FiPackage size={16} />
            </div>
            <h2 className="text-base font-semibold text-slate-900">Skill 库</h2>
            <span className="text-xs px-1.5 py-0.5 rounded-full bg-stone-100 text-slate-600 font-medium">
              {skills.length}
            </span>
          </div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1.5 rounded-lg hover:bg-stone-100 text-slate-500"
          >
            <FiX size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4">
          <p className="text-xs text-slate-500 mb-4 leading-relaxed">
            上传的 Skill 会保存在 user 层级。默认不会出现在任何 session 中 —
            需在会话的 SkillBar 上点 '+' 显式启用。
          </p>

          {error && (
            <div className="mb-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-red-700 text-xs">
              <FiAlertCircle className="mt-0.5 shrink-0" size={13} />
              <span>{error}</span>
            </div>
          )}

          {loading ? (
            <div className="text-center text-sm text-slate-400 py-10">加载中…</div>
          ) : skills.length === 0 ? (
            <div className="text-center py-10">
              <FiPackage size={32} className="mx-auto text-slate-300 mb-3" />
              <div className="text-sm text-slate-500 mb-1">还没有 user-level Skill</div>
              <div className="text-xs text-slate-400">点击下方按钮上传第一个 Skill</div>
            </div>
          ) : (
            <div className="space-y-2">
              {skills.map((s) => (
                <div
                  key={s.id || s.name}
                  className="group flex items-start gap-3 px-3 py-2.5 rounded-xl border border-stone-200 bg-white hover:border-indigo-200 hover:bg-stone-50/50 transition"
                >
                  <div className="w-8 h-8 rounded-lg bg-indigo-50 text-indigo-600 flex items-center justify-center shrink-0">
                    <FiPackage size={14} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-slate-900 truncate">
                        {s.name}
                      </span>
                      {s.version && (
                        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-stone-100 text-slate-600">
                          v{s.version}
                        </span>
                      )}
                    </div>
                    {s.description && (
                      <div className="text-xs text-slate-500 mt-0.5 line-clamp-2">
                        {s.description}
                      </div>
                    )}
                    {s.created_at && (
                      <div className="text-[10px] text-slate-400 mt-1">
                        {formatDate(s.created_at)}
                      </div>
                    )}
                  </div>
                  <button
                    onClick={() => handleDelete(s)}
                    aria-label={`删除 ${s.name}`}
                    className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 transition opacity-0 group-hover:opacity-100"
                  >
                    <FiTrash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-stone-100 bg-stone-50/50 flex justify-between items-center">
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={handleUpload}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <FiUpload size={14} />
            {uploading ? '上传中…' : '上传 Skill'}
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-slate-600 hover:text-slate-900 transition"
          >
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}
