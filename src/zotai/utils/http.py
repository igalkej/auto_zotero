"""HTTP client factory + shared retry policy.

Everything that talks HTTP in this project should go through `make_async_client`
and wrap mutating calls in `@with_retry`. This keeps timeouts, retries, and
User-Agent consistent across S1 (OpenAlex, Semantic Scholar, OpenAI) and S2
(RSS fetch, PDF cascade).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

T = TypeVar("T")

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_USER_AGENT = "zotai/0.1.0"

_RETRYABLE: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
    httpx.RemoteProtocolError,
)


def make_user_agent(base: str = _DEFAULT_USER_AGENT, mailto: str | None = None) -> str:
    """Build a User-Agent string. OpenAlex lifts rate limit 10x when `mailto` is set."""
    if mailto:
        return f"{base} (mailto:{mailto})"
    return base


def make_async_client(
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    user_agent: str | None = None,
    base_url: str = "",
) -> httpx.AsyncClient:
    """Return a configured `httpx.AsyncClient`.

    Callers own the lifecycle — use it as an async context manager or call
    `aclose()`. Connection pooling is built-in; reuse the same client for
    multiple requests when possible.
    """
    headers = {"User-Agent": user_agent or _DEFAULT_USER_AGENT}
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers=headers,
        base_url=base_url,
        follow_redirects=True,
    )


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 30.0,
) -> T:
    """Run `fn` with exponential backoff over the standard retryable exceptions.

    On final failure, re-raises the last exception (not `RetryError`) so
    callers can catch the underlying HTTP exception directly.
    """
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        reraise=True,
    ):
        with attempt:
            return await fn()
    # Defensive: AsyncRetrying with reraise=True always raises or returns above.
    raise RetryError(last_attempt=None)  # pragma: no cover


__all__ = ["make_async_client", "make_user_agent", "with_retry"]
