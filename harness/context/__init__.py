"""Context management — pluggable context engines for token-limit handling."""

from context.engine import ContextEngine
from context.compressor import ContextCompressor

__all__ = ["ContextEngine", "ContextCompressor"]