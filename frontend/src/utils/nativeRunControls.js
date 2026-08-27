export const NATIVE_CONTROL_CAPABILITY = Object.freeze({
  interrupt: 'native_interrupt',
  followup: 'native_followup',
  steer: 'native_steer',
})

export function activeNativeRunFromCards(payload = {}) {
  const active = (payload.roots || []).filter((root) => root?.active)
  if (active.length !== 1) return null
  const root = active[0]
  if (
    typeof root.root_run_id !== 'string'
    || !/^[0-9a-f]{32}$/.test(root.root_run_id)
    || typeof root.engine_id !== 'string'
    || root.engine_id === ''
  ) return null
  return {
    runId: root.root_run_id,
    engineId: root.engine_id,
    controls: Array.isArray(root.controls) ? root.controls : [],
    controlsTruncated: root.controls_truncated === true,
  }
}

export function nativeControlCapabilities(activeRun, engineOptions = []) {
  if (!activeRun) return new Set()
  const engine = engineOptions.find((item) => item.id === activeRun.engineId)
  return new Set(Array.isArray(engine?.capabilities) ? engine.capabilities : [])
}

export function canSubmitNativeControl({
  activeRun,
  engineOptions,
  action,
  text,
  attachmentCount = 0,
  stateUnknown = false,
  sending = false,
}) {
  if (!activeRun || stateUnknown || sending) return false
  const capability = NATIVE_CONTROL_CAPABILITY[action]
  if (!capability) return false
  if (!nativeControlCapabilities(activeRun, engineOptions).has(capability)) {
    return false
  }
  if (action === 'interrupt') return true
  return attachmentCount === 0 && typeof text === 'string' && text.trim() !== ''
}

export function createNativeControlId(cryptoObject = globalThis.crypto) {
  if (typeof cryptoObject?.randomUUID !== 'function') {
    throw new Error('Secure control identity generation is unavailable')
  }
  return cryptoObject.randomUUID().replaceAll('-', '')
}

export function isTerminalNativeControlStatus(status) {
  return status === 'delivered' || status === 'rejected'
}

export function draftAfterNativeControlReceipt(draft, retry, receipt) {
  if (
    receipt?.status === 'delivered'
    && receipt.control_id === retry?.controlId
    && typeof retry?.text === 'string'
    && retry.text !== ''
    && String(draft ?? '').trim() === retry.text
  ) return ''
  return draft
}

export function pendingNativeControlId({
  activeRun,
  messages = [],
  action,
  text = null,
}) {
  const controls = Array.isArray(activeRun?.controls) ? activeRun.controls : []
  const normalizedText = action === 'interrupt' ? null : String(text ?? '').trim()
  const messagesById = new Map(messages.map((message) => [message.id, message]))
  for (let index = controls.length - 1; index >= 0; index -= 1) {
    const control = controls[index]
    if (
      control?.status !== 'pending'
      || control.action !== action
      || typeof control.control_id !== 'string'
      || !/^[0-9a-f]{32}$/.test(control.control_id)
    ) continue
    if (action === 'interrupt') return control.control_id
    const message = messagesById.get(control.message_id)
    if (
      message?.source === 'native_control'
      && typeof message.content === 'string'
      && message.content.trim() === normalizedText
    ) return control.control_id
  }
  return null
}
