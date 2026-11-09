"""Swappable LLM providers. Add a provider by subclassing LLMProvider and registering it."""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field

import httpx

from ..config import settings


class AIError(Exception):
    MESSAGES = {
        "not_configured": "AI isn't configured. Add ANTHROPIC_API_KEY, OPENAI_API_KEY or GEMINI_API_KEY (with AI_PROVIDER) to .env and restart.",
        "auth": "The AI provider rejected the API key.",
        "rate_limit": "The AI provider is rate-limiting requests. Try again in a minute.",
        "context": "The content is too long for the model, even after chunking.",
        "timeout": "The AI provider took too long to respond.",
        "overloaded": "The AI provider is overloaded. Try again shortly.",
        "api": "The AI provider returned an error.",
        "bad_output": "The AI returned output that couldn't be used.",
    }

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        super().__init__(f"{self.MESSAGES.get(kind, 'AI error.')} {detail}".strip())


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    # provider data that must be sent back unchanged on the next turn
    # (e.g. Gemini's extra_content.google.thought_signature)
    extra: dict | None = None


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


# Provider-neutral chat messages used by the agent:
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "input"}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# Tools: [{"name", "description", "parameters": <JSON schema>}]


class LLMProvider:
    name = "base"

    def __init__(self, model: str, fast_model: str):
        self.model, self.fast_model = model, fast_model

    async def complete(self, system: str, user: str, *, fast: bool = False,
                       max_tokens: int = 4000, temperature: float = 0.3) -> str:
        raise NotImplementedError

    async def chat(self, system: str, messages: list[dict], tools: list[dict], *,
                   max_tokens: int = 4000) -> AssistantTurn:
        """One assistant turn with tool calling."""
        raise NotImplementedError

    async def _post(self, url: str, headers: dict, payload: dict) -> dict:
        """POST with retries on transient errors."""
        last: AIError | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=settings.ai_timeout) as c:
                    r = await c.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                last = AIError("timeout")
            except httpx.HTTPError as e:
                last = AIError("api", type(e).__name__)
            else:
                if r.status_code == 200:
                    return r.json()
                body = r.text[:300]
                if r.status_code in (401, 403):
                    raise AIError("auth")
                if r.status_code == 400 and any(w in body.lower() for w in ("too long", "context", "maximum", "tokens")):
                    raise AIError("context")
                if r.status_code == 429:
                    last = AIError("rate_limit")
                elif r.status_code in (500, 502, 503, 504, 529):
                    last = AIError("overloaded")
                else:
                    raise AIError("api", f"(HTTP {r.status_code}: {_short_error(body)})")
            await asyncio.sleep((2 ** attempt) + random.random())
        raise last or AIError("api")


def _short_error(body: str) -> str:
    try:
        j = json.loads(body)
        err = j.get("error", j)
        return str(err.get("message", err) if isinstance(err, dict) else err)[:200]
    except (json.JSONDecodeError, AttributeError):
        return body[:200]


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    async def complete(self, system, user, *, fast=False, max_tokens=4000, temperature=0.3):  # noqa: ANN001
        data = await self._post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": settings.anthropic_api_key or "", "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": self.fast_model if fast else self.model, "max_tokens": max_tokens,
             "temperature": temperature, "system": system,
             "messages": [{"role": "user", "content": user}]},
        )
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if data.get("stop_reason") == "max_tokens" and not text:
            raise AIError("context")
        return text

    @staticmethod
    def to_messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                           for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                # consecutive tool results belong in one user message
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def chat(self, system, messages, tools, *, max_tokens=4000):  # noqa: ANN001
        data = await self._post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": settings.anthropic_api_key or "", "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            {"model": self.model, "max_tokens": max_tokens, "system": system,
             "messages": self.to_messages(messages),
             "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                       for t in tools]},
        )
        turn = AssistantTurn()
        for b in data.get("content", []):
            if b.get("type") == "text":
                turn.text += b.get("text", "")
            elif b.get("type") == "tool_use":
                turn.tool_calls.append(ToolCall(b["id"], b["name"], b.get("input") or {}))
        return turn


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions, and any compatible endpoint (Gemini, OpenRouter, local servers)."""
    name = "openai"
    token_param = "max_completion_tokens"

    def __init__(self, model: str, fast_model: str, base_url: str | None = None, api_key: str | None = None):
        super().__init__(model, fast_model)
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.openai_api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def complete(self, system, user, *, fast=False, max_tokens=4000, temperature=0.3):  # noqa: ANN001
        data = await self._post(
            self.base_url + "/chat/completions", self._headers(),
            {"model": self.fast_model if fast else self.model, self.token_param: max_tokens,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise AIError("bad_output") from e

    @staticmethod
    def to_messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg: dict = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    calls = []
                    for c in m["tool_calls"]:
                        call = {"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": json.dumps(c["input"])}}
                        if c.get("extra"):
                            call.update(c["extra"])      # e.g. {"extra_content": {"google": {...}}}
                        calls.append(call)
                    msg["tool_calls"] = calls
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    async def chat(self, system, messages, tools, *, max_tokens=4000):  # noqa: ANN001
        data = await self._post(
            self.base_url + "/chat/completions", self._headers(),
            {"model": self.model, self.token_param: max_tokens,
             "messages": self.to_messages(system, messages),
             "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["parameters"]}} for t in tools]},
        )
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise AIError("bad_output") from e
        turn = AssistantTurn(text=msg.get("content") or "")
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            extra = {"extra_content": c["extra_content"]} if c.get("extra_content") else None
            turn.tool_calls.append(ToolCall(c.get("id") or f"call_{len(turn.tool_calls)}", c["function"]["name"],
                                            args, extra))
        return turn


class GeminiProvider(OpenAIProvider):
    """Google Gemini (AI Studio key) through its OpenAI-compatible endpoint. Thought signatures on
    tool calls are preserved via ToolCall.extra, which Gemini 3 requires for multi-turn tool use."""
    name = "gemini"
    token_param = "max_tokens"

    def __init__(self, model: str, fast_model: str):
        super().__init__(model, fast_model, settings.gemini_base_url, settings.gemini_api_key)


_override: LLMProvider | None = None


def set_provider(p: LLMProvider | None) -> None:
    """Used by tests (and could be used for per-user provider selection)."""
    global _override
    _override = p


def get_provider() -> LLMProvider:
    if _override:
        return _override
    if not settings.ai_configured():
        raise AIError("not_configured")
    if settings.ai_provider == "openai":
        return OpenAIProvider(settings.openai_model, settings.openai_fast_model)
    if settings.ai_provider == "gemini":
        return GeminiProvider(settings.gemini_model, settings.gemini_fast_model)
    return AnthropicProvider(settings.anthropic_model, settings.anthropic_fast_model)


def ai_status() -> dict:
    if _override:
        return {"configured": True, "provider": _override.name, "model": _override.model}
    model = {"openai": settings.openai_model, "gemini": settings.gemini_model}.get(settings.ai_provider,
                                                                                   settings.anthropic_model)
    return {"configured": settings.ai_configured(), "provider": settings.ai_provider, "model": model}
