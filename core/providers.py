from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


class ProviderError(Exception):
    pass


@dataclass
class ToolCallDelta:
    index: int
    id: str | None
    name: str | None
    args: str


class RateLimited(ProviderError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AuthFailed(ProviderError):
    pass


class ContextTooLong(ProviderError):
    pass


class PermanentError(ProviderError):
    """Config-dead: wrong endpoint/retired model/bad creds. Don't retry same turn."""


class ToolsRejected(ProviderError):
    """Provider rejects tools payload. Retry same turn without tools."""


class StreamDied(ProviderError):
    pass


HEARTBEAT = object()


def _sse_payload(line: str) -> str | None | object:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return "\0done"
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices") or []
    if not choices:
        return HEARTBEAT
    delta = choices[0].get("delta") or {}
    piece = delta.get("content")
    if isinstance(piece, str) and piece:
        return piece
    return HEARTBEAT


class OpenAICompatClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        connect_timeout_s: float = 8.0,
        read_timeout_s: float = 30.0,
    ):
        self._owned = http is None
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout_s, read=read_timeout_s, write=30.0, pool=30.0
            )
        )

    async def aclose(self) -> None:
        if self._owned:
            await self.http.aclose()

    async def stream_chat(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[str | ToolCallDelta]:
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        request = self.http.build_request("POST", url, json=payload, headers=headers)
        try:
            response = await self.http.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{url}: timeout ({exc})") from exc
        except httpx.HTTPError as exc:
            hint = (
                (
                    " — is Ollama running? install https://ollama.com/download "
                    "then `ollama pull qwen2.5:7b`"
                )
                if "localhost" in url or "127.0.0.1" in url
                else ""
            )
            raise StreamDied(f"{url}: connection failed ({exc}){hint}") from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            body = (await response.aread()).decode(errors="replace")
            await response.aclose()
            # Groq returns 429 with "Request too large" for oversize context —
            # that is NOT rate-limit, it needs shrink, not backoff.
            if any(
                k in body.lower()
                for k in ("too large", "reduce the length", "context length", "max tokens")
            ):
                raise ContextTooLong(body[:300])
            parsed = None
            if retry_after:
                try:
                    parsed = float(retry_after)
                except ValueError:
                    parsed = None
            if parsed is None:
                # Groq/others embed the wait in the message: "try again in 2.58s"
                import re as _re

                m = _re.search(r"try again in ([\d.]+)\s*s", body)
                if m:
                    try:
                        parsed = float(m.group(1))
                    except ValueError:
                        parsed = None
            raise RateLimited(f"{url}: 429 rate limited {body[:160]}", retry_after=parsed)
        if status in (401, 403):
            body = (await response.aread()).decode(errors="replace")
            await response.aclose()
            lowered = body.lower()
            # Retired/unknown model on Zen (e.g. laguna) comes back 401 ModelError —
            # permanent, not a credential problem.
            if any(
                k in lowered
                for k in (
                    "modelerror",
                    "model_not_found",
                    "does not exist",
                    "not found",
                    "unknown model",
                )
            ):
                raise PermanentError(f"{url}: dead model: {body[:200]}")
            raise AuthFailed(f"{url}: {status} auth rejected: {body[:200]}")
        if status == 400:
            body = (await response.aread()).decode(errors="replace")
            await response.aclose()
            lowered = body.lower()
            if any(
                k in lowered
                for k in (
                    "context length",
                    "too long",
                    "max tokens",
                    "reduce the length",
                    "too large",
                )
            ):
                raise ContextTooLong(body[:300])
            # Zen Responses-only models called via /chat/completions
            if any(
                k in lowered
                for k in ("missingsessionid", "missing session", "session_id", "responses")
            ):
                raise PermanentError(f"{url}: responses-only model on chat endpoint: {body[:200]}")
            if any(
                k in lowered
                for k in ("function call", "function_call", "tools", "tool_choice", "invalid tools")
            ):
                raise ToolsRejected(f"{url}: tools rejected: {body[:200]}")
            if any(
                k in lowered
                for k in ("model_not_found", "modelerror", "does not exist", "unknown model")
            ):
                raise PermanentError(f"{url}: dead model: {body[:200]}")
            raise ProviderError(f"{url}: 400 {body[:300]}")
        if status == 413:
            # Payload too large (e.g. Groq qwen 27b with big tool history) —
            # always a shrink-and-retry case, never a generic net failure.
            body = (await response.aread()).decode(errors="replace")
            await response.aclose()
            raise ContextTooLong(body[:300])
        if status == 402:
            await response.aclose()
            raise RateLimited(f"{url}: 402 quota/credits exhausted")
        if status >= 500:
            await response.aclose()
            raise ProviderError(f"{url}: {status} server error")
        if status != 200:
            body = (await response.aread()).decode(errors="replace")
            await response.aclose()
            raise ProviderError(f"{url}: unexpected {status}: {body[:200]}")

        async for piece in self._iter_sse(response):
            yield piece

    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[str | ToolCallDelta]:
        try:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                body = (await response.aread()).decode(errors="replace")
                raise ProviderError(f"non-stream response [{content_type}]: {body[:300]}")
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    yield HEARTBEAT
                    continue
                delta = choices[0].get("delta") or {}
                tc = delta.get("tool_calls")
                if tc:
                    for frag in tc:
                        fn = frag.get("function") or {}
                        yield ToolCallDelta(
                            index=int(frag.get("index", 0)),
                            id=frag.get("id"),
                            name=fn.get("name"),
                            args=fn.get("arguments") or "",
                        )
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    yield piece
                elif not tc:
                    yield HEARTBEAT
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise StreamDied(f"stream interrupted: {exc}") from exc
        finally:
            await response.aclose()
