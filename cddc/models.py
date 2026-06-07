"""Model clients for the real agent loop.

Three real providers, all exposing the same `chat(messages, tools) -> Reply`:
  - `DeepSeekClient` - DeepSeek is OpenAI-compatible, so it's a thin wrapper over
    the `openai` async SDK pointed at api.deepseek.com.
  - `CodexClient`    - OpenAI ("Codex") is itself OpenAI-compatible, so it reuses
    the DeepSeek chat loop pointed at api.openai.com with a codex/gpt model.
  - `ClaudeClient`   - Anthropic's Messages API. The agent loop speaks OpenAI's
    wire format everywhere (see agent_worker.py), so this client translates the
    OpenAI-shaped message log + tool specs into Anthropic shape on each call and
    maps the response back into a `Reply`.
`FakeModel` returns scripted replies so the agent loop is testable in simulate.py
with no key and no cost.

Discord-agnostic. The SDKs (`openai`, `anthropic`) are imported lazily (only the
real clients need them), so the token-free sim imports this module without them.
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
    """OpenAI-compatible chat client for DeepSeek (or any compatible endpoint).

    `thinking=True` turns on V4's reasoning mode via the documented extra_body
    switch (`{"thinking": {"type": "enabled"}}`) - SAME per-token rate as
    non-thinking flash, it just emits reasoning tokens. The CoT comes back in
    `reasoning_content`; we read only `content` + tool_calls and never echo the
    CoT back (DeepSeek regenerates it - echoing it would 400). Codex (OpenAI)
    leaves this off; it would reject the param.
    """

    def __init__(self, api_key: str, base_url: str, model: str, *, thinking: bool = False) -> None:
        from openai import AsyncOpenAI  # lazy: sim doesn't need it

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self._thinking = thinking

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "tools": tools or None,
            "tool_choice": "auto" if tools else None,
        }
        if self._thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        resp = await self._client.chat.completions.create(**kwargs)
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


class CodexClient(DeepSeekClient):
    """OpenAI ("Codex") agent. OpenAI's chat API is OpenAI-compatible (it is the
    reference), so this reuses DeepSeekClient's chat loop pointed at OpenAI with a
    codex/gpt model. base_url defaults to OpenAI but stays overridable (proxies,
    Azure-style gateways)."""

    def __init__(
        self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1"
    ) -> None:
        super().__init__(api_key, base_url, model)


def _to_anthropic_tools(specs: list[dict]) -> list[dict]:
    """OpenAI function specs -> Anthropic tool schemas."""
    tools = []
    for s in specs:
        fn = s.get("function", s)
        tools.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return tools


def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-format message log -> (system prompt, Anthropic messages).

    Maps each role across: `system` is hoisted to the top-level system string;
    `assistant` becomes text + tool_use content blocks; `tool` becomes a
    tool_result block folded into a user turn (consecutive tool results merge
    into one user message, as Anthropic expects). Plain user turns pass through.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content") or "",
            }
            # Fold into the preceding user turn iff it's already a block list, so
            # the tool_use turn is answered by a single user/tool_result turn.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            content: list[dict] = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": fn["name"],
                    "input": args if isinstance(args, dict) else {},
                })
            out.append({"role": "assistant", "content": content or ""})
            continue
        # user (and any unexpected role) -> plain user text
        out.append({"role": "user", "content": m.get("content") or ""})
    return "\n\n".join(system_parts), out


class ClaudeClient:
    """Anthropic Messages API client, drop-in for the OpenAI-shaped agent loop.

    Translates the OpenAI message log + tool specs to Anthropic shape per call and
    maps the response back to a `Reply`. Extended/adaptive thinking is left OFF:
    the loop round-trips assistant turns through OpenAI format (see
    agent_worker.assistant_message), which cannot carry Anthropic `thinking`
    blocks back, and tool use with thinking on requires echoing them - so enabling
    it would 400. max_tokens is required by the API; the default is sized for the
    short tool-loop turns here (well under the SDK's non-streaming timeout guard).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str | None = None,
        max_tokens: int = 8000,
    ) -> None:
        from anthropic import AsyncAnthropic  # lazy: sim doesn't need it

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self.model = model
        self.max_tokens = max_tokens

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        system, msgs = _to_anthropic_messages(messages)
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": msgs,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
        resp = await self._client.messages.create(**kwargs)
        content = ""
        calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        usage = resp.usage
        tokens = (usage.input_tokens + usage.output_tokens) if usage else 0
        return Reply(content=content, tool_calls=calls, tokens=tokens)


class FakeModel:
    """Scripted client for sim: pops a Reply per chat() call. No key, no cost."""

    def __init__(self, script: list[Reply]) -> None:
        self._script = list(script)
        self.model = "fake"

    async def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self._script:
            return self._script.pop(0)
        return Reply(content="(fake model: out of script)", tool_calls=[])
