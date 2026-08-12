const ENTRY_SCRIPT_PATTERN = /\/assets\/[^/]+\.js(?:[?#].*)?$/

export function currentFrontendEntry(documentLike = globalThis.document) {
  const scripts = Array.from(documentLike?.scripts || [])
  const entry = scripts.find((script) => (
    ENTRY_SCRIPT_PATTERN.test(String(script?.src || ''))
  ))
  if (!entry?.src) return ''
  try {
    return new URL(entry.src, globalThis.location?.href).pathname
  } catch {
    return String(entry.src)
  }
}

export function frontendVersionChanged(currentEntry, buildInfo) {
  const availableEntry = String(buildInfo?.entry || '')
  return Boolean(
    currentEntry
    && availableEntry
    && currentEntry !== availableEntry
  )
}

export function canApplyFrontendUpdate(documentLike = globalThis.document) {
  if (!documentLike || documentLike.visibilityState === 'hidden') return false
  if (documentLike.querySelector?.('[data-chatds-reload-blocked="true"]')) {
    return false
  }
  const active = documentLike.activeElement
  const tagName = String(active?.tagName || '').toLowerCase()
  return !(
    ['input', 'textarea', 'select'].includes(tagName)
    || active?.isContentEditable
  )
}

export async function getFrontendBuildInfo(fetchImpl = globalThis.fetch) {
  const response = await fetchImpl('/build-info.json', {
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw new Error(`Build info unavailable (${response.status})`)
  return await response.json()
}
