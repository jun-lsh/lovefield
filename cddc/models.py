"""Model clients for the real agent loop.

DeepSeek is OpenAI-compatible, so `DeepSeekClient` is a thin wrapper over the
`openai` async SDK pointed at api.deepseek.com. `FakeModel` returns scripted
replies so the agent loop is testable in simulate.py with no key and no cost.

Discord-agnostic. `openai` is imported lazily (only DeepSeekClient needs it), so
the token-free sim imports this module without the SDK installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    """One assistant turn: free text and/or tool calls, plus tokens spent."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0  # total tokens for this call (drives the budget cap)


class ModelClient(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply: ...


def assistant_message(reply: Reply) -> dict:
    """Rebuild the OpenAI-format assistant message so tool results can ref it."""
    msg: dict = {"role": "assistant", "content": reply.content or ""}
    if reply.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in reply.tool_calls
        ]
    return msg


class DeepSeekClient:
    """OpenAI-compatible chat client for DeepSeek (or any compatible endpoint)."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        from openai import AsyncOpenAI  # lazy: sim doesn't need it

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        resp = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
        )
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        tokens = resp.usage.total_tokens if resp.usage else 0
        return Reply(content=msg.content or "", tool_calls=calls, tokens=tokens)


class FakeModel:
    """Scripted client for sim: pops a Reply per chat() call. No key, no cost."""

    def __init__(self, script: list[Reply]) -> None:
        self._script = list(script)
        self.model = "fake"

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self._script:
            return self._script.pop(0)
        return Reply(content="(fake model: out of script)", tool_calls=[])
