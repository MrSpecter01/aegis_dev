"""Unit tests for CachingLLM (tests/helpers/llm_cache.py).

Tests RECORD_MODE, replay mode, hashing stability, and error paths.
"""

import json
from pathlib import Path
import pytest
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration

from tests.helpers.fake_llm import FakeLLM
from tests.helpers.llm_cache import CachingLLM, _cache_key


def test_cache_key_generation():
    msg1 = [HumanMessage(content="What is the pod status?")]
    msg2 = [HumanMessage(content="What is the pod status?")]
    msg3 = [HumanMessage(content="Different question")]

    key1 = _cache_key(msg1)
    key2 = _cache_key(msg2)
    key3 = _cache_key(msg3)

    assert isinstance(key1, str)
    assert len(key1) == 16
    assert key1 == key2
    assert key1 != key3


def test_caching_llm_env_var_override(monkeypatch):
    monkeypatch.setenv("AEGIS_LLM_RECORD", "1")
    llm = CachingLLM()
    assert llm.record_mode is True

    monkeypatch.setenv("AEGIS_LLM_RECORD", "0")
    llm2 = CachingLLM()
    assert llm2.record_mode is False


def test_caching_llm_record_mode_missing_wrapped_llm(tmp_path):
    llm = CachingLLM(record_mode=True, fixture_dir=str(tmp_path), wrapped_llm=None)
    with pytest.raises(ValueError, match="CachingLLM in RECORD_MODE requires wrapped_llm to be set"):
        llm.invoke([HumanMessage(content="Hello")])


def test_caching_llm_record_mode_writes_fixture(tmp_path):
    fake = FakeLLM(response_text="Infrastructure shows OOMKilled on cartservice")
    caching_llm = CachingLLM(
        record_mode=True,
        fixture_dir=str(tmp_path),
        wrapped_llm=fake,
    )

    msg = [HumanMessage(content="Check memory status")]
    result = caching_llm.invoke(msg)

    assert result.content == "Infrastructure shows OOMKilled on cartservice"

    key = _cache_key(msg)
    fixture_file = tmp_path / f"{key}.json"
    assert fixture_file.exists()

    with open(fixture_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["cache_key"] == key
    assert data["response"] == "Infrastructure shows OOMKilled on cartservice"
    assert data["messages"][0]["content"] == "Check memory status"


def test_caching_llm_replay_mode_missing_fixture(tmp_path):
    caching_llm = CachingLLM(
        record_mode=False,
        fixture_dir=str(tmp_path),
    )

    msg = [HumanMessage(content="Unrecorded prompt")]
    with pytest.raises(FileNotFoundError, match="LLM fixture not found"):
        caching_llm.invoke(msg)


def test_caching_llm_replay_mode_reads_fixture(tmp_path):
    msg = [HumanMessage(content="Fetch Loki logs")]
    key = _cache_key(msg)
    fixture_file = tmp_path / f"{key}.json"

    fixture_data = {
        "cache_key": key,
        "messages": [{"role": "HumanMessage", "content": "Fetch Loki logs"}],
        "response": "Found OutOfMemoryError in logs",
    }
    with open(fixture_file, "w", encoding="utf-8") as f:
        json.dump(fixture_data, f)

    caching_llm = CachingLLM(
        record_mode=False,
        fixture_dir=str(tmp_path),
    )

    result = caching_llm.invoke(msg)
    assert result.content == "Found OutOfMemoryError in logs"
    assert caching_llm._llm_type == "caching"
