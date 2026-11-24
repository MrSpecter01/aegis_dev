from typing import Any, List, Optional
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration


class FakeLLM(BaseChatModel):
    response_text: str = "Fake LLM response"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        generation = ChatGeneration(message=AIMessage(content=self.response_text))
        return ChatResult(generations=[generation])

    @property
    def _llm_type(self) -> str:
        return "fake"
