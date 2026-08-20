export const DEFAULT_PERMISSION_PRESET = 'workspace_write'

export const SESSION_PERMISSION_PRESETS = [
  {
    id: 'read_only',
    shortLabel: '只读',
    label: 'Read only / 只读',
    description: '只允许读取当前 Session 工作区和只读信息源。',
  },
  {
    id: 'workspace_write',
    shortLabel: '可写需授权',
    label: 'Write but need allow / 可写但需授权',
    description: '写入和执行由原生 Harness 发起权限请求，经页面确认后继续。',
  },
  {
    id: 'session_full',
    shortLabel: '完整权限',
    label: 'Full access / Session 内完整权限',
    description: '免逐次确认，但权限仍被限制在当前 Session 沙箱与出网边界内。',
  },
]

const PRESET_IDS = new Set(SESSION_PERMISSION_PRESETS.map((preset) => preset.id))

export function normalizePermissionPreset(value) {
  return PRESET_IDS.has(value) ? value : DEFAULT_PERMISSION_PRESET
}

export function permissionPreset(value) {
  const normalized = normalizePermissionPreset(value)
  return SESSION_PERMISSION_PRESETS.find((preset) => preset.id === normalized)
}
