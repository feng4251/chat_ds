"""Construction and lookup for configured Agent Engines."""

from __future__ import annotations

from collections.abc import Iterable

from config import settings

from .base import AgentEngine, SUPPORTED_ENGINE_IDS
class AgentEngineRegistry:
    def __init__(self, engines: Iterable[AgentEngine]) -> None:
        self._engines = {engine.engine_id: engine for engine in engines}
        unknown = set(self._engines) - SUPPORTED_ENGINE_IDS
        if unknown:
            raise ValueError(f"Unsupported Agent Engine ids: {sorted(unknown)}")

    def get(self, engine_id: str) -> AgentEngine:
        try:
            return self._engines[engine_id]
        except KeyError as exc:
            raise LookupError(f"Agent Engine '{engine_id}' is not configured") from exc

    async def descriptors(self):
        return [
            await self._engines[key].describe()
            for key in sorted(self._engines)
        ]


def build_agent_engine_registry() -> AgentEngineRegistry:
    engines: list[AgentEngine] = []
    # Native engines are registered only when their trusted Supervisor is
    # enabled. Missing isolation infrastructure fails closed; there is no
    # fallback into the retired ChatDS Harness.
    if settings.claude_code_engine_enabled:
        from .claude_code import ClaudeCodeEngine

        engines.append(
            ClaudeCodeEngine(
                base_url=settings.claude_runner_url,
                internal_token=settings.internal_api_token,
                timeout_seconds=settings.claude_runner_stream_timeout_seconds,
            )
        )
    if settings.deepseek_harness_engine_enabled:
        from .deepseek_harness import DeepSeekHarnessEngine

        engines.append(
            DeepSeekHarnessEngine(
                base_url=settings.deepseek_runner_url,
                internal_token=settings.internal_api_token,
                timeout_seconds=settings.deepseek_runner_stream_timeout_seconds,
            )
        )
    return AgentEngineRegistry(engines)
