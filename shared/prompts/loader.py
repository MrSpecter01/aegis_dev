"""Prompt loader module for Aegis agents.

Centralizes loading of agent personas and task templates from the seed/prompts directory,
supporting environment variable overrides and custom directory injection.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional, Union
import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "seed" / "prompts"


def get_prompts_dir(custom_dir: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the prompts directory in hierarchical order:
    
    1. Explicit custom_dir argument
    2. AEGIS_PROMPTS_DIR environment variable
    3. Default repo seed/prompts directory
    """
    if custom_dir:
        return Path(custom_dir)
    env_dir = os.getenv("AEGIS_PROMPTS_DIR")
    if env_dir:
        return Path(env_dir)
    return DEFAULT_PROMPTS_DIR


def _load_yaml_file(filepath: Path) -> Dict[str, Any]:
    """Load and parse a YAML file."""
    if not filepath.exists():
        raise FileNotFoundError(f"Missing prompt file: {filepath}")
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}


def load_agent_prompt(agent_key: str, prompts_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load an agent persona configuration from agents.yaml."""
    base_dir = get_prompts_dir(prompts_dir)
    filepath = base_dir / "agents.yaml"
    data = _load_yaml_file(filepath)
    if agent_key not in data:
        raise KeyError(f"Agent prompt key '{agent_key}' not found in {filepath}")
    return data[agent_key]


def load_task_prompt(task_key: str, prompts_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load a task description configuration from tasks.yaml."""
    base_dir = get_prompts_dir(prompts_dir)
    filepath = base_dir / "tasks.yaml"
    data = _load_yaml_file(filepath)
    if task_key not in data:
        raise KeyError(f"Task prompt key '{task_key}' not found in {filepath}")
    return data[task_key]


def load_all_prompts(prompts_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Load all agent and task prompt definitions from the prompts directory."""
    base_dir = get_prompts_dir(prompts_dir)
    agents_file = base_dir / "agents.yaml"
    tasks_file = base_dir / "tasks.yaml"
    return {
        "agents": _load_yaml_file(agents_file),
        "tasks": _load_yaml_file(tasks_file),
    }
