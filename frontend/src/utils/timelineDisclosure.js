/**
 * Auto-open only while a block is current/live. Once the user makes an
 * explicit choice, preserve that choice across later lifecycle updates.
 */
export function liveDisclosureOpen(manualOpen, live) {
  return manualOpen === null ? Boolean(live) : Boolean(manualOpen)
}
