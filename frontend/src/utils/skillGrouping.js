function isSessionSkill(skill) {
  return skill.scope === 'session' || Boolean(skill.session_id)
}

function scopeKey(skill) {
  return isSessionSkill(skill) ? `session:${skill.session_id || 'session'}` : 'user'
}

function skillKey(skill) {
  return `${scopeKey(skill)}:${skill.id || skill.name}`
}

function legacyBundleIdentities(skills) {
  const identities = new Map()
  const cohorts = new Map()

  for (const skill of skills) {
    if (skill.bundle_id) continue

    const historicalRoot = (
      skill.bundle_root_name
      || skill.bundle_root
      || skill.bundle_parent
      || skill.source_root
    )
    if (historicalRoot) {
      identities.set(skillKey(skill), {
        bundleId: `legacy-explicit:${scopeKey(skill)}:${historicalRoot}`,
        role: (
          skill.bundle_role
          || (skill.is_bundle_child || skill.name !== historicalRoot
            ? 'supporting'
            : 'primary')
        ),
      })
      continue
    }

    if (!skill.created_at) continue
    const cohortKey = `${scopeKey(skill)}:${skill.created_at}`
    if (!cohorts.has(cohortKey)) cohorts.set(cohortKey, [])
    cohorts.get(cohortKey).push(skill)
  }

  for (const [cohortKey, cohort] of cohorts.entries()) {
    const supporting = cohort.filter((skill) => skill.category === 'skills-bundle')
    const primary = cohort.filter((skill) => skill.category !== 'skills-bundle')
    if (supporting.length === 0 || primary.length !== 1) continue

    const root = primary[0]
    const bundleId = `legacy-cohort:${cohortKey}:${root.id || root.name}`
    for (const skill of cohort) {
      identities.set(skillKey(skill), {
        bundleId,
        role: skill === root ? 'primary' : 'supporting',
      })
    }
  }
  return identities
}

/**
 * Group flat Skill API records into stable top-level display items.
 *
 * The Backend's explicit bundle identity is authoritative. Legacy fields are
 * considered only when they identify one primary without ambiguity.
 */
export function groupSkillsForDisplay(skills = []) {
  const sorted = [...skills].sort((a, b) => {
    const aSession = isSessionSkill(a) ? 0 : 1
    const bSession = isSessionSkill(b) ? 0 : 1
    return aSession - bSession
  })
  const legacy = legacyBundleIdentities(sorted)
  const identities = new Map()
  const groups = new Map()

  for (const skill of sorted) {
    const fallback = legacy.get(skillKey(skill))
    const bundleId = skill.bundle_id || fallback?.bundleId
    const role = skill.bundle_role || fallback?.role
    if (!bundleId || !['primary', 'supporting'].includes(role)) continue

    const identity = `${scopeKey(skill)}:${bundleId}`
    identities.set(skillKey(skill), identity)
    if (!groups.has(identity)) groups.set(identity, [])
    groups.get(identity).push(skill)
  }

  const emittedGroups = new Set()
  const items = []
  for (const skill of sorted) {
    const identity = identities.get(skillKey(skill))
    if (!identity) {
      items.push({
        type: 'skill',
        key: skillKey(skill),
        skill,
      })
      continue
    }
    if (emittedGroups.has(identity)) continue
    emittedGroups.add(identity)

    const members = groups.get(identity) || []
    const primaries = members.filter((member) => {
      const fallback = legacy.get(skillKey(member))
      return (member.bundle_role || fallback?.role) === 'primary'
    })
    if (members.length > 1 && primaries.length === 1) {
      const main = primaries[0]
      items.push({
        type: 'group',
        key: `bundle-${identity}`,
        main,
        children: members.filter((member) => member !== main),
      })
      continue
    }

    for (const member of members) {
      items.push({
        type: 'skill',
        key: skillKey(member),
        skill: member,
      })
    }
  }

  return {
    items,
    topLevelCount: items.length,
  }
}
