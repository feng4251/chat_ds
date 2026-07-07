"""Skills system — SKILL.md discovery, loading, and prompt caching.

Simplified from hermes-agent for chat_ds harness:
- No plugin/namespace system
- No platform matching (server-side, always Linux)
- No disabled-skills config
- No secret capture / env var requirements
- No inline-shell preprocessing
"""

from skills.scanner import iter_skill_index_files, find_all_skills
from skills.loader import parse_frontmatter, substitute_template_vars, load_skill_content
from skills.manager import SkillsManager, get_manager

__all__ = [
    "iter_skill_index_files",
    "find_all_skills",
    "parse_frontmatter",
    "substitute_template_vars",
    "load_skill_content",
    "SkillsManager",
    "get_manager",
]