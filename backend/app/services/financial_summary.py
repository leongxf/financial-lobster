from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.services.document_parser import ParsedDocument, ParsedPage
from app.services.llm_provider import LLMProvider

ProgressCallback = Callable[[str], Awaitable[None]] | None


@dataclass(frozen=True)
class TextChunk:
    index: int
    start_page: int
    end_page: int
    text: str


SYSTEM_PROMPT = """你是服务于四大投资建议/交易咨询人员的材料分析助理。
你的任务是整理材料中的客观财务和经营信息，用于辅助用户后续自行判断是否投资。

必须严格遵守：
1. 只反馈材料中出现的客观内容。
2. 不得臆造、补全、推断材料中不存在的信息。
3. 所有观点、数据、摘要尽量标注来源页码、章节、表格或 Sheet。
4. 如果材料中没有明确依据，写“未在材料中发现”或“不足以判断”。
5. 不输出投资建议、审计意见、法律意见或确定性风险结论。
6. 输出使用中文 Markdown。
"""


def build_chunks(
    pages: list[ParsedPage],
    chunk_chars: int,
    max_chunks: int,
) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    current_pages: list[ParsedPage] = []
    current_len = 0

    for page in pages:
        text = page.text.strip()
        if not text:
            continue

        if current_pages and current_len + len(text) > chunk_chars:
            chunks.append(_make_chunk(len(chunks) + 1, current_pages))
            current_pages = []
            current_len = 0

            if len(chunks) >= max_chunks:
                break

        current_pages.append(page)
        current_len += len(text)

    if current_pages and len(chunks) < max_chunks:
        chunks.append(_make_chunk(len(chunks) + 1, current_pages))

    return chunks


def _make_chunk(index: int, pages: list[ParsedPage]) -> TextChunk:
    start_page = pages[0].page_number
    end_page = pages[-1].page_number
    text = "\n\n".join(f"[第 {page.page_number} 页]\n{page.text}" for page in pages)
    return TextChunk(index=index, start_page=start_page, end_page=end_page, text=text)


async def generate_financial_summary_markdown(
    document: ParsedDocument,
    provider: LLMProvider,
    chunk_chars: int,
    max_chunks: int,
    on_progress: ProgressCallback = None,
) -> str:
    chunks = build_chunks(document.pages, chunk_chars=chunk_chars, max_chunks=max_chunks)
    if not chunks:
        return _empty_text_report(document)

    chunk_notes: list[str] = []
    total = len(chunks)
    for chunk in chunks:
        if on_progress:
            await on_progress(
                f"正在分析片段 {chunk.index}/{total}（第 {chunk.start_page}-{chunk.end_page} 页）..."
            )
        chunk_notes.append(await summarize_chunk(chunk, provider))

    if on_progress:
        await on_progress("正在合成最终 Markdown 报告...")

    return await synthesize_final_report(document, chunk_notes, provider, len(chunks))


async def summarize_chunk(chunk: TextChunk, provider: LLMProvider) -> str:
    user_prompt = f"""请整理以下材料片段中的客观财务/经营信息。

片段范围：第 {chunk.start_page} 页至第 {chunk.end_page} 页

输出要求：
- 只列材料中明确出现的信息。
- 提取主要观点及其页码。
- 提取客观数据，尽量包含指标名、数值、单位、期间、主体、来源页码。
- 不要给投资建议，不要臆造。

材料片段：
{chunk.text}
"""
    return await provider.complete(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )


async def synthesize_final_report(
    document: ParsedDocument,
    chunk_notes: list[str],
    provider: LLMProvider,
    chunk_count: int,
) -> str:
    notes = "\n\n---\n\n".join(
        f"## 片段 {index + 1} 整理结果\n{note}" for index, note in enumerate(chunk_notes)
    )
    user_prompt = f"""请基于以下分片整理结果，生成最终 Markdown 报告。

文件名：{document.file_path.name}
文件类型：{document.file_type}
页数：{document.page_count}
已分析片段数：{chunk_count}

最终报告必须包含且只包含以下一级模块：
1. 文件概览
2. 一句话摘要
3. 财务要点摘要
4. 全文概要
5. 客观数据表
6. 未在材料中发现或不足以判断的信息
7. 说明

关键要求：
- 全文概要要列出主要观点，以及观点所在页码/章节。
- 客观数据表必须是 Markdown 表格，列为：指标名、数值、单位、期间、主体、来源位置。
- 不得臆造。没有依据就写“未在材料中发现”或“不足以判断”。
- 说明中必须写明：本报告仅整理材料中的客观信息，不构成投资建议。

分片整理结果：
{notes}
"""
    return await provider.complete(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )


def _empty_text_report(document: ParsedDocument) -> str:
    return "\n".join(
        [
            "# 材料分析失败",
            "",
            "## 文件概览",
            f"- 文件名：`{document.file_path.name}`",
            f"- 文件类型：`{document.file_type}`",
            f"- 页数：`{document.page_count if document.page_count is not None else '未知'}`",
            "",
            "## 说明",
            "未能从材料中提取到可复制文本。该文件可能是扫描件或图片型 PDF。",
        ]
    )
