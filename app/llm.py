"""LLM access. One OpenAI-compatible client covers Microsoft Foundry, GitHub Models, and OpenAI."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class LLMDelta:
    text: str | None = None
    tool_index: int | None = None
    tool_id: str | None = None
    tool_name: str | None = None
    tool_args: str | None = None
    finish_reason: str | None = None


class LLMClient(Protocol):
    def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
                    ) -> AsyncIterator[LLMDelta]: ...

    async def complete_json(self, messages: list[dict[str, Any]]) -> dict[str, Any]: ...


class OpenAICompatibleLLM:
    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI

        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is not set (Foundry key, GitHub token, or OpenAI key).")
        base_url = settings.resolved_llm_base_url
        if "<resource>" in base_url:
            raise RuntimeError("Set LLM_BASE_URL to your Foundry endpoint, e.g. https://myres.openai.azure.com/openai/v1/")
        headers = {"api-key": settings.llm_api_key} if settings.llm_provider == "foundry" else None
        self.client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=base_url, default_headers=headers,
                                  max_retries=1, timeout=20)
        self.model = settings.resolved_llm_model
        self.temperature = settings.llm_temperature

    async def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
                          ) -> AsyncIterator[LLMDelta]:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True,
                                  "temperature": self.temperature}
        if tools:
            kwargs["tools"] = tools
        stream = await self.client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta is not None and delta.content:
                    yield LLMDelta(text=delta.content)
                for tc in (delta.tool_calls or []) if delta is not None else []:
                    yield LLMDelta(
                        tool_index=tc.index, tool_id=tc.id,
                        tool_name=tc.function.name if tc.function else None,
                        tool_args=tc.function.arguments if tc.function else None,
                    )
                if choice.finish_reason:
                    yield LLMDelta(finish_reason=choice.finish_reason)
        finally:
            await stream.close()

    async def complete_json(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        resp = await self.client.chat.completions.create(
            model=self.model, messages=messages, temperature=0, response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            log.warning("tracker returned non-JSON: %s", content[:200])
            return {}


# --------------------------------------------------------------------------- testing
class ScriptedLLM:
    """Deterministic stand-in for tests and offline demos.

    `script` is a list of turns. Each turn is a list of "rounds"; a round is either a
    string (spoken text) or a dict {"tool": name, "args": {...}} (a tool call), or a
    list mixing both. After a tool round, the next round in the same turn is used.
    """

    def __init__(self, script: list[list[Any]], token_delay: float = 0.0, tracker: list[dict] | None = None):
        self.script = [list(t) for t in script]
        self.token_delay = token_delay
        self.tracker = list(tracker or [])
        self.calls: list[list[dict[str, Any]]] = []

    async def stream_chat(self, messages, tools=None):
        self.calls.append(messages)
        if not self.script:
            round_ = "Okay."
        else:
            turn = self.script[0]
            round_ = turn.pop(0) if turn else "Okay."
            if not turn:
                self.script.pop(0)
        items = round_ if isinstance(round_, list) else [round_]
        tool_i = 0
        for item in items:
            if isinstance(item, str):
                for word in item.split(" "):
                    if self.token_delay:
                        await asyncio.sleep(self.token_delay)
                    yield LLMDelta(text=word + " ")
            else:
                yield LLMDelta(tool_index=tool_i, tool_id=f"call_{len(self.calls)}_{tool_i}", tool_name=item["tool"],
                               tool_args="")
                yield LLMDelta(tool_index=tool_i, tool_args=json.dumps(item.get("args", {})))
                tool_i += 1
        yield LLMDelta(finish_reason="tool_calls" if tool_i else "stop")

    async def complete_json(self, messages):
        return self.tracker.pop(0) if self.tracker else {}


def make_llm(settings: Settings) -> LLMClient:
    return OpenAICompatibleLLM(settings)
