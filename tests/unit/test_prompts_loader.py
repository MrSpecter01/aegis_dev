"""Unit tests for the centralized prompt loader module."""

import os
from pathlib import Path
import pytest

from shared.prompts.loader import (
    DEFAULT_PROMPTS_DIR,
    get_prompts_dir,
    load_agent_prompt,
    load_task_prompt,
    load_all_prompts,
)


def test_get_prompts_dir_default(monkeypatch):
    monkeypatch.delenv("AEGIS_PROMPTS_DIR", raising=False)
    resolved = get_prompts_dir()
    assert resolved == DEFAULT_PROMPTS_DIR


def test_get_prompts_dir_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("AEGIS_PROMPTS_DIR", str(tmp_path))
    resolved = get_prompts_dir()
    assert resolved == tmp_path


def test_get_prompts_dir_custom_arg(tmp_path):
    resolved = get_prompts_dir(tmp_path)
    assert resolved == tmp_path


def test_load_agent_prompt_success():
    data = load_agent_prompt("infrastructure_investigator")
    assert data["role"] == "Infrastructure Investigator"
    assert data["token_budget"] == 10000
    assert data["max_iter"] == 10


def test_load_task_prompt_success():
    data = load_task_prompt("investigate_infrastructure")
    assert "service" in data["description"]
    assert data["agent"] == "infrastructure_investigator"


def test_load_all_prompts_success():
    all_data = load_all_prompts()
    assert "agents" in all_data
    assert "tasks" in all_data
    assert "infrastructure_investigator" in all_data["agents"]
    assert "investigate_infrastructure" in all_data["tasks"]


def test_load_agent_prompt_missing_key_raises():
    with pytest.raises(KeyError, match="Agent prompt key 'nonexistent' not found"):
        load_agent_prompt("nonexistent")


def test_load_task_prompt_missing_key_raises():
    with pytest.raises(KeyError, match="Task prompt key 'nonexistent' not found"):
        load_task_prompt("nonexistent")


def test_load_prompt_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="Missing prompt file"):
        load_agent_prompt("infrastructure_investigator", prompts_dir=tmp_path)
