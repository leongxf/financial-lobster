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
            raise RuntimeError("未配置 LLM_API_KEY")

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
                    _raise_for_status_with_body(response, url)
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
            raise RuntimeError(f"LLM 响应缺少 choices：{data}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise RuntimeError(f"LLM 响应缺少 content：{data}")

        return LLMResult(content=content, usage=_parse_usage(data.get("usage") or {}))

    async def embed(
        self,
        texts: list[str],
        model: str,
        max_retries: int = 2,
    ) -> list[list[float]]:
        """调用 OpenAI 兼容 /embeddings 接口，批量返回每条文本的向量。

        复用同一套 base_url 与 api_key；embedding 模型名由调用方传入（与 chat 模型独立）。
        返回顺序与入参 texts 一一对应。
        """
        if not self.config.api_key:
            raise RuntimeError("未配置 LLM_API_KEY")
        if not texts:
            return []

        url = self.config.base_url.rstrip("/") + "/embeddings"
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
                        json={"model": model, "input": texts},
                    )
                    _raise_for_status_with_body(response, url)
                    data = response.json()
                break
            except httpx.ReadTimeout as exc:
                last_error = exc
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "Embedding read timeout, retrying %s/%s",
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(2)
        else:
            assert last_error is not None
            raise last_error

        items = data.get("data") or []
        if len(items) != len(texts):
            raise RuntimeError(
                f"Embedding 响应数量不匹配：返回 {len(items)} 条，预期 {len(texts)} 条"
            )
        # 按 index 排序，确保与入参顺序对齐。
        items.sort(key=lambda it: int(it.get("index") or 0))
        return [list(it.get("embedding") or []) for it in items]


def _raise_for_status_with_body(response: httpx.Response, url: str) -> None:
    """raise_for_status 默认不带响应体；这里把服务端的错误正文记日志并带进异常消息。

    dashscope 等服务的 4xx 响应体通常包含真正原因（如输入超长、参数非法、内容拦截），
    缺了它根本无法定位。4xx 是客户端错误，重试无意义，交由上层不再重试地抛出。
    """
    if response.status_code < 400:
        return
    body = response.text
    logger.error(
        "LLM 调用返回 HTTP %s | url=%s | body=%s",
        response.status_code,
        url,
        body[:2000],
    )
    raise RuntimeError(
        f"LLM 调用失败（HTTP {response.status_code}）：{body[:500]}"
    )


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
