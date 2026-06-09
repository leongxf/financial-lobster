from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.events import extract_file_message, extract_message_brief
from app.services.analysis_cache import AnalysisCache, sha256_file
from app.services.conversation_store import ConversationStore
from app.services.document_parser import parse_document
from app.services.financial_summary import generate_financial_summary_markdown
from app.services.llm_provider import LLMConfig, LLMProvider, TokenUsage
from app.services.markdown_report import build_parse_preview_report
from app.services.qa_service import (
    answer_question,
    build_chunk_embeddings,
    embedding_cache_file,
    extract_keywords,
    load_cached_embeddings,
    load_pages,
    retrieve_by_embedding,
    retrieve_pages,
    save_cached_embeddings,
    score_file_by_keywords,
)
from app.services.task_store import TaskStore

logger = logging.getLogger(__name__)
REPORT_TEXT_MAX_CHARS = 3500


def format_model_info(settings: Settings) -> str:
    if settings.llm_api_key:
        return f"{settings.llm_model}（provider: {settings.llm_provider}）"
    return "未配置 LLM（仅文本提取预览）"


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
) -> None:
    task_id = message_id
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    task_store = TaskStore(settings.task_storage_dir)
    analysis_cache = AnalysisCache(settings.analysis_cache_dir)
    conversation_store = ConversationStore(
        settings.conversation_storage_dir,
        recent_files_max=settings.qa_recent_files_max,
    )
    safe_name = file_name or "uploaded-file"
    storage_dir = Path(settings.local_storage_dir) / message_id
    target_path = storage_dir / safe_name

    async def notify(text: str) -> None:
        await client.reply_text(message_id, text)

    try:
        task_store.create_task(
            task_id,
            message_id=message_id,
            file_key=file_key,
            file_name=file_name,
            model=settings.llm_model,
            provider=settings.llm_provider,
        )
        await notify(build_ack_message(settings, file_name))

        task_store.update_task(task_id, status="downloading", event="downloading file")
        downloaded_path = await client.download_message_file(
            message_id,
            file_key,
            target_path,
        )
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
                error="PDF has no extractable text layer",
            )
            await notify(
                "\n".join(
                    [
                        "处理停止：未能从 PDF 中提取到可复制文本。",
                        "",
                        f"- 文件：{downloaded_path.name}",
                        f"- 页数：{page_info}",
                        "- 原因：该文件很可能是扫描件或图片型 PDF，当前 MVP 暂未接入 OCR。",
                        "",
                        "建议：请上传带文本层的 PDF，或先将扫描件转换为可搜索 PDF 后重试。",
                    ]
                )
            )
            return

        cache_hits = 0
        cache_misses = 0
        if settings.llm_api_key:
            provider = LLMProvider(
                LLMConfig(
                    provider=settings.llm_provider,
                    base_url=settings.llm_base_url,
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    timeout_ms=settings.llm_timeout_ms,
                    max_tokens=settings.llm_max_tokens,
                    temperature=settings.llm_temperature,
                )
            )
            task_store.update_task(
                task_id,
                status="analyzing",
                event="calling LLM",
                prompt_version=settings.prompt_version,
                llm_chunk_chars=settings.llm_chunk_chars,
                llm_max_chunks=settings.llm_max_chunks,
            )
            await notify(
                f"开始调用模型 {settings.llm_model} 分析"
                f"（最多 {settings.llm_max_chunks} 个片段）..."
            )
            summary_result = await generate_financial_summary_markdown(
                document=document,
                provider=provider,
                chunk_chars=settings.llm_chunk_chars,
                max_chunks=settings.llm_max_chunks,
                prompt_version=settings.prompt_version,
                file_hash=file_hash,
                cache=analysis_cache,
                on_progress=notify,
            )
            report = summary_result.markdown
            usage = summary_result.usage
            cache_hits = summary_result.cache_hits
            cache_misses = summary_result.cache_misses
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

            # 预计算向量（embedding）供追问做语义检索；中英混排材料靠它解决跨语言检索。
            # 按 file_hash 缓存：同内容文件重传直接复用，不重算。失败不阻断主流程。
            embeddings_path = ""
            if settings.llm_api_key:
                cache_dir = settings.qa_embedding_cache_dir
                try:
                    pages_data = [
                        {"page_number": p.page_number, "text": p.text}
                        for p in document.pages
                    ]
                    cached = load_cached_embeddings(cache_dir, file_hash)
                    if cached is None:
                        embed_provider = LLMProvider(
                            LLMConfig(
                                provider=settings.llm_provider,
                                base_url=settings.llm_base_url,
                                api_key=settings.llm_api_key,
                                model=settings.llm_model,
                                timeout_ms=settings.llm_timeout_ms,
                                max_tokens=settings.llm_max_tokens,
                                temperature=settings.llm_temperature,
                            )
                        )
                        chunks = await build_chunk_embeddings(
                            pages_data,
                            provider=embed_provider,
                            model=settings.qa_embedding_model,
                            chunk_chars=settings.qa_embedding_chunk_chars,
                            overlap=settings.qa_embedding_chunk_overlap,
                            batch_size=settings.qa_embedding_batch_size,
                        )
                        save_cached_embeddings(
                            cache_dir, file_hash, chunks, settings.qa_embedding_model
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
                    embeddings_path = str(embedding_cache_file(cache_dir, file_hash))
                except Exception:
                    logger.exception(
                        "[QA] failed to build embeddings for %s, fallback to keyword retrieval",
                        file_hash,
                    )
                    embeddings_path = ""

            conversation_store.upsert_file(
                sender_id,
                file_id=task_id,
                file_name=file_name,
                pages_path=str(pages_path),
                summary=summary_line[:200],
                keywords=keywords,
                file_hash=file_hash,
                embeddings_path=embeddings_path,
            )
            await notify(
                "你现在可以直接发文字向我追问这个文件的内容，例如「营业收入是多少」。"
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
) -> None:
    asyncio.run(
        process_file_message_async(
            settings=settings,
            message_id=message_id,
            file_key=file_key,
            file_name=file_name,
            sender_id=sender_id,
        )
    )


def start_file_processing(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
    sender_id: str | None = None,
) -> None:
    thread = threading.Thread(
        target=process_file_message,
        args=(settings, message_id, file_key, file_name, sender_id),
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
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    conversation_store = ConversationStore(
        settings.conversation_storage_dir,
        recent_files_max=settings.qa_recent_files_max,
    )

    async def notify(text: str) -> None:
        await client.reply_text(message_id, text)

    if not settings.llm_api_key:
        await notify("未配置 LLM_API_KEY，暂时无法回答追问。")
        return

    files = conversation_store.list_files(sender_id)
    if not files:
        await notify("我还没有你最近上传的文件，请先发送一个 PDF 给我分析后再追问。")
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
        provider = LLMProvider(
            LLMConfig(
                provider=settings.llm_provider,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_ms=settings.llm_timeout_ms,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            )
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
            try:
                context = await retrieve_by_embedding(
                    question=question,
                    chunks=cached_chunks,
                    provider=provider,
                    model=settings.qa_embedding_model,
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


def extract_text_question(payload: dict) -> tuple[str, str, str] | None:
    """从文本消息事件中提取 (message_id, sender_open_id, 问题文本)。非文本消息返回 None。"""
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
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
    text = str((content or {}).get("text") or "").strip()
    if not text:
        return None

    sender = event.get("sender")
    sender_id = None
    if isinstance(sender, dict) and isinstance(sender.get("sender_id"), dict):
        sender_id = sender["sender_id"].get("open_id")
    if not sender_id:
        return None

    return message_id, sender_id, text


def handle_message_receive(data: P2ImMessageReceiveV1, settings: Settings) -> None:
    raw = lark.JSON.marshal(data)
    payload = json.loads(raw)

    # 额外推送旁路：先给管理员推一条提醒，再走原有文件处理逻辑（原逻辑保持不变）。
    notify_admin_on_message(payload, settings)

    file_message = extract_file_message(payload)
    if file_message is None:
        # 非文件消息：尝试作为文本追问处理。
        question = extract_text_question(payload)
        if question is not None:
            q_message_id, q_sender_id, q_text = question
            logger.info(
                "received feishu text question",
                extra={"message_id": q_message_id, "sender_id": q_sender_id},
            )
            start_question_processing(
                settings=settings,
                message_id=q_message_id,
                sender_id=q_sender_id,
                question=q_text,
            )
            return

        message_type = (
            payload.get("event", {}).get("message", {}).get("message_type")
        )
        logger.info("ignored non-file feishu message", extra={"message_type": message_type})
        return

    if not file_message.message_id or not file_message.file_key:
        logger.warning("file message missing message_id or file_key", extra={"payload": payload})
        return

    logger.info(
        "received feishu file message",
        extra={
            "message_id": file_message.message_id,
            "file_name": file_message.file_name,
            "file_key": file_message.file_key,
        },
    )
    start_file_processing(
        settings=settings,
        message_id=file_message.message_id,
        file_key=file_message.file_key,
        file_name=file_message.file_name,
        sender_id=file_message.sender_id,
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")

    def on_message_receive(data: P2ImMessageReceiveV1) -> None:
        handle_message_receive(data, settings)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
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
