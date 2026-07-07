"""Memory system for chat_ds harness — persistent curated memory per user.

Simplified from hermes-agent. No external provider plugin system; only the
built-in file-based store backed by MEMORY.md / USER.md under data/memories/.
"""

from memory.store import MemoryStore
from memory.scrubber import StreamingContextScrubber, build_memory_context_block
from memory.manager import MemoryManager

__all__ = [
    "MemoryStore",
    "StreamingContextScrubber",
    "build_memory_context_block",
    "MemoryManager",
]