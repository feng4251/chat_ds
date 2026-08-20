import test from 'node:test'
import assert from 'node:assert/strict'

import {
  DEFAULT_PERMISSION_PRESET,
  SESSION_PERMISSION_PRESETS,
  normalizePermissionPreset,
} from './permissionPresets.js'

test('native Session permission choices remain closed and default to approval', () => {
  assert.deepEqual(
    SESSION_PERMISSION_PRESETS.map((preset) => preset.id),
    ['read_only', 'workspace_write', 'session_full'],
  )
  assert.equal(DEFAULT_PERMISSION_PRESET, 'workspace_write')
  assert.equal(normalizePermissionPreset(undefined), 'workspace_write')
  assert.equal(normalizePermissionPreset('fixture-specific-mode'), 'workspace_write')
})
