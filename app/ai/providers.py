"""Swappable LLM providers. Add a provider by subclassing LLMProvider and registering it."""
from __future__ import annotations

import asyncio
import json
import random

import httpx

from ..config import settings


class AIError(Exception):
    MESSAGES = {
        "not_configured": "AI isn't configured. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env and restart.",
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


class LLMProvider:
    name = "base"

    def __init__(self, model: str, fast_model: str):
        self.model, self.fast_model = model, fast_model

    async def complete(self, system: str, user: str, *, fast: bool = False,
                       max_tokens: int = 4000, temperature: float = 0.3) -> str:
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


class OpenAIProvider(LLMProvider):
    name = "openai"

    async def complete(self, system, user, *, fast=False, max_tokens=4000, temperature=0.3):  # noqa: ANN001
        data = await self._post(
            settings.openai_base_url.rstrip("/") + "/chat/completions",
            {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            {"model": self.fast_model if fast else self.model, "max_completion_tokens": max_tokens,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise AIError("bad_output") from e


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
    return AnthropicProvider(settings.anthropic_model, settings.anthropic_fast_model)


def ai_status() -> dict:
    if _override:
        return {"configured": True, "provider": _override.name, "model": _override.model}
    return {"configured": settings.ai_configured(), "provider": settings.ai_provider,
            "model": settings.openai_model if settings.ai_provider == "openai" else settings.anthropic_model}
