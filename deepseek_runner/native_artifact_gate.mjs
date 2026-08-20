/** Native DSH Stop frontier for immutable Skill-declared artifacts. */

import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'

import { createUserMessage } from '@deepseek-ai/dsh-llm'


export const name = 'chatds-native-artifact-gate'
const SOURCE = Object.freeze({ kind: 'plugin', plugin: name })
const PROJECTION_PATH = '/runtime/controller/native-artifacts.json'
const MAX_PROJECTION_BYTES = 32 * 1024 * 1024
const MAX_RECEIPT_BYTES = 2 * 1024 * 1024
const SAFE_SKILL_NAME = /^[A-Za-z0-9._-]{1,128}$/
const SAFE_SESSION_ID = /^chatds-[0-9a-f]{32}$/


function loadBoundary() {
  const path = process.env.CHATDS_DSH_ARTIFACT_PROJECTION
  if (path === undefined || path === '') return undefined
  if (path !== PROJECTION_PATH) {
    throw new Error('chatds-native-artifact-gate: projection path is invalid')
  }
  let bytes
  try {
    bytes = readFileSync(path)
  } catch {
    throw new Error('chatds-native-artifact-gate: projection is unavailable')
  }
  if (bytes.length === 0 || bytes.length > MAX_PROJECTION_BYTES) {
    throw new Error('chatds-native-artifact-gate: projection size is invalid')
  }
  let value
  try {
    value = JSON.parse(bytes.toString('utf8'))
  } catch {
    throw new Error('chatds-native-artifact-gate: projection is malformed')
  }
  if (
    value === null
    || typeof value !== 'object'
    || Array.isArray(value)
    || value.schema !== 'chatds.deepseek-artifact-gate.v1'
    || typeof value.native_session_id !== 'string'
    || !SAFE_SESSION_ID.test(value.native_session_id)
    || !Array.isArray(value.bound_skill_names)
    || value.bound_skill_names.some((item) => (
      typeof item !== 'string' || !SAFE_SKILL_NAME.test(item)
    ))
    || (
      value.workflow_run_name !== null
      && typeof value.workflow_run_name !== 'string'
    )
  ) throw new Error('chatds-native-artifact-gate: projection is invalid')
  if (
    value.workflow_run_name !== null
    && (
      typeof value.workflow_run_name !== 'string'
      || !/^skill-workflow-[0-9a-f]{16}$/.test(value.workflow_run_name)
    )
  ) throw new Error('chatds-native-artifact-gate: projection is invalid')
  return Object.freeze({
    nativeSessionId: value.native_session_id,
    boundSkillNames: Object.freeze([...new Set(value.bound_skill_names)].sort()),
    workflowRunName: value.workflow_run_name,
  })
}


function parseSkillArgument(value) {
  if (typeof value === 'string') {
    if (Buffer.byteLength(value, 'utf8') > 2 * 1024 * 1024) return undefined
    try { value = JSON.parse(value || '{}') } catch { return undefined }
  }
  const candidate = value?.name
  return typeof candidate === 'string' && SAFE_SKILL_NAME.test(candidate)
    ? candidate
    : undefined
}


function toolResultPassed(data, callId) {
  const content = data?.message?.content
  if (!Array.isArray(content)) return false
  const matches = content.filter((block) => (
    block?.type === 'tool-result'
    && block.toolCallId === callId
    && typeof block.isError === 'boolean'
  ))
  return matches.length === 1 && matches[0].isError === false
}


function workflowFrontierPassed(session, runName) {
  if (runName === null) return true
  const runIds = new Set()
  for (const event of session.events) {
    if (event.type === 'tool-workflow/run-start' && event.data?.name === runName) {
      runIds.add(event.data.runId)
    }
  }
  if (runIds.size !== 1) return false
  const [runId] = runIds
  return session.events.some((event) => (
    event.type === 'tool-workflow/run-end'
    && event.data?.runId === runId
    && event.data?.stopReason === 'completed'
  ))
}


function evaluate(activeSkillNames) {
  const result = spawnSync(
    '/usr/local/bin/python',
    ['-I', '-m', 'deepseek_runner.artifact_gate', PROJECTION_PATH],
    {
      input: JSON.stringify({ active_skill_names: [...activeSkillNames].sort() }),
      encoding: 'utf8',
      timeout: 60000,
      maxBuffer: MAX_RECEIPT_BYTES,
      windowsHide: true,
    },
  )
  if (result.error !== undefined || result.status !== 0 || result.signal !== null) {
    throw new Error('chatds-native-artifact-gate: evaluator failed')
  }
  try {
    const receipt = JSON.parse(result.stdout)
    if (
      receipt === null
      || typeof receipt !== 'object'
      || !['passed', 'failed', 'not_applicable'].includes(receipt.status)
      || !Array.isArray(receipt.findings)
    ) throw new Error('invalid receipt')
    return receipt
  } catch {
    throw new Error('chatds-native-artifact-gate: evaluator returned invalid receipt')
  }
}


export function apply(ctx) {
  const boundary = loadBoundary()
  if (boundary === undefined) return
  const calls = new Map()
  const active = new Set(boundary.boundSkillNames)
  let correctionCount = 0

  ctx.on('session/event', (session, event) => {
    if (String(session.id) !== boundary.nativeSessionId) return
    const data = event.data
    if (event.type === 'tool/call' && data?.name === 'skill') {
      const skillName = parseSkillArgument(data.arguments)
      if (typeof data.callId === 'string' && skillName !== undefined) {
        calls.set(data.callId, skillName)
      }
    } else if (event.type === 'tool/result') {
      const callId = data?.message?.source?.callId
      const skillName = calls.get(callId)
      if (skillName !== undefined && toolResultPassed(data, callId)) {
        active.add(skillName)
      }
    } else if (
      event.type === 'tool/code-dispatch'
      && data?.name === 'skill'
      && data.isError === false
    ) {
      const skillName = parseSkillArgument(data.arguments)
      if (skillName !== undefined) active.add(skillName)
    }
  })

  ctx.on('agent/turn-stopping', ({ agent }) => {
    if (String(agent.session.id) !== boundary.nativeSessionId) return
    // The workflow plugin owns the earlier mandatory frontier.  Do not issue
    // two continuations from the same Stop edge.
    if (!workflowFrontierPassed(agent.session, boundary.workflowRunName)) return
    const receipt = evaluate(active)
    if (receipt.status !== 'failed') return
    if (correctionCount === 0) {
      correctionCount += 1
      let findings = JSON.stringify(receipt.findings.slice(0, 128))
      if (findings.length > 32000) findings = findings.slice(0, 32000)
      agent.steer(createUserMessage({
        content: [{
          type: 'text',
          text: 'Machine artifact receipt is incomplete. Preserve valid work, repair every finding, rerun any declared merge and verification, then finish. Findings: ' + findings,
        }],
        source: SOURCE,
      }))
      return
    }
    throw new Error('chatds-native-artifact-gate: artifact contract remains incomplete')
  })
}
