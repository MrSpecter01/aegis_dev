"""Prompt loader and templates package."""

from shared.prompts.loader import (
    DEFAULT_PROMPTS_DIR,
    get_prompts_dir,
    load_agent_prompt,
    load_task_prompt,
    load_all_prompts,
)

__all__ = [
    "DEFAULT_PROMPTS_DIR",
    "get_prompts_dir",
    "load_agent_prompt",
    "load_task_prompt",
    "load_all_prompts",
]
