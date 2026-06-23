from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from pathlib import Path

import httpx
import lark_oapi as lark
from lark_oapi.api.application.v6 import P2ApplicationBotMenuV6
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.events import extract_file_message, extract_message_brief
from app.services.analysis_cache import AnalysisCache, sha256_file
from app.services.cards import build_done_card
from app.services.conversation_store import ConversationStore
from app.services.document_parser import parse_document
from app.services.event_dedup import EventDeduplicator
from app.services.financial_summary import generate_financial_summary_markdown
from app.services.llm_provider import (
    LLMConfig,
    LLMError,
    LLMProvider,
    TokenUsage,
    build_chat_provider,
)
from app.services.markdown_report import build_parse_preview_report
from app.services.qa_service import (
    answer_question,
    build_chunk_embeddings,
    embedding_cache_file,
    extract_keywords,
    load_cached_embedding_model,
    load_cached_embeddings,
    load_pages,
    retrieve_by_embedding,
    retrieve_pages,
    save_cached_embeddings,
    score_file_by_keywords,
)
from app.services.session_store import SessionStore
from app.services.task_store import TaskStore
from app.skills.base import IncomingMessage, SkillContext, SkillRouter
from app.skills.compliance import COMPLIANCE_PROMPT
from app.skills.registry import build_registry
from app.tools.base import ToolRegistry
from app.tools.web_search import WebSearchTool

logger = logging.getLogger(__name__)
REPORT_TEXT_MAX_CHARS = 3500


def build_embedding_provider(settings: Settings) -> LLMProvider:
    """构造 embedding 专用 provider：base_url/api_key 取 embedding 配置（未配置则回退 chat）。

    embedding 可独立于 chat 走另一平台，故不能复用 chat provider。模型名由各调用方
    按 embedding_model_chain / 缓存记录的模型显式传入，这里的 model 仅作占位默认值。
    """
    return LLMProvider(
        LLMConfig(
            provider=settings.llm_provider,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.qa_embedding_model,
            timeout_ms=settings.llm_timeout_ms,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        )
    )


def format_model_info(settings: Settings) -> str:
    if settings.llm_api_key:
        return f"{settings.llm_model}（provider: {settings.llm_provider}）"
    return "未配置 LLM（仅文本提取预览）"


async def suggest_followup_question(
    provider: LLMProvider,
    *,
    report: str,
    keywords: list[str],
) -> str | None:
    """基于已生成的财务摘要，让模型给出一个用户最可能追问的问题。

    用 report（已是对文件内容的概括）作为输入既准确又省 token；
    失败或返回为空时返回 None，由调用方回退到通用引导文案，不阻断主流程。
    """
    context = report.strip()[:2000]
    keyword_hint = "、".join(keywords[:10]) if keywords else "无"
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个财务文件助手。根据给定的财务摘要，"
                "推测用户最可能对该文件追问的一个问题。"
                "要求：只输出这一个问题本身，不要解释、不要引号、不要编号，"
                "问题需具体、与文件内容强相关，控制在 30 字以内。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文件关键词：{keyword_hint}\n\n"
                f"财务摘要：\n{context}\n\n"
                "请给出一个用户最可能提出的问题："
            ),
        },
    ]
    try:
        result = await provider.complete(messages)
    except Exception:
        logger.exception("failed to generate followup question suggestion")
        return None
    question = result.content.strip().splitlines()[0].strip().strip("「」\"'") if result.content.strip() else ""
    return question or None


def build_ack_message(settings: Settings, file_name: str | None) -> str:
    display_name = file_name or "未命名文件"
    return "\n".join(
        [
            "已收到文件，开始处理。",
            "",
            f"- 模型：{format_model_info(settings)}",
            f"- 文件：{display_name}",
            "",
            "当前步骤：下载文件中...",
        ]
    )


def _format_size(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def check_upload_allowed(
    settings: Settings,
    file_name: str | None,
    file_size: int | None,
) -> str | None:
    """下载前的上传门禁：校验文件类型白名单与大小上限。

    通过返回 None；被拒则返回给用户的中文提示文案。file_size 在飞书事件中可能缺失，
    缺失时跳过大小校验，由下载后基于真实文件大小再兜底一次。
    """
    suffix = Path(file_name or "").suffix.lower()
    allowed = settings.allowed_extensions
    if suffix not in allowed:
        allowed_text = "、".join(sorted(allowed)) if allowed else "无"
        return "\n".join(
            [
                "无法处理该文件：文件类型不在支持范围内。",
                "",
                f"- 文件：{file_name or '未命名文件'}",
                f"- 类型：{suffix or '未知'}",
                f"- 当前支持：{allowed_text}",
            ]
        )
    if file_size is not None and file_size > settings.max_file_size_bytes:
        return "\n".join(
            [
                "无法处理该文件：超过大小上限。",
                "",
                f"- 文件：{file_name or '未命名文件'}",
                f"- 大小：约 {_format_size(file_size)}",
                f"- 上限：{settings.max_file_size_mb}MB",
            ]
        )
    return None


def format_token_usage(
    usage: TokenUsage,
    *,
    cache_hits: int = 0,
    cache_misses: int = 0,
) -> str:
    return "\n".join(
        [
            "## Token 使用量",
            f"- Input tokens：{usage.input_tokens:,}",
            f"- Output tokens：{usage.output_tokens:,}",
            f"- Total tokens：{usage.total_tokens:,}",
            f"- 分片缓存：命中 {cache_hits}，未命中 {cache_misses}",
        ]
    )


def build_report_file_path(storage_dir: Path, source_file_name: str, report: str) -> Path:
    stem = Path(source_file_name).stem or "report"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "report"
    report_path = storage_dir / f"{safe_stem}_financial_summary.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


async def send_report_result(
    client: FeishuClient,
    message_id: str,
    storage_dir: Path,
    source_file_name: str,
    report: str,
    usage: TokenUsage,
    cache_hits: int = 0,
    cache_misses: int = 0,
) -> Path:
    usage_text = format_token_usage(
        usage,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )
    report_with_usage = report + "\n\n---\n\n" + usage_text
    report_path = build_report_file_path(storage_dir, source_file_name, report_with_usage)
    if len(report_with_usage) <= REPORT_TEXT_MAX_CHARS:
        await client.reply_text(
            message_id,
            "分析完成，报告如下：\n\n" + report_with_usage,
        )
        return report_path

    await client.reply_text(
        message_id,
        "\n".join(
            [
                "分析完成，报告较长，已生成 Markdown 附件。",
                "",
                f"- 文件：{report_path.name}",
                "",
                usage_text,
            ]
        ),
    )
    await client.reply_file(message_id, report_path, file_name=report_path.name)
    return report_path


async def process_file_message_async(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
    sender_id: str | None = None,
    file_size: int | None = None,
) -> None:
    maybe_purge_user_memory(settings, sender_id)
    task_id = message_id
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    task_store = TaskStore(settings.task_storage_dir)
    analysis_cache = AnalysisCache(settings.analysis_cache_dir)
    conversation_store = build_conversation_store(settings)
    safe_name = file_name or "uploaded-file"
    storage_dir = Path(settings.local_storage_dir) / message_id
    target_path = storage_dir / safe_name

    async def notify(text: str) -> None:
        # 进度通知是尽力而为：飞书瞬时网络抖动导致的发送失败不应中断已在进行的分析。
        try:
            await client.reply_text(message_id, text)
        except Exception:
            logger.warning(
                "failed to send progress notification for %s", message_id, exc_info=True
            )

    try:
        task_store.create_task(
            task_id,
            message_id=message_id,
            file_key=file_key,
            file_name=file_name,
            model=settings.llm_model,
            provider=settings.llm_provider,
        )

        # 上传门禁：下载前先按类型白名单与已知大小拦截，避免浪费带宽/磁盘。
        reject_reason = check_upload_allowed(settings, file_name, file_size)
        if reject_reason is not None:
            task_store.update_task(
                task_id,
                status="rejected",
                event="upload rejected",
                error=reject_reason.splitlines()[0],
            )
            await notify(reject_reason)
            return

        await notify(build_ack_message(settings, file_name))

        task_store.update_task(task_id, status="downloading", event="downloading file")
        downloaded_path = await client.download_message_file(
            message_id,
            file_key,
            target_path,
        )

        # 下载后基于真实文件大小再兜底一次（飞书事件可能不带 file_size）。
        actual_size = downloaded_path.stat().st_size
        if actual_size > settings.max_file_size_bytes:
            downloaded_path.unlink(missing_ok=True)
            reason = "\n".join(
                [
                    "无法处理该文件：超过大小上限。",
                    "",
                    f"- 文件：{downloaded_path.name}",
                    f"- 大小：约 {_format_size(actual_size)}",
                    f"- 上限：{settings.max_file_size_mb}MB",
                ]
            )
            task_store.update_task(
                task_id,
                status="rejected",
                event="upload rejected: oversize",
                error=f"file too large: {actual_size} bytes",
            )
            await notify(reason)
            return
        logger.info(
            "downloaded feishu file",
            extra={
                "message_id": message_id,
                "file_key": file_key,
                "local_path": str(downloaded_path),
            },
        )
        file_hash = sha256_file(downloaded_path)
        task_store.update_task(
            task_id,
            status="downloaded",
            event="file downloaded",
            local_file_path=str(downloaded_path),
            file_hash=file_hash,
        )
        await notify(f"文件下载完成：{downloaded_path.name}")

        task_store.update_task(task_id, status="parsing", event="extracting text")
        document = parse_document(downloaded_path)
        char_count = len(document.text)
        page_info = document.page_count if document.page_count is not None else "未知"
        extracted_text_path = storage_dir / "extracted_text.txt"
        extracted_text_path.write_text(document.text, encoding="utf-8")
        # 保存分页文本，供后续追问按页检索。
        pages_path = storage_dir / "pages.json"
        pages_path.write_text(
            json.dumps(
                [{"page_number": p.page_number, "text": p.text} for p in document.pages],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        task_store.update_task(
            task_id,
            status="parsed",
            event="text extracted",
            file_type=document.file_type,
            page_count=document.page_count,
            extracted_text_chars=char_count,
            extracted_text_path=str(extracted_text_path),
            pages_path=str(pages_path),
        )
        await notify(
            f"文本提取完成：{page_info} 页，约 {char_count:,} 字符。"
            f"文件类型：{document.file_type}。"
        )
        if not document.text.strip():
            task_store.update_task(
                task_id,
                status="failed",
                event="no extractable text",
                error=f"{document.file_type} has no extractable text",
            )
            if document.file_type == "pdf":
                reason = "该文件很可能是扫描件或图片型 PDF，当前 MVP 暂未接入 OCR。"
                suggestion = "请上传带文本层的 PDF，或先将扫描件转换为可搜索 PDF 后重试。"
            else:
                reason = "该文件可能为空，或不包含可提取的文本/表格内容。"
                suggestion = "请确认文件内容非空后重试。"
            await notify(
                "\n".join(
                    [
                        "处理停止：未能从文件中提取到可用文本。",
                        "",
                        f"- 文件：{downloaded_path.name}",
                        f"- 文件类型：{document.file_type}",
                        f"- 页数：{page_info}",
                        f"- 原因：{reason}",
                        "",
                        f"建议：{suggestion}",
                    ]
                )
            )
            return

        cache_hits = 0
        cache_misses = 0
        if settings.llm_api_key:
            provider = build_chat_provider(
                LLMConfig(
                    provider=settings.llm_provider,
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    timeout_ms=settings.llm_timeout_ms,
                    max_tokens=settings.llm_max_tokens,
                    temperature=settings.llm_temperature,
                ),
                settings.fallback_models,
            )
            task_store.update_task(
                task_id,
                status="analyzing",
                event="calling LLM",
                prompt_version=settings.prompt_version,
                llm_chunk_chars=settings.llm_chunk_chars,
                llm_max_pages=settings.llm_max_pages,
            )
            await notify(
                f"开始调用模型 {settings.llm_model} 分析"
                f"（最多分析 {settings.llm_max_pages} 页）..."
            )
            summary_result = await generate_financial_summary_markdown(
                document=document,
                provider=provider,
                chunk_chars=settings.llm_chunk_chars,
                max_pages=settings.llm_max_pages,
                max_chunks=settings.llm_max_chunks,
                prompt_version=settings.prompt_version,
                file_hash=file_hash,
                reduce_group_size=settings.llm_reduce_group_size,
                reduce_max_chars=settings.llm_reduce_max_chars,
                map_concurrency=settings.llm_map_concurrency,
                cache=analysis_cache,
                on_progress=notify,
            )
            report = summary_result.markdown
            usage = summary_result.usage
            cache_hits = summary_result.cache_hits
            cache_misses = summary_result.cache_misses
            if summary_result.truncated:
                await notify(
                    f"提示：文件共 {summary_result.total_pages} 页，"
                    f"本次报告仅分析了前 {summary_result.analyzed_pages} 页"
                    f"（受最大分析页数 {settings.llm_max_pages} 限制）。"
                    "后续追问检索仍覆盖全文。"
                )
        else:
            await notify("未配置 LLM_API_KEY，返回解析预览。")
            report = build_parse_preview_report(document)
            usage = TokenUsage()

        task_store.update_task(
            task_id,
            status="reporting",
            event="sending report",
            token_usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
            cache={"hits": cache_hits, "misses": cache_misses},
        )
        report_path = await send_report_result(
            client=client,
            message_id=message_id,
            storage_dir=storage_dir,
            source_file_name=safe_name,
            report=report,
            usage=usage,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
        )
        task_store.update_task(
            task_id,
            status="succeeded",
            event="task completed",
            report_path=str(report_path),
        )

        # 写入文件画像到会话存储，支持后续多轮追问（按 open_id 维护最近 N 个文件）。
        if sender_id:
            keywords = extract_keywords(document.text)
            summary_line = next(
                (line.strip() for line in report.splitlines() if line.strip()),
                safe_name,
            )

            # 先登记文件（embeddings_path 暂空），确保报告发出后用户能立即追问：
            # 向量未就绪时 process_question_async 会自动回退关键词检索。否则文件要等下方
            # 整篇 embedding 算完才登记，期间追问会误报"还没有最近上传的文件"。
            entry_key = file_hash or task_id
            conversation_store.upsert_file(
                sender_id,
                file_id=task_id,
                file_name=file_name,
                pages_path=str(pages_path),
                summary=summary_line[:200],
                keywords=keywords,
                file_hash=file_hash,
                embeddings_path="",
            )

            # 预计算向量（embedding）供追问做语义检索；中英混排材料靠它解决跨语言检索。
            # 按 file_hash 缓存：同内容文件重传直接复用，不重算。算完后补写 embeddings_path
            # 升级为向量检索；失败不阻断主流程（仍可关键词检索）。
            if settings.embedding_api_key:
                cache_dir = settings.qa_embedding_cache_dir
                try:
                    pages_data = [
                        {"page_number": p.page_number, "text": p.text}
                        for p in document.pages
                    ]
                    cached = load_cached_embeddings(cache_dir, file_hash)
                    if cached is None:
                        embed_provider = build_embedding_provider(settings)
                        chunks, embedding_model_used = await build_chunk_embeddings(
                            pages_data,
                            provider=embed_provider,
                            models=settings.embedding_model_chain,
                            chunk_chars=settings.qa_embedding_chunk_chars,
                            overlap=settings.qa_embedding_chunk_overlap,
                            batch_size=settings.qa_embedding_batch_size,
                        )
                        save_cached_embeddings(
                            cache_dir, file_hash, chunks, embedding_model_used
                        )
                        logger.info(
                            "[QA] embeddings built | file_hash=%s | chunks=%d",
                            file_hash,
                            len(chunks),
                        )
                    else:
                        logger.info(
                            "[QA] embeddings cache hit | file_hash=%s | chunks=%d",
                            file_hash,
                            len(cached),
                        )
                    conversation_store.update_embeddings_path(
                        sender_id, entry_key, str(embedding_cache_file(cache_dir, file_hash))
                    )
                except Exception:
                    logger.exception(
                        "[QA] failed to build embeddings for %s, fallback to keyword retrieval",
                        file_hash,
                    )

            example_question = "营业收入是多少"
            if settings.llm_api_key:
                suggested = await suggest_followup_question(
                    provider,
                    report=report,
                    keywords=keywords,
                )
                if suggested:
                    example_question = suggested
            await notify(
                f"你现在可以直接发文字向我追问这个文件的内容，例如「{example_question}」。"
            )
    except httpx.ReadTimeout:
        logger.exception("LLM timeout while processing file message %s", message_id)
        task_store.update_task(
            task_id,
            status="failed",
            event="LLM timeout",
            error="LLM read timeout",
        )
        await notify(
            "处理失败：模型调用超时。"
            f"当前超时设置 {settings.llm_timeout_ms // 1000} 秒，"
            "大文件可在 .env 调大 LLM_TIMEOUT_MS 或减少 LLM_MAX_CHUNKS 后重试。"
        )
    except LLMError as exc:
        # 平台类错误（欠费/鉴权/限流等）：给用户可执行的友好提示，原始 body 仅入日志。
        logger.error(
            "LLM error while processing file message %s | category=%s | status=%s | body=%s",
            message_id,
            exc.category,
            exc.status_code,
            exc.body[:500],
        )
        task_store.update_task(
            task_id,
            status="failed",
            event=f"LLM error: {exc.category}",
            error=str(exc),
        )
        await notify(str(exc))
    except Exception as exc:
        logger.exception("failed to process file message %s", message_id)
        task_store.update_task(
            task_id,
            status="failed",
            event="task failed",
            error=str(exc),
        )
        await notify(f"处理失败：{exc}")


def process_file_message(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
    sender_id: str | None = None,
    file_size: int | None = None,
) -> None:
    asyncio.run(
        process_file_message_async(
            settings=settings,
            message_id=message_id,
            file_key=file_key,
            file_name=file_name,
            sender_id=sender_id,
            file_size=file_size,
        )
    )


def start_file_processing(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
    sender_id: str | None = None,
    file_size: int | None = None,
) -> None:
    thread = threading.Thread(
        target=process_file_message,
        args=(settings, message_id, file_key, file_name, sender_id, file_size),
        daemon=True,
    )
    thread.start()


def build_admin_notification(sender_id: str | None, message_type: str | None, summary: str) -> str:
    sender_text = sender_id or "未知用户"
    type_text = message_type or "未知类型"
    return "\n".join(
        [
            "机器人收到一条新消息：",
            "",
            f"- 发送者 open_id：{sender_text}",
            f"- 消息类型：{type_text}",
            f"- 内容：{summary}",
        ]
    )


async def push_admin_notification_async(settings: Settings, text: str) -> None:
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    await client.send_text(
        receive_id=settings.feishu_admin_receive_id,
        text=text,
        receive_id_type=settings.feishu_admin_receive_id_type,
    )


def push_admin_notification(settings: Settings, text: str) -> None:
    try:
        asyncio.run(push_admin_notification_async(settings, text))
    except Exception:
        # 额外推送失败不能影响主流程，仅记录日志。
        logger.exception("failed to push admin notification")


def notify_admin_on_message(payload: dict, settings: Settings) -> None:
    """额外推送旁路：独立于原有文件处理逻辑。

    - 始终把发送者 open_id 打印到日志，便于拿自己的 ID。
    - 已配置管理员接收人时，主动给管理员推一条提醒。
    """
    brief = extract_message_brief(payload)
    if brief is None:
        return

    logger.info(
        "feishu message sender open_id",
        extra={
            "sender_open_id": brief.sender_id,
            "message_type": brief.message_type,
            "chat_id": brief.chat_id,
        },
    )

    if not settings.feishu_admin_receive_id:
        return

    text = build_admin_notification(brief.sender_id, brief.message_type, brief.summary)
    thread = threading.Thread(
        target=push_admin_notification,
        args=(settings, text),
        daemon=True,
    )
    thread.start()


async def process_question_async(
    settings: Settings,
    message_id: str,
    sender_id: str,
    question: str,
) -> None:
    """处理用户对已上传文件的文字追问。"""
    maybe_purge_user_memory(settings, sender_id)
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    conversation_store = build_conversation_store(settings)

    async def notify(text: str) -> None:
        await client.reply_text(message_id, text)

    if not settings.llm_api_key:
        await notify("未配置 LLM_API_KEY，暂时无法回答追问。")
        return

    files = conversation_store.list_files(sender_id)
    if not files:
        await notify(
            "我还没有你最近上传的文件，请先发送一个文件"
            "（支持 PDF / Word / Excel / CSV）给我分析后再追问。"
        )
        return

    # 第一级：按关键字重合度在最近文件中选最相关的；都不命中则用当前（最近活跃）文件。
    best = max(files, key=lambda f: score_file_by_keywords(question, f.get("keywords", [])))
    if score_file_by_keywords(question, best.get("keywords", [])) == 0:
        best = files[0]  # list_files 已按最近活跃排序

    # history 读写键统一用 entry_key（= 会话存储里的字典键；file_hash 去重后它不等于 file_id）。
    file_id = best.get("entry_key") or best["file_id"]
    pages = load_pages(best.get("pages_path", ""))
    if not pages:
        await notify("该文件的解析内容已不可用，请重新上传后再追问。")
        return

    try:
        provider = build_chat_provider(
            LLMConfig(
                provider=settings.llm_provider,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_ms=settings.llm_timeout_ms,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            ),
            settings.fallback_models,
        )
        history = conversation_store.recent_history(
            sender_id, file_id, max_turns=settings.qa_history_max_turns
        )

        # 检索：优先向量语义检索（解决中英混排跨语言失配），失败/无缓存回退关键词检索。
        context = None
        retrieval_mode = "keyword"
        cached_chunks = load_cached_embeddings(
            settings.qa_embedding_cache_dir, best.get("file_hash") or ""
        )
        if cached_chunks:
            # 查询必须用该文件入库时的 embedding 模型，否则向量空间错配；
            # 若该模型此刻额度耗尽，retrieve 会抛错并回退到下方关键词检索。
            cached_embedding_model = (
                load_cached_embedding_model(
                    settings.qa_embedding_cache_dir, best.get("file_hash") or ""
                )
                or settings.qa_embedding_model
            )
            try:
                # embedding 走独立 provider（可能是另一平台），不能复用 chat provider。
                context = await retrieve_by_embedding(
                    question=question,
                    chunks=cached_chunks,
                    provider=build_embedding_provider(settings),
                    model=cached_embedding_model,
                    top_k=settings.qa_retrieve_top_k,
                    max_chars=settings.qa_context_max_chars,
                )
                retrieval_mode = "embedding"
            except Exception:
                logger.exception(
                    "[QA] embedding retrieval failed, fallback to keyword | %s", message_id
                )
                context = None
        if context is None:
            context = retrieve_pages(
                question,
                pages,
                top_k=settings.qa_retrieve_top_k,
                max_chars=settings.qa_context_max_chars,
            )

        result = await answer_question(
            question=question,
            context=context,
            history=history,
            provider=provider,
            retrieval_mode=retrieval_mode,
        )
        conversation_store.append_history(
            sender_id,
            file_id,
            question=question,
            answer=result.answer,
            max_turns=settings.qa_history_max_turns,
        )
        source = f"（基于《{best.get('file_name') or '当前文件'}》"
        if result.page_numbers:
            source += "，参考第 " + "、".join(str(p) for p in result.page_numbers) + " 页"
        source += "）"
        await notify(result.answer + "\n\n" + source)
    except httpx.ReadTimeout:
        logger.exception("LLM timeout while answering question %s", message_id)
        await notify("回答超时，请稍后重试，或缩短问题后再试。")
    except LLMError as exc:
        logger.error(
            "LLM error while answering question %s | category=%s | status=%s | body=%s",
            message_id,
            exc.category,
            exc.status_code,
            exc.body[:500],
        )
        await notify(str(exc))
    except Exception as exc:
        logger.exception("failed to answer question %s", message_id)
        await notify(f"回答失败：{exc}")


def process_question(
    settings: Settings,
    message_id: str,
    sender_id: str,
    question: str,
) -> None:
    asyncio.run(process_question_async(settings, message_id, sender_id, question))


def start_question_processing(
    settings: Settings,
    message_id: str,
    sender_id: str,
    question: str,
) -> None:
    thread = threading.Thread(
        target=process_question,
        args=(settings, message_id, sender_id, question),
        daemon=True,
    )
    thread.start()


def extract_message_id(payload: dict) -> str | None:
    """从事件 payload 中提取 message_id，用于事件级去重。"""
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("message_id")
    return message_id if isinstance(message_id, str) and message_id else None


def _strip_mentions(text: str, message: dict) -> str:
    mentions = message.get("mentions")
    if not isinstance(mentions, list):
        return text.strip()
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = mention.get("key")
        if isinstance(key, str) and key:
            text = text.replace(key, "")
    return text.strip()


def normalize_incoming(payload: dict) -> IncomingMessage | None:
    """将飞书消息事件归一化为 IncomingMessage。"""
    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    file_message = extract_file_message(payload)
    if file_message is not None:
        if not file_message.message_id or not file_message.file_key:
            return None
        return IncomingMessage(
            message_id=file_message.message_id,
            sender_id=file_message.sender_id,
            chat_id=file_message.chat_id,
            msg_type="file",
            file_key=file_message.file_key,
            file_name=file_message.file_name,
            file_size=file_message.file_size,
            raw_payload=payload,
        )

    message = event.get("message")
    if not isinstance(message, dict) or message.get("message_type") != "text":
        return None

    message_id = message.get("message_id")
    if not message_id:
        return None

    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    text = _strip_mentions(str((content or {}).get("text") or ""), message)
    if not text:
        return None

    sender = event.get("sender")
    sender_id = None
    if isinstance(sender, dict) and isinstance(sender.get("sender_id"), dict):
        sender_id = sender["sender_id"].get("open_id")
    if not sender_id:
        return None

    return IncomingMessage(
        message_id=message_id,
        sender_id=sender_id,
        chat_id=message.get("chat_id"),
        msg_type="text",
        text=text,
        raw_payload=payload,
    )


def build_conversation_store(settings: Settings) -> ConversationStore:
    return ConversationStore(
        settings.conversation_storage_dir,
        recent_files_max=settings.qa_recent_files_max,
    )


def run_memory_purge(settings: Settings, *, reason: str) -> None:
    if not settings.user_memory_cleanup_enabled:
        return
    stats = build_conversation_store(settings).purge_all_expired(
        ttl_days=settings.user_memory_ttl_days,
    )
    logger.info(
        "user memory purge (%s)",
        reason,
        extra={
            "users_scanned": stats.users_scanned,
            "users_deleted": stats.users_deleted,
            "files_removed": stats.files_removed,
        },
    )


def maybe_purge_user_memory(settings: Settings, open_id: str | None) -> None:
    if not open_id or not settings.user_memory_cleanup_enabled:
        return
    stats = build_conversation_store(settings).purge_expired(
        open_id,
        ttl_days=settings.user_memory_ttl_days,
    )
    if stats.files_removed or stats.users_deleted:
        logger.info(
            "lazy user memory purge",
            extra={
                "open_id": open_id,
                "users_deleted": stats.users_deleted,
                "files_removed": stats.files_removed,
            },
        )


def _memory_retention_loop(settings: Settings) -> None:
    interval_seconds = max(1, settings.user_memory_cleanup_interval_hours) * 3600
    while True:
        time.sleep(interval_seconds)
        run_memory_purge(settings, reason="periodic")


def start_memory_retention_thread(settings: Settings) -> None:
    if not settings.user_memory_cleanup_enabled:
        return
    thread = threading.Thread(
        target=_memory_retention_loop,
        args=(settings,),
        daemon=True,
        name="memory-retention",
    )
    thread.start()


def build_tool_registry(settings: Settings) -> ToolRegistry:
    tools = ToolRegistry()
    if settings.search_key:
        tools.register(
            WebSearchTool(
                base_url=settings.search_endpoint,
                api_key=settings.search_key,
                engine=settings.search_engine,
            )
        )
    return tools


def create_skill_router(settings: Settings) -> tuple[SkillRouter, object]:
    registry = build_registry(settings)

    def ctx_factory() -> SkillContext:
        return SkillContext(
            settings=settings,
            client=FeishuClient(settings.feishu_app_id, settings.feishu_app_secret),
            tools=build_tool_registry(settings),
            compliance_prompt=COMPLIANCE_PROMPT,
            conversation_store=build_conversation_store(settings),
            task_store=TaskStore(settings.task_storage_dir),
            session_store=SessionStore(settings.session_storage_dir),
            analysis_cache=AnalysisCache(settings.analysis_cache_dir),
            registry=registry,
        )

    return SkillRouter(registry, ctx_factory), registry


async def route_message_async(settings: Settings, msg: IncomingMessage) -> None:
    maybe_purge_user_memory(settings, msg.sender_id)
    router, _ = create_skill_router(settings)
    await router.route_message_async(msg)


async def route_card_async(settings: Settings, ca) -> None:
    maybe_purge_user_memory(settings, ca.operator_id)
    router, _ = create_skill_router(settings)
    await router.route_card_async(ca)


async def route_menu_async(settings: Settings, open_id: str, event_key: str) -> None:
    maybe_purge_user_memory(settings, open_id)
    router, _ = create_skill_router(settings)
    await router.route_menu_async(open_id, event_key)


def start_message_processing(settings: Settings, msg: IncomingMessage) -> None:
    thread = threading.Thread(
        target=lambda: asyncio.run(route_message_async(settings, msg)),
        daemon=True,
    )
    thread.start()


def start_card_action_processing(settings: Settings, ca) -> None:
    thread = threading.Thread(
        target=lambda: asyncio.run(route_card_async(settings, ca)),
        daemon=True,
    )
    thread.start()


def start_menu_processing(settings: Settings, open_id: str, event_key: str) -> None:
    thread = threading.Thread(
        target=lambda: asyncio.run(route_menu_async(settings, open_id, event_key)),
        daemon=True,
    )
    thread.start()


def handle_card_action(data: P2CardActionTrigger, settings: Settings) -> P2CardActionTriggerResponse:
    from app.integrations.feishu.events import extract_card_action

    ca = extract_card_action(data)
    if ca is None:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "无效操作"}})

    card_dedup = EventDeduplicator(settings.card_event_dedup_dir)
    if ca.token and not card_dedup.mark_if_new(ca.token):
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "处理中…"}})

    logger.info(
        "received feishu card action",
        extra={
            "action": ca.action,
            "skill_id": ca.skill_id,
            "operator_id": ca.operator_id,
            "token": ca.token,
        },
    )

    if ca.action == "cancel":
        if ca.operator_id:
            SessionStore(settings.session_storage_dir).clear(ca.operator_id)
        return P2CardActionTriggerResponse(
            {
                "toast": {"type": "info", "content": "已取消"},
                "card": {"type": "raw", "data": build_done_card("已取消。")},
            }
        )

    start_card_action_processing(settings, ca)
    return P2CardActionTriggerResponse(
        {
            "toast": {"type": "info", "content": "已开始处理"},
            "card": {"type": "raw", "data": build_done_card("已收到，正在处理…")},
        }
    )


def handle_bot_menu(data: P2ApplicationBotMenuV6, settings: Settings) -> None:
    """处理自定义菜单点击事件：按 event_key 路由到对应 Skill 的起始流程。"""
    from app.integrations.feishu.events import extract_bot_menu

    menu = extract_bot_menu(data)
    if menu is None or not menu.operator_id:
        logger.info("ignored bot menu event without operator/event_key")
        return

    # 去重：快速重复点击同一菜单（同秒）只处理一次。
    dedup = EventDeduplicator(settings.card_event_dedup_dir)
    dedup_key = f"menu:{menu.operator_id}:{menu.event_key}:{menu.timestamp}"
    if menu.timestamp is not None and not dedup.mark_if_new(dedup_key):
        logger.info("ignored duplicate bot menu event", extra={"dedup_key": dedup_key})
        return

    logger.info(
        "received feishu bot menu event",
        extra={"operator_id": menu.operator_id, "event_key": menu.event_key},
    )
    start_menu_processing(settings, menu.operator_id, menu.event_key)


def handle_message_receive(data: P2ImMessageReceiveV1, settings: Settings) -> None:
    raw = lark.JSON.marshal(data)
    payload = json.loads(raw)

    message_id = extract_message_id(payload)
    deduplicator = EventDeduplicator(settings.event_dedup_dir)
    if message_id and not deduplicator.mark_if_new(message_id):
        logger.info("ignored duplicate feishu event", extra={"message_id": message_id})
        return

    notify_admin_on_message(payload, settings)

    msg = normalize_incoming(payload)
    if msg is None:
        message_type = (
            payload.get("event", {}).get("message", {}).get("message_type")
        )
        logger.info("ignored non-file feishu message", extra={"message_type": message_type})
        return

    if msg.msg_type == "file":
        logger.info(
            "received feishu file message",
            extra={
                "message_id": msg.message_id,
                "file_name": msg.file_name,
                "file_key": msg.file_key,
            },
        )
    else:
        logger.info(
            "received feishu text message",
            extra={"message_id": msg.message_id, "sender_id": msg.sender_id},
        )

    start_message_processing(settings, msg)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("未配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

    run_memory_purge(settings, reason="startup")
    start_memory_retention_thread(settings)

    def on_message_receive(data: P2ImMessageReceiveV1) -> None:
        handle_message_receive(data, settings)

    def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        return handle_card_action(data, settings)

    def on_bot_menu(data: P2ApplicationBotMenuV6) -> None:
        handle_bot_menu(data, settings)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_application_bot_menu_v6(on_bot_menu)
        .build()
    )

    cli = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=event_handler,
    )

    logger.info("starting feishu websocket worker")
    cli.start()


if __name__ == "__main__":
    main()
