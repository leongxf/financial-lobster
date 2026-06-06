from dataclasses import dataclass
import asyncio
import logging

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


class LLMProvider:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    async def complete(self, messages: list[dict[str, str]], max_retries: int = 2) -> str:
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

        return content
