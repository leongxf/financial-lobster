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


class LLMError(RuntimeError):
    """LLM 平台返回的错误，附带分类信息，便于上层给用户可执行的友好提示。

    category 取值：billing/auth/rate_limit/model/bad_request/server/unknown。
    str(LLMError) 即为面向用户的中文提示；body 为原始响应体，仅用于日志排查。
    """

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.body = body


def _classify_llm_error(status_code: int, body: str) -> tuple[str, str]:
    """按“跨平台通用信号”给 HTTP 错误分类，不依赖具体平台的 JSON 结构。

    各家（阿里百炼/OpenAI/各类兼容网关）返回格式不同，但欠费、鉴权、限流等错误
    在文本里几乎都带相同的关键词。命中已知类别给可执行提示；未命中回退通用提示，
    并保留截断后的原始信息供排查。返回 (category, 面向用户的中文提示)。
    """
    text = body.lower()

    def has(*words: str) -> bool:
        return any(w in text for w in words)

    # 欠费 / 余额不足 / 额度耗尽（阿里百炼 Arrearage、OpenAI insufficient_quota 等）
    if has(
        "arrearage",
        "overdue",
        "欠费",
        "余额不足",
        "余额不够",
        "insufficient_quota",
        "insufficient balance",
        "insufficient_user_quota",
        "account is in good standing",
        "billing",
    ):
        return (
            "billing",
            "处理失败：模型服务调用被拒绝，账户疑似欠费或额度不足。"
            "请检查所配置 LLM 平台的账户余额/账单后重试。",
        )

    # 鉴权失败：Key 无效 / 无权限
    if status_code in (401, 403) or has(
        "invalid api key",
        "incorrect api key",
        "invalid_api_key",
        "unauthorized",
        "authentication",
        "permission denied",
        "无效的apikey",
        "鉴权失败",
    ):
        return (
            "auth",
            "处理失败：模型服务鉴权失败，API Key 可能无效或无访问权限。"
            "请检查 LLM_API_KEY 与 LLM_BASE_URL 配置。",
        )

    # 限流 / 超出速率配额
    if status_code == 429 or has(
        "rate limit",
        "too many requests",
        "请求过于频繁",
        "限流",
        "throttl",
    ):
        return (
            "rate_limit",
            "处理失败：模型服务限流，请求过于频繁或超出速率配额。请稍后重试。",
        )

    # 模型不存在 / 名称错误 / 无权限调用该模型
    if has(
        "model not found",
        "model_not_found",
        "does not exist",
        "unknown model",
        "无效的model",
    ):
        return (
            "model",
            "处理失败：所配置的模型不可用，模型名可能无效或无调用权限。"
            "请检查 LLM_MODEL 配置。",
        )

    if status_code >= 500:
        return (
            "server",
            "处理失败：模型服务暂时不可用（服务端错误），请稍后重试。",
        )

    return (
        "unknown",
        f"处理失败：模型服务调用失败（HTTP {status_code}）。原始信息：{body[:200]}",
    )


def _raise_for_status_with_body(response: httpx.Response) -> None:
    """非 2xx 时读取响应体并抛出已分类的 LLMError。

    httpx 默认的 raise_for_status() 会丢弃响应体，导致欠费/鉴权等关键信息丢失；
    这里先保留 body 再分类，确保上层能给出友好提示、日志能留底排查。
    """
    if response.is_success:
        return
    try:
        body = response.text
    except Exception:
        body = ""
    category, friendly = _classify_llm_error(response.status_code, body)
    raise LLMError(
        friendly,
        category=category,
        status_code=response.status_code,
        body=body,
    )


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
                    _raise_for_status_with_body(response)
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
                    _raise_for_status_with_body(response)
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
