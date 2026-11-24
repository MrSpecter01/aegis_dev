"""CachingLLM — record/replay harness for LLM responses.

In RECORD_MODE (AEGIS_LLM_RECORD=1), wraps a real LLM and writes every
response to a JSON fixture file under tests/fixtures/llm_responses/.

In replay mode (default), reads responses from fixture files and returns
them deterministically — no live LLM calls, safe for CI.

Fixture file naming: <fixture_dir>/<cache_key>.json
Cache key: sha256 of the serialised message list (role + content tuples).
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration

_DEFAULT_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "llm_responses"


def _cache_key(messages: List[BaseMessage]) -> str:
    """Stable hash of the message list for use as a fixture filename."""
    payload = json.dumps(
        [{"role": m.__class__.__name__, "content": m.content} for m in messages],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class CachingLLM(BaseChatModel):
    """LLM wrapper that records responses to fixtures and replays them in CI.

    Attributes:
        wrapped_llm: The real LLM to call in RECORD_MODE. Unused in replay mode.
        fixture_dir: Directory where fixture JSON files are stored/read.
        record_mode: When True, call the real LLM and write fixture. When False,
                     read from fixture. Defaults to AEGIS_LLM_RECORD env var.
    """

    wrapped_llm: Optional[Any] = None
    fixture_dir: str = str(_DEFAULT_FIXTURE_DIR)
    record_mode: bool = False

    def model_post_init(self, __context: Any) -> None:
        # Allow env var to override record_mode at construction time
        if os.getenv("AEGIS_LLM_RECORD", "0") == "1":
            object.__setattr__(self, "record_mode", True)

    def _fixture_path(self, key: str) -> Path:
        return Path(self.fixture_dir) / f"{key}.json"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        key = _cache_key(messages)
        fixture = self._fixture_path(key)

        if self.record_mode:
            if self.wrapped_llm is None:
                raise ValueError("CachingLLM in RECORD_MODE requires wrapped_llm to be set.")
            result: ChatResult = self.wrapped_llm._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )
            fixture.parent.mkdir(parents=True, exist_ok=True)
            with open(fixture, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "cache_key": key,
                        "messages": [
                            {"role": m.__class__.__name__, "content": m.content}
                            for m in messages
                        ],
                        "response": result.generations[0].message.content,
                    },
                    f,
                    indent=2,
                )
            return result

        # Replay mode
        if not fixture.exists():
            raise FileNotFoundError(
                f"LLM fixture not found: {fixture}\n"
                f"Run with AEGIS_LLM_RECORD=1 against a live LLM to record it."
            )
        with open(fixture, "r", encoding="utf-8") as f:
            data = json.load(f)
        response_text: str = data["response"]
        generation = ChatGeneration(message=AIMessage(content=response_text))
        return ChatResult(generations=[generation])

    @property
    def _llm_type(self) -> str:
        return "caching"
