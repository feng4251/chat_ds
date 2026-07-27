import assert from 'node:assert/strict'
import test from 'node:test'

import { groupSkillsForDisplay } from './skillGrouping.js'

function member(name, role, overrides = {}) {
  return {
    id: name,
    name,
    scope: 'session',
    session_id: 'conversation',
    bundle_id: 'bundle-v23',
    bundle_role: role,
    bundle_root_name: 'clinical-trial-design',
    ...overrides,
  }
}

test('one bundle plus an independent Skill remains two top-level items', () => {
  const skills = [
    member('clinical-trial-design', 'primary'),
    ...Array.from({ length: 18 }, (_, index) => (
      member(`database-${index}`, 'supporting')
    )),
    member('visual-browser-operator', 'primary', {
      bundle_id: 'bundle-browser',
      bundle_root_name: 'visual-browser-operator',
    }),
  ]

  const grouped = groupSkillsForDisplay(skills)
  assert.equal(grouped.topLevelCount, 2)
  assert.equal(grouped.items[0].type, 'group')
  assert.equal(grouped.items[0].main.name, 'clinical-trial-design')
  assert.equal(grouped.items[0].children.length, 18)
  assert.equal(grouped.items[1].type, 'skill')
  assert.equal(grouped.items[1].skill.name, 'visual-browser-operator')
})

test('legacy upload cohort is grouped without absorbing a later Skill', () => {
  const importedAt = '2026-07-24T08:36:49'
  const skills = [
    {
      id: 'main',
      name: 'clinical-trial-design',
      scope: 'session',
      session_id: 'conversation',
      created_at: importedAt,
    },
    {
      id: 'child',
      name: 'database-helper',
      scope: 'session',
      session_id: 'conversation',
      category: 'skills-bundle',
      created_at: importedAt,
    },
    {
      id: 'browser',
      name: 'visual-browser-operator',
      scope: 'session',
      session_id: 'conversation',
      created_at: '2026-07-27T00:52:50',
    },
  ]

  const grouped = groupSkillsForDisplay(skills)
  assert.equal(grouped.topLevelCount, 2)
  assert.deepEqual(
    grouped.items.map((item) => item.type),
    ['group', 'skill'],
  )
})

test('ambiguous legacy roots are never guessed into one bundle', () => {
  const createdAt = '2026-07-27T00:00:00'
  const skills = [
    { id: 'one', name: 'one', scope: 'session', created_at: createdAt },
    { id: 'two', name: 'two', scope: 'session', created_at: createdAt },
    {
      id: 'child',
      name: 'child',
      scope: 'session',
      category: 'skills-bundle',
      created_at: createdAt,
    },
  ]

  const grouped = groupSkillsForDisplay(skills)
  assert.equal(grouped.topLevelCount, 3)
  assert.ok(grouped.items.every((item) => item.type === 'skill'))
})
