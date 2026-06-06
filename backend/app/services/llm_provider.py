import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_ms: int
    max_tokens: int
    temperature: float


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class LLMResult:
    content: str
    usage: TokenUsage


class LLMProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    async def complete(
        self,
        messages: list[dict[str, str]],
        max_retries: int = 2,
    ) -> LLMResult:
        if not self.config.api_key:
            raise RuntimeError("LLM_API_KEY is required")

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        read_timeout = self.config.timeout_ms / 1000
        timeout = httpx.Timeout(connect=15.0, read=read_timeout, write=15.0, pool=15.0)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {self.config.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.config.model,
                            "messages": messages,
                            "temperature": self.config.temperature,
                            "max_tokens": self.config.max_tokens,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                break
            except httpx.ReadTimeout as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "LLM read timeout, retrying %s/%s",
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(2)
        else:
            assert last_error is not None
            raise last_error

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM response has no choices: {data}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise RuntimeError(f"LLM response has no content: {data}")

        return LLMResult(content=content, usage=_parse_usage(data.get("usage") or {}))


def _parse_usage(raw_usage: dict) -> TokenUsage:
    input_tokens = _as_int(
        raw_usage.get("prompt_tokens")
        or raw_usage.get("input_tokens")
        or raw_usage.get("input_token")
    )
    output_tokens = _as_int(
        raw_usage.get("completion_tokens")
        or raw_usage.get("output_tokens")
        or raw_usage.get("output_token")
    )
    total_tokens = _as_int(raw_usage.get("total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
