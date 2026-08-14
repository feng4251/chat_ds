export function compatibleModelsForEngine(models, engineId) {
  if (!engineId) return [...models]
  return models.filter((model) => (
    Array.isArray(model.compatible_engines)
    && model.compatible_engines.includes(engineId)
  ))
}

export function modelForEngine(models, engineId, currentModelId, defaultModelId) {
  const candidates = compatibleModelsForEngine(models, engineId)
  if (candidates.some((model) => model.id === currentModelId)) return currentModelId
  if (defaultModelId && candidates.some((model) => model.id === defaultModelId)) {
    return defaultModelId
  }
  return candidates.find((model) => model.is_default)?.id || candidates[0]?.id || ''
}
