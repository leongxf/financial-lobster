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
from app.integrations.feishu.events import extract_file_message
from app.services.document_parser import parse_document
from app.services.financial_summary import generate_financial_summary_markdown
from app.services.llm_provider import LLMConfig, LLMProvider, TokenUsage
from app.services.markdown_report import build_parse_preview_report

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


def format_token_usage(usage: TokenUsage) -> str:
    return "\n".join(
        [
            "## Token 使用量",
            f"- Input tokens：{usage.input_tokens:,}",
            f"- Output tokens：{usage.output_tokens:,}",
            f"- Total tokens：{usage.total_tokens:,}",
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
) -> None:
    usage_text = format_token_usage(usage)
    report_with_usage = report + "\n\n---\n\n" + usage_text
    if len(report_with_usage) <= REPORT_TEXT_MAX_CHARS:
        await client.reply_text(
            message_id,
            "分析完成，报告如下：\n\n" + report_with_usage,
        )
        return

    report_path = build_report_file_path(storage_dir, source_file_name, report_with_usage)
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


async def process_file_message_async(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
) -> None:
    client = FeishuClient(settings.feishu_app_id, settings.feishu_app_secret)
    safe_name = file_name or "uploaded-file"
    storage_dir = Path(settings.local_storage_dir) / message_id
    target_path = storage_dir / safe_name

    async def notify(text: str) -> None:
        await client.reply_text(message_id, text)

    try:
        await notify(build_ack_message(settings, file_name))

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
        await notify(f"文件下载完成：{downloaded_path.name}")

        document = parse_document(downloaded_path)
        char_count = len(document.text)
        page_info = document.page_count if document.page_count is not None else "未知"
        await notify(
            f"文本提取完成：{page_info} 页，约 {char_count:,} 字符。"
            f"文件类型：{document.file_type}。"
        )
        if not document.text.strip():
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
            await notify(
                f"开始调用模型 {settings.llm_model} 分析"
                f"（最多 {settings.llm_max_chunks} 个片段）..."
            )
            summary_result = await generate_financial_summary_markdown(
                document=document,
                provider=provider,
                chunk_chars=settings.llm_chunk_chars,
                max_chunks=settings.llm_max_chunks,
                on_progress=notify,
            )
            report = summary_result.markdown
            usage = summary_result.usage
        else:
            await notify("未配置 LLM_API_KEY，返回解析预览。")
            report = build_parse_preview_report(document)
            usage = TokenUsage()

        await send_report_result(
            client=client,
            message_id=message_id,
            storage_dir=storage_dir,
            source_file_name=safe_name,
            report=report,
            usage=usage,
        )
    except httpx.ReadTimeout:
        logger.exception("LLM timeout while processing file message %s", message_id)
        await notify(
            "处理失败：模型调用超时。"
            f"当前超时设置 {settings.llm_timeout_ms // 1000} 秒，"
            "大文件可在 .env 调大 LLM_TIMEOUT_MS 或减少 LLM_MAX_CHUNKS 后重试。"
        )
    except Exception as exc:
        logger.exception("failed to process file message %s", message_id)
        await notify(f"处理失败：{exc}")


def process_file_message(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
) -> None:
    asyncio.run(
        process_file_message_async(
            settings=settings,
            message_id=message_id,
            file_key=file_key,
            file_name=file_name,
        )
    )


def start_file_processing(
    settings: Settings,
    message_id: str,
    file_key: str,
    file_name: str | None,
) -> None:
    thread = threading.Thread(
        target=process_file_message,
        args=(settings, message_id, file_key, file_name),
        daemon=True,
    )
    thread.start()


def handle_message_receive(data: P2ImMessageReceiveV1, settings: Settings) -> None:
    raw = lark.JSON.marshal(data)
    payload = json.loads(raw)

    file_message = extract_file_message(payload)
    if file_message is None:
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
