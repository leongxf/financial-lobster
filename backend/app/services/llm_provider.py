import asyncio
import logging
from dataclasses import dataclass, replace

import httpx

logger = logging.getLogger(__name__)

# 续写指令：上文因长度被截断时，要求模型从中断处接着输出且不重复已有内容，便于各轮直接拼接。
_CONTINUE_PROMPT = (
    "你上一条回复因长度限制被截断了。请从中断处继续输出剩余内容："
    "直接接着写，不要重复任何已经输出过的内容，不要重复标题或表头，不要加任何前言。"
)


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
    # 模型停止原因："stop"=正常结束；"length"=因 max_tokens 截断（需续写补全）。
    finish_reason: str = "stop"


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
        # 免费额度耗尽（如阿里百炼 AllocationQuota.FreeTierOnly，返回 403 易被误判为鉴权）。
        # 必须在 auth 分支之前命中，归到 billing 才能触发备用模型 fallback。
        "freetieronly",
        "allocationquota",
        "free tier",
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
            except httpx.TransportError as exc:
                # 涵盖连接超时(ConnectTimeout)/读超时(ReadTimeout)/连接错误等瞬时传输层故障，
                # 这些都值得重试；已分类的 LLMError 继承自 RuntimeError，不会落到这里。
                last_error = exc
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "LLM request transport error (%s), retrying %s/%s",
                    type(exc).__name__,
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

        finish_reason = str(choices[0].get("finish_reason") or "stop")
        return LLMResult(
            content=content,
            usage=_parse_usage(data.get("usage") or {}),
            finish_reason=finish_reason,
        )

    async def complete_until_done(
        self,
        messages: list[dict[str, str]],
        max_rounds: int = 20,
    ) -> LLMResult:
        """完整生成：因 max_tokens 截断（finish_reason=="length"）时自动续写并拼接。

        通用于任意内容形态（长表格/长叙述/长清单）与任意文件类型——只看 finish_reason，
        在中断处续写直到模型正常结束（"stop"）或达到 max_rounds 上限。
        续写时把已生成内容作为 assistant 轮回填，并要求"接着写、不重复"，故各轮直接拼接即可。
        注意：回填的已生成内容会逐轮增大输入；max_rounds 同时作为防跑飞与防输入超限的硬上限。
        """
        parts: list[str] = []
        usage = TokenUsage()
        finish_reason = "stop"
        for round_index in range(max(1, max_rounds)):
            convo = list(messages)
            accumulated = "".join(parts)
            if accumulated:
                convo.append({"role": "assistant", "content": accumulated})
                convo.append({"role": "user", "content": _CONTINUE_PROMPT})
            result = await self.complete(convo)
            parts.append(result.content)
            usage += result.usage
            finish_reason = result.finish_reason
            if finish_reason != "length":
                break
            logger.info(
                "complete_until_done: 第 %s 轮因长度截断，继续续写", round_index + 1
            )
        else:
            logger.warning(
                "complete_until_done 达到 max_rounds=%s 仍被截断，输出可能不完整", max_rounds
            )
        return LLMResult(
            content="".join(parts), usage=usage, finish_reason=finish_reason
        )

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
            except httpx.TransportError as exc:
                # 同 complete：连接超时等瞬时传输层故障也纳入重试。
                last_error = exc
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "Embedding request transport error (%s), retrying %s/%s",
                    type(exc).__name__,
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


class FallbackProvider(LLMProvider):
    """同账号、按额度耗尽顺序切换模型的 chat provider。

    主模型额度耗尽（LLMError.category == "billing"）时，永久切到下一个备用模型并重试；
    其余错误（鉴权/限流/模型不存在/服务端等）不切换，直接上抛，避免把配置错误当额度问题。
    所有候选共用同一账号（base_url/api_key），只是 model 不同，因此 embed 等沿用 base 配置即可。

    备用模型列表为空时不会构造本类（见 build_chat_provider），退化为单模型：额度耗尽即报错。
    """

    def __init__(self, base_config: LLMConfig, fallback_models: list[str]) -> None:
        seen = {base_config.model}
        configs = [base_config]
        for model in fallback_models:
            if model and model not in seen:
                seen.add(model)
                configs.append(replace(base_config, model=model))
        self._providers = [LLMProvider(config) for config in configs]
        # 游标只前进不回退：已知耗尽的模型不再重撞（map 阶段并发，故加锁保护推进）。
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def config(self) -> LLMConfig:  # type: ignore[override]
        return self._providers[self._cursor].config

    async def complete(
        self,
        messages: list[dict[str, str]],
        max_retries: int = 2,
    ) -> LLMResult:
        last_error: Exception | None = None
        for index in range(self._cursor, len(self._providers)):
            try:
                return await self._providers[index].complete(messages, max_retries=max_retries)
            except LLMError as exc:
                last_error = exc
                has_next = index + 1 < len(self._providers)
                if exc.category == "billing" and has_next:
                    async with self._lock:
                        self._cursor = max(self._cursor, index + 1)
                    logger.warning(
                        "模型 %s 额度耗尽，切换到备用模型 %s",
                        self._providers[index].config.model,
                        self._providers[index + 1].config.model,
                    )
                    continue
                raise
        assert last_error is not None
        raise last_error


def build_chat_provider(
    base_config: LLMConfig,
    fallback_models: list[str],
) -> LLMProvider:
    """有备用模型则返回带额度切换的 FallbackProvider，否则返回单模型 LLMProvider。"""
    if fallback_models:
        return FallbackProvider(base_config, fallback_models)
    return LLMProvider(base_config)


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
