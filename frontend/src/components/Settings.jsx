import { useState, useEffect } from 'react'
import { FiX, FiTrash2, FiPlus } from 'react-icons/fi'
import { getCustomModels, createCustomModel, deleteCustomModel, getModels } from '../api'

const EMPTY_FORM = {
  model_id: '',
  model_name: '',
  provider: 'openai',
  base_url: '',
  api_key: '',
  is_multimodal: false,
  extra_headers: '',
}

export default function Settings({ open, onClose, onModelsChanged }) {
  const [tab, setTab] = useState('all')
  const [items, setItems] = useState([])
  const [allModels, setAllModels] = useState([])
  const [form, setForm] = useState(EMPTY_FORM)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showForm, setShowForm] = useState(false)

  useEffect(() => {
    if (!open) return
    load()
    loadAllModels()
  }, [open])

  async function load() {
    try {
      setItems(await getCustomModels())
    } catch {
      setItems([])
    }
  }

  async function loadAllModels() {
    try {
      setAllModels(await getModels())
    } catch {
      setAllModels([])
    }
  }

  async function add(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await createCustomModel(form)
      setForm(EMPTY_FORM)
      setShowForm(false)
      await load()
      onModelsChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function remove(id) {
    if (!confirm('确认删除该模型配置?')) return
    try {
      await deleteCustomModel(id)
      await load()
      onModelsChanged?.()
    } catch (err) {
      setError(err.message)
    }
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-white border border-gray-200 rounded-2xl shadow-xl w-full max-w-2xl max-h-[85vh] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between p-4 border-b border-gray-200">
          <h2 className="text-lg font-semibold text-gray-900">模型管理</h2>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1 rounded-lg hover:bg-gray-100 text-gray-500 hover:text-gray-800"
          >
            <FiX size={20} />
          </button>
        </div>

        <div className="flex border-b border-gray-200 px-4">
          <button
            onClick={() => setTab('all')}
            className={
              'px-3 py-2 text-sm border-b-2 -mb-px ' +
              (tab === 'all'
                ? 'text-gray-900 border-blue-500'
                : 'text-gray-500 border-transparent hover:text-gray-800')
            }
          >
            全部模型
          </button>
          <button
            onClick={() => setTab('custom')}
            className={
              'px-3 py-2 text-sm border-b-2 -mb-px ' +
              (tab === 'custom'
                ? 'text-gray-900 border-blue-500'
                : 'text-gray-500 border-transparent hover:text-gray-800')
            }
          >
            自定义模型
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {error && (
            <div className="mb-3 p-2 bg-red-50 border border-red-200 text-red-700 rounded text-sm">
              {error}
            </div>
          )}

          {tab === 'all' && (
            <div className="space-y-2">
              {allModels.length === 0 && (
                <div className="text-sm text-gray-400 text-center py-6">暂无模型</div>
              )}
              {allModels.map((m) => {
                const isBuiltin = m.provider === 'builtin'
                return (
                  <div
                    key={m.id}
                    className="flex items-center justify-between gap-3 p-3 bg-gray-50 border border-gray-100 rounded-lg"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-gray-900 font-medium truncate">
                          {m.name}
                        </span>
                        {isBuiltin && (
                          <span className="text-[10px] bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded font-medium">
                            内置
                          </span>
                        )}
                        {m.is_default && (
                          <span className="text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-medium">
                            默认
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-gray-500 truncate mt-0.5">
                        <span>{m.id}</span>
                        {m.is_multimodal && <span className="ml-2 text-purple-600">多模态</span>}
                        {m.capabilities && m.capabilities.length > 0 && (
                          <span className="ml-2 text-gray-400">
                            {m.capabilities.join(', ')}
                          </span>
                        )}
                      </div>
                    </div>
                    {!isBuiltin && (
                      <button
                        onClick={() => {
                          const cm = items.find((x) => x.model_id === m.id || x.id === m.id)
                          if (cm) remove(cm.id)
                        }}
                        className="p-1.5 text-gray-400 hover:text-red-500"
                        title="删除"
                      >
                        <FiTrash2 size={16} />
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {tab === 'custom' && (
            <>
          <div className="space-y-2 mb-4">
            {items.length === 0 && !showForm && (
              <div className="text-sm text-gray-400 text-center py-6">
                还没有自定义模型,可添加任意 OpenAI / Anthropic 兼容接口
              </div>
            )}
            {items.map((m) => (
              <div
                key={m.id}
                className="flex items-center justify-between gap-3 p-3 bg-gray-50 border border-gray-100 rounded-lg"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-gray-900 font-medium truncate">
                    {m.model_name}
                  </div>
                  <div className="text-xs text-gray-500 truncate">
                    {m.provider} · {m.model_id} · {m.base_url}
                    {m.is_multimodal ? ' · 多模态' : ''}
                  </div>
                </div>
                <button
                  onClick={() => remove(m.id)}
                  className="p-1.5 text-gray-400 hover:text-red-500"
                  title="删除"
                >
                  <FiTrash2 size={16} />
                </button>
              </div>
            ))}
          </div>

          {!showForm ? (
            <button
              onClick={() => {
                setShowForm(true)
                setError('')
              }}
              className="w-full py-2 border border-dashed border-gray-300 rounded-lg text-sm text-gray-500 hover:text-gray-800 hover:border-gray-400 flex items-center justify-center gap-1"
            >
              <FiPlus size={16} />
              添加自定义模型
            </button>
          ) : (
            <form onSubmit={add} className="space-y-3 p-3 bg-gray-50 border border-gray-100 rounded-lg">
              <div className="grid grid-cols-2 gap-3">
                <Field
                  label="模型 ID"
                  hint="如 gpt-4o、claude-3-5-sonnet-20241022"
                  value={form.model_id}
                  onChange={(v) => setForm({ ...form, model_id: v })}
                  required
                />
                <Field
                  label="显示名称"
                  value={form.model_name}
                  onChange={(v) => setForm({ ...form, model_name: v })}
                  required
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">协议类型</label>
                  <select
                    value={form.provider}
                    onChange={(e) => setForm({ ...form, provider: e.target.value })}
                    className="w-full bg-white border border-gray-300 rounded px-2 py-1.5 text-sm text-gray-800"
                  >
                    <option value="openai">OpenAI 兼容</option>
                    <option value="anthropic">Anthropic 兼容</option>
                    <option value="custom">自定义</option>
                  </select>
                </div>
                <div className="flex items-center gap-2 mt-5">
                  <input
                    id="mm"
                    type="checkbox"
                    checked={form.is_multimodal}
                    onChange={(e) =>
                      setForm({ ...form, is_multimodal: e.target.checked })
                    }
                    className="w-4 h-4"
                  />
                  <label htmlFor="mm" className="text-sm text-gray-700">
                    多模态(支持图片)
                  </label>
                </div>
              </div>
              <Field
                label="Base URL"
                hint="如 https://api.openai.com/v1 或 http://10.10.x.x:port/v1"
                value={form.base_url}
                onChange={(v) => setForm({ ...form, base_url: v })}
                required
              />
              <Field
                label="API Key"
                value={form.api_key}
                onChange={(v) => setForm({ ...form, api_key: v })}
                type="password"
                required
              />
              <Field
                label="额外请求头（JSON，可选）"
                hint='例如 {"X-Organization":"team-a"}'
                value={form.extra_headers}
                onChange={(v) => setForm({ ...form, extra_headers: v })}
              />
              <div className="flex gap-2 justify-end">
                <button
                  type="button"
                  onClick={() => {
                    setShowForm(false)
                    setForm(EMPTY_FORM)
                    setError('')
                  }}
                  className="px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded"
                >
                  取消
                </button>
                <button
                  type="submit"
                  disabled={loading}
                  className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                >
                  {loading ? '保存中…' : '保存'}
                </button>
              </div>
            </form>
          )}
            </>
          )}

          </div>
      </div>
    </div>
  )
}

function Field({ label, hint, value, onChange, type = 'text', required }) {
  return (
    <div>
      <label className="block text-xs text-gray-600 mb-1">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        required={required}
        className="w-full bg-white border border-gray-300 rounded px-2 py-1.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      {hint && <div className="text-[10px] text-gray-400 mt-0.5">{hint}</div>}
    </div>
  )
}
