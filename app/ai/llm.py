"""Provider-agnostic chat interface. Production uses Bedrock via its OpenAI-compatible API; tests use FakeLLM."""

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

from openai import APIError, AsyncOpenAI

from app.core.config import get_settings


class LLMUnavailable(Exception):
    """The model provider failed or isn't configured."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResult:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    model: str


class Thinking:
    """Yielded once by a stream when a reasoning model starts thinking, before any answer text."""


THINKING = Thinking()

# A stream yields text as it is written, possibly THINKING first, and always ends with the full LLMResult.
StreamItem = str | Thinking | LLMResult


class LLMClient(Protocol):
    model: str

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800) -> LLMResult: ...

    def stream(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800) -> AsyncIterator[StreamItem]: ...


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


class OpenAICompatibleLLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float):
        self.model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=2)

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800) -> LLMResult:
        try:
            r = await self._client.chat.completions.create(
                model=self.model, messages=messages, tools=tools or None, max_tokens=max_tokens, temperature=0.3
            )
        except APIError as e:
            raise LLMUnavailable(f"{type(e).__name__}: {getattr(e, 'status_code', '')}") from e
        msg = r.choices[0].message
        calls = [ToolCall(id=c.id, name=c.function.name, arguments=_parse_arguments(c.function.arguments)) for c in msg.tool_calls or []]
        usage = r.usage
        return LLMResult(
            text=(msg.content or "").strip(),
            tool_calls=calls,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=self.model,
        )

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800) -> AsyncIterator[StreamItem]:
        """Like `complete`, but yields the answer as it is written. Tool calls arrive in fragments and are
        reassembled; usage comes in the final chunk, so metering is as exact as for `complete`."""
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = None
        thinking = False
        try:
            response = await self._client.chat.completions.create(
                model=self.model, messages=messages, tools=tools or None, max_tokens=max_tokens, temperature=0.3,
                stream=True, stream_options={"include_usage": True},
            )
            async for chunk in response:
                if chunk.usage:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not thinking and (delta.model_extra or {}).get("reasoning"):
                    thinking = True
                    yield THINKING
                if delta.content:
                    text.append(delta.content)
                    yield delta.content
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.id or slot["id"]
                    if tc.function:
                        slot["name"] += tc.function.name or ""
                        slot["arguments"] += tc.function.arguments or ""
        except APIError as e:
            raise LLMUnavailable(f"{type(e).__name__}: {getattr(e, 'status_code', '')}") from e
        yield LLMResult(
            text="".join(text).strip(),
            tool_calls=[
                ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=_parse_arguments(c["arguments"]))
                for i, c in sorted(calls.items())
            ],
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=self.model,
        )


Step = LLMResult | Callable[[list[dict]], LLMResult]


@dataclass
class FakeLLM:
    """Scripted stand-in for tests: returns the queued results in order and records every request."""

    script: list[Step] = field(default_factory=list)
    model: str = "fake-model"
    requests: list[list[dict]] = field(default_factory=list)

    async def complete(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800) -> LLMResult:
        self.requests.append([dict(m) for m in messages])
        if not self.script:
            raise LLMUnavailable("FakeLLM script exhausted")
        step = self.script.pop(0)
        return step(messages) if callable(step) else step

    async def stream(self, messages: list[dict], tools: list[dict] | None = None, max_tokens: int = 800) -> AsyncIterator[StreamItem]:
        """The scripted result, delivered word by word the way a real stream arrives."""
        result = await self.complete(messages, tools, max_tokens)
        for piece in re.findall(r"\S+\s*", result.text):
            yield piece
        yield result


def text(content: str, tokens: tuple[int, int] = (100, 20)) -> LLMResult:
    return LLMResult(text=content, tool_calls=[], input_tokens=tokens[0], output_tokens=tokens[1], model="fake-model")


def tool_call(name: str, arguments: dict | None = None, tokens: tuple[int, int] = (100, 10), call_id: str | None = None) -> LLMResult:
    call = ToolCall(id=call_id or f"call_{name}", name=name, arguments=arguments or {})
    return LLMResult(text="", tool_calls=[call], input_tokens=tokens[0], output_tokens=tokens[1], model="fake-model")


@lru_cache
def _real_llm() -> OpenAICompatibleLLM:
    s = get_settings()
    if not s.llm_api_key:
        raise LLMUnavailable("No LLM API key configured")
    return OpenAICompatibleLLM(s.openai_base_url, s.llm_api_key, s.llm_chat_model, s.llm_timeout_seconds)


def get_llm() -> LLMClient:
    """FastAPI dependency; tests override it with a FakeLLM."""
    if get_settings().llm_provider == "fake":
        return FakeLLM()
    return _real_llm()
